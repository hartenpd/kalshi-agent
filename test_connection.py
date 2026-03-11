"""
Quick test script to verify your Kalshi API credentials work.

Run with:  uv run test_connection.py

If it prints your balance, you're good to go.
If it errors, check your .env file and private key.
"""

import os
import certifi
from dotenv import load_dotenv
from kalshi_python_sync import Configuration, KalshiClient

# Fix macOS SSL certificates — certifi provides a reliable CA bundle
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

# Load credentials from .env
load_dotenv()

KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./keys/private_key.pem")
KALSHI_ENV = os.getenv("KALSHI_ENV", "demo")

# Pick the right URL
API_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "production": "https://api.elections.kalshi.com/trade-api/v2",
}
API_URL = API_URLS.get(KALSHI_ENV, API_URLS["demo"])


def main():
    print(f"Testing Kalshi API connection...")
    print(f"  Environment: {KALSHI_ENV}")
    print(f"  API URL:     {API_URL}")
    print(f"  Key ID:      {KALSHI_API_KEY_ID}")
    print(f"  Key file:    {KALSHI_PRIVATE_KEY_PATH}")
    print()

    # -- Step 1: Read the private key --
    key_path = KALSHI_PRIVATE_KEY_PATH
    if not os.path.isabs(key_path):
        key_path = os.path.join(os.path.dirname(__file__), key_path)

    try:
        with open(key_path, "r") as f:
            private_key_pem = f.read()
        print("✓ Private key loaded successfully")
    except FileNotFoundError:
        print(f"✗ Private key NOT FOUND at: {key_path}")
        print("  Make sure your .pem file is in the keys/ folder.")
        return

    # -- Step 2: Create the client --
    config = Configuration(host=API_URL)
    config.api_key_id = KALSHI_API_KEY_ID
    config.private_key_pem = private_key_pem

    client = KalshiClient(configuration=config)
    print("✓ Client created")

    # -- Step 3: Fetch the balance (proves auth works) --
    try:
        response = client.get_balance()

        balance = response.balance / 100
        portfolio = response.portfolio_value / 100

        print("✓ API connection successful!\n")
        print(f"  Available balance:  ${balance:,.2f}")
        print(f"  Portfolio value:    ${portfolio:,.2f}")
        print(f"  Total equity:       ${balance + portfolio:,.2f}")
    except Exception as e:
        error_str = str(e)
        print(f"✗ API call failed: {e}\n")

        if "401" in error_str or "Unauthorized" in error_str:
            # 401 means we connected but auth was rejected
            print("  Got 401 Unauthorized — the connection works but your")
            print("  credentials were rejected. Common fixes:")
            print("  - The API key ID in .env must match the private key")
            print("  - Generate a new key pair at https://kalshi.com/account/api")
            print("  - If using demo, generate keys at https://demo.kalshi.com")
            print("  - Demo and production keys are NOT interchangeable")
        elif "SSL" in error_str or "certificate" in error_str:
            print("  SSL certificate error. Try running:")
            print("  uv add certifi")
        else:
            print("  Common fixes:")
            print("  - Double-check KALSHI_API_KEY_ID in .env")
            print("  - Make sure the private key matches the API key")
            print("  - If using demo, verify your demo account is active")
            print(f"  - Try opening {API_URL} in a browser to check connectivity")


if __name__ == "__main__":
    main()
