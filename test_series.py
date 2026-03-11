"""
Test script: Fetch sports game markets via series_ticker filtering.

The key insight: Kalshi organizes simple game markets under UPPERCASE
series tickers like KXNBAGAME, KXMLSGAME, KXNHLGAME, etc. The general
get_markets() search only returns KXMVE parlays, so we need to filter
by series_ticker to find the straight win/loss markets.
"""

from datetime import datetime, timezone
from server import get_client, KALSHI_ENV, API_URL

# All known sports game series on Kalshi (uppercase required)
SPORTS_SERIES = {
    "KXNBAGAME": "NBA",
    "KXMLSGAME": "MLS",
    "KXNHLGAME": "NHL",
    "KXEPLGAME": "EPL",
    "KXMLBGAME": "MLB",
    "KXNFLGAME": "NFL",
    "KXUCLGAME": "UCL",
}


def fetch_series_markets(client, series_ticker: str, label: str):
    """Fetch all open markets for a given series."""
    print(f"\n{'═' * 60}")
    print(f"{label} — series_ticker='{series_ticker}'")
    print(f"{'═' * 60}")

    all_markets = []
    cursor = None
    for _ in range(5):
        kwargs = {
            "series_ticker": series_ticker,
            "status": "open",
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.get_markets(**kwargs)
        all_markets.extend(resp.markets)
        cursor = resp.cursor
        if not cursor:
            break

    print(f"  Found {len(all_markets)} open market(s)\n")

    for m in all_markets[:10]:
        yes = m.yes_bid or 0
        no = m.no_bid or 0
        close = str(m.close_time)[:16] if m.close_time else "N/A"
        print(f"  {m.ticker}")
        print(f"    {m.title}")
        print(f"    YES: {yes}¢  |  NO: {no}¢  |  Close: {close}")
        print()

    if len(all_markets) > 10:
        print(f"  ... plus {len(all_markets) - 10} more markets")

    return all_markets


if __name__ == "__main__":
    print(f"Kalshi Sports Series Test ({KALSHI_ENV})")
    print(f"API: {API_URL}")
    print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    client = get_client()

    # Test each sports series
    for series_ticker, label in SPORTS_SERIES.items():
        fetch_series_markets(client, series_ticker, label)
