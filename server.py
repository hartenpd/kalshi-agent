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
from kalshi_python_sync.models.order import Order as _OrderModel
from kalshi_python_sync.models.market_position import MarketPosition as _MarketPositionModel
from kalshi_python_sync.models.event_position import EventPosition as _EventPositionModel
from kalshi_python_sync.models.fill import Fill as _FillModel



# ══════════════════════════════════════════════════════════════════════════════
# SDK COMPATIBILITY PATCHES
# ══════════════════════════════════════════════════════════════════════════════
# The Kalshi API returns data that doesn't match the SDK's strict Pydantic
# models. We patch from_dict() on each model to fix the data before it hits
# Pydantic validation, so the SDK doesn't crash.

# Patch 1: Market model — the API now returns dollar-string fields
# (e.g. yes_bid_dollars: "0.2800") and sends null for the old
# cent-integer fields (yes_bid, volume, etc.). The SDK marks these
# as required non-nullable, so we default nulls to 0.
_original_market_from_dict = _MarketModel.from_dict.__func__

# Fields the SDK expects as non-nullable int or float, but the API
# now sends as null (replaced by *_dollars / *_fp string equivalents).
_NUMERIC_DEFAULTS = {
    "yes_bid": 0, "yes_ask": 0, "no_bid": 0, "no_ask": 0,
    "last_price": 0, "volume": 0, "volume_24h": 0,
    "open_interest": 0, "notional_value": 0,
    "previous_yes_bid": 0, "previous_yes_ask": 0,
    "previous_price": 0, "liquidity": 0,
    "risk_limit_cents": 0,
}
# String fields the SDK expects as non-nullable str.
_STRING_DEFAULTS = {
    "category": "",
    "yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0000",
    "no_bid_dollars": "0.0000", "no_ask_dollars": "0.0000",
    "last_price_dollars": "0.0000",
    "notional_value_dollars": "0.0000",
    "previous_yes_bid_dollars": "0.0000",
    "previous_yes_ask_dollars": "0.0000",
    "previous_price_dollars": "0.0000",
    "liquidity_dollars": "0.0000",
}


@classmethod  # type: ignore[misc]
def _patched_market_from_dict(cls, obj):
    if isinstance(obj, dict):
        # Default null numeric fields to 0
        for field, default in _NUMERIC_DEFAULTS.items():
            if obj.get(field) is None:
                obj[field] = default
        # Default null string fields to their safe defaults
        for field, default in _STRING_DEFAULTS.items():
            if obj.get(field) is None:
                obj[field] = default
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

# Patch 3: Order model — the API returns null for several int fields
# (yes_price, no_price, fill_count, etc.) that the SDK marks as required
# non-nullable. Default them to 0 so Pydantic doesn't crash.
_original_order_from_dict = _OrderModel.from_dict.__func__

_ORDER_NUMERIC_DEFAULTS = {
    "yes_price": 0, "no_price": 0,
    "fill_count": 0, "remaining_count": 0, "initial_count": 0,
    "taker_fees": 0, "maker_fees": 0,
    "taker_fill_cost": 0, "maker_fill_cost": 0,
    "queue_position": 0,
}
_ORDER_STRING_DEFAULTS = {
    "yes_price_dollars": "0.0000", "no_price_dollars": "0.0000",
    "taker_fill_cost_dollars": "0.0000", "maker_fill_cost_dollars": "0.0000",
}


@classmethod  # type: ignore[misc]
def _patched_order_from_dict(cls, obj):
    if isinstance(obj, dict):
        for field, default in _ORDER_NUMERIC_DEFAULTS.items():
            if obj.get(field) is None:
                obj[field] = default
        for field, default in _ORDER_STRING_DEFAULTS.items():
            if obj.get(field) is None:
                obj[field] = default
    return _original_order_from_dict(cls, obj)


_OrderModel.from_dict = _patched_order_from_dict

# Patch 4: MarketPosition model — API returns null for int fields that now
# have *_dollars string equivalents. Default nulls to 0 so Pydantic is happy.
_original_position_from_dict = _MarketPositionModel.from_dict.__func__

_POSITION_NUMERIC_DEFAULTS = {
    "total_traded": 0, "position": 0, "market_exposure": 0,
    "realized_pnl": 0, "resting_orders_count": 0, "fees_paid": 0,
}
_POSITION_STRING_DEFAULTS = {
    "total_traded_dollars": "0.0000", "market_exposure_dollars": "0.0000",
    "realized_pnl_dollars": "0.0000", "fees_paid_dollars": "0.0000",
}


@classmethod  # type: ignore[misc]
def _patched_position_from_dict(cls, obj):
    if isinstance(obj, dict):
        for field, default in _POSITION_NUMERIC_DEFAULTS.items():
            if obj.get(field) is None:
                obj[field] = default
        for field, default in _POSITION_STRING_DEFAULTS.items():
            if obj.get(field) is None:
                obj[field] = default
    return _original_position_from_dict(cls, obj)


_MarketPositionModel.from_dict = _patched_position_from_dict

# Patch 4b: EventPosition model — same null-int pattern as MarketPosition.
_original_event_position_from_dict = _EventPositionModel.from_dict.__func__

_EVENT_POS_NUMERIC_DEFAULTS = {
    "total_cost": 0, "total_cost_shares": 0, "event_exposure": 0,
    "realized_pnl": 0, "fees_paid": 0,
}
_EVENT_POS_STRING_DEFAULTS = {
    "total_cost_dollars": "0.0000", "event_exposure_dollars": "0.0000",
    "realized_pnl_dollars": "0.0000", "fees_paid_dollars": "0.0000",
}


@classmethod  # type: ignore[misc]
def _patched_event_position_from_dict(cls, obj):
    if isinstance(obj, dict):
        for field, default in _EVENT_POS_NUMERIC_DEFAULTS.items():
            if obj.get(field) is None:
                obj[field] = default
        for field, default in _EVENT_POS_STRING_DEFAULTS.items():
            if obj.get(field) is None:
                obj[field] = default
    return _original_event_position_from_dict(cls, obj)


_EventPositionModel.from_dict = _patched_event_position_from_dict

# Patch 5: Fill model — API returns null for int/float fields (count, price,
# yes_price, no_price) that the SDK marks as required non-nullable.
_original_fill_from_dict = _FillModel.from_dict.__func__

_FILL_NUMERIC_DEFAULTS = {
    "count": 0, "yes_price": 0, "no_price": 0, "price": 0,
}
_FILL_STRING_DEFAULTS = {
    "yes_price_fixed": "0.0000", "no_price_fixed": "0.0000",
}


@classmethod  # type: ignore[misc]
def _patched_fill_from_dict(cls, obj):
    if isinstance(obj, dict):
        for field, default in _FILL_NUMERIC_DEFAULTS.items():
            if obj.get(field) is None:
                obj[field] = default
        for field, default in _FILL_STRING_DEFAULTS.items():
            if obj.get(field) is None:
                obj[field] = default
    return _original_fill_from_dict(cls, obj)


_FillModel.from_dict = _patched_fill_from_dict


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

    # Analyst picks — logs every pick from any analyst MCP server,
    # whether or not we actually trade it. Used for calibration tracking.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS analyst_picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            sport TEXT NOT NULL,              -- 'NBA', 'MLS', 'EPL', etc.
            game TEXT NOT NULL,               -- 'Cleveland at Orlando'
            game_date TEXT NOT NULL,           -- '2026-03-11'
            pick TEXT NOT NULL,               -- 'Cleveland' or 'home'
            confidence INTEGER NOT NULL,      -- 1-5 stars
            model_probability REAL NOT NULL,  -- 0.70
            market_price INTEGER,             -- Kalshi price in cents, NULL if no market
            edge REAL,                        -- calculated edge, NULL if no market
            bet_placed INTEGER NOT NULL DEFAULT 0,  -- 0 or 1
            bet_amount REAL,                  -- dollars, NULL if no bet
            outcome TEXT NOT NULL DEFAULT 'pending', -- 'win', 'loss', 'push', 'pending'
            pnl REAL,                         -- profit/loss in dollars, NULL until settled
            methodology TEXT NOT NULL DEFAULT 'flat_v1'  -- tracks which model version made this pick
        )
    """)

    # Migration: add methodology column to existing databases that don't have it yet
    existing_cols = [
        row[1] for row in cursor.execute("PRAGMA table_info(analyst_picks)").fetchall()
    ]
    if "methodology" not in existing_cols:
        cursor.execute(
            "ALTER TABLE analyst_picks ADD COLUMN methodology TEXT NOT NULL DEFAULT 'flat_v1'"
        )

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


class KillSwitchError(Exception):
    """Raised when an order is attempted while the kill switch is ON."""
    pass


def _safe_create_order(**order_kwargs) -> object:
    """
    The ONLY function in this project that is allowed to call
    client.create_order().  Every order placement MUST go through here.

    Safety guarantee: re-checks the kill switch immediately before the
    API call, closing the race-condition window between the tool-level
    check and the actual HTTP request.

    Raises:
        KillSwitchError  – if the kill switch is ON
        Exception        – any error from the Kalshi SDK / network
    """
    # Final kill-switch gate — this is the last line of defense.
    if _is_kill_switch_on():
        raise KillSwitchError(
            "BLOCKED: Kill switch is ON at the moment of order submission. "
            "This is the safety wrapper's final check."
        )

    client = get_client()
    return client.create_order(**order_kwargs)


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


def _dollars(market, field: str) -> float:
    """
    Read a price/volume from a market, preferring the new dollar-string
    fields over the deprecated cent-integer fields.

    The Kalshi API migrated from cent integers (yes_bid=28) to dollar
    strings (yes_bid_dollars="0.2800"). The old fields are now null.
    This helper reads the dollars field first, falls back to cents / 100.
    """
    # Try the _dollars or _fp string field first (these have real data)
    dollars_field = f"{field}_dollars"
    fp_field = f"{field}_fp"
    for attr in (dollars_field, fp_field):
        val = getattr(market, attr, None)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    # Fall back to the cent-integer field (deprecated, usually 0 now)
    val = getattr(market, field, None)
    if val is not None:
        return val / 100
    return 0.0


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
                yes_price = _dollars(m, "yes_bid")
                no_price = _dollars(m, "no_bid")
                volume = _dollars(m, "volume")
                close_str = str(m.close_time) if m.close_time else "N/A"
                title = (
                    m.title if len(m.title) <= 80
                    else m.title[:77] + "..."
                )
                lines.append(
                    f"  Ticker: {m.ticker}\n"
                    f"  Title:  {title}\n"
                    f"  YES: ${yes_price:.2f}  |  NO: ${no_price:.2f}  |  "
                    f"Vol: {volume:.0f}  |  Closes: {close_str}\n"
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

        yes_bid = _dollars(m, "yes_bid")
        yes_ask = _dollars(m, "yes_ask")
        no_bid = _dollars(m, "no_bid")
        no_ask = _dollars(m, "no_ask")
        last_price = _dollars(m, "last_price")
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
            f"Volume:         {_dollars(m, 'volume'):.0f}",
            f"24h Volume:     {_dollars(m, 'volume_24h'):.0f}",
            f"Open Interest:  {_dollars(m, 'open_interest'):.0f}",
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
            # Prefer _dollars fields (always populated) over deprecated
            # cents fields (now null from API, defaulted to 0 by our patch).
            exposure = float(p.market_exposure_dollars)
            pnl = float(p.realized_pnl_dollars)

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
                    # Prefer _fixed dollar fields (always populated) over
                    # deprecated cents fields (now null, defaulted to 0).
                    price_str = (
                        f.yes_price_fixed if f.side == "yes"
                        else f.no_price_fixed
                    )
                    time_str = (
                        str(f.created_time) if f.created_time else "N/A"
                    )
                    lines.append(
                        f"  {f.ticker}\n"
                        f"    {f.action.upper()} {f.count} {f.side.upper()} "
                        f"@ ${price_str}  |  {time_str}\n"
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
    #
    # IMPORTANT: The API call and response parsing are separated so that
    # if the HTTP request succeeds (order went through on Kalshi) but
    # the SDK's Pydantic model fails to parse the response, we still
    # log the trade as "submitted" — not "failed".  Only a genuine API
    # error (4xx/5xx, network failure) should be logged as "failed".
    #
    # All orders go through _safe_create_order() which re-checks the
    # kill switch as a final safety gate before the API call.

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

    # ── Step 1: Send the order to Kalshi via safety wrapper ─────────
    # _safe_create_order() re-checks the kill switch immediately before
    # the API call, closing any race-condition window.
    # If the call fails, the order never reached Kalshi → log as "failed".
    try:
        response = _safe_create_order(**order_kwargs)
    except KillSwitchError as ks:
        # Kill switch was toggled ON between the tool-level check and now
        _log_trade(
            ticker=ticker, side=side, action="buy", quantity=quantity,
            price_cents=limit_price_cents or 0, status="rejected",
            error_message=str(ks),
        )
        return (
            "🛑 ORDER BLOCKED: Kill switch was enabled between safety "
            "checks and order submission (race condition caught).\n\n"
            "No order was sent to Kalshi."
        )
    except Exception as e:
        _log_trade(
            ticker=ticker, side=side, action="buy", quantity=quantity,
            price_cents=limit_price_cents or 0, status="failed",
            error_message=str(e),
        )
        return _format_error(f"placing order on '{ticker}'", e)

    # ── Step 2: Parse the response ──────────────────────────────────
    # If we get here, the API accepted the order (HTTP 2xx). The order
    # went through regardless of whether the SDK can parse the response.
    # Log as "submitted" even if parsing blows up.
    try:
        order = response.order
        order_id = order.order_id
        order_status = order.status
    except Exception as parse_err:
        # API succeeded but response parsing failed (Pydantic issues).
        # Log as submitted with a warning — the order IS on Kalshi.
        _log_trade(
            ticker=ticker, side=side, action="buy", quantity=quantity,
            price_cents=limit_price_cents or 0, status="submitted",
            order_id="",
            error_message=f"Order submitted but response parse failed: {parse_err}",
        )

        price_display = (
            f"{limit_price_cents}¢" if limit_price_cents
            else "market price"
        )
        return (
            f"⚠️ Order submitted to Kalshi but response parsing failed.\n\n"
            f"  Ticker:    {ticker}\n"
            f"  Side:      {side.upper()}\n"
            f"  Quantity:  {quantity} contract(s)\n"
            f"  Price:     {price_display}\n\n"
            f"  Parse error: {parse_err}\n\n"
            f"The order likely went through. Use get_positions or "
            f"reconcile_positions to verify."
        )

    # ── Happy path: API call + parsing both succeeded ───────────────
    _log_trade(
        ticker=ticker,
        side=side,
        action="buy",
        quantity=quantity,
        price_cents=limit_price_cents or 0,
        status="submitted",
        order_id=order_id,
    )

    price_display = (
        f"{limit_price_cents}¢" if limit_price_cents
        else "market price"
    )

    return (
        f"✅ Order submitted successfully!\n\n"
        f"  Order ID:  {order_id}\n"
        f"  Ticker:    {ticker}\n"
        f"  Side:      {side.upper()}\n"
        f"  Quantity:  {quantity} contract(s)\n"
        f"  Price:     {price_display}\n"
        f"  Status:    {order_status}\n\n"
        f"Use get_order_history to track this order."
    )


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
def get_performance_report(
    methodology: str = "market_aware_v1",
    compare_all: bool = False,
) -> str:
    """
    Generate a performance report from the local trade log and analyst picks.

    Shows trade activity plus analyst pick performance filtered by methodology.

    Args:
        methodology: Only include analyst picks from this model version (default 'market_aware_v1').
        compare_all: If True, show analyst pick stats for all methodologies side-by-side.
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

    # ── Analyst Pick Performance ─────────────────────────────────────
    conn2 = _get_db()
    if compare_all:
        pick_rows = conn2.execute(
            """
            SELECT * FROM analyst_picks
            WHERE outcome IN ('win', 'loss', 'push')
            ORDER BY methodology
            """
        ).fetchall()
    else:
        pick_rows = conn2.execute(
            """
            SELECT * FROM analyst_picks
            WHERE outcome IN ('win', 'loss', 'push') AND methodology = ?
            """,
            (methodology,),
        ).fetchall()
    conn2.close()

    if pick_rows:
        lines.append("\n── Analyst Pick Performance ──")

        if compare_all:
            # Side-by-side methodology comparison
            methods: dict[str, list] = defaultdict(list)
            for r in pick_rows:
                methods[r["methodology"]].append(r)

            lines.append(
                f"  {'Methodology':<20} {'Picks':>5}  {'Win%':>5}  {'P&L':>9}"
            )
            for method_name in sorted(methods.keys()):
                m_rows = methods[method_name]
                m_wins = sum(1 for r in m_rows if r["outcome"] == "win")
                m_wl = sum(1 for r in m_rows if r["outcome"] in ("win", "loss"))
                m_wr = m_wins / m_wl if m_wl > 0 else 0
                m_bets = [r for r in m_rows if r["bet_placed"] and r["pnl"] is not None]
                m_pnl = sum(r["pnl"] for r in m_bets)
                lines.append(
                    f"  {method_name:<20} {len(m_rows):>5}  {m_wr:>5.0%}  ${m_pnl:>+8,.2f}"
                )
        else:
            # Single methodology summary
            p_wins = sum(1 for r in pick_rows if r["outcome"] == "win")
            p_wl = sum(1 for r in pick_rows if r["outcome"] in ("win", "loss"))
            p_wr = p_wins / p_wl if p_wl > 0 else 0
            p_bets = [r for r in pick_rows if r["bet_placed"] and r["pnl"] is not None]
            p_pnl = sum(r["pnl"] for r in p_bets)
            lines.append(
                f"  Methodology:     {methodology}\n"
                f"  Settled picks:   {len(pick_rows)}\n"
                f"  Win rate:        {p_wr:.0%}\n"
                f"  P&L from bets:   ${p_pnl:+,.2f}"
            )

    return "\n".join(lines)


# ─── Analyst Pick Tracking ────────────────────────────────────────────────────

@mcp.tool()
def log_analyst_pick(
    sport: str,
    game: str,
    game_date: str,
    pick: str,
    confidence: int,
    model_probability: float,
    market_price: int | None = None,
    edge: float | None = None,
    bet_placed: bool = False,
    bet_amount: float | None = None,
    methodology: str = "market_aware_v1",
) -> str:
    """
    Log a pick from any analyst to the calibration tracker.

    Call this for EVERY pick — not just ones we trade. This builds
    the dataset needed for calibration analysis (predicted vs actual).

    Args:
        sport: League name — 'NBA', 'MLS', 'EPL', 'NHL', etc.
        game: Matchup description — 'Cleveland at Orlando'
        game_date: Date of the game — '2026-03-11'
        pick: Which side — 'Cleveland', 'home', 'Over 220.5', etc.
        confidence: Analyst confidence 1-5 stars
        model_probability: Model's estimated win probability (e.g. 0.70)
        market_price: Kalshi market price in cents (e.g. 55). None if no market found.
        edge: Calculated edge as decimal (e.g. 0.15). None if no market.
        bet_placed: Whether we actually placed a trade on this pick
        bet_amount: How much we bet in dollars. None if no bet.
        methodology: Which model version made this pick (e.g. 'market_aware_v1').
            Used for A/B tracking between model versions.

    Outcome defaults to 'pending' — use settle_analyst_pick after the game.
    """
    if not (1 <= confidence <= 5):
        return "Error: confidence must be between 1 and 5."
    if not (0 < model_probability < 1):
        return "Error: model_probability must be between 0 and 1."

    conn = _get_db()
    cursor = conn.execute(
        """
        INSERT INTO analyst_picks
            (sport, game, game_date, pick, confidence, model_probability,
             market_price, edge, bet_placed, bet_amount, outcome, methodology)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            sport.upper(),
            game,
            game_date,
            pick,
            confidence,
            model_probability,
            market_price,
            edge,
            1 if bet_placed else 0,
            bet_amount,
            methodology,
        ),
    )
    pick_id = cursor.lastrowid
    conn.commit()
    conn.close()

    edge_str = f"{edge * 100:+.1f}%" if edge is not None else "N/A"
    market_str = f"{market_price}¢" if market_price is not None else "no market"
    bet_str = f"${bet_amount:.2f}" if bet_amount else "no bet"

    return (
        f"Pick #{pick_id} logged.\n\n"
        f"  Sport:       {sport.upper()}\n"
        f"  Game:        {game}\n"
        f"  Date:        {game_date}\n"
        f"  Pick:        {pick}\n"
        f"  Confidence:  {'★' * confidence}{'☆' * (5 - confidence)}\n"
        f"  Model prob:  {model_probability:.0%}\n"
        f"  Market:      {market_str}\n"
        f"  Edge:        {edge_str}\n"
        f"  Bet:         {bet_str}\n"
        f"  Methodology: {methodology}\n"
        f"  Outcome:     pending"
    )


@mcp.tool()
def settle_analyst_pick(pick_id: int, outcome: str) -> str:
    """
    Settle a pick after the game is played.

    Args:
        pick_id: ID of the pick to settle (from log_analyst_pick)
        outcome: 'win', 'loss', or 'push'

    Calculates P&L automatically if a bet was placed:
      - Win:  profit = bet_amount × (1 / market_probability - 1)
      - Loss: profit = -bet_amount
      - Push: profit = 0
    """
    outcome = outcome.lower().strip()
    if outcome not in ("win", "loss", "push"):
        return "Error: outcome must be 'win', 'loss', or 'push'."

    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM analyst_picks WHERE id = ?", (pick_id,)
    ).fetchone()

    if not row:
        conn.close()
        return f"Error: pick #{pick_id} not found."

    if row["outcome"] != "pending":
        conn.close()
        return (
            f"Pick #{pick_id} is already settled as '{row['outcome']}'. "
            f"No changes made."
        )

    # Calculate P&L if a bet was placed
    pnl = None
    if row["bet_placed"] and row["bet_amount"]:
        if outcome == "win":
            # Payout is $1 per contract, cost was market_price cents
            # Profit = payout - cost. For bet_amount dollars at market_price:
            # contracts = bet_amount / (market_price / 100)
            # profit = contracts * (1 - market_price/100)
            if row["market_price"] and row["market_price"] > 0:
                market_frac = row["market_price"] / 100
                pnl = row["bet_amount"] * (1 / market_frac - 1)
            else:
                # No market price recorded — estimate from model
                pnl = row["bet_amount"] * (1 / row["model_probability"] - 1)
        elif outcome == "loss":
            pnl = -row["bet_amount"]
        else:  # push
            pnl = 0.0

        # Round to 2 decimal places
        pnl = round(pnl, 2)

    conn.execute(
        "UPDATE analyst_picks SET outcome = ?, pnl = ? WHERE id = ?",
        (outcome, pnl, pick_id),
    )
    conn.commit()
    conn.close()

    pnl_str = f"${pnl:+,.2f}" if pnl is not None else "N/A (no bet)"

    return (
        f"Pick #{pick_id} settled.\n\n"
        f"  Game:     {row['game']}\n"
        f"  Pick:     {row['pick']}\n"
        f"  Outcome:  {outcome.upper()}\n"
        f"  P&L:      {pnl_str}"
    )


@mcp.tool()
def list_analyst_picks(
    status: str | None = None,
    sport: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    methodology: str | None = None,
    limit: int = 20,
) -> str:
    """
    List analyst picks with optional filters, newest first.

    Read-only — just viewing, no changes.

    Args:
        status: Filter by outcome — 'pending', 'win', 'loss', or 'push'. None = all.
        sport: Filter by league — 'NBA', 'MLS', 'EPL', etc. None = all.
        start_date: Only picks on or after this date — '2026-03-01'. None = no lower bound.
        end_date: Only picks on or before this date — '2026-03-14'. None = no upper bound.
        methodology: Filter by model version — 'flat_v1', 'market_aware_v1', etc. None = all.
        limit: Max picks to return (default 20). Use 0 for all.
    """
    # Validate filters
    if status is not None:
        status = status.lower().strip()
        if status not in ("pending", "win", "loss", "push"):
            return "Error: status must be 'pending', 'win', 'loss', or 'push'."

    clauses = []
    params: list = []

    if status is not None:
        clauses.append("outcome = ?")
        params.append(status)
    if sport is not None:
        clauses.append("sport = ?")
        params.append(sport.upper())
    if start_date is not None:
        clauses.append("game_date >= ?")
        params.append(start_date)
    if end_date is not None:
        clauses.append("game_date <= ?")
        params.append(end_date)
    if methodology is not None:
        clauses.append("methodology = ?")
        params.append(methodology)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_clause = f"LIMIT {limit}" if limit > 0 else ""

    conn = _get_db()
    rows = conn.execute(
        f"SELECT * FROM analyst_picks {where} ORDER BY game_date DESC, id DESC {limit_clause}",
        params,
    ).fetchall()
    conn.close()

    if not rows:
        return "No picks found matching those filters."

    # Build header showing active filters
    filters = []
    if status:
        filters.append(f"status={status}")
    if sport:
        filters.append(f"sport={sport.upper()}")
    if start_date:
        filters.append(f"from {start_date}")
    if end_date:
        filters.append(f"to {end_date}")
    if methodology:
        filters.append(f"methodology={methodology}")
    filter_str = f" ({', '.join(filters)})" if filters else ""

    lines = [f"Analyst Picks{filter_str} — {len(rows)} result(s)\n"]

    for row in rows:
        edge_str = f"{row['edge'] * 100:+.1f}%" if row["edge"] is not None else "N/A"
        market_str = f"{row['market_price']}¢" if row["market_price"] is not None else "no market"
        bet_str = f"${row['bet_amount']:.2f}" if row["bet_amount"] else "no bet"
        pnl_str = f"${row['pnl']:+,.2f}" if row["pnl"] is not None else "N/A"

        lines.append(
            f"  #{row['id']}  {row['game_date']}  [{row['sport']}]  "
            f"{row['game']}  →  {row['pick']}\n"
            f"      Confidence: {'★' * row['confidence']}{'☆' * (5 - row['confidence'])}  "
            f"Model: {row['model_probability']:.0%}  Market: {market_str}  Edge: {edge_str}\n"
            f"      Outcome: {row['outcome']}  Bet: {bet_str}  P&L: {pnl_str}  "
            f"Method: {row['methodology']}\n"
        )

    return "\n".join(lines)


@mcp.tool()
def get_analyst_pick(pick_id: int) -> str:
    """
    Get full details for a single analyst pick by ID.

    Read-only — just viewing, no changes.

    Args:
        pick_id: ID of the pick to view (from log_analyst_pick)
    """
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM analyst_picks WHERE id = ?", (pick_id,)
    ).fetchone()
    conn.close()

    if not row:
        return f"Error: pick #{pick_id} not found."

    edge_str = f"{row['edge'] * 100:+.1f}%" if row["edge"] is not None else "N/A"
    market_str = f"{row['market_price']}¢" if row["market_price"] is not None else "no market"
    bet_str = f"${row['bet_amount']:.2f}" if row["bet_amount"] else "no bet"
    pnl_str = f"${row['pnl']:+,.2f}" if row["pnl"] is not None else "N/A"

    return (
        f"Pick #{row['id']}\n\n"
        f"  Logged:      {row['timestamp']}\n"
        f"  Sport:       {row['sport']}\n"
        f"  Game:        {row['game']}\n"
        f"  Date:        {row['game_date']}\n"
        f"  Pick:        {row['pick']}\n"
        f"  Confidence:  {'★' * row['confidence']}{'☆' * (5 - row['confidence'])}\n"
        f"  Model prob:  {row['model_probability']:.0%}\n"
        f"  Market:      {market_str}\n"
        f"  Edge:        {edge_str}\n"
        f"  Bet placed:  {'Yes' if row['bet_placed'] else 'No'}\n"
        f"  Bet amount:  {bet_str}\n"
        f"  Methodology: {row['methodology']}\n"
        f"  Outcome:     {row['outcome']}\n"
        f"  P&L:         {pnl_str}"
    )


@mcp.tool()
def delete_analyst_pick(pick_id: int) -> str:
    """
    Delete a pick from the calibration tracker entirely.

    Returns the pick details so you can confirm with the user before
    calling this tool. Once deleted, the pick cannot be recovered.

    Args:
        pick_id: ID of the pick to delete (from log_analyst_pick)
    """
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM analyst_picks WHERE id = ?", (pick_id,)
    ).fetchone()

    if not row:
        conn.close()
        return f"Error: pick #{pick_id} not found."

    # Build detail string before deleting
    edge_str = f"{row['edge'] * 100:+.1f}%" if row["edge"] is not None else "N/A"
    market_str = f"{row['market_price']}¢" if row["market_price"] is not None else "no market"
    bet_str = f"${row['bet_amount']:.2f}" if row["bet_amount"] else "no bet"
    pnl_str = f"${row['pnl']:+,.2f}" if row["pnl"] is not None else "N/A"

    conn.execute("DELETE FROM analyst_picks WHERE id = ?", (pick_id,))
    conn.commit()
    conn.close()

    return (
        f"Pick #{pick_id} DELETED.\n\n"
        f"  Sport:       {row['sport']}\n"
        f"  Game:        {row['game']}\n"
        f"  Date:        {row['game_date']}\n"
        f"  Pick:        {row['pick']}\n"
        f"  Confidence:  {'★' * row['confidence']}{'☆' * (5 - row['confidence'])}\n"
        f"  Model prob:  {row['model_probability']:.0%}\n"
        f"  Market:      {market_str}\n"
        f"  Edge:        {edge_str}\n"
        f"  Bet:         {bet_str}\n"
        f"  Outcome:     {row['outcome']}\n"
        f"  P&L:         {pnl_str}"
    )


@mcp.tool()
def edit_analyst_pick(
    pick_id: int,
    sport: str | None = None,
    game: str | None = None,
    game_date: str | None = None,
    pick: str | None = None,
    confidence: int | None = None,
    model_probability: float | None = None,
    market_price: int | None = None,
    edge: float | None = None,
    bet_placed: bool | None = None,
    bet_amount: float | None = None,
    outcome: str | None = None,
    pnl: float | None = None,
    methodology: str | None = None,
) -> str:
    """
    Update any field on an existing analyst pick.

    Only supply the fields you want to change — everything else stays
    the same. Useful for correcting typos, fixing a wrong probability,
    or updating bet details after the fact.

    Args:
        pick_id: ID of the pick to edit
        sport: New league name (e.g. 'NBA')
        game: New matchup description
        game_date: New game date (e.g. '2026-03-15')
        pick: New pick value
        confidence: New confidence 1-5
        model_probability: New model probability (0-1)
        market_price: New market price in cents
        edge: New edge as decimal
        bet_placed: Whether a bet was placed
        bet_amount: New bet amount in dollars
        outcome: New outcome ('pending', 'win', 'loss', 'push')
        pnl: New P&L value in dollars
        methodology: Model version tag (e.g. 'flat_v1', 'market_aware_v1')
    """
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM analyst_picks WHERE id = ?", (pick_id,)
    ).fetchone()

    if not row:
        conn.close()
        return f"Error: pick #{pick_id} not found."

    # Validate optional fields
    if confidence is not None and not (1 <= confidence <= 5):
        conn.close()
        return "Error: confidence must be between 1 and 5."
    if model_probability is not None and not (0 < model_probability < 1):
        conn.close()
        return "Error: model_probability must be between 0 and 1."
    if outcome is not None and outcome.lower().strip() not in (
        "pending", "win", "loss", "push"
    ):
        conn.close()
        return "Error: outcome must be 'pending', 'win', 'loss', or 'push'."

    # Build SET clause from provided fields only
    updates = {}
    if sport is not None:
        updates["sport"] = sport.upper()
    if game is not None:
        updates["game"] = game
    if game_date is not None:
        updates["game_date"] = game_date
    if pick is not None:
        updates["pick"] = pick
    if confidence is not None:
        updates["confidence"] = confidence
    if model_probability is not None:
        updates["model_probability"] = model_probability
    if market_price is not None:
        updates["market_price"] = market_price
    if edge is not None:
        updates["edge"] = edge
    if bet_placed is not None:
        updates["bet_placed"] = 1 if bet_placed else 0
    if bet_amount is not None:
        updates["bet_amount"] = bet_amount
    if outcome is not None:
        updates["outcome"] = outcome.lower().strip()
    if pnl is not None:
        updates["pnl"] = pnl
    if methodology is not None:
        updates["methodology"] = methodology

    if not updates:
        conn.close()
        return "No fields to update. Supply at least one field to change."

    # Build and execute the UPDATE query
    set_clause = ", ".join(f"{col} = ?" for col in updates)
    values = list(updates.values()) + [pick_id]
    conn.execute(
        f"UPDATE analyst_picks SET {set_clause} WHERE id = ?", values
    )
    conn.commit()

    # Re-read the updated row
    updated = conn.execute(
        "SELECT * FROM analyst_picks WHERE id = ?", (pick_id,)
    ).fetchone()
    conn.close()

    # Show what changed
    changed = ", ".join(updates.keys())
    edge_str = f"{updated['edge'] * 100:+.1f}%" if updated["edge"] is not None else "N/A"
    market_str = f"{updated['market_price']}¢" if updated["market_price"] is not None else "no market"
    bet_str = f"${updated['bet_amount']:.2f}" if updated["bet_amount"] else "no bet"
    pnl_str = f"${updated['pnl']:+,.2f}" if updated["pnl"] is not None else "N/A"

    return (
        f"Pick #{pick_id} updated. Changed: {changed}\n\n"
        f"  Sport:       {updated['sport']}\n"
        f"  Game:        {updated['game']}\n"
        f"  Date:        {updated['game_date']}\n"
        f"  Pick:        {updated['pick']}\n"
        f"  Confidence:  {'★' * updated['confidence']}{'☆' * (5 - updated['confidence'])}\n"
        f"  Model prob:  {updated['model_probability']:.0%}\n"
        f"  Market:      {market_str}\n"
        f"  Edge:        {edge_str}\n"
        f"  Bet:         {bet_str}\n"
        f"  Methodology: {updated['methodology']}\n"
        f"  Outcome:     {updated['outcome']}\n"
        f"  P&L:         {pnl_str}"
    )


@mcp.tool()
def get_calibration_report(
    methodology: str = "market_aware_v1",
    compare_all: bool = False,
) -> str:
    """
    Calibration report: how accurate are the analyst's predictions?

    Groups all settled picks by sport and confidence level, then shows:
      - Predicted probability vs actual win rate
      - Whether positive-edge bets were actually profitable
      - Breakdown by sport
      - Sample size for each bucket

    This is the key tool for improving predictions over time.
    Needs at least a few settled picks to be useful.

    Args:
        methodology: Only include picks from this model version (default 'market_aware_v1').
            Lets you evaluate one methodology at a time.
        compare_all: If True, ignore the methodology filter and show a side-by-side
            comparison of all methodologies. Useful for A/B testing model versions.
    """
    conn = _get_db()

    if compare_all:
        # Fetch all settled picks across all methodologies
        rows = conn.execute(
            """
            SELECT * FROM analyst_picks
            WHERE outcome IN ('win', 'loss', 'push')
            ORDER BY game_date DESC
            """
        ).fetchall()
    else:
        # Filter to a single methodology
        rows = conn.execute(
            """
            SELECT * FROM analyst_picks
            WHERE outcome IN ('win', 'loss', 'push') AND methodology = ?
            ORDER BY game_date DESC
            """,
            (methodology,),
        ).fetchall()
    conn.close()

    if not rows:
        if compare_all:
            return (
                "No settled picks yet. Use settle_analyst_pick after games "
                "finish to build calibration data."
            )
        return (
            f"No settled picks for methodology '{methodology}'. "
            f"Try compare_all=True to see all methodologies, or check "
            f"list_analyst_picks to see what's available."
        )

    # ── If compare_all, show side-by-side methodology comparison ─────
    if compare_all:
        methods: dict[str, list] = defaultdict(list)
        for r in rows:
            methods[r["methodology"]].append(r)

        lines = ["═══ Calibration Report — All Methodologies ═══\n"]
        lines.append(
            f"  {'Methodology':<20} {'Picks':>5}  {'Win%':>5}  "
            f"{'Avg Model':>10}  {'P&L':>9}  {'ROI':>6}"
        )

        for method_name in sorted(methods.keys()):
            m_rows = methods[method_name]
            m_n = len(m_rows)
            m_wins = sum(1 for r in m_rows if r["outcome"] == "win")
            m_wl = sum(1 for r in m_rows if r["outcome"] in ("win", "loss"))
            m_wr = m_wins / m_wl if m_wl > 0 else 0
            m_avg_model = sum(r["model_probability"] for r in m_rows) / m_n
            m_bets = [r for r in m_rows if r["bet_placed"] and r["pnl"] is not None]
            m_pnl = sum(r["pnl"] for r in m_bets)
            m_wagered = sum(r["bet_amount"] for r in m_bets if r["bet_amount"])
            m_roi = (m_pnl / m_wagered * 100) if m_wagered > 0 else 0

            lines.append(
                f"  {method_name:<20} {m_n:>5}  {m_wr:>5.0%}  "
                f"{m_avg_model:>10.0%}  ${m_pnl:>+8,.2f}  {m_roi:>+5.1f}%"
            )

        lines.append(
            f"\nTotal settled picks across all methodologies: {len(rows)}"
        )
        lines.append(
            "\nRun get_calibration_report(methodology='<name>') for a "
            "detailed breakdown of a specific methodology."
        )
        return "\n".join(lines)

    # ── Single-methodology detailed report ───────────────────────────
    total = len(rows)
    wins = sum(1 for r in rows if r["outcome"] == "win")
    losses = sum(1 for r in rows if r["outcome"] == "loss")
    pushes = sum(1 for r in rows if r["outcome"] == "push")
    win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0

    # Overall P&L from bets that were actually placed
    bets = [r for r in rows if r["bet_placed"] and r["pnl"] is not None]
    total_pnl = sum(r["pnl"] for r in bets)
    total_wagered = sum(r["bet_amount"] for r in bets if r["bet_amount"])
    roi = (total_pnl / total_wagered * 100) if total_wagered > 0 else 0

    lines = [
        f"═══ Calibration Report — {methodology} ═══\n",
        f"Total settled picks:  {total}  ({wins}W / {losses}L / {pushes}P)",
        f"Overall win rate:     {win_rate:.0%}",
    ]
    if bets:
        lines.extend([
            f"Bets placed:         {len(bets)}",
            f"Total wagered:       ${total_wagered:,.2f}",
            f"Total P&L:           ${total_pnl:+,.2f}",
            f"ROI:                 {roi:+.1f}%",
        ])

    # ── Calibration by confidence level ──────────────────────────────
    lines.append("\n── Calibration by Confidence ──")
    lines.append(f"  {'Stars':<8} {'Picks':>5}  {'Win%':>5}  {'Avg Model':>10}  {'Verdict'}")

    for stars in range(5, 0, -1):
        bucket = [r for r in rows if r["confidence"] == stars]
        if not bucket:
            continue
        n = len(bucket)
        bucket_wins = sum(1 for r in bucket if r["outcome"] == "win")
        bucket_wl = sum(1 for r in bucket if r["outcome"] in ("win", "loss"))
        actual_wr = bucket_wins / bucket_wl if bucket_wl > 0 else 0
        avg_model = sum(r["model_probability"] for r in bucket) / n

        # Compare predicted vs actual to check calibration
        diff = actual_wr - avg_model
        if abs(diff) < 0.05:
            verdict = "well calibrated"
        elif diff > 0:
            verdict = f"underconfident by {abs(diff):.0%}"
        else:
            verdict = f"overconfident by {abs(diff):.0%}"

        star_str = '★' * stars + '☆' * (5 - stars)
        lines.append(
            f"  {star_str} {n:>5}  {actual_wr:>5.0%}  {avg_model:>10.0%}  {verdict}"
        )

    # ── Breakdown by sport ───────────────────────────────────────────
    sports: dict[str, list] = defaultdict(list)
    for r in rows:
        sports[r["sport"]].append(r)

    if len(sports) > 1:
        lines.append("\n── Breakdown by Sport ──")
        lines.append(f"  {'Sport':<6} {'Picks':>5}  {'Win%':>5}  {'Avg Model':>10}  {'P&L':>8}")

        for sport in sorted(sports.keys()):
            s_rows = sports[sport]
            s_n = len(s_rows)
            s_wins = sum(1 for r in s_rows if r["outcome"] == "win")
            s_wl = sum(1 for r in s_rows if r["outcome"] in ("win", "loss"))
            s_wr = s_wins / s_wl if s_wl > 0 else 0
            s_avg_model = sum(r["model_probability"] for r in s_rows) / s_n
            s_bets = [r for r in s_rows if r["bet_placed"] and r["pnl"] is not None]
            s_pnl = sum(r["pnl"] for r in s_bets)
            lines.append(
                f"  {sport:<6} {s_n:>5}  {s_wr:>5.0%}  {s_avg_model:>10.0%}  ${s_pnl:>+7,.2f}"
            )

    # ── Edge accuracy ────────────────────────────────────────────────
    edge_picks = [r for r in rows if r["edge"] is not None]
    if edge_picks:
        pos_edge = [r for r in edge_picks if r["edge"] > 0]
        neg_edge = [r for r in edge_picks if r["edge"] <= 0]

        lines.append("\n── Edge Accuracy ──")

        if pos_edge:
            pe_wins = sum(1 for r in pos_edge if r["outcome"] == "win")
            pe_wl = sum(1 for r in pos_edge if r["outcome"] in ("win", "loss"))
            pe_wr = pe_wins / pe_wl if pe_wl > 0 else 0
            pe_bets = [r for r in pos_edge if r["bet_placed"] and r["pnl"] is not None]
            pe_pnl = sum(r["pnl"] for r in pe_bets)
            lines.append(
                f"  Positive edge picks: {len(pos_edge)} "
                f"({pe_wr:.0%} win rate, ${pe_pnl:+,.2f} P&L)"
            )

        if neg_edge:
            ne_wins = sum(1 for r in neg_edge if r["outcome"] == "win")
            ne_wl = sum(1 for r in neg_edge if r["outcome"] in ("win", "loss"))
            ne_wr = ne_wins / ne_wl if ne_wl > 0 else 0
            lines.append(
                f"  Negative edge picks: {len(neg_edge)} "
                f"({ne_wr:.0%} win rate — should be avoided)"
            )

    # ── Tip ──────────────────────────────────────────────────────────
    if total < 20:
        lines.append(
            f"\n⚠ Small sample size ({total} picks). "
            f"Need 20+ settled picks for meaningful calibration."
        )

    return "\n".join(lines)


# ─── Position Reconciliation ─────────────────────────────────────────────────

@mcp.tool()
def reconcile_positions() -> str:
    """
    Compare Kalshi's actual positions against the local SQLite trade log.

    Flags mismatches:
      - Positions on Kalshi that have no matching "submitted" trade locally
      - Local trades logged as "failed" that actually filled on Kalshi
      - Quantity or side discrepancies between Kalshi and the local log

    Use this after suspected order issues (e.g. response parse failures)
    to make sure your local records match reality.
    """
    try:
        # ── Fetch real positions from Kalshi ────────────────────────────
        client = get_client()
        all_positions = []
        cursor = None
        for _ in range(5):
            kwargs = {"limit": 100}
            if cursor:
                kwargs["cursor"] = cursor
            response = client.get_positions(**kwargs)
            all_positions.extend(response.market_positions)
            cursor = response.cursor
            if not cursor:
                break

        # Build a dict of active Kalshi positions: ticker → {side, qty, exposure}
        kalshi_map: dict[str, dict] = {}
        for p in all_positions:
            if p.position == 0:
                continue
            kalshi_map[p.ticker] = {
                "side": "yes" if p.position > 0 else "no",
                "qty": abs(p.position),
                "exposure": float(p.market_exposure_dollars),
                "pnl": float(p.realized_pnl_dollars),
            }

        # ── Fetch local trade log ───────────────────────────────────────
        conn = _get_db()
        rows = conn.execute(
            """
            SELECT ticker, side, quantity, status, order_id, error_message
            FROM trades
            WHERE status IN ('submitted', 'filled', 'failed')
            ORDER BY timestamp DESC
            """
        ).fetchall()
        conn.close()

        # Build a summary of local trades per ticker:
        # net quantity submitted/filled per side
        local_map: dict[str, dict] = {}
        failed_tickers: dict[str, list] = {}
        for r in rows:
            t = r["ticker"]
            if r["status"] in ("submitted", "filled"):
                if t not in local_map:
                    local_map[t] = {"side": r["side"], "qty": 0}
                local_map[t]["qty"] += r["quantity"]
            if r["status"] == "failed":
                if t not in failed_tickers:
                    failed_tickers[t] = []
                failed_tickers[t].append({
                    "qty": r["quantity"],
                    "side": r["side"],
                    "error": r["error_message"],
                })

        # ── Compare ─────────────────────────────────────────────────────
        issues = []
        matched = []

        # Check every Kalshi position against local log
        for ticker, kpos in kalshi_map.items():
            lpos = local_map.get(ticker)
            if lpos is None:
                # Kalshi has it but local DB doesn't — ghost trade
                issues.append(
                    f"⚠ GHOST POSITION: {ticker}\n"
                    f"    Kalshi: {kpos['qty']} {kpos['side'].upper()} "
                    f"(${kpos['exposure']:,.2f} exposure)\n"
                    f"    Local DB: no submitted/filled trades found\n"
                )
                # Check if it was logged as "failed"
                if ticker in failed_tickers:
                    for ft in failed_tickers[ticker]:
                        issues.append(
                            f"    → Found FAILED trade: {ft['qty']} "
                            f"{ft['side'].upper()} — error: {ft['error']}\n"
                            f"    → This trade likely went through despite "
                            f"being logged as failed!\n"
                        )
            else:
                # Both have it — check for discrepancies
                if kpos["qty"] != lpos["qty"] or kpos["side"] != lpos["side"]:
                    issues.append(
                        f"⚠ MISMATCH: {ticker}\n"
                        f"    Kalshi: {kpos['qty']} {kpos['side'].upper()}\n"
                        f"    Local:  {lpos['qty']} {lpos['side'].upper()}\n"
                    )
                else:
                    matched.append(
                        f"  ✓ {ticker}: "
                        f"{kpos['qty']} {kpos['side'].upper()} — matches"
                    )

        # Check for local submitted trades with no Kalshi position
        # (could mean the order was cancelled or expired)
        for ticker, lpos in local_map.items():
            if ticker not in kalshi_map:
                issues.append(
                    f"⚠ LOCAL ONLY: {ticker}\n"
                    f"    Local DB: {lpos['qty']} {lpos['side'].upper()} "
                    f"(submitted/filled)\n"
                    f"    Kalshi: no position found — may have been "
                    f"cancelled, expired, or already settled\n"
                )

        # ── Build report ────────────────────────────────────────────────
        lines = ["═══ Position Reconciliation ═══\n"]
        lines.append(
            f"Kalshi positions: {len(kalshi_map)}  |  "
            f"Local submitted/filled: {len(local_map)}\n"
        )

        if issues:
            lines.append(f"🚨 {len(issues)} issue(s) found:\n")
            lines.extend(issues)
        else:
            lines.append("✅ No discrepancies found.\n")

        if matched:
            lines.append(f"\nMatched ({len(matched)}):")
            lines.extend(matched)

        return "\n".join(lines)

    except Exception as e:
        return _format_error("reconciling positions", e)


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
