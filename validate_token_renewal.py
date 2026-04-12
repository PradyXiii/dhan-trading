#!/usr/bin/env python3
"""
validate_token_renewal.py
─────────────────────────
Manually verify that Dhan's PUT /v2/RenewToken call works.

Run on the VM anytime a token has just been generated (or every morning):
    python3 validate_token_renewal.py

What it checks:
  1. Current token is valid (GET /v2/fundlimit → 200)
  2. RenewToken call succeeds (PUT /v2/RenewToken)
  3. Prints the full response so you can see the new expiry/value
  4. Confirms token is still valid immediately after renewal
"""

import os
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")

if not TOKEN or not CLIENT_ID:
    print("ERROR: DHAN_ACCESS_TOKEN or DHAN_CLIENT_ID not set in .env")
    raise SystemExit(1)

# Show token fingerprint (first 12 + last 6 chars) — enough to compare before/after
# without exposing the full token
def _token_fingerprint(t):
    if len(t) < 20:
        return "***"
    return f"{t[:12]}...{t[-6:]}"

print("=" * 60)
print(f"  Token fingerprint : {_token_fingerprint(TOKEN)}")
print(f"  Client ID         : {CLIENT_ID}")
print(f"  Time              : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 60)

# ── Step 1: confirm current token is valid ────────────────────
print("\n[1/3] Checking current token validity (GET /v2/fundlimit)...")
r1 = requests.get(
    "https://api.dhan.co/v2/fundlimit",
    headers={"access-token": TOKEN, "client-id": CLIENT_ID,
             "Content-Type": "application/json"},
    timeout=10,
)
if r1.status_code == 200:
    try:
        fund = r1.json()
        avail = fund.get("availabelBalance") or fund.get("availableBalance") or "—"
        print(f"  ✓ Token VALID  (HTTP 200)  |  Available balance: ₹{avail:,.0f}"
              if isinstance(avail, (int, float)) else
              f"  ✓ Token VALID  (HTTP 200)  |  Available balance: {avail}")
    except Exception:
        print(f"  ✓ Token VALID  (HTTP 200)")
elif r1.status_code == 401:
    print(f"  ✗ Token EXPIRED (HTTP 401) — renew at dhan.co → API settings")
    raise SystemExit(1)
else:
    print(f"  ? Unexpected status: HTTP {r1.status_code}  body: {r1.text[:200]}")

# ── Step 2: call RenewToken ───────────────────────────────────
print("\n[2/3] Calling GET /v2/RenewToken...")
r2 = requests.get(
    "https://api.dhan.co/v2/RenewToken",
    headers={"access-token": TOKEN, "dhanClientId": CLIENT_ID},
    timeout=10,
)
print(f"  HTTP status  : {r2.status_code}")

# Track the active token — may change after renewal
active_token = TOKEN
new_expiry   = None

if r2.status_code == 200:
    try:
        body      = r2.json()
        new_token = body.get("token")
        new_expiry = body.get("expiryTime") or body.get("expiresAt") or body.get("expires_at")

        if new_token and new_token != TOKEN:
            print(f"\n  ⚡ NEW TOKEN issued: {_token_fingerprint(new_token)}")
            print(f"  ⏰  Expires at: {new_expiry or 'see response body'}")
            print(f"\n  Auto-updating .env with new token...")
            import re as _re
            env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
            if os.path.exists(env_path):
                with open(env_path, "r") as f:
                    content = f.read()
                new_content = _re.sub(
                    r"^DHAN_ACCESS_TOKEN=.*$",
                    f"DHAN_ACCESS_TOKEN={new_token}",
                    content,
                    flags=_re.MULTILINE,
                )
                with open(env_path, "w") as f:
                    f.write(new_content)
                print(f"  ✓ .env updated — old token invalidated, new token persisted")
                active_token = new_token   # use new token for step 3
            else:
                print(f"  ✗ .env not found at {env_path} — update DHAN_ACCESS_TOKEN manually")
        else:
            print(f"  ✓ Renewal 200 (no new token in response)")
    except Exception as e:
        print(f"  ✓ Renewal 200 (could not parse response: {e})")
else:
    print(f"  Response body: {r2.text[:300]}")
    print(f"  ✗ Renewal FAILED — check token and CLIENT_ID in .env")

# ── Step 3: confirm NEW token works ──────────────────────────
print("\n[3/3] Confirming NEW token is valid (GET /v2/fundlimit)...")
r3 = requests.get(
    "https://api.dhan.co/v2/fundlimit",
    headers={"access-token": active_token, "client-id": CLIENT_ID,
             "Content-Type": "application/json"},
    timeout=10,
)
if r3.status_code == 200:
    print("  ✓ Token VALID after renewal  (HTTP 200)")
else:
    print(f"  ✗ Token status after renewal: HTTP {r3.status_code}  — {r3.text[:200]}")

print("\n" + "=" * 60)
print("  Summary")
print("=" * 60)
print(f"  Before renewal : {'✓ valid' if r1.status_code == 200 else '✗ invalid'}")
print(f"  RenewToken call: {'✓ success' if r2.status_code == 200 else f'✗ HTTP {r2.status_code}'}")
print(f"  After renewal  : {'✓ valid' if r3.status_code == 200 else '✗ invalid'}")
print()
print("  The token auto-renews daily at 9:15 AM (auto_trader.py)")
print("  and at 11 PM (model_evolver.py) — no manual steps needed.")
print("=" * 60)
