#!/usr/bin/env python3
"""
check_margins.py — Live margin requirement checker for all NF strategies.

Queries Dhan /margincalculator/multi for each strategy (1 lot, equal qty all legs),
then shows max affordable lots vs account balance.

Usage:
    python3 check_margins.py           # current expiry
    python3 check_margins.py --lots 5  # show cost for specific lot count
"""
import argparse
import os
import sys
from datetime import date
from math import floor

import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
if not TOKEN or not CLIENT_ID:
    print("ERROR: DHAN_ACCESS_TOKEN or DHAN_CLIENT_ID missing from .env")
    sys.exit(1)

HEADERS = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

UNDERLYING_SCRIP = 13       # Nifty50
LOT_SIZE         = 65 if date.today() >= date(2026, 1, 6) else 75
SPREAD_WIDTH     = 150      # ATM ± 150 pts (3 strikes × 50 pt spacing)
MAX_LOTS_IC      = 10
MAX_LOTS_STRADDLE = 5


# ── API helpers ────────────────────────────────────────────────────────────────

def get_capital() -> float:
    resp = requests.get("https://api.dhan.co/v2/fundlimit", headers=HEADERS, timeout=10)
    if resp.status_code != 200:
        print(f"  fundlimit API {resp.status_code}: {resp.text[:120]}")
        return 0.0
    d   = resp.json()
    bal = (d.get("availabelBalance") or d.get("availableBalance") or d.get("net") or 0)
    return float(bal)


def get_expiry() -> str:
    resp = requests.post(
        "https://api.dhan.co/v2/optionchain/expirylist",
        headers=HEADERS,
        json={"UnderlyingScrip": UNDERLYING_SCRIP, "UnderlyingSeg": "IDX_I"},
        timeout=10,
    )
    if resp.status_code == 200:
        expiries = resp.json().get("data", [])
        if expiries:
            return expiries[0]
    print("  Could not fetch expiry — using today as fallback")
    return date.today().strftime("%Y-%m-%d")


def get_option_chain(expiry_str: str) -> tuple:
    """Returns (spot, oc_dict) or (None, None)."""
    resp = requests.post(
        "https://api.dhan.co/v2/optionchain",
        headers=HEADERS,
        json={"UnderlyingScrip": UNDERLYING_SCRIP, "UnderlyingSeg": "IDX_I",
              "Expiry": expiry_str},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"  OC API {resp.status_code}: {resp.text[:120]}")
        return None, None
    data  = resp.json()
    inner = data.get("data") or {}
    spot  = float(
        data.get("last_price") or data.get("lastTradedPrice") or
        (inner.get("last_price")      if isinstance(inner, dict) else 0) or
        (inner.get("underlyingPrice") if isinstance(inner, dict) else 0) or 0
    )
    if not spot:
        print("  OC returned 200 but spot = 0")
        return None, None
    oc = (inner.get("oc") if isinstance(inner, dict) else {}) or {}
    return spot, oc


def get_leg(oc: dict, strike: float, opt_type: str) -> tuple:
    """Returns (security_id, ltp) or (None, 0.0)."""
    for k in [f"{float(strike):.6f}", str(int(strike)), f"{float(strike):.1f}"]:
        if k in oc:
            sub = oc[k].get(opt_type.lower()) or {}
            sid = sub.get("security_id") or sub.get("securityId")
            ltp = float(sub.get("last_price") or sub.get("ltp") or 0)
            if sid and ltp > 0:
                return str(sid), ltp
    return None, 0.0


def calc_margin(legs: list) -> float:
    """
    legs: list of dicts with keys:
        securityId, transactionType (BUY/SELL), price, quantity
    Returns total margin for 1 lot from Dhan API, or 0 on error.
    """
    scrip_list = [
        {
            "exchangeSegment": "NSE_FNO",
            "transactionType": leg["transactionType"],
            "quantity":        leg["quantity"],
            "productType":     "MARGIN",
            "securityId":      str(leg["securityId"]),
            "price":           float(leg["price"]),
            "triggerPrice":    0,
        }
        for leg in legs
    ]
    payload = {
        "dhanClientId":    CLIENT_ID,
        "includePosition": True,
        "includeOrders":   True,
        "scripList":       scrip_list,
    }
    try:
        resp = requests.post(
            "https://api.dhan.co/v2/margincalculator/multi",
            headers=HEADERS, json=payload, timeout=10,
        )
        if resp.status_code == 200:
            d = resp.json()
            m = float(d.get("total_margin") or d.get("totalMargin") or 0)
            return m
        else:
            print(f"  margin API {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        print(f"  margin API error: {e}")
    return 0.0


# ── Strategy definitions ───────────────────────────────────────────────────────

def build_strategy_legs(strategy: str, atm: float, oc: dict) -> tuple:
    """
    Build (legs_list, net_credit_per_share, description) for each strategy.
    Returns (None, 0, reason) if any leg missing from OC.
    """
    sw = SPREAD_WIDTH

    if strategy == "iron_condor":
        # SELL ATM CE, BUY ATM+SW CE, SELL ATM PE, BUY ATM-SW PE
        ce_s_sid, ce_s_ltp = get_leg(oc, atm,      "ce")
        ce_l_sid, ce_l_ltp = get_leg(oc, atm + sw, "ce")
        pe_s_sid, pe_s_ltp = get_leg(oc, atm,      "pe")
        pe_l_sid, pe_l_ltp = get_leg(oc, atm - sw, "pe")
        missing = [n for n, s in [("CE ATM", ce_s_sid), (f"CE+{sw}", ce_l_sid),
                                   ("PE ATM", pe_s_sid), (f"PE-{sw}", pe_l_sid)] if not s]
        if missing:
            return None, 0, f"missing {missing}"
        legs = [
            {"securityId": ce_s_sid, "transactionType": "SELL", "price": ce_s_ltp, "quantity": LOT_SIZE},
            {"securityId": ce_l_sid, "transactionType": "BUY",  "price": ce_l_ltp, "quantity": LOT_SIZE},
            {"securityId": pe_s_sid, "transactionType": "SELL", "price": pe_s_ltp, "quantity": LOT_SIZE},
            {"securityId": pe_l_sid, "transactionType": "BUY",  "price": pe_l_ltp, "quantity": LOT_SIZE},
        ]
        credit = (ce_s_ltp - ce_l_ltp) + (pe_s_ltp - pe_l_ltp)
        desc = (f"SELL ATM CE ₹{ce_s_ltp:.1f}, BUY CE+{sw} ₹{ce_l_ltp:.1f}, "
                f"SELL ATM PE ₹{pe_s_ltp:.1f}, BUY PE-{sw} ₹{pe_l_ltp:.1f}")
        return legs, credit, desc

    elif strategy == "short_straddle":
        # SELL ATM CE + SELL ATM PE
        ce_sid, ce_ltp = get_leg(oc, atm, "ce")
        pe_sid, pe_ltp = get_leg(oc, atm, "pe")
        missing = [n for n, s in [("CE ATM", ce_sid), ("PE ATM", pe_sid)] if not s]
        if missing:
            return None, 0, f"missing {missing}"
        legs = [
            {"securityId": ce_sid, "transactionType": "SELL", "price": ce_ltp, "quantity": LOT_SIZE},
            {"securityId": pe_sid, "transactionType": "SELL", "price": pe_ltp, "quantity": LOT_SIZE},
        ]
        credit = ce_ltp + pe_ltp
        desc = f"SELL ATM CE ₹{ce_ltp:.1f}, SELL ATM PE ₹{pe_ltp:.1f}"
        return legs, credit, desc

    elif strategy == "short_strangle":
        # SELL ATM+SW CE + SELL ATM-SW PE
        ce_sid, ce_ltp = get_leg(oc, atm + sw, "ce")
        pe_sid, pe_ltp = get_leg(oc, atm - sw, "pe")
        missing = [n for n, s in [(f"CE+{sw}", ce_sid), (f"PE-{sw}", pe_sid)] if not s]
        if missing:
            return None, 0, f"missing {missing}"
        legs = [
            {"securityId": ce_sid, "transactionType": "SELL", "price": ce_ltp, "quantity": LOT_SIZE},
            {"securityId": pe_sid, "transactionType": "SELL", "price": pe_ltp, "quantity": LOT_SIZE},
        ]
        credit = ce_ltp + pe_ltp
        desc = f"SELL CE+{sw} ₹{ce_ltp:.1f}, SELL PE-{sw} ₹{pe_ltp:.1f}"
        return legs, credit, desc

    elif strategy == "bear_call":
        # SELL ATM CE + BUY ATM+SW CE
        ce_s_sid, ce_s_ltp = get_leg(oc, atm,      "ce")
        ce_l_sid, ce_l_ltp = get_leg(oc, atm + sw, "ce")
        missing = [n for n, s in [("CE ATM", ce_s_sid), (f"CE+{sw}", ce_l_sid)] if not s]
        if missing:
            return None, 0, f"missing {missing}"
        legs = [
            {"securityId": ce_s_sid, "transactionType": "SELL", "price": ce_s_ltp, "quantity": LOT_SIZE},
            {"securityId": ce_l_sid, "transactionType": "BUY",  "price": ce_l_ltp, "quantity": LOT_SIZE},
        ]
        credit = ce_s_ltp - ce_l_ltp
        desc = f"SELL ATM CE ₹{ce_s_ltp:.1f}, BUY CE+{sw} ₹{ce_l_ltp:.1f}"
        return legs, credit, desc

    elif strategy == "bull_put":
        # SELL ATM PE + BUY ATM-SW PE
        pe_s_sid, pe_s_ltp = get_leg(oc, atm,      "pe")
        pe_l_sid, pe_l_ltp = get_leg(oc, atm - sw, "pe")
        missing = [n for n, s in [("PE ATM", pe_s_sid), (f"PE-{sw}", pe_l_sid)] if not s]
        if missing:
            return None, 0, f"missing {missing}"
        legs = [
            {"securityId": pe_s_sid, "transactionType": "SELL", "price": pe_s_ltp, "quantity": LOT_SIZE},
            {"securityId": pe_l_sid, "transactionType": "BUY",  "price": pe_l_ltp, "quantity": LOT_SIZE},
        ]
        credit = pe_s_ltp - pe_l_ltp
        desc = f"SELL ATM PE ₹{pe_s_ltp:.1f}, BUY PE-{sw} ₹{pe_l_ltp:.1f}"
        return legs, credit, desc

    return None, 0, "unknown strategy"


STRATEGY_CONFIGS = {
    # key: (display_name, max_lots)
    "iron_condor":   ("Iron Condor (4-leg)",       MAX_LOTS_IC),
    "short_straddle":("Short Straddle (2-leg)",     MAX_LOTS_STRADDLE),
    "short_strangle":("Short Strangle OTM±150 (2-leg)", MAX_LOTS_STRADDLE),
    "bear_call":     ("Bear Call Spread (2-leg)",   MAX_LOTS_IC),
    "bull_put":      ("Bull Put Spread (2-leg)",    MAX_LOTS_IC),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lots", type=int, default=None,
                    help="Show cost for this specific lot count (default: compute max affordable)")
    args = ap.parse_args()

    print("\n" + "="*70)
    print("  NF Strategy Margin Check")
    print("="*70)

    print("\nFetching account balance...")
    capital = get_capital()
    print(f"  Available balance: ₹{capital:,.0f}")

    print("\nFetching nearest expiry...")
    expiry_str = get_expiry()
    print(f"  Expiry: {expiry_str}")

    print("\nFetching option chain...")
    spot, oc = get_option_chain(expiry_str)
    if not spot:
        print("  ERROR: could not fetch option chain")
        sys.exit(1)

    atm = round(spot / 50) * 50
    print(f"  Nifty spot: {spot:.1f}  →  ATM strike: {atm:.0f}")
    print(f"  Lot size: {LOT_SIZE}")

    print("\n" + "="*70)
    print(f"  {'Strategy':<32} {'Margin/lot':>10} {'MaxLots':>8} {'CapNeeded':>12} {'Credit/lot':>11} {'Affordable?':>12}")
    print("="*70)

    results = []
    for key, (name, hard_cap) in STRATEGY_CONFIGS.items():
        legs, credit_per_share, desc = build_strategy_legs(key, atm, oc)
        if legs is None:
            print(f"  {name:<32}  SKIPPED — {desc}")
            continue

        margin_1lot = calc_margin(legs)
        if margin_1lot <= 0:
            print(f"  {name:<32}  ERROR — margin API returned 0")
            continue

        credit_per_lot = credit_per_share * LOT_SIZE

        if args.lots:
            use_lots    = args.lots
            total_marg  = margin_1lot * use_lots
            affordable  = "✅" if total_marg <= capital else "❌ insufficient"
        else:
            max_by_cap  = floor(capital / margin_1lot)
            use_lots    = min(hard_cap, max_by_cap)
            total_marg  = margin_1lot * use_lots
            affordable  = f"✅ {use_lots} lots" if use_lots >= 1 else "❌ 0 lots (need more capital)"

        results.append({
            "name":          name,
            "margin_1lot":   margin_1lot,
            "max_cap_lots":  floor(capital / margin_1lot),
            "hard_cap":      hard_cap,
            "affordable_lots": use_lots,
            "total_margin":  total_marg,
            "credit_per_lot": credit_per_lot,
            "desc":          desc,
        })

        cap_needed = margin_1lot * (args.lots or use_lots)
        print(f"  {name:<32} ₹{margin_1lot:>9,.0f} {hard_cap:>8} ₹{cap_needed:>11,.0f} "
              f"₹{credit_per_lot:>10,.0f}  {affordable}")

    print("="*70)

    if results:
        print("\n  Leg details:")
        for r in results:
            _, _, desc = build_strategy_legs(
                [k for k, (n, _) in STRATEGY_CONFIGS.items() if n == r["name"]][0],
                atm, oc
            )
            print(f"\n  {r['name']}")
            print(f"    {r['desc']}")
            print(f"    Margin/lot:  ₹{r['margin_1lot']:,.0f}")
            print(f"    Credit/lot:  ₹{r['credit_per_lot']:,.0f}  (₹{r['credit_per_lot']/LOT_SIZE:.1f}/share)")
            lots_str = (f"{args.lots} lots → ₹{r['margin_1lot']*args.lots:,.0f} total margin"
                        if args.lots
                        else f"max affordable: {r['affordable_lots']} lots "
                             f"(cap={r['hard_cap']}, balance allows {r['max_cap_lots']}) "
                             f"→ ₹{r['total_margin']:,.0f} total margin")
            print(f"    Sizing:      {lots_str}")
            print(f"    Daily income (at sizing): "
                  f"₹{r['credit_per_lot'] * r['affordable_lots']:,.0f}")

    print()


if __name__ == "__main__":
    main()
