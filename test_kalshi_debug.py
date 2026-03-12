"""
Debug script — fetch raw NBA game markets from Kalshi and identify
which fields cause SDK Pydantic parsing errors.

Step 1: Raw HTTP request to see exactly what the API returns
Step 2: Try parsing through SDK and catch the exact validation error
"""

import os
import json
import time
import base64
import certifi
import requests
from datetime import datetime, timezone
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives import hashes
from dotenv import load_dotenv

load_dotenv()

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./keys/private_key.pem")
KALSHI_ENV = os.getenv("KALSHI_ENV", "demo")

API_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "production": "https://api.elections.kalshi.com/trade-api/v2",
}
BASE_URL = API_URLS.get(KALSHI_ENV, API_URLS["demo"])


def sign_request(method: str, path: str, timestamp_ms: int) -> str:
    """Sign a Kalshi API request using the RSA private key."""
    with open(PRIVATE_KEY_PATH, "rb") as f:
        private_key = load_pem_private_key(f.read(), password=None)

    message = f"{timestamp_ms}{method}{path}".encode("utf-8")

    if isinstance(private_key, Ed25519PrivateKey):
        signature = private_key.sign(message)
    else:
        # RSA key
        signature = private_key.sign(message, PKCS1v15(), hashes.SHA256())

    return base64.b64encode(signature).decode("utf-8")


def raw_api_get(path: str, params: dict = None) -> dict:
    """Make a raw authenticated GET request to Kalshi API."""
    url = f"{BASE_URL}{path}"
    timestamp_ms = int(time.time() * 1000)
    signature = sign_request("GET", path, timestamp_ms)

    headers = {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        "Content-Type": "application/json",
    }

    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


# ═══════════════════════════════════════════════════════════════════
# STEP 1: Raw API call — see exactly what Kalshi sends back
# ═══════════════════════════════════════════════════════════════════

print("=" * 70)
print("STEP 1: Raw API response for KXNBAGAME series (first 5 markets)")
print("=" * 70)

try:
    data = raw_api_get("/markets", params={
        "series_ticker": "KXNBAGAME",
        "status": "open",
        "limit": 5,
    })

    markets = data.get("markets", [])
    print(f"\nGot {len(markets)} markets back\n")

    if not markets:
        print("No open NBA markets found. Trying without status filter...")
        data = raw_api_get("/markets", params={
            "series_ticker": "KXNBAGAME",
            "limit": 5,
        })
        markets = data.get("markets", [])
        print(f"Got {len(markets)} markets (any status)\n")

    # Print raw JSON for first few markets so we can see null fields
    for i, m in enumerate(markets[:3]):
        print(f"--- Market {i+1}: {m.get('ticker', '???')} ---")
        # Show all fields, highlighting nulls and empty values
        for key, val in sorted(m.items()):
            flag = ""
            if val is None:
                flag = "  ⬅ NULL"
            elif val == "":
                flag = "  ⬅ EMPTY STRING"
            elif isinstance(val, list) and len(val) == 0:
                flag = "  ⬅ EMPTY LIST"
            print(f"  {key}: {json.dumps(val)[:100]}{flag}")
        print()

except Exception as e:
    print(f"Raw API call failed: {e}")
    import traceback
    traceback.print_exc()


# ═══════════════════════════════════════════════════════════════════
# STEP 2: Try parsing through SDK — catch exact Pydantic error
# ═══════════════════════════════════════════════════════════════════

print("=" * 70)
print("STEP 2: Parse raw response through SDK Market.from_dict()")
print("=" * 70)

from kalshi_python_sync.models.market import Market as MarketModel

# Try parsing WITHOUT any patches first — see the raw error
if markets:
    # Restore original from_dict temporarily
    original_from_dict = MarketModel.from_dict.__func__ if hasattr(MarketModel.from_dict, '__func__') else MarketModel.from_dict

    for i, raw_market in enumerate(markets[:5]):
        ticker = raw_market.get("ticker", "???")
        print(f"\nParsing market {i+1}: {ticker}")
        try:
            # Try with a FRESH copy so patches don't mutate our data
            market_copy = json.loads(json.dumps(raw_market))
            parsed = MarketModel.from_dict(market_copy)
            print(f"  ✓ Parsed OK — status={parsed.status}, result={parsed.result}")
        except Exception as e:
            print(f"  ✗ PARSE FAILED: {type(e).__name__}")
            error_str = str(e)
            # Print the full error details
            if hasattr(e, 'errors'):
                # Pydantic ValidationError — show each field error
                for err in e.errors():
                    print(f"    Field: {'.'.join(str(x) for x in err['loc'])}")
                    print(f"    Type:  {err['type']}")
                    print(f"    Msg:   {err['msg']}")
                    # Show the actual value that was sent
                    field_name = err['loc'][-1] if err['loc'] else '?'
                    actual_val = raw_market.get(field_name)
                    print(f"    Actual value from API: {json.dumps(actual_val)}")
                    print()
            else:
                print(f"    {error_str[:500]}")
else:
    print("\nNo markets to parse — skipping step 2")


# ═══════════════════════════════════════════════════════════════════
# STEP 3: Check ALL fields for null/unexpected values across markets
# ═══════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("STEP 3: Scan all fields across all fetched markets for nulls")
print("=" * 70)

if markets:
    # Fetch more markets for a broader scan
    try:
        data_full = raw_api_get("/markets", params={
            "series_ticker": "KXNBAGAME",
            "limit": 200,
        })
        all_markets = data_full.get("markets", [])
    except Exception:
        all_markets = markets

    print(f"\nScanning {len(all_markets)} markets for null/unexpected values...\n")

    # Track which fields have null values and what values appear
    null_fields = {}  # field -> count of nulls
    field_sample_values = {}  # field -> set of sample values

    for m in all_markets:
        for key, val in m.items():
            if val is None:
                null_fields[key] = null_fields.get(key, 0) + 1
            # Track sample values for enum-like fields
            if key in ("status", "result", "market_type", "response_price_units",
                        "price_level_structure", "strike_type"):
                if key not in field_sample_values:
                    field_sample_values[key] = set()
                field_sample_values[key].add(str(val))

    if null_fields:
        print("Fields that had NULL values:")
        for field, count in sorted(null_fields.items(), key=lambda x: -x[1]):
            print(f"  {field}: NULL in {count}/{len(all_markets)} markets")
    else:
        print("No NULL fields found across all markets.")

    print("\nEnum-like field values seen:")
    for field, vals in sorted(field_sample_values.items()):
        print(f"  {field}: {vals}")
else:
    print("\nNo markets to scan.")

print("\n✅ Debug complete")
