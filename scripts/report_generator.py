"""
report_generator.py — Full pipeline orchestrator for the sports EV model.

Pipeline:
    get_odds_df()              (odds_fetcher)
        → find_all_positive_ev()   (ev_calculator)
        → _apply_sport_adjustments()  (sport_adjustments, per-row)
        → print_rich_report()      (rich terminal output)
        → save_csv()               (reports/ev_report_YYYY-MM-DD.csv)

Usage:
    python scripts/report_generator.py --sports basketball_nba icehockey_nhl --save
"""

import sys
import os
import argparse
import logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from scripts.odds_fetcher import get_odds_df, SPORT_KEYS, MARKETS
from models.ev_calculator import find_all_positive_ev, EV_THRESHOLD_PCT, DEFAULT_STAKE, MAX_CREDIBLE_EV_PCT
from models.projection_ev import find_positive_ev_model
from models.sport_adjustments import (
    apply_adjustments,
    GameContext,
    ADJUSTMENT_CONFIG,
    SOCCER_SPORT_KEYS,
)
from scripts.context_fetcher import build_context, fetch_game_projections, match_team
from config import LEAGUES, LOG_LEVEL, LOCAL_TZ

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

console = Console()

# Reverse LEAGUES dict for sport_key → friendly name lookup
_SPORT_KEY_TO_NAME: dict = {v: k for k, v in LEAGUES.items()}

# ---------------------------------------------------------------------------
# Sport adjustments integration
# ---------------------------------------------------------------------------

def _apply_sport_adjustments(
    ev_df: pd.DataFrame,
    sport_context: dict = None,
) -> pd.DataFrame:
    """
    Iterate ev_df rows, build a GameContext from available metadata
    (including real-time context from context_fetcher when provided),
    call apply_adjustments(), and patch adjusted_prob / confidence_multiplier
    / adj_flags / adj_warnings / effective_ev_pct onto each row.

    Parameters
    ----------
    ev_df : pd.DataFrame
        +EV bets from find_all_positive_ev().
    sport_context : dict, optional
        {sport_key: {normalised_team_name: context_dict}}
        Pre-fetched from build_context() for each sport in the DataFrame.
        When absent, GameContext fields default to neutral values.
    """
    sport_context = sport_context or {}

    records = []
    for _, row in ev_df.iterrows():
        outcome_names = [row["outcome_name"]]
        probs = [row["true_prob"]]

        sport_key = row.get("sport_key", "")
        game_str = row.get("game", "")

        # Parse home/away from "Away @ Home" format
        if " @ " in game_str:
            away_team = game_str.split(" @ ")[0].strip()
            home_team = game_str.split(" @ ")[1].strip()
        else:
            home_team = ""
            away_team = ""

        # Look up real-time context for each team
        ctx_map = sport_context.get(sport_key, {})
        home_key = match_team(home_team, list(ctx_map.keys())) if ctx_map and home_team else None
        away_key = match_team(away_team, list(ctx_map.keys())) if ctx_map and away_team else None
        home_ctx = ctx_map.get(home_key, {}) if home_key else {}
        away_ctx = ctx_map.get(away_key, {}) if away_key else {}

        ctx = GameContext(
            game_id=row.get("game_id", ""),
            sport_key=sport_key,
            home_team=home_team,
            away_team=away_team,
            # NHL — goalie confirmation (None = unknown → confidence warning)
            home_goalie_confirmed=home_ctx.get("goalie_confirmed", None),
            away_goalie_confirmed=away_ctx.get("goalie_confirmed", None),
            # NBA — back-to-back rest penalty
            home_b2b=home_ctx.get("b2b", False),
            away_b2b=away_ctx.get("b2b", False),
            # Home/away win% splits (NHL + NBA)
            home_win_pct_home=home_ctx.get("home_win_pct", None),
            away_win_pct_away=away_ctx.get("away_win_pct", None),
            # Injuries (shared)
            home_injuries=home_ctx.get("injuries", []),
            away_injuries=away_ctx.get("injuries", []),
            # Recent form (NHL + NBA; None when unavailable)
            home_last10_wins=home_ctx.get("last10_wins"),
            away_last10_wins=away_ctx.get("last10_wins"),
            home_streak_val=home_ctx.get("streak_val", 0),
            away_streak_val=away_ctx.get("streak_val", 0),
        )

        try:
            result = apply_adjustments(ctx, probs, outcome_names)
            adj_prob = result["adjusted_probs"][0]
            conf_mult = result["confidence_multipliers"][0] if result.get("confidence_multipliers") else 1.0
        except Exception:
            adj_prob = row["true_prob"]
            conf_mult = 1.0
            result = {"flags": [], "warnings": []}

        effective_ev_pct = row["ev_pct"] * conf_mult

        # Build concise trend summary strings for storage/display
        def _trend_str(last10: int | None, streak: int) -> str:
            if last10 is None:
                return ""
            w, l = last10, 10 - last10
            streak_part = ""
            if abs(streak) >= 2:
                streak_part = f" W{streak}" if streak > 0 else f" L{abs(streak)}"
            return f"{w}-{l}{streak_part}"

        home_trend = _trend_str(ctx.home_last10_wins, ctx.home_streak_val)
        away_trend = _trend_str(ctx.away_last10_wins, ctx.away_streak_val)

        records.append({
            **row.to_dict(),
            "adjusted_prob": round(adj_prob, 4),
            "confidence_mult": round(conf_mult, 3),
            "adj_flags": "|".join(f for f in result.get("flags", []) if f),
            "adj_warnings": "; ".join(result.get("warnings", [])),
            "effective_ev_pct": round(effective_ev_pct, 4),
            "home_trend": home_trend,
            "away_trend": away_trend,
        })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Projection map builder
# ---------------------------------------------------------------------------

def _build_projection_map(odds_df: pd.DataFrame) -> dict:
    """
    Fetch Optimal game projections for every unique game in odds_df.

    Returns
    -------
    dict  {game_id (str): projection_dict}

    Projection dict shape (from fetch_game_projections):
        home_win_probability, total_mean, total_p25, total_p75,
        spread_mean, spread_p25, spread_p75,
        home_score_mean, away_score_mean,
        home_team, away_team
    """
    proj_map: dict = {}

    # Deduplicate by game_id
    unique = (
        odds_df.groupby("game_id", as_index=False)
        .first()[["game_id", "away_team", "home_team", "sport_key"]]
    )
    total = len(unique)

    for _, row in unique.iterrows():
        gid        = str(row["game_id"])
        game_str   = f"{row['away_team']} @ {row['home_team']}"
        sport_key  = str(row["sport_key"])
        try:
            proj = fetch_game_projections(game_str, sport_key)
            if proj:
                proj_map[gid] = proj
        except Exception as exc:
            log.debug("Projection fetch skipped for %s: %s", game_str, exc)

    log.info(
        "Projection map: %d/%d games covered by model projections.",
        len(proj_map), total,
    )
    return proj_map


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    sport_keys: list = None,
    markets: list = None,
    ev_threshold: float = EV_THRESHOLD_PCT,
    stake: float = DEFAULT_STAKE,
    apply_adjustments_flag: bool = True,
) -> pd.DataFrame:
    """
    Fetch odds → build projection map → compute model-based EV →
    apply adjustments → return final DataFrame.

    The Optimal model projections are the primary probability source.
    Games not covered by Optimal (e.g. some soccer fixtures) fall back
    to the traditional no-vig sportsbook-consensus approach.

    Returns
    -------
    pd.DataFrame with columns:
        game, market, outcome_name, bookmaker, american_odds, true_prob,
        implied_prob, ev, ev_pct, positive_ev, commence_time, sport_key,
        game_id, sharp_book, sharp_vig_pct, prob_source,
        adjusted_prob, confidence_mult, adj_flags, adj_warnings, effective_ev_pct,
        proj_away_score, proj_home_score, proj_total, proj_home_win_prob,
        proj_away_display, proj_home_display
    """
    sport_keys = sport_keys or SPORT_KEYS
    markets = markets or MARKETS

    log.info("Fetching odds for %d sport(s): %s", len(sport_keys), sport_keys)
    odds_df = get_odds_df(sport_keys=sport_keys, markets=markets)

    ev_markets = list(markets)

    if odds_df.empty:
        log.warning("No odds data returned from API.")
        return pd.DataFrame()

    # ── Fetch model projections for all games ─────────────────────────────────
    log.info("Fetching Optimal model projections...")
    proj_map = _build_projection_map(odds_df)

    # ── Model-based EV calculation ────────────────────────────────────────────
    log.info("Computing model-based EV (threshold=%.1f%%)...", ev_threshold)
    ev_df = find_positive_ev_model(
        odds_df, proj_map,
        markets=ev_markets,
        ev_threshold=ev_threshold,
        stake=stake,
    )

    if ev_df.empty:
        log.info("No +EV bets found above %.1f%% threshold.", ev_threshold)
        return ev_df

    # ── Attach projection snapshot columns for display ────────────────────────
    # IMPORTANT: only attach to model-backed bets (prob_source == "model").
    # No-vig fallback bets must NOT receive projection scores — the model
    # probability was NOT used for those bets, so the scores could contradict
    # the displayed bet direction.
    def _safe_f(val):
        try:
            v = float(val)
            return None if (v != v) else v   # NaN check
        except (TypeError, ValueError):
            return None

    def _proj_val(row, field, as_float=True):
        """Return projection field only for model-backed bets."""
        if row.get("prob_source") != "model":
            return None
        val = proj_map.get(str(row.get("game_id", "")), {}).get(field)
        if as_float:
            return _safe_f(val)
        s = str(val) if val is not None else ""
        return s if s not in ("", "nan", "None") else None

    ev_df["proj_away_score"]    = ev_df.apply(lambda r: _proj_val(r, "away_score_mean"),         axis=1)
    ev_df["proj_home_score"]    = ev_df.apply(lambda r: _proj_val(r, "home_score_mean"),         axis=1)
    ev_df["proj_total"]         = ev_df.apply(lambda r: _proj_val(r, "total_mean"),              axis=1)
    ev_df["proj_home_win_prob"] = ev_df.apply(lambda r: _proj_val(r, "home_win_probability"),    axis=1)
    ev_df["proj_away_display"]  = ev_df.apply(lambda r: _proj_val(r, "away_team", False),        axis=1)
    ev_df["proj_home_display"]  = ev_df.apply(lambda r: _proj_val(r, "home_team", False),        axis=1)

    # ── Sport-specific adjustments ────────────────────────────────────────────
    if apply_adjustments_flag:
        log.info("Applying sport-specific adjustments...")
        sport_context: dict = {}
        for sport in ev_df["sport_key"].unique():
            if sport in ("icehockey_nhl", "basketball_nba"):
                sport_context[sport] = build_context(sport)
        ev_df = _apply_sport_adjustments(ev_df, sport_context=sport_context)
    else:
        ev_df["adjusted_prob"]    = ev_df["true_prob"]
        ev_df["confidence_mult"]  = 1.0
        ev_df["adj_flags"]        = ""
        ev_df["adj_warnings"]     = ""
        ev_df["effective_ev_pct"] = ev_df["ev_pct"]

    # ── Final sanity filter ───────────────────────────────────────────────────
    # Remove any bets still showing EV above the credibility ceiling.
    # expected_value() already clamps these to 0, but this catches any path
    # that bypassed that function (e.g., future code additions).
    pre_len = len(ev_df)
    ev_df = ev_df[ev_df["ev_pct"] <= MAX_CREDIBLE_EV_PCT].copy()
    removed = pre_len - len(ev_df)
    if removed:
        log.warning(
            "Sanity filter removed %d bet(s) with EV > %.1f%% — "
            "likely a data/projection matching error.",
            removed, MAX_CREDIBLE_EV_PCT,
        )

    ev_df = ev_df.sort_values("effective_ev_pct", ascending=False).reset_index(drop=True)

    # ── All-book odds comparison map ──────────────────────────────────────────
    # For each +EV row, gather every book's price for the same game/market/outcome.
    # Stored as compact JSON so the dashboard can render an inline comparison table.
    import json as _json
    all_book_odds_list = []
    for _, ev_row in ev_df.iterrows():
        gid   = str(ev_row.get("game_id", ""))
        mkt   = str(ev_row.get("market", ""))
        out   = str(ev_row.get("outcome_name", ""))
        point = ev_row.get("point")
        mask  = (
            (odds_df["game_id"].astype(str) == gid) &
            (odds_df["market"].astype(str) == mkt) &
            (odds_df["outcome_name"].astype(str) == out)
        )
        if point is not None:
            try:
                _pf = float(point)
                if _pf == _pf:  # not NaN
                    mask = mask & (odds_df["point"].astype(float) == _pf)
            except (TypeError, ValueError):
                pass
        subset = (
            odds_df[mask][["bookmaker", "price"]]
            .drop_duplicates(subset="bookmaker", keep="first")
        )
        book_map: dict = {}
        for _, r in subset.iterrows():
            try:
                book_map[r["bookmaker"]] = int(r["price"])
            except (TypeError, ValueError):
                pass
        all_book_odds_list.append(_json.dumps(book_map) if book_map else None)
    ev_df["all_book_odds"] = all_book_odds_list

    return ev_df


# ---------------------------------------------------------------------------
# Rich terminal output
# ---------------------------------------------------------------------------

def _ev_color(ev_pct: float, dimmed: bool = False) -> str:
    """Return a rich style string based on EV%."""
    if dimmed:
        return "dim"
    if ev_pct > 5.0:
        return "bold green"
    if ev_pct >= 3.0:
        return "yellow"
    return "red"


def _format_odds(american_odds) -> str:
    try:
        n = int(american_odds)
        return f"+{n}" if n > 0 else str(n)
    except (ValueError, TypeError):
        return str(american_odds)


def _format_time(ts) -> str:
    try:
        if hasattr(ts, "strftime"):
            if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                ts = ts.astimezone(LOCAL_TZ)
            return ts.strftime("%a %b %d %I:%M%p CT")
        return str(ts)
    except Exception:
        return ""


def print_rich_report(ev_df: pd.DataFrame, sports_scanned: list, run_ts: datetime) -> None:
    """Print a color-coded rich terminal report grouped by league."""
    if ev_df.empty:
        console.print(Panel("[yellow]No +EV bets found.[/yellow]", title="EV Report"))
        return

    # --- Header panel ---
    total_bets = len(ev_df)
    avg_ev = ev_df["effective_ev_pct"].mean()
    max_ev = ev_df["effective_ev_pct"].max()
    total_ev_dollars = ev_df["ev"].sum()
    sports_label = ", ".join(_SPORT_KEY_TO_NAME.get(s, s) for s in sports_scanned)

    header_lines = [
        f"[bold]Run:[/bold]     {run_ts.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S CT')}",
        f"[bold]Sports:[/bold]  {sports_label}",
        f"[bold]Bets:[/bold]    {total_bets} +EV opportunities found",
        f"[bold]Avg EV:[/bold]  {avg_ev:.2f}%   [bold]Max EV:[/bold] {max_ev:.2f}%   "
        f"[bold]Total EV$:[/bold] ${total_ev_dollars:.2f} (per ${int(ev_df['ev'].abs().mean()/avg_ev*100):.0f} stake each)" if total_bets > 0 else "",
    ]
    console.print(Panel("\n".join(header_lines), title="[bold cyan]Sports EV Model — Daily Report[/bold cyan]", border_style="cyan"))
    console.print()

    # --- One table per sport ---
    for sport_key, sport_df in ev_df.groupby("sport_key"):
        league_name = _SPORT_KEY_TO_NAME.get(sport_key, sport_key)

        table = Table(
            title=f"[bold]{league_name}[/bold]  ({len(sport_df)} bets)",
            box=box.SIMPLE_HEAVY,
            header_style="bold white on dark_blue",
            show_lines=False,
            expand=True,
        )

        table.add_column("Game", style="white", min_width=30)
        table.add_column("Mkt", style="cyan", width=8)
        table.add_column("Outcome", style="white", min_width=18)
        table.add_column("Book", style="blue", width=12)
        table.add_column("Odds", justify="right", width=6)
        table.add_column("True%", justify="right", width=7)
        table.add_column("Impl%", justify="right", width=7)
        table.add_column("EV$", justify="right", width=7)
        table.add_column("EV%", justify="right", width=8)
        table.add_column("Flags", style="dim", min_width=12)
        table.add_column("Game Time", style="dim", width=18)

        sport_df_sorted = sport_df.sort_values("effective_ev_pct", ascending=False)

        for _, row in sport_df_sorted.iterrows():
            conf_mult = row.get("confidence_mult", 1.0)
            dimmed = conf_mult < 1.0
            ev_pct_val = row["effective_ev_pct"]
            ev_style = _ev_color(ev_pct_val, dimmed=dimmed)

            ev_pct_str = f"{ev_pct_val:.2f}%"
            if dimmed:
                ev_pct_str += " ⚠"

            flags = row.get("adj_flags", "") or row.get("adj_warnings", "")
            flags_str = flags[:25] + "…" if len(str(flags)) > 25 else str(flags)

            table.add_row(
                row.get("game", ""),
                row.get("market", ""),
                row.get("outcome_name", ""),
                row.get("bookmaker", ""),
                _format_odds(row.get("american_odds")),
                f"{row.get('true_prob', 0):.1%}",
                f"{row.get('implied_prob', 0):.1%}",
                f"${row.get('ev', 0):.2f}",
                Text(ev_pct_str, style=ev_style),
                flags_str,
                _format_time(row.get("commence_time")),
            )

        console.print(table)

    # --- Footer ---
    console.print()
    console.print(
        f"  [green bold]■[/green bold] EV > 5%   "
        f"[yellow]■[/yellow] EV 3–5%   "
        f"⚠ = confidence penalty applied (e.g. unconfirmed goalie)\n"
    )


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def save_csv(ev_df: pd.DataFrame, output_dir: str = "reports") -> str:
    """Save the full EV DataFrame to a dated CSV. Returns the file path."""
    os.makedirs(output_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(output_dir, f"ev_report_{date_str}.csv")
    ev_df.to_csv(path, index=False)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sports EV Model — daily report generator")
    parser.add_argument("--sports", nargs="+", default=SPORT_KEYS, metavar="SPORT_KEY",
                        help="Sport keys to scan (default: all configured)")
    parser.add_argument("--markets", nargs="+", default=MARKETS, metavar="MARKET",
                        help="Markets: h2h spreads totals (default: all)")
    parser.add_argument("--threshold", type=float, default=EV_THRESHOLD_PCT,
                        help=f"Minimum EV%% to include (default: {EV_THRESHOLD_PCT})")
    parser.add_argument("--stake", type=float, default=DEFAULT_STAKE,
                        help=f"Notional stake per bet in $ (default: {DEFAULT_STAKE})")
    parser.add_argument("--no-adjustments", action="store_true",
                        help="Skip sport-specific adjustments")
    parser.add_argument("--save", action="store_true",
                        help="Save output to reports/ev_report_YYYY-MM-DD.csv")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress rich terminal output (useful for cron)")
    args = parser.parse_args()

    run_ts = datetime.now(timezone.utc)

    ev_df = run_pipeline(
        sport_keys=args.sports,
        markets=args.markets,
        ev_threshold=args.threshold,
        stake=args.stake,
        apply_adjustments_flag=not args.no_adjustments,
    )

    if not args.quiet:
        print_rich_report(ev_df, sports_scanned=args.sports, run_ts=run_ts)

    if args.save:
        if ev_df.empty:
            console.print("[yellow]No data to save.[/yellow]")
        else:
            path = save_csv(ev_df)
            console.print(f"[green]Saved:[/green] {path}  ({len(ev_df)} rows)")

    return 0 if not ev_df.empty else 1


if __name__ == "__main__":
    sys.exit(main())
