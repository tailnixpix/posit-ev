"""
test_fetch.py — Verify odds_fetcher.py works for NHL.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from scripts.odds_fetcher import get_odds_df, get_best_lines

def test_nhl_fetch():
    print("=" * 60)
    print("TEST: Fetching NHL odds (h2h, spreads, totals)")
    print("=" * 60)

    df = get_odds_df(sport_keys=["icehockey_nhl"], markets=["h2h", "spreads", "totals"])

    assert not df.empty, "ERROR: No data returned — check API key or NHL schedule."

    print(f"\n[PASS] Rows returned: {len(df)}")
    print(f"       Games found:   {df['game_id'].nunique()}")
    print(f"       Bookmakers:    {sorted(df['bookmaker'].unique())}")
    print(f"       Markets:       {sorted(df['market'].unique())}")

    print("\n--- Sample: First 5 rows ---")
    sample_cols = ["home_team", "away_team", "bookmaker", "market", "outcome_name", "price", "point"]
    print(df[sample_cols].head(5).to_string(index=False))

    print("\n--- Sample: Best moneyline per outcome ---")
    best = get_best_lines(df, market="h2h")
    if not best.empty:
        print(best[["home_team", "away_team", "bookmaker", "outcome_name", "price"]].head(10).to_string(index=False))
    else:
        print("No h2h lines found.")

    print("\n--- Games on slate ---")
    games = df[["home_team", "away_team", "commence_time"]].drop_duplicates("home_team")
    for _, row in games.iterrows():
        print(f"  {row['away_team']} @ {row['home_team']}  —  {row['commence_time'].strftime('%a %b %d %I:%M %p UTC')}")

    print("\n[ALL TESTS PASSED]")

if __name__ == "__main__":
    test_nhl_fetch()
