"""
Quick test: search for sports markets using the same client setup as server.py.

Run with:  uv run test_markets.py

This calls the tool functions directly — no MCP server needed.
"""

from server import get_sports_markets, get_market_details

# -- Test 1: Search by team name (how Kalshi labels sports markets) --
print("=" * 60)
print("Searching for 'Charlotte' markets...")
print("=" * 60)
result = get_sports_markets("Charlotte")
print(result)

# -- Test 2: Grab details on the first ticker found --
tickers = [
    line.strip().replace("Ticker: ", "")
    for line in result.split("\n")
    if line.strip().startswith("Ticker:")
]

if tickers:
    first_ticker = tickers[0]
    print("\n" + "=" * 60)
    print(f"Fetching details for: {first_ticker}")
    print("=" * 60)
    details = get_market_details(first_ticker)
    print(details)
else:
    print("\nNo tickers found — skipping detail lookup.")
