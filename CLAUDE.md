# Kalshi Sports Betting Agent

A FastMCP server that lets Claude interact with Kalshi's prediction market API for sports betting (NBA, MLS, NFL, etc.).

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

## MCP Tools

### Market Discovery
- **get_sports_markets** — search for available sports markets (NBA, MLS, NFL, etc.)
- **get_market_details** — get price, volume, and order book for a specific market ticker

### Account
- **get_balance** — check account balance
- **get_positions** — see current open positions
- **get_order_history** — see past trades

### Trading
- **place_order** — buy a YES or NO contract at a specified price
- **cancel_order** — cancel an open order

### Analytics
- **calculate_edge** — compare model probability vs market price
- **calculate_bet_size** — quarter-Kelly criterion bet sizing
- **get_performance_report** — win rate, ROI, P&L breakdown by sport

### System
- **toggle_kill_switch** — emergency stop for all trading
- **get_system_status** — shows kill switch state, daily P&L, trades today

## Safety Rules

- NEVER place an order without the calling code confirming
- Always check the kill switch before any trade
- Max single bet: `MAX_BET` from `.env` (default $10)
- Max daily loss: `MAX_DAILY_LOSS` from `.env` (default $50)
- All trades logged to local SQLite database (`kalshi_agent.db`)
- If daily loss limit is hit, automatically enable kill switch

## Tech Stack

- **Package manager:** uv
- **Environment variables:** python-dotenv
- **Local data storage:** SQLite (`kalshi_agent.db`)
- **MCP framework:** FastMCP (same pattern as the MLS, EPL, and NBA analyst MCP servers)
- **Python:** 3.12
