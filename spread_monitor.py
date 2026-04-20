#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
spread_monitor.py — Intraday SL/TP watcher for credit spreads
==============================================================
Runs every 5 min during market hours (9:30 AM → 3:10 PM IST).

What it does:
  1. Reads data/today_trade.json — today's spread trade
  2. Skips if not a credit spread (strategy != bear_call_credit / bull_put_credit)
  3. Skips if exit already recorded (exit_done == True)
  4. Fetches live LTP for both legs via Dhan marketfeed/ltp
  5. Computes current spread cost = short_ltp - long_ltp
  6. If spread >= SL trigger → close both legs (BUY back short, SELL long)
     If spread <= TP trigger → close both legs
  7. Writes exit fields to today_trade.json + paper_trades.csv row
  8. Sends Telegram exit alert

PAPER MODE: no real Dhan orders — only writes to today_trade.json +
  paper_trades.csv. Same SL/TP logic, same alerts, just no money moves.

Cron (every 5 min, 9:30 AM → 3:10 PM IST = 4:00 UTC → 9:40 UTC):
  */5 4-9 * * 1-5 cd ~/dhan-trading && python3 spread_monitor.py >> logs/spread_monitor.log 2>&1
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

# Locked from backtest (+₹36.2L over 5y)
CREDIT_SL_FRAC = 0.5     # stop when spread grows 50% above entry credit
CREDIT_TP_FRAC = 0.65    # take profit when 65% of credit is in pocket

MKT_OPEN  = dt_time(9,  30)
MKT_CLOSE = dt_time(15, 10)   # stop at 3:10; exit_positions.py owns 3:15+

SPREAD_STRATEGIES = {"bear_call_credit", "bull_put_credit"}

_LOCK_FILE = "/tmp/spread_monitor.lock"
_lock_fh   = None


def _acquire_lock():
    global _lock_fh
    _lock_fh = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)   # another run in progress; silent exit


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


def _close_spread(intent: dict) -> dict:
    """
    Close both legs:
      - BUY back the short leg (buy to cover)
      - SELL the long hedge leg
    """
    short_sid = str(intent["short_sid"])
    long_sid  = str(intent["long_sid"])
    qty       = int(intent["lots"]) * int(intent.get("lot_size", 30))
    day_tag   = date.today().strftime("%Y%m%d")

    buy_back = {
        "dhanClientId":      CLIENT_ID,
        "correlationId":     f"spread_exit_buy_{day_tag}",
        "transactionType":   "BUY",
        "exchangeSegment":   "NSE_FNO",
        "productType":       "MARGIN",
        "orderType":         "MARKET",
        "validity":          "DAY",
        "securityId":        short_sid,
        "quantity":          qty,
        "price":             0,
        "triggerPrice":      0,
        "disclosedQuantity": 0,
    }
    sell_long = {**buy_back,
                 "correlationId": f"spread_exit_sell_{day_tag}",
                 "transactionType": "SELL",
                 "securityId": long_sid}

    if DRY_RUN:
        return {"close_short": "DRY_RUN", "close_long": "DRY_RUN"}

    result = {}
    try:
        r1 = requests.post("https://api.dhan.co/v2/orders",
                           headers=HEADERS, json=buy_back, timeout=15)
        result["close_short"] = r1.json()
    except Exception as e:
        result["close_short_err"] = str(e)

    time.sleep(2)   # let exchange settle before 2nd leg

    try:
        r2 = requests.post("https://api.dhan.co/v2/orders",
                           headers=HEADERS, json=sell_long, timeout=15)
        result["close_long"] = r2.json()
    except Exception as e:
        result["close_long_err"] = str(e)

    return result


def _update_paper_csv_exit(intent: dict):
    """
    Update today's row in paper_trades.csv with exit fields.
    Rewrites the whole file (small — one row per trading day).
    """
    if not os.path.exists(PAPER_CSV):
        return
    try:
        with open(PAPER_CSV) as f:
            rows = list(csv.DictReader(f))
        today_s = date.today().isoformat()

        # Add exit columns to header if missing
        exit_cols = ["exit_reason", "exit_spread", "exit_short_ltp",
                     "exit_long_ltp", "exit_time", "pnl_inr"]
        for r in rows:
            for c in exit_cols:
                r.setdefault(c, "")

        # Find today's row, populate exit fields
        for r in rows:
            if r.get("date") == today_s:
                r["exit_reason"]     = intent.get("exit_reason", "")
                r["exit_spread"]     = intent.get("exit_spread", "")
                r["exit_short_ltp"]  = intent.get("exit_short_ltp", "")
                r["exit_long_ltp"]   = intent.get("exit_long_ltp", "")
                r["exit_time"]       = intent.get("exit_time", "")
                r["pnl_inr"]         = intent.get("pnl_inr", "")
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


def main():
    _acquire_lock()

    if not FORCE and not _in_market_hours():
        return

    intent = _load_intent()
    if not intent:
        return

    strategy = intent.get("strategy", "")
    if strategy not in SPREAD_STRATEGIES:
        return   # naked option trade — not our concern

    if intent.get("exit_done"):
        return   # already closed

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

    net_credit    = float(intent.get("net_credit", 0))
    current_cost  = short_ltp - long_ltp    # cost to close spread now
    sl_trigger    = net_credit * (1 + CREDIT_SL_FRAC)
    tp_trigger    = net_credit * (1 - CREDIT_TP_FRAC)

    hit_sl = current_cost >= sl_trigger
    hit_tp = current_cost <= tp_trigger

    if not (hit_sl or hit_tp):
        return   # still within band, keep monitoring

    reason = "SL" if hit_sl else "TP"
    paper  = (intent.get("order_mode") == "PAPER" or
              intent.get("mode") == "PAPER")

    # Close position (or simulate in paper)
    close_result = {}
    if not paper:
        close_result = _close_spread(intent)
        notify.log(f"Spread exit ({reason}) — close result: {close_result}")
    else:
        notify.log(f"Spread exit ({reason}) — PAPER, no real order")

    # P&L per share = credit received - cost to close
    pnl_per_share = net_credit - current_cost
    qty           = int(intent.get("lots", 0)) * int(intent.get("lot_size", 30))
    total_pnl     = round(pnl_per_share * qty, 2)

    # Persist exit state
    intent["exit_done"]      = True
    intent["exit_reason"]    = reason
    intent["exit_spread"]    = round(current_cost, 2)
    intent["exit_short_ltp"] = round(short_ltp, 2)
    intent["exit_long_ltp"]  = round(long_ltp, 2)
    intent["exit_time"]      = datetime.now().strftime("%H:%M")
    intent["pnl_inr"]        = total_pnl
    _save_intent(intent)

    if paper:
        _update_paper_csv_exit(intent)

    emoji   = "🟢" if reason == "TP" else "🔴"
    verdict = "65% of credit kept — winner" if reason == "TP" else \
              "Spread grew 50% above credit — stopped out"
    mode_tag = "[PAPER]" if paper else ""
    strategy_name = ("Bear Call Spread" if strategy == "bear_call_credit"
                     else "Bull Put Spread")

    notify.send(
        f"{emoji} <b>Spread {reason} Hit {mode_tag}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Strategy      {strategy_name}\n"
        f"Entry credit  ₹{net_credit:.0f} / share\n"
        f"Exit cost     ₹{current_cost:.0f} / share  (short ₹{short_ltp:.0f} − long ₹{long_ltp:.0f})\n"
        f"Shares        {qty}  ({intent.get('lots', 0)} lot)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>P&amp;L  ₹{total_pnl:+,.0f}</b>\n"
        f"Reason        {verdict}\n"
        f"<i>{'No real order — paper trade tracked only.' if paper else 'Both legs closed on Dhan.'}</i>"
    )


if __name__ == "__main__":
    main()
