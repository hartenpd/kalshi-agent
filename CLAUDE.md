# Kalshi Sports Betting Agent

A FastMCP server that lets Claude interact with the Kalshi prediction market API for sports betting. It searches markets, places trades, tracks analyst picks across multiple sport analysts, and runs calibration analysis.

## Architecture Overview

This is the **trading execution layer** in a multi-server system:

- **NBA Analyst** (`~/Desktop/nba-analyst`) — analyzes NBA matchups, generates picks
- **MLS Analyst** (`~/Desktop/mls-analyst`) — analyzes MLS matchups, generates picks
- **EPL Analyst** (`~/Desktop/epl-analyst`) — analyzes EPL matchups, generates picks
- **Kalshi Agent** (this project) — receives picks from all analysts, finds Kalshi markets, calculates edge, places trades, and tracks calibration

The analysts call `log_analyst_pick` on this server for every pick they make (traded or not). After games finish, `settle_analyst_pick` records the outcome and calculates P&L. The `get_calibration_report` tool then compares predicted vs actual win rates to improve future predictions.

All four servers are FastMCP servers using the same pattern: `@mcp.tool()` decorated functions in a single `server.py`, communicating via MCP protocol over stdio.

## SDK

Uses `kalshi_python_sync` (NOT the deprecated `kalshi-python`).

```python
from kalshi_python_sync import Configuration, KalshiClient
```

## Authentication

RSA key pair authentication. API Key ID and private key path are loaded from `.env` via `python-dotenv`.

```
KALSHI_API_KEY_ID=<your-key-id>
KALSHI_PRIVATE_KEY_PATH=./keys/private_key.pem
KALSHI_ENV=demo
```

## API URLs

| Environment | URL |
|---|---|
| Demo | `https://demo-api.kalshi.co/trade-api/v2` |
| Production | `https://api.elections.kalshi.com/trade-api/v2` |

Read `KALSHI_ENV` from `.env` to choose which one. Default to **demo**.

## MCP Tools (20 total)

### Market Discovery
- **get_sports_markets** — search for sports markets by team, player, or league name (NBA, MLS, EPL, etc.)
- **get_market_details** — get YES/NO prices, volume, open interest, and order book for a specific ticker

### Account
- **get_balance** — check available balance and portfolio value in dollars
- **get_positions** — list all open positions with side, contracts, exposure, and realized P&L
- **get_order_history** — show last 20 Kalshi fills and last 20 local trade log entries

### Trading
- **place_order** — buy YES or NO contracts with kill switch, max bet, and daily loss safety checks
- **cancel_order** — cancel an open order by Kalshi order ID

### Analytics
- **calculate_edge** — compare model probability vs market price, rates edge as strong/moderate/insufficient/negative
- **calculate_bet_size** — quarter-Kelly criterion sizing, capped at 5% of bankroll
- **get_performance_report** — trade count, total spent, and daily breakdown from the local trade log

### Analyst Pick Tracking
- **log_analyst_pick** — log a pick from any analyst (sport, game, confidence, model probability, edge, bet info)
- **settle_analyst_pick** — settle a pick as win/loss/push, auto-calculates P&L if a bet was placed
- **list_analyst_picks** — list picks with optional filters (status, sport, date range, limit), newest first
- **get_analyst_pick** — get full details for a single pick by ID
- **delete_analyst_pick** — permanently delete a pick by ID
- **edit_analyst_pick** — update any field(s) on an existing pick
- **get_calibration_report** — predicted vs actual win rates by confidence level and sport, edge accuracy

### System / Risk Management
- **toggle_kill_switch** — toggle trading on/off (defaults ON, auto-enables on daily loss limit breach)
- **get_system_status** — kill switch state, today's exposure, trade count, remaining daily budget
- **reconcile_positions** — compare Kalshi positions against local trade log, flags ghost trades and mismatches

## SQLite Schema

Database file: `kalshi_agent.db` (next to `server.py`, git-ignored)

### trades
Logs every order attempt (successful or not).
```
id              INTEGER PRIMARY KEY AUTOINCREMENT
timestamp       TEXT NOT NULL DEFAULT (datetime('now'))
ticker          TEXT NOT NULL
side            TEXT NOT NULL           -- 'yes' or 'no'
action          TEXT NOT NULL           -- 'buy' or 'sell'
quantity        INTEGER NOT NULL
price_cents     INTEGER                 -- price per contract in cents
total_cost_cents INTEGER                -- quantity * price_cents
order_id        TEXT                    -- Kalshi order ID if successful
status          TEXT NOT NULL           -- 'submitted', 'filled', 'failed', 'cancelled', 'rejected'
error_message   TEXT                    -- why it failed (if applicable)
event_title     TEXT DEFAULT ''         -- human-readable game name
```

### system_config
Key-value store for kill switch and other flags.
```
key    TEXT PRIMARY KEY
value  TEXT NOT NULL
```
Seeded with `('kill_switch', 'on')` via INSERT OR IGNORE on first run.

### analyst_picks
Every pick from any analyst MCP server, traded or not.
```
id                INTEGER PRIMARY KEY AUTOINCREMENT
timestamp         TEXT NOT NULL DEFAULT (datetime('now'))
sport             TEXT NOT NULL              -- 'NBA', 'MLS', 'EPL', etc.
game              TEXT NOT NULL              -- 'Cleveland at Orlando'
game_date         TEXT NOT NULL              -- '2026-03-11'
pick              TEXT NOT NULL              -- 'Cleveland' or 'home' or 'Over 220.5'
confidence        INTEGER NOT NULL           -- 1-5 stars
model_probability REAL NOT NULL              -- 0.70
market_price      INTEGER                    -- Kalshi price in cents, NULL if no market
edge              REAL                       -- calculated edge, NULL if no market
bet_placed        INTEGER NOT NULL DEFAULT 0 -- 0 or 1 (SQLite boolean)
bet_amount        REAL                       -- dollars, NULL if no bet
outcome           TEXT NOT NULL DEFAULT 'pending' -- 'win', 'loss', 'push', 'pending'
pnl               REAL                       -- profit/loss in dollars, NULL until settled
```

## SDK Pydantic Compatibility Patches

**DO NOT REMOVE THESE PATCHES.** They are in `server.py` lines 30-218.

### Why they exist
Kalshi migrated their API from cent-integer fields (e.g. `yes_bid: 28`) to dollar-string fields (e.g. `yes_bid_dollars: "0.2800"`). The old cent fields now return `null`. But the `kalshi_python_sync` SDK's Pydantic models still mark these old fields as required and non-nullable. Without patches, every API call crashes with a Pydantic `ValidationError`.

Each patch monkey-patches `from_dict()` on the affected model class to default null fields to `0` (ints) or `"0.0000"` (strings) before Pydantic validation runs.

### Patched models

1. **Market** (`_patched_market_from_dict`) — defaults 14 numeric fields (`yes_bid`, `no_bid`, `volume`, `open_interest`, etc.) to `0` and 10 dollar-string fields to `"0.0000"`. Also maps unknown `status` enum values to `"settled"` and unknown `result` values to `""`.

2. **Orderbook** (`_patched_orderbook_from_dict`) — the API returns `yes_dollars`/`no_dollars` arrays where quantity is an int, but the SDK expects `List[List[StrictStr]]`. Patch converts all values to strings.

3. **Order** (`_patched_order_from_dict`) — defaults 10 null int fields (`yes_price`, `no_price`, `fill_count`, `remaining_count`, etc.) to `0` and 4 dollar-string fields to `"0.0000"`.

4. **MarketPosition** (`_patched_position_from_dict`) — defaults 6 null int fields (`total_traded`, `position`, `market_exposure`, etc.) to `0` and 4 dollar-string fields to `"0.0000"`.

5. **EventPosition** (`_patched_event_position_from_dict`) — defaults 5 null int fields (`total_cost`, `event_exposure`, etc.) to `0` and 4 dollar-string fields to `"0.0000"`.

6. **Fill** (`_patched_fill_from_dict`) — defaults 4 null int fields (`count`, `price`, `yes_price`, `no_price`) to `0` and 2 fixed-price string fields to `"0.0000"`.

## Key Learnings & Gotchas

### Market discovery
- NBA markets use series ticker `KXNBAGAME`, not text search. Same pattern for all sports: `KXMLSGAME`, `KXNHLGAME`, `KXEPLGAME`, `KXMLBGAME`, `KXNFLGAME`, `KXUCLGAME`.
- Ticker format: `KXNBAGAME-26MAR12DALMEM-MEM` (series, date+teams code, pick side).
- Market close times are set ~2 weeks after the actual game date. **Filter by `status="open"`, not by date.** Date filtering breaks everything.
- The `_dollars()` helper reads dollar-string fields first, falls back to deprecated cent fields. Always use it instead of accessing market attributes directly.

### Trading safety
- **Kill switch defaults to ON.** Must be explicitly toggled off before any trades go through.
- **`_safe_create_order()` is the ONLY path to place trades.** It re-checks the kill switch immediately before the API call to close the race-condition window.
- Daily loss limit auto-enables the kill switch when breached.
- API call success and response parsing are separated in `place_order` — if the HTTP request succeeds but Pydantic fails to parse the response, the trade is still logged as "submitted" (not "failed"), because the order DID go through on Kalshi.

### Analyst pick logging
- **Log EVERY pick from every analyst — not just ones we trade.** The calibration report needs the full dataset including untraded picks to detect overconfidence and sport-level biases.
- All analyst servers (NBA, MLS, EPL) use dynamic season calculation to determine the current season.
- Sport is stored uppercase in the database.

### macOS SSL
- Uses `certifi` to fix macOS SSL certificate issues: `os.environ.setdefault("SSL_CERT_FILE", certifi.where())`.

## File Inventory

| File | Description |
|---|---|
| `server.py` | Main FastMCP server — all 20 MCP tools, SDK patches, DB setup, Kalshi client |
| `generate_dashboard.py` | Reads `kalshi_agent.db` and produces `dashboard.html` with calibration charts and pick log |
| `main.py` | Default entry point stub (unused — server runs via `server.py`) |
| `test_connection.py` | Verifies API credentials work by fetching account balance |
| `test_kalshi_debug.py` | Debug script that makes raw HTTP requests to identify SDK Pydantic parse errors |
| `test_markets.py` | Quick test that calls `get_sports_markets` and `get_market_details` directly |
| `test_series.py` | Fetches open markets for all known sports series tickers |
| `pyproject.toml` | Project config — Python 3.12+, deps: kalshi-python-sync, mcp[cli], python-dotenv, certifi |
| `.env` | API credentials and environment config (git-ignored) |
| `.gitignore` | Ignores `.env`, `keys/`, `__pycache__/`, `.venv/`, `kalshi_agent.db`, `dashboard.html` |
| `.python-version` | Python 3.12 |
| `keys/` | RSA key pair for Kalshi API authentication (git-ignored) |
| `kalshi_agent.db` | SQLite database — trades, system config, analyst picks (git-ignored) |
| `dashboard.html` | Generated HTML dashboard with calibration and performance charts (git-ignored) |
| `uv.lock` | Lockfile for uv package manager |
| `README.md` | Empty placeholder |

## Tech Stack

- **Package manager:** uv
- **Environment variables:** python-dotenv
- **Local data storage:** SQLite (`kalshi_agent.db`)
- **MCP framework:** FastMCP
- **Python:** 3.12
- **SSL fix:** certifi
