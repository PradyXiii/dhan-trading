#!/usr/bin/env python3
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
import os
import sys
import requests
from datetime import date
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
    notify.log(f"EOD exit check — {date.today().strftime('%d %b %Y')} 3:15 PM IST")

    positions = get_open_bn_positions()
    if not positions:
        notify.log("No open BankNifty positions — nothing to square off. SL/TP already hit.")
        return

    mode      = "  [DRY RUN]" if DRY_RUN else ""
    results   = []

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
        f"🔒  <b>EOD Squareoff{mode}</b>",
        "─────────────────────",
        f"{date.today().strftime('%d %b %Y')}  ·  3:15 PM IST",
        "",
    ]
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


if __name__ == "__main__":
    main()
