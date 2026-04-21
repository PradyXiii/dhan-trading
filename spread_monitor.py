#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
spread_monitor.py — Intraday SL/TP watcher for credit spreads + NF Iron Condor
===============================================================================
Runs every 1 min during market hours (9:30 AM → 3:10 PM IST).
fcntl lock prevents overlapping runs.

What it does:
  1. Reads data/today_trade.json — today's trade
  2. Handles two strategy families:
     a. bear_call_credit / bull_put_credit  → 2-leg BNF credit spread
     b. nf_iron_condor                      → 4-leg NF IC
  3. Fetches live LTPs for all legs
  4. Computes current spread cost vs entry net_credit
  5. SL / TP hit → close all legs, write exit to today_trade.json, Telegram

PAPER MODE: no real orders, writes exit state to json + paper_trades.csv.

Cron (every 1 min, 9:30 AM → 3:10 PM IST = UTC 4-9):
  * 4-9 * * 1-5 cd ~/dhan-trading && python3 spread_monitor.py >> logs/spread_monitor.log 2>&1
"""
import os
import sys
import csv
import json
import time
import fcntl
import atexit
import requests
from datetime import date, datetime, time as dt_time
from dotenv import load_dotenv

import notify

load_dotenv()
TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID",    "")
DRY_RUN   = "--dry-run" in sys.argv
FORCE     = "--force" in sys.argv    # skip market-hours gate

HEADERS = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

DATA_DIR   = "data"
INTENT     = f"{DATA_DIR}/today_trade.json"
PAPER_CSV  = f"{DATA_DIR}/paper_trades.csv"

CREDIT_SL_FRAC = 0.5     # SL: spread cost grew 50% above entry credit
CREDIT_TP_FRAC = 0.90    # TP: retain 90% of credit (scan_ic_rr.py: +18% P&L vs 0.65)

MKT_OPEN  = dt_time(9,  30)
MKT_CLOSE = dt_time(15, 10)   # hand off to exit_positions.py at 3:15

SPREAD_STRATEGIES = {"bear_call_credit", "bull_put_credit"}
IC_STRATEGY       = "nf_iron_condor"

_LOCK_FILE = "/tmp/spread_monitor.lock"
_lock_fh   = None


def _acquire_lock():
    global _lock_fh
    _lock_fh = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)


def _release_lock():
    if _lock_fh:
        try:
            fcntl.flock(_lock_fh, fcntl.LOCK_UN)
            _lock_fh.close()
            os.remove(_LOCK_FILE)
        except OSError:
            pass


atexit.register(_release_lock)


def _in_market_hours() -> bool:
    now = datetime.now().time()
    return MKT_OPEN <= now <= MKT_CLOSE


def _load_intent() -> dict:
    if not os.path.exists(INTENT):
        return {}
    try:
        with open(INTENT) as f:
            d = json.load(f)
        if d.get("date") != date.today().isoformat():
            return {}
        return d
    except Exception:
        return {}


def _save_intent(d: dict):
    with open(INTENT, "w") as f:
        json.dump(d, f, indent=2)


def _get_ltps(security_ids: list) -> dict:
    """Fetch LTPs from Dhan marketfeed. Returns {sid_str: ltp_float}."""
    try:
        payload = {"NSE_FNO": [int(s) for s in security_ids]}
        resp = requests.post("https://api.dhan.co/v2/marketfeed/ltp",
                             headers=HEADERS, json=payload, timeout=10)
        if resp.status_code != 200:
            notify.log(f"Spread monitor LTP API {resp.status_code}: {resp.text[:100]}")
            return {}
        d = resp.json()
        seg = (d.get("data") or {}).get("NSE_FNO") or {}
        out = {}
        for sid in security_ids:
            k = str(sid)
            entry = seg.get(k) or seg.get(int(k)) or {}
            ltp = float(entry.get("last_price") or entry.get("lastTradedPrice") or 0)
            out[k] = ltp
        return out
    except Exception as e:
        notify.log(f"Spread monitor LTP fetch failed: {e}")
        return {}


# ── 2-leg spread close (BNF bear_call / bull_put) ────────────────────────────

def _close_spread(intent: dict) -> dict:
    """BUY back short leg, SELL long leg."""
    short_sid = str(intent["short_sid"])
    long_sid  = str(intent["long_sid"])
    qty       = int(intent["lots"]) * int(intent.get("lot_size", 30))
    day_tag   = date.today().strftime("%Y%m%d")

    base = {
        "dhanClientId":      CLIENT_ID,
        "exchangeSegment":   "NSE_FNO",
        "productType":       "MARGIN",
        "orderType":         "MARKET",
        "validity":          "DAY",
        "quantity":          qty,
        "price":             0,
        "triggerPrice":      0,
        "disclosedQuantity": 0,
    }

    if DRY_RUN:
        return {"close_short": "DRY_RUN", "close_long": "DRY_RUN"}

    result = {}
    try:
        r1 = requests.post("https://api.dhan.co/v2/orders",
                           headers=HEADERS,
                           json={**base, "correlationId": f"spread_exit_buy_{day_tag}",
                                 "transactionType": "BUY", "securityId": short_sid},
                           timeout=15)
        result["close_short"] = r1.json()
    except Exception as e:
        result["close_short_err"] = str(e)

    time.sleep(2)

    try:
        r2 = requests.post("https://api.dhan.co/v2/orders",
                           headers=HEADERS,
                           json={**base, "correlationId": f"spread_exit_sell_{day_tag}",
                                 "transactionType": "SELL", "securityId": long_sid},
                           timeout=15)
        result["close_long"] = r2.json()
    except Exception as e:
        result["close_long_err"] = str(e)

    return result


# ── 4-leg IC close (NF iron condor) ──────────────────────────────────────────

def _close_ic(intent: dict) -> dict:
    """
    Close all 4 IC legs:
    BUY back CE short, SELL CE long, BUY back PE short, SELL PE long.
    """
    qty     = int(intent["lots"]) * int(intent.get("lot_size", 65))
    day_tag = date.today().strftime("%Y%m%d")

    base = {
        "dhanClientId":      CLIENT_ID,
        "exchangeSegment":   "NSE_FNO",
        "productType":       "MARGIN",
        "orderType":         "MARKET",
        "validity":          "DAY",
        "quantity":          qty,
        "price":             0,
        "triggerPrice":      0,
        "disclosedQuantity": 0,
    }

    if DRY_RUN:
        return {leg: "DRY_RUN" for leg in
                ["ce_buy_back", "ce_sell_long", "pe_buy_back", "pe_sell_long"]}

    legs = [
        ("BUY",  intent["ce_short_sid"], "ce_buy_back"),
        ("SELL", intent["ce_long_sid"],  "ce_sell_long"),
        ("BUY",  intent["pe_short_sid"], "pe_buy_back"),
        ("SELL", intent["pe_long_sid"],  "pe_sell_long"),
    ]
    result = {}
    for trans, sid, tag in legs:
        try:
            r = requests.post(
                "https://api.dhan.co/v2/orders", headers=HEADERS,
                json={**base, "correlationId": f"ic_exit_{tag}_{day_tag}",
                      "transactionType": trans, "securityId": str(sid)},
                timeout=15,
            )
            result[tag] = r.json()
            oid = result[tag].get("orderId") or result[tag].get("order_id")
            notify.log(f"IC exit {trans} {tag} orderId={oid}")
        except Exception as e:
            result[f"{tag}_err"] = str(e)
            notify.log(f"IC exit {trans} {tag} FAILED: {e}")
        time.sleep(1)

    return result


# ── paper_trades.csv exit update ─────────────────────────────────────────────

def _update_paper_csv_exit(intent: dict):
    """Update today's row in paper_trades.csv with exit fields."""
    if not os.path.exists(PAPER_CSV):
        return
    try:
        with open(PAPER_CSV) as f:
            rows = list(csv.DictReader(f))
        today_s = date.today().isoformat()

        exit_cols = [
            "exit_reason", "exit_spread",
            "exit_short_ltp", "exit_long_ltp",           # 2-leg spread
            "exit_ce_short_ltp", "exit_ce_long_ltp",     # IC
            "exit_pe_short_ltp", "exit_pe_long_ltp",     # IC
            "exit_time", "pnl_inr",
        ]
        for r in rows:
            for c in exit_cols:
                r.setdefault(c, "")

        for r in rows:
            if r.get("date") == today_s:
                for c in exit_cols:
                    if c in intent:
                        r[c] = intent[c]
                break

        if not rows:
            return
        fieldnames = list(rows[0].keys())
        with open(PAPER_CSV, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    except Exception as e:
        notify.log(f"paper_trades.csv exit update failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    _acquire_lock()

    if not FORCE and not _in_market_hours():
        return

    intent = _load_intent()
    if not intent:
        return

    strategy = intent.get("strategy", "")
    if strategy not in SPREAD_STRATEGIES and strategy != IC_STRATEGY:
        return   # naked option — not our concern

    if intent.get("exit_done"):
        return   # already closed

    paper = (intent.get("order_mode") == "PAPER" or intent.get("mode") == "PAPER")

    # ═══════════════════════════════════════════════════════════════════════════
    # NF IRON CONDOR PATH
    # ═══════════════════════════════════════════════════════════════════════════
    if strategy == IC_STRATEGY:
        ce_short_sid = str(intent.get("ce_short_sid", ""))
        ce_long_sid  = str(intent.get("ce_long_sid",  ""))
        pe_short_sid = str(intent.get("pe_short_sid", ""))
        pe_long_sid  = str(intent.get("pe_long_sid",  ""))

        if not all([ce_short_sid, ce_long_sid, pe_short_sid, pe_long_sid]):
            notify.log("IC monitor: missing leg SIDs in today_trade.json")
            return

        ltps = _get_ltps([ce_short_sid, ce_long_sid, pe_short_sid, pe_long_sid])
        ce_short_ltp = ltps.get(ce_short_sid, 0.0)
        ce_long_ltp  = ltps.get(ce_long_sid,  0.0)
        pe_short_ltp = ltps.get(pe_short_sid, 0.0)
        pe_long_ltp  = ltps.get(pe_long_sid,  0.0)

        if any(ltp <= 0 for ltp in [ce_short_ltp, ce_long_ltp,
                                     pe_short_ltp, pe_long_ltp]):
            notify.log(
                f"IC monitor: LTP zero — CE {ce_short_ltp:.0f}/{ce_long_ltp:.0f}  "
                f"PE {pe_short_ltp:.0f}/{pe_long_ltp:.0f}"
            )
            return

        net_credit   = float(intent.get("net_credit", 0))
        ce_cost      = ce_short_ltp - ce_long_ltp
        pe_cost      = pe_short_ltp - pe_long_ltp
        current_cost = ce_cost + pe_cost

        sl_trigger = net_credit * (1 + CREDIT_SL_FRAC)
        tp_trigger = net_credit * (1 - CREDIT_TP_FRAC)

        hit_sl = current_cost >= sl_trigger
        hit_tp = current_cost <= tp_trigger

        if not (hit_sl or hit_tp):
            return

        reason = "SL" if hit_sl else "TP"

        if not paper:
            close_result = _close_ic(intent)
            notify.log(f"IC exit ({reason}) — {close_result}")
        else:
            notify.log(f"IC exit ({reason}) — PAPER, no real order")

        qty           = int(intent.get("lots", 0)) * int(intent.get("lot_size", 65))
        pnl_per_share = net_credit - current_cost
        total_pnl     = round(pnl_per_share * qty, 2)

        intent.update({
            "exit_done":          True,
            "exit_reason":        reason,
            "exit_spread":        round(current_cost, 2),
            "exit_ce_short_ltp":  round(ce_short_ltp, 2),
            "exit_ce_long_ltp":   round(ce_long_ltp, 2),
            "exit_pe_short_ltp":  round(pe_short_ltp, 2),
            "exit_pe_long_ltp":   round(pe_long_ltp, 2),
            "exit_time":          datetime.now().strftime("%H:%M"),
            "pnl_inr":            total_pnl,
        })
        _save_intent(intent)
        if paper:
            _update_paper_csv_exit(intent)

        emoji   = "🟢" if reason == "TP" else "🔴"
        verdict = "65% of credit kept — winner" if reason == "TP" else \
                  "IC spread cost doubled — stopped out"
        mode_tag = "[PAPER] " if paper else ""

        notify.send(
            f"{emoji} <b>Nifty IC {reason} Hit {mode_tag}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Entry credit  ₹{net_credit:.0f} / share\n"
            f"Exit cost     ₹{current_cost:.0f} / share\n"
            f"  CE spread   ₹{ce_short_ltp:.0f} − ₹{ce_long_ltp:.0f} = ₹{ce_cost:.0f}\n"
            f"  PE spread   ₹{pe_short_ltp:.0f} − ₹{pe_long_ltp:.0f} = ₹{pe_cost:.0f}\n"
            f"Shares        {qty}  ({intent.get('lots', 0)} lot)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>P&amp;L  ₹{total_pnl:+,.0f}</b>\n"
            f"Reason        {verdict}\n"
            f"<i>{'No real order — paper tracked.' if paper else 'All 4 IC legs closed on Dhan.'}</i>"
        )
        return

    # ═══════════════════════════════════════════════════════════════════════════
    # 2-LEG CREDIT SPREAD PATH  (bear_call_credit / bull_put_credit)
    # ═══════════════════════════════════════════════════════════════════════════
    short_sid = str(intent.get("short_sid", ""))
    long_sid  = str(intent.get("long_sid", ""))
    if not short_sid or not long_sid:
        notify.log("Spread monitor: missing leg IDs in today_trade.json")
        return

    ltps = _get_ltps([short_sid, long_sid])
    short_ltp = ltps.get(short_sid, 0.0)
    long_ltp  = ltps.get(long_sid,  0.0)
    if short_ltp <= 0 or long_ltp <= 0:
        notify.log(f"Spread monitor: LTPs unavailable short={short_ltp} long={long_ltp}")
        return

    net_credit   = float(intent.get("net_credit", 0))
    current_cost = short_ltp - long_ltp
    sl_trigger   = net_credit * (1 + CREDIT_SL_FRAC)
    tp_trigger   = net_credit * (1 - CREDIT_TP_FRAC)

    hit_sl = current_cost >= sl_trigger
    hit_tp = current_cost <= tp_trigger

    if not (hit_sl or hit_tp):
        return

    reason = "SL" if hit_sl else "TP"

    if not paper:
        close_result = _close_spread(intent)
        notify.log(f"Spread exit ({reason}) — close result: {close_result}")
    else:
        notify.log(f"Spread exit ({reason}) — PAPER, no real order")

    pnl_per_share = net_credit - current_cost
    qty           = int(intent.get("lots", 0)) * int(intent.get("lot_size", 30))
    total_pnl     = round(pnl_per_share * qty, 2)

    intent.update({
        "exit_done":      True,
        "exit_reason":    reason,
        "exit_spread":    round(current_cost, 2),
        "exit_short_ltp": round(short_ltp, 2),
        "exit_long_ltp":  round(long_ltp, 2),
        "exit_time":      datetime.now().strftime("%H:%M"),
        "pnl_inr":        total_pnl,
    })
    _save_intent(intent)
    if paper:
        _update_paper_csv_exit(intent)

    emoji   = "🟢" if reason == "TP" else "🔴"
    verdict = "65% of credit kept — winner" if reason == "TP" else \
              "Spread grew 50% above credit — stopped out"
    mode_tag = "[PAPER] " if paper else ""
    strategy_name = ("Bear Call Spread" if strategy == "bear_call_credit"
                     else "Bull Put Spread")

    notify.send(
        f"{emoji} <b>Spread {reason} Hit {mode_tag}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Strategy      {strategy_name}\n"
        f"Entry credit  ₹{net_credit:.0f} / share\n"
        f"Exit cost     ₹{current_cost:.0f} / share  "
        f"(short ₹{short_ltp:.0f} − long ₹{long_ltp:.0f})\n"
        f"Shares        {qty}  ({intent.get('lots', 0)} lot)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>P&amp;L  ₹{total_pnl:+,.0f}</b>\n"
        f"Reason        {verdict}\n"
        f"<i>{'No real order — paper trade tracked only.' if paper else 'Both legs closed on Dhan.'}</i>"
    )


if __name__ == "__main__":
    main()
