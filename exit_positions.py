#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
exit_positions.py — EOD position squareoff at 3:15 PM IST
==========================================================
Closes any open BankNifty F&O positions via Dhan MARKET SELL.
MARGIN (NRML) positions do NOT auto-square off — this script does it.

If SL or TP already fired, netQty will be 0 → nothing to do.
If neither fired by 3:15 PM → squareoff at market price.

Cron (3:15 PM IST = 9:45 AM UTC):
  45 9 * * 1-5 cd ~/dhan-trading && python3 exit_positions.py >> logs/exit.log 2>&1
"""
import json
import os
import sys
import requests
import pandas as pd
from datetime import date, datetime
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
        marker = os.path.join(DATA_DIR, f"exit_completed_{date.today().isoformat()}.marker")
        with open(marker, "w") as f:
            f.write(f"exit_positions.py completed at "
                    f"{datetime.now().strftime('%H:%M:%S IST')}\n")
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
    today = date.today()
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
        if td.get("date") != date.today().isoformat():
            return {}
        return td
    except Exception:
        return {}


def _get_today_bn_sells() -> list:
    """
    Fetch today's tradebook from Dhan and return SELL fills for today's BN option.
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
    Build and send exit Telegram notification matching the entry alert format.
    Called when position is already closed (intraday SL/TP) OR after EOD squareoff.
    `sells` is a list of SELL fill dicts from the tradebook.
    """
    signal   = today_trade.get("signal", "?")
    strike   = today_trade.get("strike", 0)
    lots     = today_trade.get("lots", 0)
    entry    = float(today_trade.get("oracle_premium", 0))
    sl_price = float(today_trade.get("sl_price", 0))
    tp_price = float(today_trade.get("tp_price", 0))
    expiry   = today_trade.get("expiry", "?")

    lot_size = 30  # BankNifty Jan 2026+

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
        f"📤 <b>Trade Closed  ·  {date.today().strftime('%d %b %Y')}</b>",
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


def get_open_bn_positions():
    """Return list of open BankNifty F&O positions (netQty > 0) from Dhan."""
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

    return [
        p for p in positions
        if int(p.get("netQty", 0)) > 0
        and p.get("exchangeSegment", "") == "NSE_FNO"
        and "BANKNIFTY" in str(
            p.get("tradingSymbol", p.get("securityId", ""))
        ).upper()
    ]


def square_off(pos) -> dict:
    """Place a MARKET SELL to close one open position. Returns API response."""
    security_id  = str(pos.get("securityId", pos.get("security_id", "")))
    net_qty      = int(pos.get("netQty", 0))
    product_type = pos.get("productType", "MARGIN")

    if DRY_RUN:
        return {"status": "DRY_RUN", "securityId": security_id, "qty": net_qty}

    payload = {
        "dhanClientId":      CLIENT_ID,
        "correlationId":     f"eod_{date.today().strftime('%Y%m%d')}_{security_id[-6:]}",
        "transactionType":   "SELL",
        "exchangeSegment":   "NSE_FNO",
        "productType":       product_type,
        "orderType":         "MARKET",
        "validity":          "DAY",
        "securityId":        security_id,
        "quantity":          net_qty,
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


def main():
    today_label = date.today().strftime("%d %b %Y")
    notify.log(f"EOD exit check — {today_label} 3:15 PM IST")

    # Holiday guard — banknifty.csv has no row for today → NSE is closed
    if not DRY_RUN and not _is_trading_day():
        notify.log(f"Market holiday ({today_label}) — skipping squareoff.")
        return

    positions = get_open_bn_positions()
    if not positions:
        notify.log("No open BankNifty positions — nothing to square off. SL/TP already hit.")
        # Send exit Telegram if we placed a trade today (SL or TP fired intraday)
        today_trade = _load_today_trade()
        if today_trade:
            sells = [] if DRY_RUN else _get_today_bn_sells()
            _send_exit_telegram(today_trade, sells)
        _write_exit_marker()
        return

    mode        = "  [DRY RUN]" if DRY_RUN else ""
    today_trade = _load_today_trade()
    results     = []

    for pos in positions:
        symbol   = str(pos.get("tradingSymbol", pos.get("securityId", "?")))
        net_qty  = int(pos.get("netQty", 0))
        avg      = float(pos.get("costPrice",   pos.get("buyAvg",         0)))
        ltp      = float(pos.get("lastTradedPrice", pos.get("ltp",        0)))
        pnl      = float(pos.get("unrealizedProfit", pos.get("unrealizedPnl", 0)))

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

    total_pnl = sum(r["pnl"] for r in results)
    sign      = "+" if total_pnl >= 0 else ""

    lines = [
        f"📤 <b>Trade Closed — EOD{mode}  ·  {date.today().strftime('%d %b %Y')}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Closed at 3:15 PM IST  ·  🔒 EOD close",
        "",
    ]
    if today_trade:
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
    notify.send("\n".join(lines))
    _write_exit_marker()


if __name__ == "__main__":
    main()
