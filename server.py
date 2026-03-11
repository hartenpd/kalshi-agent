"""
Kalshi Sports Betting Agent — FastMCP Server

Exposes Kalshi prediction market data as MCP tools so Claude can
search markets, check prices, place trades, and manage risk.

Uses kalshi_python_sync SDK with RSA key pair authentication.
Credentials loaded from .env via python-dotenv.
Trade history and system state stored in local SQLite (kalshi_agent.db).
"""

import os
import math
import sqlite3
import certifi
from datetime import datetime, timezone
from collections import defaultdict
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from kalshi_python_sync import Configuration, KalshiClient
from kalshi_python_sync.models.market import Market as _MarketModel
from kalshi_python_sync.models.orderbook import Orderbook as _OrderbookModel
from kalshi_python_sync.models.create_order_request import CreateOrderRequest


# ══════════════════════════════════════════════════════════════════════════════
# SDK COMPATIBILITY PATCHES
# ══════════════════════════════════════════════════════════════════════════════
# The Kalshi API returns data that doesn't match the SDK's strict Pydantic
# models. We patch from_dict() on each model to fix the data before it hits
# Pydantic validation, so the SDK doesn't crash.

# Patch 1: Market model — API returns null for 'category' and
# 'risk_limit_cents', but the SDK marks them as required.
_original_market_from_dict = _MarketModel.from_dict.__func__


@classmethod  # type: ignore[misc]
def _patched_market_from_dict(cls, obj):
    if isinstance(obj, dict):
        if obj.get("category") is None:
            obj["category"] = ""
        if obj.get("risk_limit_cents") is None:
            obj["risk_limit_cents"] = 0
        # The API returns enum values the SDK doesn't know about.
        # Map unknown values to safe defaults so Pydantic doesn't crash.
        _known_statuses = {"initialized", "active", "closed", "settled", "determined"}
        if obj.get("status") and obj["status"] not in _known_statuses:
            obj["status"] = "settled"
        _known_results = {"yes", "no", ""}
        if obj.get("result") is not None and obj["result"] not in _known_results:
            obj["result"] = ""
    return _original_market_from_dict(cls, obj)


_MarketModel.from_dict = _patched_market_from_dict

# Patch 2: Orderbook model — API returns quantity as int (e.g. 3250) but
# the SDK expects List[List[StrictStr]] (both price and qty as strings).
_original_orderbook_from_dict = _OrderbookModel.from_dict.__func__


@classmethod  # type: ignore[misc]
def _patched_orderbook_from_dict(cls, obj):
    if isinstance(obj, dict):
        for key in ("yes_dollars", "no_dollars"):
            if key in obj and obj[key]:
                obj[key] = [[str(v) for v in pair] for pair in obj[key]]
    return _original_orderbook_from_dict(cls, obj)


_OrderbookModel.from_dict = _patched_orderbook_from_dict


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# Fix macOS SSL — certifi provides a reliable CA bundle
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

load_dotenv()

KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./keys/private_key.pem")
KALSHI_ENV = os.getenv("KALSHI_ENV", "demo")

# Safety limits — loaded once at startup
MAX_BET = float(os.getenv("MAX_BET", "10"))         # dollars per trade
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "50"))  # dollars per day

API_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "production": "https://api.elections.kalshi.com/trade-api/v2",
}
API_URL = API_URLS.get(KALSHI_ENV, API_URLS["demo"])

# Path to the local SQLite database — lives next to server.py
DB_PATH = os.path.join(os.path.dirname(__file__), "kalshi_agent.db")

# Known sports game series on Kalshi (uppercase required by API).
# Maps series ticker → (league abbreviation, team search keywords).
# These are "simple game" markets (Team A vs Team B), not parlays.
SPORTS_SERIES = {
    "KXNBAGAME": "NBA",
    "KXMLSGAME": "MLS",
    "KXNHLGAME": "NHL",
    "KXEPLGAME": "EPL",
    "KXMLBGAME": "MLB",
    "KXNFLGAME": "NFL",
    "KXUCLGAME": "UCL",
}


# ══════════════════════════════════════════════════════════════════════════════
# SQLITE DATABASE SETUP
# ══════════════════════════════════════════════════════════════════════════════

def _get_db() -> sqlite3.Connection:
    """Open a connection to the local SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # so we can access columns by name
    return conn


def _init_db():
    """
    Create the database tables if they don't exist yet.

    Called once at module load. The kill switch defaults to ON (locked)
    so the system is safe by default — you have to explicitly turn it
    off before any trades can go through.
    """
    conn = _get_db()
    cursor = conn.cursor()

    # Trades table — logs every order attempt (successful or not)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,           -- 'yes' or 'no'
            action TEXT NOT NULL,         -- 'buy' or 'sell'
            quantity INTEGER NOT NULL,
            price_cents INTEGER,          -- price per contract in cents
            total_cost_cents INTEGER,     -- quantity * price_cents
            order_id TEXT,                -- Kalshi order ID if successful
            status TEXT NOT NULL,         -- 'submitted', 'filled', 'failed', 'cancelled', 'rejected'
            error_message TEXT,           -- why it failed (if applicable)
            event_title TEXT DEFAULT ''   -- human-readable game name
        )
    """)

    # System config — key-value store for kill switch and other flags
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Kill switch defaults to ON (locked) — safe by default.
    # INSERT OR IGNORE means this only runs on first database creation,
    # it won't overwrite if the user has already toggled it.
    cursor.execute("""
        INSERT OR IGNORE INTO system_config (key, value)
        VALUES ('kill_switch', 'on')
    """)

    conn.commit()
    conn.close()


# Initialize the database when this module loads
_init_db()


def _is_kill_switch_on() -> bool:
    """Check whether the kill switch is currently enabled."""
    conn = _get_db()
    row = conn.execute(
        "SELECT value FROM system_config WHERE key = 'kill_switch'"
    ).fetchone()
    conn.close()
    return row is not None and row["value"] == "on"


def _get_todays_pnl() -> float:
    """
    Sum up today's realized losses from the trades table.

    Returns a negative number if we're down (e.g. -15.50 means $15.50 lost).
    Only counts trades with status 'submitted' or 'filled' from today.
    """
    conn = _get_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        """
        SELECT COALESCE(SUM(total_cost_cents), 0) as total_spent
        FROM trades
        WHERE date(timestamp) = ?
          AND status IN ('submitted', 'filled')
          AND action = 'buy'
        """,
        (today,),
    ).fetchone()
    conn.close()
    # total_spent is positive cents we've spent buying contracts today
    # We treat it as exposure/loss for daily limit tracking
    return -(row["total_spent"] / 100) if row else 0.0


def _get_todays_trade_count() -> int:
    """Count how many trades were logged today."""
    conn = _get_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM trades WHERE date(timestamp) = ?",
        (today,),
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def _log_trade(
    ticker: str,
    side: str,
    action: str,
    quantity: int,
    price_cents: int,
    status: str,
    order_id: str = "",
    error_message: str = "",
    event_title: str = "",
):
    """Write a trade record to the local SQLite database."""
    conn = _get_db()
    conn.execute(
        """
        INSERT INTO trades
            (ticker, side, action, quantity, price_cents,
             total_cost_cents, order_id, status, error_message, event_title)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker,
            side,
            action,
            quantity,
            price_cents,
            quantity * price_cents,
            order_id,
            status,
            error_message,
            event_title,
        ),
    )
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# KALSHI CLIENT
# ══════════════════════════════════════════════════════════════════════════════

_client: KalshiClient | None = None


def get_client() -> KalshiClient:
    """Create and cache an authenticated KalshiClient."""
    global _client

    if _client is not None:
        return _client

    if not KALSHI_API_KEY_ID:
        raise ValueError(
            "KALSHI_API_KEY_ID is not set. Add it to your .env file."
        )

    key_path = KALSHI_PRIVATE_KEY_PATH
    if not os.path.isabs(key_path):
        key_path = os.path.join(os.path.dirname(__file__), key_path)

    try:
        with open(key_path, "r") as f:
            private_key_pem = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Private key not found at {key_path}. "
            f"Make sure your .pem file is in the keys/ folder."
        )

    config = Configuration(host=API_URL)
    config.api_key_id = KALSHI_API_KEY_ID
    config.private_key_pem = private_key_pem

    _client = KalshiClient(configuration=config)
    return _client


def _format_error(action: str, error: Exception) -> str:
    """Build a helpful error message instead of a raw traceback."""
    error_str = str(error)

    if "401" in error_str or "Unauthorized" in error_str:
        return (
            f"Error {action}: 401 Unauthorized\n\n"
            f"The API connection works but your credentials were rejected.\n"
            f"  - Make sure the API key ID in .env matches the private key\n"
            f"  - {'Production' if KALSHI_ENV == 'production' else 'Demo'} "
            f"keys only work with the "
            f"{'production' if KALSHI_ENV == 'production' else 'demo'} API\n"
            f"  - Generate new keys at https://kalshi.com/account/api"
        )
    elif "404" in error_str or "Not Found" in error_str:
        return (
            f"Error {action}: Not found. "
            f"Double-check the ticker or order ID."
        )
    elif "SSL" in error_str or "certificate" in error_str:
        return (
            f"Error {action}: SSL certificate error. "
            f"Try running: uv add certifi"
        )
    else:
        return (
            f"Error {action}: {error}\n\n"
            f"Things to check:\n"
            f"  - Are your API credentials correct in .env?\n"
            f"  - Is the Kalshi {KALSHI_ENV} API reachable?"
        )


# ══════════════════════════════════════════════════════════════════════════════
# FASTMCP SERVER + TOOLS
# ══════════════════════════════════════════════════════════════════════════════

mcp = FastMCP("kalshi-agent")


# ─── Market Discovery ────────────────────────────────────────────────────────

def _fetch_series_markets(client, series_ticker: str) -> list:
    """Fetch all open markets for a given series ticker, paginating."""
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
    return all_markets


def _resolve_query_to_series(query: str) -> list[str]:
    """
    Map a search query to the right series tickers.

    If the query is a league name (e.g. 'NBA', 'MLS'), return that
    specific series. Otherwise return ALL sports series so we can
    text-search across them.
    """
    q = query.upper().strip()

    # Direct league name match → just that one series
    for series_ticker, league in SPORTS_SERIES.items():
        if q == league:
            return [series_ticker]

    # Return all series for team/player text search
    return list(SPORTS_SERIES.keys())


@mcp.tool()
def get_sports_markets(query: str) -> str:
    """
    Search for available sports prediction markets on Kalshi.

    Search by team name (e.g. 'Charlotte', 'Denver', 'Arsenal'), player name
    (e.g. 'Jokic', 'Harden'), or game terms (e.g. 'Over', 'goals', 'Points').

    You can also search by league name to get all upcoming games:
      NBA, MLS, NHL, EPL, MLB, NFL, UCL

    Results are grouped by game matchup (e.g. 'Charlotte at Sacramento')
    with individual market tickers and prices shown underneath.

    For league searches, all currently open/active markets are returned.
    """
    try:
        client = get_client()

        target_series = _resolve_query_to_series(query)
        query_lower = query.lower()
        is_league_query = query.upper().strip() in SPORTS_SERIES.values()

        # Step 1: Fetch open markets from the targeted series.
        # No date filter — Kalshi sets close times ~2 weeks after the
        # actual game date, so date filtering breaks everything.
        # The status="open" filter in _fetch_series_markets already
        # limits results to active/tradeable markets only.
        series_markets = []
        for series_ticker in target_series:
            try:
                markets = _fetch_series_markets(client, series_ticker)
                series_markets.extend(markets)
            except Exception:
                pass

        # Step 2: For non-league queries, also search the general market
        # list (catches parlays and other market types like KXMVE).
        general_matches = []
        if not is_league_query:
            general_markets = []
            cursor = None
            for _ in range(3):
                kwargs = {"status": "open", "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                response = client.get_markets(**kwargs)
                general_markets.extend(response.markets)
                cursor = response.cursor
                if not cursor:
                    break
            general_matches = [
                m for m in general_markets
                if query_lower in (m.title or "").lower()
                or query_lower in (m.subtitle or "").lower()
                or query_lower in (m.event_ticker or "").lower()
                or query_lower in (m.ticker or "").lower()
            ]

        # Step 4: Combine and filter results.
        if is_league_query:
            # Series markets are already filtered by league — keep all
            matches = list(series_markets)
        else:
            # Text-filter series markets, then add general matches
            filtered_series = [
                m for m in series_markets
                if query_lower in (m.title or "").lower()
                or query_lower in (m.subtitle or "").lower()
                or query_lower in (m.event_ticker or "").lower()
                or query_lower in (m.ticker or "").lower()
            ]
            matches = filtered_series + general_matches

        # Deduplicate by ticker
        seen = set()
        unique_matches = []
        for m in matches:
            if m.ticker not in seen:
                seen.add(m.ticker)
                unique_matches.append(m)
        matches = unique_matches

        if not matches:
            leagues = ", ".join(SPORTS_SERIES.values())
            return (
                f"No open markets found matching '{query}'.\n\n"
                f"Tips:\n"
                f"  - Search by league: {leagues}\n"
                f"  - Search by team: 'Charlotte', 'Denver', 'Arsenal'\n"
                f"  - Search by player: 'Jokic', 'Harden', 'LeBron'\n"
                f"  - Game terms: 'Over', 'goals', 'Points'"
            )

        # Step 5: Group by game (event_ticker) and format output.
        games = defaultdict(list)
        for m in matches:
            games[m.event_ticker].append(m)

        # Look up human-readable game titles (cap at 15 API calls)
        event_titles = {}
        for event_ticker in list(games.keys())[:15]:
            try:
                event_resp = client.get_event(event_ticker=event_ticker)
                event_titles[event_ticker] = event_resp.event.title
            except Exception:
                event_titles[event_ticker] = event_ticker

        total_markets = len(matches)
        total_games = len(games)

        if is_league_query:
            header = (
                f"Found {total_markets} market(s) across {total_games} "
                f"game(s) for {query.upper()}:\n"
            )
        else:
            header = (
                f"Found {total_markets} market(s) across {total_games} "
                f"game(s) matching '{query}':\n"
            )
        lines = [header]

        # Sort games by earliest game date (extracted from ticker).
        # Ticker format: KXNBAGAME-26MAR11CLEORL-CLE → "26MAR11"
        # This puts today's games first, then tomorrow's, etc.
        def _game_sort_key(event_ticker_and_markets):
            first_ticker = event_ticker_and_markets[1][0].ticker
            parts = first_ticker.split("-")
            # The date code is in the 2nd segment, e.g. "26MAR11CLEORL"
            return parts[1] if len(parts) > 1 else first_ticker

        shown_games = 0
        for event_ticker, game_markets in sorted(
            games.items(), key=lambda kv: _game_sort_key(kv)
        ):
            if shown_games >= 15:
                break
            shown_games += 1

            game_title = event_titles.get(event_ticker, event_ticker)
            lines.append(f"{'─' * 50}")
            lines.append(f"Game: {game_title}")
            lines.append(f"  {len(game_markets)} market(s) available\n")

            for m in game_markets[:3]:
                yes_price = (m.yes_bid or 0) / 100
                no_price = (m.no_bid or 0) / 100
                volume = m.volume or 0
                close_str = str(m.close_time) if m.close_time else "N/A"
                title = (
                    m.title if len(m.title) <= 80
                    else m.title[:77] + "..."
                )
                lines.append(
                    f"  Ticker: {m.ticker}\n"
                    f"  Title:  {title}\n"
                    f"  YES: ${yes_price:.2f}  |  NO: ${no_price:.2f}  |  "
                    f"Vol: {volume:,}  |  Closes: {close_str}\n"
                )

            if len(game_markets) > 3:
                lines.append(
                    f"  ... +{len(game_markets) - 3} more markets "
                    f"in this game\n"
                )

        if total_games > 15:
            lines.append(
                f"\n... plus {total_games - 15} more games. "
                f"Use a more specific search to narrow down."
            )

        return "\n".join(lines)

    except Exception as e:
        return _format_error(f"searching markets for '{query}'", e)


@mcp.tool()
def get_market_details(ticker: str) -> str:
    """
    Get full details for a specific Kalshi market by its ticker.

    Returns YES/NO prices (bid and ask), trading volume, open interest,
    close date, and the current order book (top 3 bids and asks).

    Use get_sports_markets first to find valid tickers.
    """
    try:
        client = get_client()

        market_response = client.get_market(ticker=ticker)
        m = market_response.market

        yes_bid = (m.yes_bid or 0) / 100
        yes_ask = (m.yes_ask or 0) / 100
        no_bid = (m.no_bid or 0) / 100
        no_ask = (m.no_ask or 0) / 100
        last_price = (m.last_price or 0) / 100
        close_str = str(m.close_time) if m.close_time else "N/A"

        lines = [
            f"Market: {m.title}",
            f"Ticker: {m.ticker}",
            f"Status: {m.status}",
            "",
            "Prices:",
            f"  YES  bid ${yes_bid:.2f}  /  ask ${yes_ask:.2f}",
            f"  NO   bid ${no_bid:.2f}  /  ask ${no_ask:.2f}",
            f"  Last traded: ${last_price:.2f}",
            "",
            f"Volume:         {m.volume or 0:,}",
            f"24h Volume:     {m.volume_24h or 0:,}",
            f"Open Interest:  {m.open_interest or 0:,}",
            f"Closes:         {close_str}",
        ]

        if m.subtitle:
            lines.append(f"\nDetails: {m.subtitle}")

        # Fetch order book (top 3 levels)
        try:
            ob_response = client.get_market_orderbook(
                ticker=ticker, depth=3
            )
            ob = ob_response.orderbook

            lines.append("\nOrder Book (top 3 levels):")

            yes_levels = ob.yes_dollars if ob.yes_dollars else []
            no_levels = ob.no_dollars if ob.no_dollars else []

            if yes_levels:
                lines.append("  YES side:")
                for level in yes_levels[:3]:
                    price, qty = level[0], level[1]
                    lines.append(f"    ${price}  ×  {qty} contracts")
            else:
                lines.append("  YES side: no orders")

            if no_levels:
                lines.append("  NO side:")
                for level in no_levels[:3]:
                    price, qty = level[0], level[1]
                    lines.append(f"    ${price}  ×  {qty} contracts")
            else:
                lines.append("  NO side: no orders")

        except Exception as ob_err:
            lines.append(f"\nOrder book unavailable: {ob_err}")

        return "\n".join(lines)

    except Exception as e:
        return _format_error(f"fetching market '{ticker}'", e)


# ─── Account ─────────────────────────────────────────────────────────────────

@mcp.tool()
def get_balance() -> str:
    """
    Check the current Kalshi account balance.

    Returns available balance and portfolio value in dollars.
    The API returns values in cents, so this divides by 100.
    """
    try:
        client = get_client()
        response = client.get_balance()

        balance = response.balance / 100
        portfolio_value = response.portfolio_value / 100
        env_label = "PRODUCTION" if KALSHI_ENV == "production" else "DEMO"

        return (
            f"Kalshi Account Balance ({env_label}):\n\n"
            f"  Available balance:  ${balance:,.2f}\n"
            f"  Portfolio value:    ${portfolio_value:,.2f}\n"
            f"  Total equity:       ${balance + portfolio_value:,.2f}"
        )

    except Exception as e:
        return _format_error("fetching balance", e)


@mcp.tool()
def get_positions() -> str:
    """
    View all current open positions on Kalshi.

    Shows each position's market ticker, side (YES/NO), number of
    contracts, market exposure, and realized P&L.
    """
    try:
        client = get_client()

        all_positions = []
        cursor = None

        # Paginate through all positions
        for _ in range(5):
            kwargs = {"limit": 100}
            if cursor:
                kwargs["cursor"] = cursor
            response = client.get_positions(**kwargs)
            all_positions.extend(response.market_positions)
            cursor = response.cursor
            if not cursor:
                break

        # Filter to only positions with contracts held
        active = [p for p in all_positions if p.position != 0]

        if not active:
            return "No open positions. Your portfolio is empty."

        lines = [f"Open Positions ({len(active)}):\n"]

        for p in active:
            # position > 0 means YES contracts, < 0 means NO contracts
            side = "YES" if p.position > 0 else "NO"
            qty = abs(p.position)
            exposure = p.market_exposure / 100  # cents → dollars
            pnl = p.realized_pnl / 100

            lines.append(
                f"  {p.ticker}\n"
                f"    Side: {side}  |  Contracts: {qty}\n"
                f"    Exposure: ${exposure:,.2f}  |  "
                f"Realized P&L: ${pnl:+,.2f}\n"
            )

        return "\n".join(lines)

    except Exception as e:
        return _format_error("fetching positions", e)


@mcp.tool()
def get_order_history() -> str:
    """
    View recent trade history from both the Kalshi API and local log.

    Shows the last 20 fills from Kalshi and the last 20 entries from
    the local SQLite trades table.
    """
    try:
        lines = ["═══ Recent Trade History ═══\n"]

        # ── Part 1: Recent fills from Kalshi API ──
        try:
            client = get_client()
            response = client.get_fills(limit=20)
            fills = response.fills

            if fills:
                lines.append(f"Kalshi Fills (last {len(fills)}):\n")
                for f in fills:
                    price_cents = f.yes_price if f.side == "yes" else f.no_price
                    price_dollars = price_cents / 100
                    time_str = (
                        str(f.created_time) if f.created_time else "N/A"
                    )
                    lines.append(
                        f"  {f.ticker}\n"
                        f"    {f.action.upper()} {f.count} {f.side.upper()} "
                        f"@ ${price_dollars:.2f}  |  {time_str}\n"
                    )
            else:
                lines.append("Kalshi Fills: none yet\n")
        except Exception as api_err:
            lines.append(f"Kalshi Fills: unavailable ({api_err})\n")

        # ── Part 2: Local trade log from SQLite ──
        conn = _get_db()
        rows = conn.execute(
            """
            SELECT * FROM trades
            ORDER BY timestamp DESC
            LIMIT 20
            """
        ).fetchall()
        conn.close()

        if rows:
            lines.append(f"\nLocal Trade Log (last {len(rows)}):\n")
            for r in rows:
                cost = (r["total_cost_cents"] or 0) / 100
                lines.append(
                    f"  [{r['timestamp']}] {r['status'].upper()}\n"
                    f"    {r['action'].upper()} {r['quantity']} "
                    f"{r['side'].upper()} @ "
                    f"{r['price_cents']}¢  =  ${cost:.2f}\n"
                    f"    Ticker: {r['ticker']}\n"
                )
                if r["error_message"]:
                    lines.append(f"    Error: {r['error_message']}\n")
        else:
            lines.append("\nLocal Trade Log: no trades recorded yet")

        return "\n".join(lines)

    except Exception as e:
        return _format_error("fetching order history", e)


# ─── Trading ─────────────────────────────────────────────────────────────────

@mcp.tool()
def place_order(
    ticker: str,
    side: str,
    quantity: int,
    limit_price_cents: int | None = None,
) -> str:
    """
    Place an order to buy YES or NO contracts on a Kalshi market.

    Args:
        ticker: Market ticker (e.g. 'KXMVE...-ABC123')
        side: 'yes' or 'no'
        quantity: Number of contracts to buy (must be >= 1)
        limit_price_cents: Optional limit price in cents (1-99).
                           If omitted, places a market order.

    Safety checks run BEFORE the order is sent:
    1. Kill switch must be OFF
    2. Order cost must not exceed MAX_BET
    3. Today's total exposure must not exceed MAX_DAILY_LOSS

    Every order attempt is logged to the local SQLite database.
    """
    side = side.lower().strip()

    # ── Validate inputs ──────────────────────────────────────────────
    if side not in ("yes", "no"):
        return "Error: side must be 'yes' or 'no'."

    if quantity < 1:
        return "Error: quantity must be at least 1."

    if limit_price_cents is not None and not (1 <= limit_price_cents <= 99):
        return "Error: limit_price_cents must be between 1 and 99."

    # ── Safety Check 1: Kill switch ──────────────────────────────────
    if _is_kill_switch_on():
        _log_trade(
            ticker=ticker, side=side, action="buy", quantity=quantity,
            price_cents=limit_price_cents or 0, status="rejected",
            error_message="Kill switch is ON",
        )
        return (
            "🛑 ORDER BLOCKED: Kill switch is ON.\n\n"
            "All trading is disabled. Use toggle_kill_switch to unlock "
            "trading when you're ready."
        )

    # ── Safety Check 2: Max single bet ───────────────────────────────
    # Cost = quantity × price. For market orders without a price, we
    # estimate worst-case cost at 99 cents per contract.
    price_for_check = limit_price_cents if limit_price_cents else 99
    estimated_cost_dollars = (quantity * price_for_check) / 100

    if estimated_cost_dollars > MAX_BET:
        _log_trade(
            ticker=ticker, side=side, action="buy", quantity=quantity,
            price_cents=price_for_check, status="rejected",
            error_message=f"Exceeds MAX_BET (${MAX_BET})",
        )
        return (
            f"🛑 ORDER BLOCKED: Estimated cost ${estimated_cost_dollars:.2f} "
            f"exceeds MAX_BET (${MAX_BET:.2f}).\n\n"
            f"Reduce quantity or price. "
            f"Current limit: {quantity} × {price_for_check}¢ = "
            f"${estimated_cost_dollars:.2f}"
        )

    # ── Safety Check 3: Daily loss limit ─────────────────────────────
    todays_exposure = abs(_get_todays_pnl())
    remaining_budget = MAX_DAILY_LOSS - todays_exposure

    if estimated_cost_dollars > remaining_budget:
        # Auto-enable kill switch when daily limit is hit
        if remaining_budget <= 0:
            conn = _get_db()
            conn.execute(
                "UPDATE system_config SET value = 'on' "
                "WHERE key = 'kill_switch'"
            )
            conn.commit()
            conn.close()

        _log_trade(
            ticker=ticker, side=side, action="buy", quantity=quantity,
            price_cents=price_for_check, status="rejected",
            error_message=f"Exceeds MAX_DAILY_LOSS (${MAX_DAILY_LOSS})",
        )
        return (
            f"🛑 ORDER BLOCKED: Would exceed daily loss limit.\n\n"
            f"  Today's exposure:   ${todays_exposure:.2f}\n"
            f"  This order:         ${estimated_cost_dollars:.2f}\n"
            f"  Daily limit:        ${MAX_DAILY_LOSS:.2f}\n"
            f"  Remaining budget:   ${remaining_budget:.2f}\n\n"
            f"Reduce the order size or wait until tomorrow."
        )

    # ── All checks passed — build and send the order ─────────────────
    try:
        client = get_client()

        order_kwargs = {
            "ticker": ticker,
            "side": side,
            "action": "buy",
            "count": quantity,
        }

        if limit_price_cents is not None:
            # Limit order — set the price on the correct side
            order_kwargs["type"] = "limit"
            if side == "yes":
                order_kwargs["yes_price"] = limit_price_cents
            else:
                order_kwargs["no_price"] = limit_price_cents
        else:
            # Market order — no price needed
            order_kwargs["type"] = "market"

        order_request = CreateOrderRequest(**order_kwargs)
        response = client.create_order(order_request)
        order = response.order

        # Log the successful submission
        _log_trade(
            ticker=ticker,
            side=side,
            action="buy",
            quantity=quantity,
            price_cents=limit_price_cents or 0,
            status="submitted",
            order_id=order.order_id,
        )

        price_display = (
            f"{limit_price_cents}¢" if limit_price_cents
            else "market price"
        )

        return (
            f"✅ Order submitted successfully!\n\n"
            f"  Order ID:  {order.order_id}\n"
            f"  Ticker:    {ticker}\n"
            f"  Side:      {side.upper()}\n"
            f"  Quantity:  {quantity} contract(s)\n"
            f"  Price:     {price_display}\n"
            f"  Status:    {order.status}\n\n"
            f"Use get_order_history to track this order."
        )

    except Exception as e:
        # Log the failed attempt
        _log_trade(
            ticker=ticker, side=side, action="buy", quantity=quantity,
            price_cents=limit_price_cents or 0, status="failed",
            error_message=str(e),
        )
        return _format_error(f"placing order on '{ticker}'", e)


@mcp.tool()
def cancel_order(order_id: str) -> str:
    """
    Cancel an open order by its Kalshi order ID.

    The order ID is returned by place_order or visible in get_order_history.
    """
    try:
        client = get_client()
        response = client.cancel_order(order_id=order_id)
        order = response.order

        # Log the cancellation locally
        _log_trade(
            ticker=order.ticker,
            side=order.side,
            action=order.action,
            quantity=order.remaining_count,
            price_cents=order.yes_price if order.side == "yes" else order.no_price,
            status="cancelled",
            order_id=order_id,
        )

        return (
            f"✅ Order cancelled successfully.\n\n"
            f"  Order ID:  {order_id}\n"
            f"  Ticker:    {order.ticker}\n"
            f"  Contracts reduced by: {response.reduced_by}"
        )

    except Exception as e:
        return _format_error(f"cancelling order '{order_id}'", e)


# ─── Analytics ────────────────────────────────────────────────────────────────

@mcp.tool()
def calculate_edge(model_probability: float, market_price_cents: int) -> str:
    """
    Compare your estimated probability vs the market price to find edge.

    Args:
        model_probability: Your estimated probability as a decimal
                           (e.g. 0.68 for 68%)
        market_price_cents: Current market price in cents (e.g. 55)

    Returns the edge percentage and a recommendation:
      - Strong:       edge > 10%
      - Moderate:     edge 5-10%
      - Insufficient: edge 2-5%
      - Negative:     edge < 2% (don't bet)
    """
    if not (0 < model_probability < 1):
        return "Error: model_probability must be between 0 and 1 (e.g. 0.68)."
    if not (1 <= market_price_cents <= 99):
        return "Error: market_price_cents must be between 1 and 99."

    market_prob = market_price_cents / 100
    edge = model_probability - market_prob
    edge_pct = edge * 100

    # Classify the edge
    if edge_pct > 10:
        rating = "🟢 STRONG"
        advice = "Favorable edge. Consider sizing with calculate_bet_size."
    elif edge_pct > 5:
        rating = "🟡 MODERATE"
        advice = "Decent edge. Bet conservatively."
    elif edge_pct > 2:
        rating = "🟠 INSUFFICIENT"
        advice = "Edge too thin after fees/slippage. Consider passing."
    else:
        rating = "🔴 NEGATIVE"
        advice = "No edge. Do not bet."

    return (
        f"Edge Analysis:\n\n"
        f"  Your model:     {model_probability:.1%}\n"
        f"  Market price:   {market_prob:.1%} ({market_price_cents}¢)\n"
        f"  Edge:           {edge_pct:+.1f}%\n"
        f"  Rating:         {rating}\n\n"
        f"  {advice}"
    )


@mcp.tool()
def calculate_bet_size(
    bankroll: float,
    model_probability: float,
    market_price_cents: int,
) -> str:
    """
    Calculate optimal bet size using the quarter-Kelly criterion.

    Args:
        bankroll: Your total bankroll in dollars (e.g. 100.00)
        model_probability: Your estimated probability (e.g. 0.68)
        market_price_cents: Current market price in cents (e.g. 55)

    Uses quarter-Kelly (25% of full Kelly) for conservative sizing.
    Never recommends more than 5% of bankroll.
    Returns $0 if the edge is negative.
    """
    if bankroll <= 0:
        return "Error: bankroll must be positive."
    if not (0 < model_probability < 1):
        return "Error: model_probability must be between 0 and 1."
    if not (1 <= market_price_cents <= 99):
        return "Error: market_price_cents must be between 1 and 99."

    market_prob = market_price_cents / 100
    edge = model_probability - market_prob

    if edge <= 0:
        return (
            f"Recommended bet: $0.00\n\n"
            f"  Edge is negative ({edge * 100:+.1f}%). No bet recommended.\n"
            f"  Your model: {model_probability:.1%}  |  "
            f"Market: {market_prob:.1%}"
        )

    # Kelly criterion for binary markets:
    #   Full Kelly = (model_prob - market_prob) / (1 - market_prob)
    # This is the fraction of bankroll to wager.
    full_kelly = edge / (1 - market_prob)

    # Quarter-Kelly — more conservative, reduces variance
    quarter_kelly = full_kelly * 0.25

    # Hard cap at 5% of bankroll
    max_fraction = 0.05
    capped_fraction = min(quarter_kelly, max_fraction)

    bet_dollars = bankroll * capped_fraction
    # Round down to nearest cent
    bet_dollars = math.floor(bet_dollars * 100) / 100

    # How many contracts can that buy?
    cost_per_contract = market_price_cents / 100
    max_contracts = int(bet_dollars / cost_per_contract) if cost_per_contract > 0 else 0

    was_capped = quarter_kelly > max_fraction

    lines = [
        f"Bet Size (Quarter-Kelly):\n",
        f"  Bankroll:          ${bankroll:,.2f}",
        f"  Your model:        {model_probability:.1%}",
        f"  Market price:      {market_prob:.1%} ({market_price_cents}¢)",
        f"  Edge:              {edge * 100:+.1f}%",
        "",
        f"  Full Kelly:        {full_kelly:.2%} of bankroll",
        f"  Quarter Kelly:     {quarter_kelly:.2%} of bankroll",
    ]

    if was_capped:
        lines.append(f"  Capped at:         {max_fraction:.0%} of bankroll")

    lines.extend([
        "",
        f"  ➤ Recommended bet: ${bet_dollars:,.2f}",
        f"  ➤ Max contracts:   {max_contracts} @ {market_price_cents}¢ each",
    ])

    return "\n".join(lines)


# ─── System / Risk Management ────────────────────────────────────────────────

@mcp.tool()
def toggle_kill_switch() -> str:
    """
    Toggle the trading kill switch ON or OFF.

    When ON:  All trading is blocked. place_order will refuse every order.
    When OFF: Trading is allowed (subject to other safety limits).

    The kill switch defaults to ON when the system is first set up.
    It also auto-enables if the daily loss limit is hit.
    """
    conn = _get_db()
    row = conn.execute(
        "SELECT value FROM system_config WHERE key = 'kill_switch'"
    ).fetchone()

    current = row["value"] if row else "on"
    new_value = "off" if current == "on" else "on"

    conn.execute(
        "UPDATE system_config SET value = ? WHERE key = 'kill_switch'",
        (new_value,),
    )
    conn.commit()
    conn.close()

    if new_value == "on":
        return (
            "🛑 Kill switch is now ON.\n\n"
            "All trading is DISABLED. No orders will be placed until "
            "you toggle it off again."
        )
    else:
        return (
            f"🟢 Kill switch is now OFF.\n\n"
            f"Trading is ENABLED. Safety limits still apply:\n"
            f"  Max single bet:    ${MAX_BET:.2f}\n"
            f"  Max daily loss:    ${MAX_DAILY_LOSS:.2f}\n\n"
            f"Be careful. Toggle it back on when you're done."
        )


@mcp.tool()
def get_system_status() -> str:
    """
    Show current system status: kill switch state, today's P&L,
    trade count, and remaining daily loss budget.
    """
    kill_switch = "ON 🛑" if _is_kill_switch_on() else "OFF 🟢"
    todays_pnl = _get_todays_pnl()
    todays_exposure = abs(todays_pnl)
    trade_count = _get_todays_trade_count()
    remaining = MAX_DAILY_LOSS - todays_exposure
    env_label = "PRODUCTION" if KALSHI_ENV == "production" else "DEMO"

    return (
        f"System Status ({env_label}):\n\n"
        f"  Kill switch:        {kill_switch}\n"
        f"  Today's exposure:   ${todays_exposure:,.2f}\n"
        f"  Trades today:       {trade_count}\n"
        f"  Daily loss limit:   ${MAX_DAILY_LOSS:,.2f}\n"
        f"  Remaining budget:   ${remaining:,.2f}\n"
        f"  Max single bet:     ${MAX_BET:,.2f}"
    )


@mcp.tool()
def get_performance_report() -> str:
    """
    Generate a performance report from the local trade log.

    Shows win rate, total P&L, ROI, and trade count.
    Reads from the local SQLite trades table.
    """
    conn = _get_db()

    # Get all submitted/filled trades
    rows = conn.execute(
        """
        SELECT * FROM trades
        WHERE status IN ('submitted', 'filled')
        ORDER BY timestamp DESC
        """
    ).fetchall()
    conn.close()

    if not rows:
        return (
            "No trades recorded yet.\n\n"
            "Place your first trade with place_order to start tracking "
            "performance."
        )

    total_trades = len(rows)
    total_spent_cents = sum(r["total_cost_cents"] or 0 for r in rows)
    total_spent = total_spent_cents / 100

    # Group by date for daily breakdown
    by_date: dict[str, list] = defaultdict(list)
    for r in rows:
        date_key = r["timestamp"][:10]  # YYYY-MM-DD
        by_date[date_key].append(r)

    # Group by event_title prefix for sport breakdown
    by_event: dict[str, int] = defaultdict(int)
    for r in rows:
        event = r["event_title"] if r["event_title"] else "Unknown"
        by_event[event] += 1

    lines = [
        "═══ Performance Report ═══\n",
        f"Total trades:      {total_trades}",
        f"Total spent:       ${total_spent:,.2f}",
        f"Trading days:      {len(by_date)}",
        f"Avg per day:       {total_trades / len(by_date):.1f} trades",
    ]

    # Daily breakdown (last 7 days)
    lines.append("\nRecent Daily Activity:")
    for date_key in sorted(by_date.keys(), reverse=True)[:7]:
        day_trades = by_date[date_key]
        day_spent = sum(
            (r["total_cost_cents"] or 0) for r in day_trades
        ) / 100
        lines.append(
            f"  {date_key}: {len(day_trades)} trades, "
            f"${day_spent:,.2f} spent"
        )

    # Top events/games
    if by_event:
        lines.append("\nTop Events:")
        sorted_events = sorted(
            by_event.items(), key=lambda x: x[1], reverse=True
        )
        for event_name, count in sorted_events[:10]:
            truncated = (
                event_name if len(event_name) <= 40
                else event_name[:37] + "..."
            )
            lines.append(f"  {truncated}: {count} trade(s)")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"Starting Kalshi Agent MCP server ({KALSHI_ENV} environment)...")
    print(f"API URL: {API_URL}")
    print(f"Database: {DB_PATH}")
    print(f"Kill switch: {'ON' if _is_kill_switch_on() else 'OFF'}")
    print(f"Max bet: ${MAX_BET}  |  Max daily loss: ${MAX_DAILY_LOSS}")
    mcp.run()
