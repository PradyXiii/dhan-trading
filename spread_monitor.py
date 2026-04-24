#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
spread_monitor.py — Intraday SL/TP watcher for credit spreads + NF Iron Condor
===============================================================================
Runs every 1 min during market hours (9:30 AM → 3:10 PM IST).
fcntl lock prevents overlapping runs.

What it does:
  1. Reads data/today_trade.json — today's trade
  2. Handles three strategy families:
     a. nf_iron_condor                      → 4-leg IC (SL only, no TP — EOD)
     b. nf_short_straddle                   → 2-leg straddle (BUY back both; SL only, no TP)
     c. bear_call_credit / bull_put_credit  → 2-leg credit spread (SL + TP)
  3. Fetches live LTPs for all legs via /v2/marketfeed/ltp
  4. Computes current cost vs entry net_credit
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
from datetime import date, datetime, time as dt_time, timedelta, timezone
_IST = timezone(timedelta(hours=5, minutes=30))
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
CREDIT_TP_FRAC = 0.65    # TP for 2-leg spreads (bear_call / bull_put)
# IC has no TP — backtest proves holding to EOD adds +18% P&L. IC only exits on SL or EOD.

MKT_OPEN  = dt_time(9,  30)
MKT_CLOSE = dt_time(15, 10)   # hand off to exit_positions.py at 3:15

SPREAD_STRATEGIES   = {"bear_call_credit", "bull_put_credit"}
IC_STRATEGY         = "nf_iron_condor"
STRADDLE_STRATEGY   = "nf_short_straddle"
STRADDLE_SL_FRAC    = 0.5    # SL: buyback cost >= net_credit * 1.5

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
        if d.get("date") != datetime.now(_IST).date().isoformat():
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


# ── Primary exit: DELETE /v2/positions ───────────────────────────────────────

def _exit_all_api() -> bool:
    """
    Primary exit: DELETE /v2/positions — one call closes all open positions.
    Returns True on SUCCESS; False means fall back to leg-by-leg functions.
    """
    try:
        resp = requests.delete("https://api.dhan.co/v2/positions",
                               headers=HEADERS, timeout=15)
        data = resp.json()
        if data.get("status") == "SUCCESS":
            notify.log(f"EXIT ALL via DELETE /v2/positions: {data.get('message', '')}")
            return True
        notify.log(f"EXIT ALL API non-SUCCESS {resp.status_code}: {data}")
        return False
    except Exception as e:
        notify.log(f"EXIT ALL API exception: {e}")
        return False


# ── 2-leg spread close (BNF bear_call / bull_put) ────────────────────────────

def _close_spread(intent: dict) -> dict:
    """BUY back short leg first, then SELL long leg. Short first = no naked exposure between legs."""
    short_sid = str(intent["short_sid"])
    long_sid  = str(intent["long_sid"])
    qty       = int(intent["lots"]) * int(intent.get("lot_size", 30))
    day_tag   = datetime.now(_IST).date().strftime("%Y%m%d")

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
    Close all 4 IC legs — shorts first, then longs.
    Order matters: buying back shorts first removes margin obligation before
    selling wings. Selling a long wing while short is still open = naked short.
    Sequence: BUY CE short → BUY PE short → SELL CE long → SELL PE long.
    """
    qty     = int(intent["lots"]) * int(intent.get("lot_size", 65))
    day_tag = datetime.now(_IST).date().strftime("%Y%m%d")

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
                ["ce_buy_back", "pe_buy_back", "ce_sell_long", "pe_sell_long"]}

    legs = [
        # Shorts first — removes obligation + margin risk immediately
        ("BUY",  intent["ce_short_sid"], "ce_buy_back"),
        ("BUY",  intent["pe_short_sid"], "pe_buy_back"),
        # Longs second — wings are already paid for, safe to sell after shorts closed
        ("SELL", intent["ce_long_sid"],  "ce_sell_long"),
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


# ── 2-leg straddle close (NF short straddle) ─────────────────────────────────

def _close_straddle(intent: dict, ce_ltp: float = 0.0, pe_ltp: float = 0.0) -> dict:
    """
    BUY back both short legs. Close the challenged (higher LTP = more ITM) leg first
    to stop the bleeding. The winning (lower LTP, nearly OTM) leg closes second.
    """
    qty     = int(intent["lots"]) * int(intent.get("lot_size", 65))
    day_tag = datetime.now(_IST).date().strftime("%Y%m%d")

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
        return {"ce_buy_back": "DRY_RUN", "pe_buy_back": "DRY_RUN"}

    # Higher LTP = more ITM = challenged leg = close first
    legs = [
        ("BUY", intent["ce_sid"], "ce_buy_back", ce_ltp),
        ("BUY", intent["pe_sid"], "pe_buy_back", pe_ltp),
    ]
    legs.sort(key=lambda x: x[3], reverse=True)  # ITM (highest cost) first

    result = {}
    for trans, sid, tag, _ in legs:
        try:
            r = requests.post(
                "https://api.dhan.co/v2/orders", headers=HEADERS,
                json={**base, "correlationId": f"straddle_exit_{tag}_{day_tag}",
                      "transactionType": trans, "securityId": str(sid)},
                timeout=15,
            )
            result[tag] = r.json()
            oid = result[tag].get("orderId") or result[tag].get("order_id")
            notify.log(f"Straddle exit BUY {tag} orderId={oid}")
        except Exception as e:
            result[f"{tag}_err"] = str(e)
            notify.log(f"Straddle exit BUY {tag} FAILED: {e}")
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
        today_s = datetime.now(_IST).date().isoformat()

        exit_cols = [
            "exit_reason", "exit_spread",
            "exit_short_ltp", "exit_long_ltp",   # 2-leg spread
            "ce_short_exit", "ce_long_exit",      # IC
            "pe_short_exit", "pe_long_exit",      # IC
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
    if strategy not in SPREAD_STRATEGIES and strategy not in (IC_STRATEGY, STRADDLE_STRATEGY):
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

        # IC: no TP. Backtest shows EOD-only = +18% P&L vs TP=0.65. Always hold to 3:15 PM.
        hit_sl = current_cost >= sl_trigger

        if not hit_sl:
            return

        reason = "SL"

        if not paper:
            if not _exit_all_api():                    # primary: one DELETE call
                close_result = _close_ic(intent)       # backup: leg-by-leg
                notify.log(f"IC exit backup ({reason}) — {close_result}")
        else:
            notify.log(f"IC exit ({reason}) — PAPER, no real order")

        qty           = int(intent.get("lots", 0)) * int(intent.get("lot_size", 65))
        pnl_per_share = net_credit - current_cost
        total_pnl     = round(pnl_per_share * qty, 2)

        intent.update({
            "exit_done":          True,
            "exit_reason":        reason,
            "exit_spread":        round(current_cost, 2),
            "ce_short_exit":  round(ce_short_ltp, 2),
            "ce_long_exit":   round(ce_long_ltp, 2),
            "pe_short_exit":  round(pe_short_ltp, 2),
            "pe_long_exit":   round(pe_long_ltp, 2),
            "exit_time":          datetime.now(_IST).strftime("%H:%M"),
            "pnl_inr":            total_pnl,
        })
        _save_intent(intent)
        if paper:
            _update_paper_csv_exit(intent)

        emoji   = "🔴"
        verdict = "IC spread cost doubled — stopped out"
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
    # SHORT STRADDLE PATH  (nf_short_straddle)
    # ═══════════════════════════════════════════════════════════════════════════
    if strategy == STRADDLE_STRATEGY:
        ce_sid = str(intent.get("ce_sid", ""))
        pe_sid = str(intent.get("pe_sid", ""))

        if not ce_sid or not pe_sid:
            notify.log("Straddle monitor: missing ce_sid/pe_sid in today_trade.json")
            return

        ltps     = _get_ltps([ce_sid, pe_sid])
        ce_ltp   = ltps.get(ce_sid, 0.0)
        pe_ltp   = ltps.get(pe_sid, 0.0)

        if ce_ltp <= 0 or pe_ltp <= 0:
            notify.log(
                f"Straddle monitor: LTP zero — CE {ce_ltp:.0f}  PE {pe_ltp:.0f}")
            return

        net_credit   = float(intent.get("net_credit", 0))
        current_cost = ce_ltp + pe_ltp  # buyback cost to close both shorts
        sl_trigger   = net_credit * (1 + STRADDLE_SL_FRAC)

        # Straddle: no TP — hold to EOD 3:15 PM for maximum theta decay.
        hit_sl = current_cost >= sl_trigger

        if not hit_sl:
            return

        reason = "SL"

        if not paper:
            if not _exit_all_api():                                                    # primary
                close_result = _close_straddle(intent, ce_ltp=ce_ltp, pe_ltp=pe_ltp)  # backup
                notify.log(f"Straddle exit backup ({reason}) — {close_result}")
        else:
            notify.log(f"Straddle exit ({reason}) — PAPER, no real order")

        qty           = int(intent.get("lots", 0)) * int(intent.get("lot_size", 65))
        pnl_per_share = net_credit - current_cost
        total_pnl     = round(pnl_per_share * qty, 2)

        intent.update({
            "exit_done":      True,
            "exit_reason":    reason,
            "exit_spread":    round(current_cost, 2),
            "exit_ce_ltp":    round(ce_ltp, 2),
            "exit_pe_ltp":    round(pe_ltp, 2),
            "exit_time":      datetime.now(_IST).strftime("%H:%M"),
            "pnl_inr":        total_pnl,
        })
        _save_intent(intent)
        if paper:
            _update_paper_csv_exit(intent)

        mode_tag = "[PAPER] " if paper else ""
        notify.send(
            f"🔴 <b>Straddle SL Hit {mode_tag}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Entry credit  ₹{net_credit:.0f} / share\n"
            f"Buyback cost  ₹{current_cost:.0f}  "
            f"(CE ₹{ce_ltp:.0f} + PE ₹{pe_ltp:.0f})\n"
            f"Shares        {qty}  ({intent.get('lots', 0)} lot)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>P&amp;L  ₹{total_pnl:+,.0f}</b>\n"
            f"<i>{'No real order — paper tracked.' if paper else 'Both straddle legs closed on Dhan.'}</i>"
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
        if not _exit_all_api():                    # primary: one DELETE call
            close_result = _close_spread(intent)   # backup: leg-by-leg
            notify.log(f"Spread exit backup ({reason}) — {close_result}")
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
        "exit_time":      datetime.now(_IST).strftime("%H:%M"),
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
