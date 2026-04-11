#!/usr/bin/env python3
"""
test_order.py — Multi-strategy AMO test
========================================
Tries three routes in order until one succeeds:
  A) NSE_EQ CNC LIMIT AMO (equity, with amoTime)
  B) NSE_EQ CNC LIMIT AMO (equity, without amoTime)
  C) NSE_FNO MARGIN LIMIT AMO (BankNifty CE, fetches live option chain)

Strategy C is the real production path used by auto_trader.py.

Run on GCP VM:
  cd ~/dhan-trading && python3 test_order.py

Cancel from Dhan app → Orders → Cancel once confirmed.
"""

import os, sys, json, requests
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
print("\n[1] Token check via /v2/fundlimit ...")
r = requests.get("https://api.dhan.co/v2/fundlimit", headers=HEADERS, timeout=10)
if r.status_code != 200:
    print(f"    FAIL {r.status_code}: {r.text[:200]}")
    sys.exit(1)
bal = (r.json().get("availabelBalance") or r.json().get("availableBalance")
       or r.json().get("net") or "?")
print(f"    OK — Balance: ₹{bal}")


def post_order(label, payload):
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
        print(f"  JSON parse error: {e}  raw={resp.text[:200]}")
        return {}


def success(result, label):
    oid = result.get("orderId") or result.get("order_id")
    if oid:
        print(f"\n✅  ORDER PLACED via {label}! orderId={oid}")
        print("   → Dhan app → Orders → should show PENDING AMO")
        print("   → Cancel before next market open (Mon 9:15 AM) if you don't want it filled")
        return True
    return False


# ── Step 2a: Confirm base equity params are OK (MARKET, no AMO) ──────────────
print("\n[2a] Diagnostic — NSE_EQ MARKET, no AMO (expect DH-906) ...")
diag = post_order(
    "[2a] NSE_EQ CNC MARKET (diagnostic)",
    {
        "dhanClientId":    CLIENT_ID,
        "transactionType": "BUY",
        "exchangeSegment": "NSE_EQ",
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
if diag.get("errorCode") != "DH-906":
    print(f"\n⛔  Unexpected error in base params: {diag}")
    sys.exit(1)
print("  → DH-906 confirmed — base params valid.\n")


# ── NOTE: Dhan AMO acceptance window is WEEKDAYS ONLY ────────────────────────
# Mon-Fri after 3:30 PM IST until next day 8:59 AM IST.
# On Saturday/Sunday Dhan returns DH-906 "Account not enabled for Online Trading"
# for ANY afterMarketOrder:true request — this is a misleading error; the account
# IS enabled, Dhan just doesn't accept AMO orders on weekends.
# ── Step 2b: Equity CNC AMO with amoTime ──────────────────────────────────────
r_a = post_order(
    "[2b] NSE_EQ CNC LIMIT AMO — price ₹1000, amoTime=OPEN",
    {
        "dhanClientId":    CLIENT_ID,
        "correlationId":   f"tamo{date.today().strftime('%Y%m%d')}a",
        "transactionType": "BUY",
        "exchangeSegment": "NSE_EQ",
        "productType":     "CNC",
        "orderType":       "LIMIT",
        "validity":        "DAY",
        "securityId":      "3045",
        "quantity":        1,
        "price":           1000.00,
        "disclosedQuantity": 0,
        "triggerPrice":    0,
        "afterMarketOrder": True,
        "amoTime":         "OPEN",
    },
)
if success(r_a, "NSE_EQ CNC AMO (with amoTime)"):
    sys.exit(0)


# ── Step 2c: Equity CNC AMO without amoTime ───────────────────────────────────
r_b = post_order(
    "[2c] NSE_EQ CNC LIMIT AMO — price ₹1000, no amoTime",
    {
        "dhanClientId":    CLIENT_ID,
        "correlationId":   f"tamo{date.today().strftime('%Y%m%d')}b",
        "transactionType": "BUY",
        "exchangeSegment": "NSE_EQ",
        "productType":     "CNC",
        "orderType":       "LIMIT",
        "validity":        "DAY",
        "securityId":      "3045",
        "quantity":        1,
        "price":           1000.00,
        "disclosedQuantity": 0,
        "triggerPrice":    0,
        "afterMarketOrder": True,
    },
)
if success(r_b, "NSE_EQ CNC AMO (without amoTime)"):
    sys.exit(0)


# ── Step 2d: F&O — fetch live BankNifty option chain, place real NSE_FNO AMO ─
# This is the ACTUAL production path auto_trader.py uses.
print("\n[2d] Fetching BankNifty option chain for real NSE_FNO CE security_id ...")
try:
    chain_resp = requests.post(
        "https://api.dhan.co/v2/optionchain",
        headers=HEADERS,
        json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I", "Expiry": ""},
        timeout=15,
    )
    print(f"  Option chain HTTP {chain_resp.status_code}")
    if chain_resp.status_code != 200:
        print(f"  Option chain unavailable (weekend/holiday): {chain_resp.text[:120]}")
        raise RuntimeError("chain not available")
    chain = chain_resp.json()
    if not isinstance(chain, dict):
        raise RuntimeError(f"unexpected response type: {type(chain)}")

    # Parse: chain['data']['811']['oc'] = {strike: {ce: {...}, pe: {...}}}
    inner = (chain.get("data") or {}).get("811") or {}
    oc    = inner.get("oc") or {}
    spot  = float(inner.get("last_price") or inner.get("lastPrice") or 0)
    print(f"  BankNifty spot: ₹{spot}")
    print(f"  Strikes in chain: {len(oc)}")

    # Find an ATM or near-ATM CE with a valid price
    sid, ce_strike, ce_price = None, None, None
    for key in sorted(oc.keys()):
        strike = float(key)
        if spot and abs(strike - spot) > 2000:
            continue                          # skip deeply OTM
        sub = oc[key].get("ce") or oc[key].get("CE") or {}
        price = float(sub.get("last_price") or sub.get("ltp") or
                      sub.get("lastPrice")  or sub.get("close_price") or
                      sub.get("closePrice") or 0)
        raw_sid = sub.get("security_id") or sub.get("securityId")
        if raw_sid and price > 0:
            sid       = str(raw_sid)
            ce_strike = strike
            ce_price  = price
            break

    if not sid:
        print("  Could not find any CE with a valid security_id + price. Skip 2d.")
    else:
        # Place AMO at 40% of close_price — below market, above likely circuit floor
        amo_price = round(max(1.0, ce_price * 0.40), 1)
        lot_size  = 30    # current BankNifty lot size
        r_c = post_order(
            f"[2d] NSE_FNO MARGIN LIMIT AMO — BN CE {ce_strike:.0f} "
            f"qty={lot_size} price=₹{amo_price} (40% of close ₹{ce_price})",
            {
                "dhanClientId":    CLIENT_ID,
                "correlationId":   f"tamo{date.today().strftime('%Y%m%d')}c",
                "transactionType": "BUY",
                "exchangeSegment": "NSE_FNO",
                "productType":     "MARGIN",
                "orderType":       "LIMIT",
                "validity":        "DAY",
                "securityId":      sid,
                "quantity":        lot_size,
                "price":           amo_price,
                "disclosedQuantity": 0,
                "triggerPrice":    0,
                "afterMarketOrder": True,
            },
        )
        if success(r_c, f"NSE_FNO MARGIN AMO (BN CE {ce_strike:.0f})"):
            sys.exit(0)

except Exception as e:
    print(f"  Option chain fetch error: {e}")


print("\n⛔  All AMO attempts failed.")
print("   Equity CNC AMO may not be available on weekends via Dhan v2 API.")
print("   Run this script on a WEEKDAY EVENING (Mon-Fri after 3:30 PM IST)")
print("   and the NSE_EQ AMO should work within the 3:45 PM–8:59 AM window.")
