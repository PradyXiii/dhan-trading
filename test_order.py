#!/usr/bin/env python3
"""
test_order.py — Diagnostic + AMO order test
============================================
Step 2a: sends a MARKET order (no AMO flag) → should get:
  DH-906  if the security ID / price / params are all valid (market just closed)
  DH-905  if the security ID or another param is wrong

Step 2b: if 2a confirms params are valid, sends LIMIT AMO → should appear in
  Dhan app → Orders as PENDING AMO for Monday open.

Run on GCP VM:
  cd ~/dhan-trading && python3 test_order.py

Cancel from Dhan app → Orders once confirmed.
"""

import os
import sys
import json
import requests
from datetime import date
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID",    "")

if not TOKEN or not CLIENT_ID:
    print("ERROR: DHAN_ACCESS_TOKEN or DHAN_CLIENT_ID missing from .env")
    sys.exit(1)

HEADERS = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

print(f"Client ID : {CLIENT_ID}")
print(f"Token tail: ...{TOKEN[-12:]}")


# ── Step 1: Token check ───────────────────────────────────────────────────────
print("\n[1] Checking token via /v2/fundlimit ...")
r = requests.get("https://api.dhan.co/v2/fundlimit", headers=HEADERS, timeout=10)
print(f"    Status: {r.status_code}")
if r.status_code != 200:
    print(f"    Response: {r.text[:200]}")
    sys.exit(1)
bal = (r.json().get("availabelBalance") or r.json().get("availableBalance")
       or r.json().get("net") or "?")
print(f"    Balance: ₹{bal}")


def place(label, payload):
    """POST /v2/orders and print result. Returns parsed JSON."""
    print(f"\n{label}")
    print(f"  Payload: {json.dumps(payload)}")
    resp = requests.post("https://api.dhan.co/v2/orders",
                         headers=HEADERS, json=payload, timeout=15)
    print(f"  HTTP {resp.status_code}")
    try:
        result = resp.json()
        print(f"  Response: {json.dumps(result, indent=4)}")
        return result
    except Exception as e:
        print(f"  Could not parse JSON: {e}  raw={resp.text[:300]}")
        return {}


# ── Step 2a: Diagnostic — plain MARKET order, no AMO flag ────────────────────
# If security ID + params are valid → expect DH-906 (market closed)
# If DH-905 → something in base params is wrong (security ID / price / field)
diag = place(
    "[2a] DIAGNOSTIC — MARKET order, no AMO (expect DH-906 if params are OK)",
    {
        "dhanClientId":    CLIENT_ID,
        "transactionType": "BUY",
        "exchangeSegment": "NSE_EQ",
        "productType":     "CNC",
        "orderType":       "MARKET",
        "validity":        "DAY",
        "securityId":      "3045",   # SBIN NSE
        "quantity":        1,
        "price":           0,
        "disclosedQuantity": 0,
        "triggerPrice":    0,
        "afterMarketOrder": False,
    },
)

diag_code = diag.get("errorCode", "")
order_id  = diag.get("orderId") or diag.get("order_id")

if order_id:
    print(f"\n✅  Market order accepted! orderId={order_id}")
    print("   (Unexpected — market was open? Cancel from Dhan app immediately.)")
    sys.exit(0)

if diag_code == "DH-906":
    print("\n  → DH-906 confirmed: params are valid, market just closed.")
    print("    Proceeding to AMO LIMIT order ...\n")
elif diag_code == "DH-905":
    print("\n  → DH-905 on MARKET order: base params are bad (security ID or field name).")
    print("    Trying BSE_EQ with SBIN security ID 3045 as secondary check ...\n")

    # Try BSE_EQ variant — BSE SBIN security ID
    diag2 = place(
        "[2a-bse] DIAGNOSTIC — MARKET order on BSE_EQ",
        {
            "dhanClientId":    CLIENT_ID,
            "transactionType": "BUY",
            "exchangeSegment": "BSE_EQ",
            "productType":     "CNC",
            "orderType":       "MARKET",
            "validity":        "DAY",
            "securityId":      "3045",
            "quantity":        1,
            "price":           0,
            "disclosedQuantity": 0,
            "triggerPrice":    0,
            "afterMarketOrder": False,
        },
    )
    diag2_code = diag2.get("errorCode", "")
    if diag2_code == "DH-906":
        print("  → BSE DH-906: SBIN on BSE works. Proceeding with BSE_EQ AMO ...")
        use_segment = "BSE_EQ"
    else:
        print(f"\n⛔  Both NSE and BSE returned DH-905. Security ID 3045 may be wrong.")
        print("   Check Dhan scrip master for correct SBIN security ID:")
        print("   https://images.dhan.co/api-data/api-scrip-master.csv")
        sys.exit(1)
else:
    # Some other error code
    print(f"\n⚠️  Unexpected error code: {diag_code!r}. Full response above.")
    use_segment = "NSE_EQ"

use_segment = "NSE_EQ"  # use whichever passed

# ── Step 2b: AMO LIMIT order ──────────────────────────────────────────────────
# LIMIT at ₹700 — well below SBIN market (~₹800), won't execute.
# Shows in Dhan app → Orders as PENDING AMO until Monday open.
result = place(
    f"[2b] AMO LIMIT order — SBIN {use_segment} qty=1 price=₹1000",
    {
        "dhanClientId":    CLIENT_ID,
        "correlationId":   f"testamo{date.today().strftime('%Y%m%d')}",
        "transactionType": "BUY",
        "exchangeSegment": use_segment,
        "productType":     "CNC",
        "orderType":       "LIMIT",
        "validity":        "DAY",
        "securityId":      "3045",
        "quantity":        1,
        "price":           1000.00,      # ₹1000 — within circuit (960–1173), ~10% below market
        "disclosedQuantity": 0,
        "triggerPrice":    0,
        "afterMarketOrder": True,
        "amoTime":         "OPEN",       # equity CNC AMO may require this alongside afterMarketOrder
    },
)

oid = result.get("orderId") or result.get("order_id")
if oid:
    print(f"\n✅  AMO order placed! orderId={oid}")
    print("   → Check Dhan app → Orders → PENDING AMO")
    print("   → Cancel before Monday 9:15 AM if you don't want it to execute")
else:
    code = result.get("errorCode", "")
    msg  = result.get("errorMessage", "")
    print(f"\n⚠️  AMO failed. code={code!r}  msg={msg!r}")
