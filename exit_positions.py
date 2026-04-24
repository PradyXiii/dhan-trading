#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
exit_positions.py — EOD position squareoff at 3:15 PM IST
==========================================================
Closes any open Nifty50 F&O positions via Dhan MARKET SELL.
MARGIN (NRML) positions do NOT auto-square off — this script does it.

If SL or TP already fired, netQty will be 0 → nothing to do.
If neither fired by 3:15 PM → squareoff at market price.

Cron (3:15 PM IST = 9:45 AM UTC — retries until 3:20 PM hard deadline):
  45 9 * * 1-5 cd ~/dhan-trading && python3 exit_positions.py >> logs/exit.log 2>&1
"""
import json
import os
import sys
import time
import requests
import pandas as pd
from datetime import date, datetime, timedelta, timezone
_IST = timezone(timedelta(hours=5, minutes=30))
from dotenv import load_dotenv

import notify

load_dotenv()
TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID",    "")
DRY_RUN   = "--dry-run" in sys.argv

HEADERS = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

DATA_DIR = "data"


def _write_exit_marker():
    """
    Write data/exit_completed_YYYY-MM-DD.marker so tomorrow's auto_trader.py
    can verify that today's exit script ran successfully.
    Written for both 'positions closed' and 'nothing to close' outcomes —
    what matters is the script ran, not that it had anything to do.
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        marker = os.path.join(DATA_DIR, f"exit_completed_{datetime.now(_IST).date().isoformat()}.marker")
        with open(marker, "w") as f:
            f.write(f"exit_positions.py completed at "
                    f"{datetime.now(_IST).strftime('%H:%M:%S IST')}\n")
        notify.log(f"Exit marker written: {marker}")
    except Exception as e:
        notify.log(f"Could not write exit marker: {e}")


# NSE Trading Holidays 2026 — update each December from NSE's annual circular.
# Tentative moon-based dates marked; verify vs official circular for accuracy.
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 19),   # Chhatrapati Shivaji Maharaj Jayanti
    date(2026, 3, 20),   # Holi
    date(2026, 4,  3),   # Good Friday
    date(2026, 4,  6),   # Ram Navami
    date(2026, 4, 14),   # Dr. B.R. Ambedkar Jayanti
    date(2026, 5,  1),   # Maharashtra Day
    date(2026, 6, 27),   # Bakri Id (tentative)
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 27),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 21),  # Dussehra (tentative)
    date(2026, 11,  1),  # Diwali Laxmi Pujan (tentative)
    date(2026, 11,  2),  # Diwali Balipratipada (tentative)
    date(2026, 11, 24),  # Guru Nanak Jayanti (tentative)
    date(2026, 12, 25),  # Christmas
}


def _is_trading_day() -> bool:
    """Return True if today is an NSE trading day (weekday + not in holiday list).
    CSV-presence check removed — Dhan historical API never returns today's candle
    pre-market or pre-close, causing real trading days to be skipped.
    """
    today = datetime.now(_IST).date()
    if today.weekday() >= 5:
        return False
    return today not in NSE_HOLIDAYS_2026


def _load_today_trade() -> dict:
    """Read data/today_trade.json written by auto_trader.py. Returns {} if missing."""
    path = os.path.join(DATA_DIR, "today_trade.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            td = json.load(f)
        if td.get("date") != datetime.now(_IST).date().isoformat():
            return {}
        return td
    except Exception:
        return {}


def _get_today_nf_sells() -> list:
    """
    Fetch today's tradebook from Dhan and return SELL fills for today's NF option.
    Returns list of fill dicts (may be empty on API failure).
    """
    try:
        resp = requests.get("https://api.dhan.co/v2/trades", headers=HEADERS, timeout=10)
    except Exception as e:
        notify.log(f"Tradebook fetch failed: {e}")
        return []
    if resp.status_code != 200:
        notify.log(f"Tradebook API {resp.status_code}: {resp.text[:80]}")
        return []
    trades = resp.json()
    if not isinstance(trades, list):
        trades = trades.get("data", [])
    td = _load_today_trade()
    sec_id = str(td.get("security_id", ""))
    return [
        t for t in trades
        if t.get("transactionType") == "SELL"
        and str(t.get("securityId", "")) == sec_id
        and t.get("exchangeSegment", "") == "NSE_FNO"
    ] if sec_id else []


def _classify_exit(exit_price: float, sl_price: float, tp_price: float) -> str:
    """Return human-readable exit reason based on where exit_price lands."""
    sl_tol = sl_price * 0.05  # 5% tolerance — SL-M slippage
    tp_tol = tp_price * 0.05
    if exit_price <= sl_price + sl_tol:
        return "🔴 Stop-loss hit"
    if exit_price >= tp_price - tp_tol:
        return "🟢 Target hit"
    return "🔒 EOD close"


def _send_exit_telegram(today_trade: dict, sells: list):
    """
    Build and send exit Telegram notification.
    Called when position is already closed (intraday SL/TP fired) OR after EOD squareoff.
    Branches on today_trade.get("strategy"):
      - "bear_call_credit" / "bull_put_credit" → spread schema (short/long strikes, net_credit)
      - else → legacy naked-option schema (single strike, oracle_premium, sl_price/tp_price)
    Naked path is fallback — not reachable while auto_trader.py keeps CREDIT_SPREAD_MODE=True.
    """
    strategy = today_trade.get("strategy", "")
    today_label = datetime.now(_IST).date().strftime("%d %b %Y")

    # ── NF Iron Condor path ──────────────────────────────────────────────────
    if strategy == "nf_iron_condor":
        if today_trade.get("exit_done"):
            notify.log("IC exit already handled by spread_monitor.py — skipping duplicate.")
            return
        net_credit  = float(today_trade.get("net_credit", 0))
        lots        = int(today_trade.get("lots", 0))
        lot_size    = int(today_trade.get("lot_size", 65))
        pnl_inr     = float(today_trade.get("pnl_inr", 0))
        exit_spread = float(today_trade.get("exit_spread", 0))
        exit_time   = today_trade.get("exit_time", "")
        paper       = today_trade.get("order_mode") == "PAPER"
        mode_tag    = "[PAPER] " if paper else ""
        pnl_sign    = "+" if pnl_inr >= 0 else ""
        atm         = int(today_trade.get("atm_strike", 0))
        sw          = int(today_trade.get("spread_width", 150))
        lines = [
            f"⏹  <b>{mode_tag}Nifty IC Closed  ·  {today_label}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"Strategy    Iron Condor",
            f"Wings       ATM ± {sw}pt  (ATM={atm})",
            f"Lots        {lots}  ·  {lots * lot_size} shares",
            "",
            f"Entry credit  ₹{net_credit:.0f} / share",
        ]
        if exit_spread > 0:
            lines.append(f"Exit cost     ₹{exit_spread:.0f} / share")
        if exit_time:
            lines.append(f"Exit time     {exit_time} IST")
        lines += [
            "",
            f"<b>P&amp;L  {pnl_sign}₹{pnl_inr:,.0f}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "<i>All 4 IC legs squared off at 3:15 PM — no SL/TP triggered.</i>",
        ]
        notify.send("\n".join(lines))
        return

    # ── Credit spread path ────────────────────────────────────────────────────
    if strategy in ("bear_call_credit", "bull_put_credit"):
        if today_trade.get("exit_done"):
            # spread_monitor.py already sent the exit Telegram — no duplicate
            notify.log("Spread exit already handled by spread_monitor.py — skipping duplicate.")
            return
        # Spread closed by EOD squareoff (spread_monitor didn't trigger)
        strategy_name = ("Bear Call Spread" if strategy == "bear_call_credit"
                         else "Bull Put Spread")
        short_strike = float(today_trade.get("short_strike", 0))
        long_strike  = float(today_trade.get("long_strike", 0))
        net_credit   = float(today_trade.get("net_credit", 0))
        lots         = int(today_trade.get("lots", 0))
        lot_size     = int(today_trade.get("lot_size", 30))
        pnl_inr      = float(today_trade.get("pnl_inr", 0))
        exit_spread  = float(today_trade.get("exit_spread", 0))
        exit_time    = today_trade.get("exit_time", "")
        paper        = today_trade.get("order_mode") == "PAPER"
        mode_tag     = "[PAPER] " if paper else ""
        pnl_sign     = "+" if pnl_inr >= 0 else ""
        opt_type     = "CE" if today_trade.get("signal") == "CALL" else "PE"
        lines = [
            f"⏹  <b>{mode_tag}Spread Closed  ·  {today_label}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"Strategy    {strategy_name}",
            f"Legs        SELL {int(short_strike)} {opt_type} / BUY {int(long_strike)} {opt_type}",
            f"Lots        {lots}  ·  {lots * lot_size} shares",
            "",
            f"Entry credit  ₹{net_credit:.0f} / share",
        ]
        if exit_spread > 0:
            lines.append(f"Exit cost    ₹{exit_spread:.0f} / share")
        if exit_time:
            lines.append(f"Exit time    {exit_time} IST")
        lines += [
            "",
            f"<b>P&amp;L  {pnl_sign}₹{pnl_inr:,.0f}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "<i>Both legs squared off at 3:15 PM — no SL/TP triggered during session.</i>",
        ]
        notify.send("\n".join(lines))
        return

    # ── Short Straddle path ───────────────────────────────────────────────────
    if strategy == "nf_short_straddle":
        if today_trade.get("exit_done"):
            notify.log("Straddle exit already handled by spread_monitor.py — skipping duplicate.")
            return
        atm_strike = float(today_trade.get("atm_strike", 0))
        net_credit = float(today_trade.get("net_credit", 0))
        lots       = int(today_trade.get("lots", 0))
        lot_size   = int(today_trade.get("lot_size", 65))
        pnl_inr    = float(today_trade.get("pnl_inr", 0))
        exit_cost  = float(today_trade.get("exit_cost", 0))
        exit_time  = today_trade.get("exit_time", "")
        paper      = today_trade.get("order_mode") == "PAPER"
        mode_tag   = "[PAPER] " if paper else ""
        pnl_sign   = "+" if pnl_inr >= 0 else ""
        lines = [
            f"⏹  <b>{mode_tag}Straddle Closed  ·  {today_label}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"Strategy    Short Straddle",
            f"Legs        SELL {int(atm_strike)} CE + SELL {int(atm_strike)} PE",
            f"Lots        {lots}  ·  {lots * lot_size} shares",
            "",
            f"Entry credit  ₹{net_credit:.0f} / share",
        ]
        if exit_cost > 0:
            lines.append(f"Buyback cost  ₹{exit_cost:.0f} / share")
        if exit_time:
            lines.append(f"Exit time     {exit_time} IST")
        lines += [
            "",
            f"<b>P&L  {pnl_sign}₹{pnl_inr:,.0f}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "<i>Both short legs bought back — straddle closed at 3:15 PM EOD.</i>",
        ]
        notify.send("\n".join(lines))
        return

    # ── Naked option path ─────────────────────────────────────────────────────
    signal   = today_trade.get("signal", "?")
    strike   = today_trade.get("strike", 0)
    lots     = today_trade.get("lots", 0)
    entry    = float(today_trade.get("oracle_premium", 0))
    sl_price = float(today_trade.get("sl_price", 0))
    tp_price = float(today_trade.get("tp_price", 0))
    expiry   = today_trade.get("expiry", "?")

    lot_size = 65  # Nifty50 Jan 2026+

    if sells:
        # Weighted average exit price across fills
        total_qty = sum(int(t.get("tradedQuantity", 0)) for t in sells)
        total_val = sum(float(t.get("tradedPrice", 0)) * int(t.get("tradedQuantity", 0))
                        for t in sells)
        exit_price = total_val / total_qty if total_qty else 0.0
        exit_time  = sells[-1].get("exchangeTime", "")[:16] if sells else ""
    else:
        exit_price = 0.0
        exit_time  = ""

    pnl_per_lot = (exit_price - entry) * lot_size
    total_pnl   = pnl_per_lot * lots
    pnl_pct     = ((exit_price - entry) / entry * 100) if entry else 0.0
    reason      = _classify_exit(exit_price, sl_price, tp_price) if exit_price else "🔒 EOD close"

    sign_pnl = "+" if total_pnl >= 0 else ""
    sign_pct = "+" if pnl_pct >= 0 else ""

    lines = [
        f"📤 <b>Trade Closed  ·  {today_label}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Signal      {signal}  ·  {strike:.0f} strike",
        f"Expiry      {expiry}",
        f"Lots        {lots}  ({lots * lot_size} qty)",
        "",
        f"Entry       ₹{entry:.2f}",
    ]
    if exit_price:
        lines += [
            f"Exit        ₹{exit_price:.2f}",
        ]
        if exit_time:
            lines.append(f"Exit time   {exit_time} IST")
    lines += [
        f"SL / TP     ₹{sl_price:.2f} / ₹{tp_price:.2f}",
        "",
        f"P&amp;L         <b>{sign_pnl}₹{total_pnl:,.0f}  ({sign_pct}{pnl_pct:.1f}%)</b>",
        f"Reason      {reason}",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    notify.send("\n".join(lines))


def get_open_positions():
    """
    Return all open NSE_FNO positions (netQty != 0) for the active strategy.
    Reads today_trade.json to determine instrument (NF IC vs BNF).
    Falls back to fetching ALL NSE_FNO positions if strategy is unknown.
    """
    try:
        resp = requests.get("https://api.dhan.co/v2/positions",
                            headers=HEADERS, timeout=10)
    except Exception as e:
        notify.send(f"⚠️ <b>Exit Script</b>\nCannot reach Dhan API: {e}")
        return []

    if resp.status_code == 401:
        notify.send("⚠️ <b>Exit Script</b>\nToken expired (401) — regenerate at dhan.co.")
        return []
    if resp.status_code != 200:
        notify.send(f"⚠️ <b>Exit Script</b>\n"
                    f"Positions API {resp.status_code}: {resp.text[:120]}")
        return []

    data      = resp.json()
    positions = data if isinstance(data, list) else data.get("data", [])

    # Determine which instrument to filter for
    today_trade = _load_today_trade()
    strategy    = today_trade.get("strategy", "")

    if strategy in ("nf_iron_condor", "nf_short_straddle", "bear_call_credit", "bull_put_credit"):
        # NF strategies: symbol contains "NIFTY" but NOT "BANKNIFTY"
        return [
            p for p in positions
            if int(p.get("netQty", 0)) != 0
            and p.get("exchangeSegment", "") == "NSE_FNO"
            and "NIFTY" in str(p.get("tradingSymbol", "")).upper()
            and "BANKNIFTY" not in str(p.get("tradingSymbol", "")).upper()
        ]
    else:
        return [
            p for p in positions
            if int(p.get("netQty", 0)) != 0
            and p.get("exchangeSegment", "") == "NSE_FNO"
            and "BANKNIFTY" in str(
                p.get("tradingSymbol", p.get("securityId", ""))
            ).upper()
        ]


def square_off(pos) -> dict:
    """
    Close one open position with a MARKET order.
    netQty > 0 (long leg)  → SELL to close
    netQty < 0 (short leg) → BUY  to cover (critical for spread short legs)
    Returns Dhan API response.
    """
    security_id  = str(pos.get("securityId", pos.get("security_id", "")))
    net_qty      = int(pos.get("netQty", 0))
    product_type = pos.get("productType", "MARGIN")

    if net_qty == 0:
        return {"status": "FLAT", "securityId": security_id}

    txn_type = "SELL" if net_qty > 0 else "BUY"
    abs_qty  = abs(net_qty)

    if DRY_RUN:
        return {"status": "DRY_RUN", "securityId": security_id,
                "qty": abs_qty, "txn": txn_type}

    payload = {
        "dhanClientId":      CLIENT_ID,
        "correlationId":     f"eod_{date.today().strftime('%Y%m%d')}_{security_id[-6:]}",
        "transactionType":   txn_type,
        "exchangeSegment":   "NSE_FNO",
        "productType":       product_type,
        "orderType":         "MARKET",
        "validity":          "DAY",
        "securityId":        security_id,
        "quantity":          abs_qty,
        "price":             0,
        "triggerPrice":      0,
        "disclosedQuantity": 0,
    }

    try:
        resp = requests.post("https://api.dhan.co/v2/orders",
                             headers=HEADERS, json=payload, timeout=15)
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


_EXIT_WINDOW_END_MINS   = 3 * 60 + 20   # 3:20 PM IST hard deadline
_RETRY_INTERVAL_SECS    = 60            # retry every 60 s within the window


def _exit_all_positions_api() -> bool:
    """
    Primary exit: DELETE /v2/positions — one Dhan call closes all open positions.
    Returns True on SUCCESS; False means fall back to _squareoff_all().
    Always returns False in DRY_RUN so dry-run logging stays in _squareoff_all().
    """
    if DRY_RUN:
        return False
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


def _positions_to_results(positions: list) -> list:
    """Build the result-dict list for Telegram when DELETE API handled the exit."""
    results = []
    for pos in positions:
        symbol  = str(pos.get("tradingSymbol", pos.get("securityId", "?")))
        net_qty = int(pos.get("netQty", 0))
        avg     = float(pos.get("costPrice",        pos.get("buyAvg",        0)))
        ltp     = float(pos.get("lastTradedPrice",  pos.get("ltp",           0)))
        pnl     = float(pos.get("unrealizedProfit", pos.get("unrealizedPnl", 0)))
        results.append({
            "symbol": symbol, "qty": net_qty, "avg": avg,
            "ltp": ltp, "pnl": pnl, "order_id": "EXIT_ALL_API", "error": None,
        })
    return results


def _ist_mins_now() -> int:
    """Current IST time in minutes since midnight."""
    now_ist = datetime.now(_IST)
    return now_ist.hour * 60 + now_ist.minute


def _squareoff_all(positions) -> list:
    """
    Square off every position — shorts (netQty < 0) first, longs (netQty > 0) second.
    Closing shorts first removes margin obligation before selling wings.
    Selling a long wing while short is still open = naked short = margin spike.
    """
    # Sort: netQty < 0 (short, BUY to cover) → netQty > 0 (long, SELL to close)
    positions = sorted(positions, key=lambda p: (0 if int(p.get("netQty", 0)) < 0 else 1))
    results = []
    for pos in positions:
        symbol  = str(pos.get("tradingSymbol", pos.get("securityId", "?")))
        net_qty = int(pos.get("netQty", 0))
        avg     = float(pos.get("costPrice",          pos.get("buyAvg",        0)))
        ltp     = float(pos.get("lastTradedPrice",    pos.get("ltp",           0)))
        pnl     = float(pos.get("unrealizedProfit",   pos.get("unrealizedPnl", 0)))

        result   = square_off(pos)
        order_id = (result.get("orderId") or result.get("order_id")
                    or result.get("status", "?"))
        error    = result.get("error") or result.get("errorMessage")

        results.append({
            "symbol":   symbol,
            "qty":      net_qty,
            "avg":      avg,
            "ltp":      ltp,
            "pnl":      pnl,
            "order_id": order_id,
            "error":    error,
        })
        if error:
            notify.log(f"Square-off FAILED {symbol}: {error}")
        else:
            notify.log(f"Square-off sent {symbol} x{net_qty} → order {order_id}")
    return results


def _build_eod_telegram(today_trade, results, exit_time_str):
    mode       = "  [DRY RUN]" if DRY_RUN else ""
    total_pnl  = sum(r["pnl"] for r in results)
    sign       = "+" if total_pnl >= 0 else ""

    lines = [
        f"📤 <b>Trade Closed — EOD{mode}  ·  {date.today().strftime('%d %b %Y')}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Closed at {exit_time_str} IST  ·  🔒 EOD close",
        "",
    ]
    if today_trade:
        td_strategy = today_trade.get("strategy", "")
        if td_strategy == "nf_iron_condor":
            lines.append("Strategy    Nifty Iron Condor")
            lines.append(
                f"Wings       ATM ± {int(today_trade.get('spread_width', 150))}pt  "
                f"(ATM={int(today_trade.get('atm_strike', 0))})"
            )
            lines.append(f"Entry credit  ₹{float(today_trade.get('net_credit', 0)):.0f} / share")
        elif td_strategy in ("bear_call_credit", "bull_put_credit"):
            td_name = ("Bear Call Spread" if td_strategy == "bear_call_credit"
                       else "Bull Put Spread")
            td_opt  = "CE" if today_trade.get("signal") == "CALL" else "PE"
            lines.append(f"Strategy    {td_name}")
            lines.append(
                f"Legs        SELL {int(today_trade.get('short_strike', 0))} {td_opt} / "
                f"BUY {int(today_trade.get('long_strike', 0))} {td_opt}"
            )
            lines.append(f"Entry credit  ₹{float(today_trade.get('net_credit', 0)):.0f} / share")
        elif td_strategy == "nf_short_straddle":
            atm = int(today_trade.get("atm_strike", 0))
            lines.append("Strategy    Short Straddle")
            lines.append(f"Legs        SELL {atm} CE + SELL {atm} PE")
            lines.append(f"Entry credit  ₹{float(today_trade.get('net_credit', 0)):.0f} / share")
        else:
            lines.append(
                f"Signal      {today_trade.get('signal','?')}  ·  "
                f"{today_trade.get('strike',0):.0f} strike  ·  "
                f"expiry {today_trade.get('expiry','?')}"
            )
            lines.append(
                f"SL / TP     ₹{today_trade.get('sl_price',0):.2f} / "
                f"₹{today_trade.get('tp_price',0):.2f}"
            )
        lines.append("")

    for r in results:
        ok = "✓" if not r["error"] else "✗"
        ps = "+" if r["pnl"] >= 0 else ""
        lines.append(
            f"{ok}  <b>{r['symbol']}</b>  x{r['qty']}\n"
            f"   Entry ₹{r['avg']:.0f}  ·  LTP ₹{r['ltp']:.0f}  "
            f"·  P&amp;L {ps}₹{r['pnl']:,.0f}"
        )
        if r["error"]:
            lines.append(f"   ⚠️ {r['error']}")

    lines += [
        "",
        f"Total P&amp;L: <b>{sign}₹{total_pnl:,.0f}</b>",
        "<i>Position closed to prevent overnight carry on NRML.</i>",
    ]
    return "\n".join(lines)


def main():
    today_label = datetime.now(_IST).date().strftime("%d %b %Y")
    notify.log(f"EOD exit check — {today_label}")

    # Holiday guard
    if not DRY_RUN and not _is_trading_day():
        notify.log(f"Market holiday ({today_label}) — skipping squareoff.")
        return

    positions = get_open_positions()
    instr_label = "Nifty IC" if _load_today_trade().get("strategy") == "nf_iron_condor" else "Nifty50"

    if not positions:
        notify.log(f"No open {instr_label} positions — SL/TP already hit.")
        today_trade = _load_today_trade()
        if today_trade:
            sells = [] if DRY_RUN else _get_today_nf_sells()
            _send_exit_telegram(today_trade, sells)
        _write_exit_marker()
        return

    today_trade = _load_today_trade()
    final_results = []
    attempt = 0

    # ── Retry loop: pulse every 60 s from 3:10 to 3:20 PM IST ────────────────
    # Primary path: DELETE /v2/positions (one Dhan call, fastest).
    # Backup path:  _squareoff_all() leg-by-leg (also handles DRY_RUN logging).
    while True:
        attempt += 1
        notify.log(f"Exit attempt #{attempt}: {len(positions)} position(s) remaining.")

        # Primary: EXIT ALL API
        if _exit_all_positions_api():
            if not DRY_RUN:
                time.sleep(15)
            remaining = get_open_positions()
            if len(remaining) == 0:
                notify.log(f"All positions closed via EXIT ALL API on attempt #{attempt}.")
                final_results.extend(_positions_to_results(positions))
                break
            notify.log(
                f"EXIT ALL API returned SUCCESS but {len(remaining)} position(s) still open "
                f"— falling back to leg-by-leg.")
            positions = remaining

        # Backup (or DRY_RUN): leg-by-leg squareoff
        results = _squareoff_all(positions)
        final_results.extend(results)

        if not DRY_RUN:
            time.sleep(15)
        remaining = get_open_positions()
        still_open = len(remaining)

        if still_open == 0:
            notify.log(f"All positions closed on attempt #{attempt}.")
            break

        now_mins = _ist_mins_now()
        if now_mins >= _EXIT_WINDOW_END_MINS:
            # Hard deadline reached — alert and break
            sym_list = ", ".join(
                str(p.get("tradingSymbol", p.get("securityId", "?")))
                for p in remaining
            )
            notify.send(
                f"🚨 <b>EXIT DEADLINE BREACH — {today_label}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"3:20 PM IST passed. {still_open} position(s) still open:\n"
                f"{sym_list}\n\n"
                f"<b>Manual action required immediately.</b>\n"
                f"Log in to Dhan and close these positions NOW."
            )
            notify.log(f"EXIT DEADLINE BREACH: {still_open} positions unclosed at 3:20 PM.")
            _write_exit_marker()
            return

        wait_secs = _RETRY_INTERVAL_SECS
        notify.log(
            f"{still_open} position(s) still open after attempt #{attempt}. "
            f"Retrying in {wait_secs}s (deadline 3:20 PM IST)."
        )
        if not DRY_RUN:
            time.sleep(wait_secs)
        positions = remaining

    # All closed — build and send summary
    now_ist = datetime.now(_IST)
    exit_time_str = now_ist.strftime("%H:%M")
    msg = _build_eod_telegram(today_trade, final_results, exit_time_str)
    notify.send(msg)
    _write_exit_marker()


if __name__ == "__main__":
    main()
