#!/usr/bin/env python3
"""
test_order.py — Fire one test equity order to prove API + IP auth
=================================================================
Places a CNC LIMIT order for 1 share of SBIN at ₹1 (will stay PENDING
since ₹1 is far below market price). Run this after market hours to
confirm that Dhan API accepts equity AMO orders from the GCP IP.

Run on GCP VM:
  cd ~/dhan-trading && python3 test_order.py

To cancel the order after confirming: Dhan app → Orders → Cancel
"""

import os
import sys
import json
import requests
from datetime import date
from dotenv import load_dotenv
from pathlib import Path

# Load credentials from .env
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

# ── Step 1: Verify token is live ──────────────────────────────────────────────
print("\n[1] Checking token via /v2/fundlimit ...")
r = requests.get("https://api.dhan.co/v2/fundlimit", headers=HEADERS, timeout=10)
print(f"    Status: {r.status_code}")
if r.status_code != 200:
    print(f"    Response: {r.text[:200]}")
    print("    Token may be expired — run refresh_token.py first")
    sys.exit(1)

d   = r.json()
bal = d.get("availabelBalance") or d.get("availableBalance") or d.get("net") or "?"
print(f"    Available balance: ₹{bal}")

# ── Step 2: Place equity CNC AMO order ───────────────────────────────────────
# SBIN (State Bank of India) — security ID 3045 on NSE
# LIMIT at ₹1 → will not execute, just sits as PENDING AMO
print("\n[2] Placing CNC LIMIT AMO order — SBIN, qty=1, price=₹1 ...")

payload = {
    "dhanClientId":       CLIENT_ID,
    "correlationId":      f"test_{date.today().strftime('%Y%m%d')}",
    "transactionType":    "BUY",
    "exchangeSegment":    "NSE_EQ",
    "productType":        "CNC",
    "orderType":          "LIMIT",
    "validity":           "DAY",
    "securityId":         "3045",       # SBIN
    "quantity":           1,
    "price":              1.00,         # ₹1 — will not execute at market price
    "afterMarketOrder":   True,         # AMO flag — required by Dhan v2
    "triggerPrice":       0,
    "disclosedQuantity":  0,
}

print(f"    Payload: {json.dumps(payload, indent=6)}")

resp = requests.post(
    "https://api.dhan.co/v2/orders",
    headers=HEADERS,
    json=payload,
    timeout=15,
)

print(f"\n    HTTP Status : {resp.status_code}")
try:
    result = resp.json()
    print(f"    Response    : {json.dumps(result, indent=6)}")
    order_id = result.get("orderId") or result.get("order_id")
    if order_id:
        print(f"\n✅  Order placed! Order ID: {order_id}")
        print(f"    → Check Dhan app → Orders to see it (PENDING AMO)")
        print(f"    → Cancel it from the app once confirmed")
    else:
        status = result.get("status", "")
        msg    = result.get("remarks") or result.get("message") or result.get("errorMessage") or ""
        print(f"\n⚠️  No order ID in response. status={status!r}  msg={msg!r}")
except Exception as e:
    print(f"    Could not parse JSON: {e}")
    print(f"    Raw response: {resp.text[:400]}")
