"""
main.py — Sports EV Model entry point.

Usage:
    python main.py --league nhl --market moneyline
    python main.py --league all --market all --save
    python main.py --league nba --market spread --threshold 2.0
"""

import sys
import argparse
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# League / market alias maps
# ---------------------------------------------------------------------------

LEAGUE_ALIASES: dict = {
    "nhl":        "icehockey_nhl",
    "nba":        "basketball_nba",
    "mlb":        "baseball_mlb",
    "epl":        "soccer_epl",
    "laliga":     "soccer_spain_la_liga",
    "bundesliga": "soccer_germany_bundesliga",
    "mls":        "soccer_usa_mls",
}

MARKET_ALIASES: dict = {
    "moneyline": "h2h",
    "spread":    "spreads",
    "total":     "totals",
    "props":     "player_props",
    # also accept the raw API names directly
    "h2h":     "h2h",
    "spreads": "spreads",
    "totals":  "totals",
}

ALL_LEAGUES  = list(LEAGUE_ALIASES.values())
ALL_MARKETS  = ["h2h", "spreads", "totals"]   # props handled separately (high API cost)

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Sports Betting EV Model — fetch odds, remove vig, find +EV bets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --league nhl --market moneyline
  python main.py --league nba --market spread --threshold 2.0
  python main.py --league all --market all --save
  python main.py --league epl laliga --market moneyline total
  python main.py --league nhl --market props --save
        """,
    )

    parser.add_argument(
        "--league", "-l",
        nargs="+",
        default=["all"],
        metavar="LEAGUE",
        help=(
            "League(s) to scan: nhl nba epl laliga bundesliga mls  "
            "or 'all' for every supported league. "
            "Multiple leagues accepted: --league nhl nba"
        ),
    )
    parser.add_argument(
        "--market", "-m",
        nargs="+",
        default=["moneyline"],
        metavar="MARKET",
        help=(
            "Market(s): moneyline spread total props  or 'all' (excl. props). "
            "Multiple markets accepted: --market moneyline spread"
        ),
    )
    parser.add_argument(
        "--threshold", "-t",
        type=float,
        default=3.0,
        metavar="PCT",
        help="Minimum EV%% to flag as a +EV bet (default: 3.0)",
    )
    parser.add_argument(
        "--stake", "-s",
        type=float,
        default=100.0,
        metavar="DOLLARS",
        help="Notional stake per bet in $ for EV dollar calculation (default: 100)",
    )
    parser.add_argument(
        "--no-adjustments",
        action="store_true",
        help="Skip sport-specific adjustments (goalie flags, B2B, euro fatigue, etc.)",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save results to reports/ev_report_YYYY-MM-DD.csv",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress rich terminal output (useful with --save for cron/scripting)",
    )
    parser.add_argument(
        "--list-leagues",
        action="store_true",
        help="Print all supported league aliases and exit",
    )

    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------

def resolve_leagues(league_args: list) -> list:
    """Map user-friendly league args to Odds API sport_key strings."""
    if "all" in league_args:
        return ALL_LEAGUES

    keys = []
    unknown = []
    for arg in league_args:
        key = LEAGUE_ALIASES.get(arg.lower())
        if key:
            keys.append(key)
        else:
            unknown.append(arg)

    if unknown:
        valid = ", ".join(LEAGUE_ALIASES.keys())
        print(f"ERROR: Unknown league(s): {unknown}. Valid options: {valid}, all")
        sys.exit(1)

    return list(dict.fromkeys(keys))   # deduplicate, preserve order


def resolve_markets(market_args: list) -> tuple:
    """
    Map user-friendly market args to Odds API market strings.
    Returns (standard_markets, include_props).
    """
    if "all" in market_args:
        return ALL_MARKETS, False   # 'all' intentionally excludes props (API cost)

    standard = []
    include_props = False
    unknown = []

    for arg in market_args:
        mapped = MARKET_ALIASES.get(arg.lower())
        if mapped == "player_props":
            include_props = True
        elif mapped:
            standard.append(mapped)
        else:
            unknown.append(arg)

    if unknown:
        valid = ", ".join(MARKET_ALIASES.keys())
        print(f"ERROR: Unknown market(s): {unknown}. Valid options: {valid}, all")
        sys.exit(1)

    standard = list(dict.fromkeys(standard))   # deduplicate
    return standard, include_props


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    args = parse_args(argv)

    # Deferred imports — keeps --list-leagues fast and avoids loading config
    # before we know whether the user just wants help text
    if args.list_leagues:
        print("\nSupported --league values:")
        for alias, key in LEAGUE_ALIASES.items():
            print(f"  {alias:<12} → {key}")
        print("  all          → all of the above")
        return 0

    from scripts.report_generator import run_pipeline, print_rich_report, save_csv
    from scripts.odds_fetcher import get_props_df
    from models.ev_calculator import find_all_positive_ev, EV_THRESHOLD_PCT
    from rich.console import Console

    console = Console()
    run_ts = datetime.now(timezone.utc)

    sport_keys   = resolve_leagues(args.league)
    std_markets, include_props = resolve_markets(args.market)

    if not args.quiet:
        league_labels = [a for a, k in LEAGUE_ALIASES.items() if k in sport_keys]
        mkt_labels = [a for a, k in MARKET_ALIASES.items()
                      if k in std_markets and a not in ("h2h", "spreads", "totals")]
        if include_props:
            mkt_labels.append("props")
        console.print(
            f"\n[bold cyan]Sports EV Model[/bold cyan] | "
            f"Leagues: [yellow]{', '.join(league_labels)}[/yellow] | "
            f"Markets: [yellow]{', '.join(mkt_labels or [m for m in std_markets]) or 'none'}[/yellow] | "
            f"Threshold: [yellow]{args.threshold}%[/yellow]\n"
        )

    # --- Standard markets (h2h / spreads / totals) ---
    ev_df = None
    if std_markets:
        ev_df = run_pipeline(
            sport_keys=sport_keys,
            markets=std_markets,
            ev_threshold=args.threshold,
            stake=args.stake,
            apply_adjustments_flag=not args.no_adjustments,
        )

        if not args.quiet:
            print_rich_report(ev_df, sports_scanned=sport_keys, run_ts=run_ts)

        if args.save and ev_df is not None and not ev_df.empty:
            path = save_csv(ev_df)
            if not args.quiet:
                console.print(f"[green]Saved:[/green] {path}  ({len(ev_df)} rows)\n")

    # --- Player props (separate pipeline, higher API cost) ---
    if include_props:
        if not args.quiet:
            console.print("[bold]Fetching player props…[/bold] (uses additional API quota)\n")

        props_df = get_props_df(sport_keys=sport_keys)
        if props_df.empty:
            if not args.quiet:
                console.print("[yellow]No player props data returned.[/yellow]\n")
        else:
            props_ev = find_all_positive_ev(
                props_df.rename(columns={"prop_market": "market"}),
                markets=list(props_df["prop_market"].unique()),
                ev_threshold=args.threshold,
                stake=args.stake,
            )
            if not args.quiet:
                print_rich_report(props_ev, sports_scanned=sport_keys, run_ts=run_ts)

            if args.save and not props_ev.empty:
                path = save_csv(props_ev.assign(report_type="props"),
                                output_dir="reports")
                if not args.quiet:
                    console.print(f"[green]Props saved:[/green] {path}\n")

    if not args.quiet and (ev_df is None or ev_df.empty) and not include_props:
        console.print("[yellow]No +EV bets found. Try lowering --threshold.[/yellow]\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
