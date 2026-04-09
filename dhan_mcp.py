#!/usr/bin/env python3
"""
dhan_mcp.py — Dhan Trading MCP Server
======================================
Exposes Dhan account data as MCP tools for Claude Code.
Ask Claude: "Show my positions", "What's today's P&L?", "Did my SL trigger?"

Register in ~/.claude/settings.json:
  {
    "mcpServers": {
      "dhan": {
        "command": "python3",
        "args": ["/home/user/dhan-trading/dhan_mcp.py"]
      }
    }
  }

Then restart Claude Code — the tools will be available in your next session.
"""

import os
import requests
from datetime import date
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()
TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID",    "")

HEADERS = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

mcp = FastMCP("Dhan Trading")


def _get(endpoint: str) -> dict:
    """GET request to Dhan API v2."""
    resp = requests.get(f"https://api.dhan.co/v2/{endpoint}",
                        headers=HEADERS, timeout=10)
    if resp.status_code == 401:
        return {"error": "Token expired. Regenerate at dhan.co → API settings."}
    if resp.status_code != 200:
        return {"error": f"API returned {resp.status_code}: {resp.text[:200]}"}
    return resp.json()


def _fmt_inr(val) -> str:
    try:
        v = float(val)
        sign = "+" if v > 0 else ""
        return f"{sign}₹{v:,.2f}"
    except Exception:
        return str(val)


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_positions() -> str:
    """
    Get all open intraday and overnight F&O positions with live unrealized P&L.
    Use this to check if a trade is currently open, and how it's performing.
    """
    data = _get("positions")
    if "error" in data:
        return data["error"]

    positions = data if isinstance(data, list) else data.get("data", [])
    if not positions:
        return "No open positions right now."

    lines = [f"Open positions as of {date.today().strftime('%d %b %Y')}:\n"]
    total_pnl = 0.0

    for p in positions:
        sym      = p.get("tradingSymbol", p.get("securityId", "?"))
        qty      = p.get("netQty",        p.get("buyQty", 0))
        avg      = p.get("costPrice",     p.get("buyAvg", 0))
        ltp      = p.get("lastTradedPrice", p.get("ltp", 0))
        pnl      = p.get("unrealizedProfit", p.get("unrealizedPnl", 0))
        prod     = p.get("productType", "")

        try:
            total_pnl += float(pnl)
        except Exception:
            pass

        lines.append(
            f"  {sym}\n"
            f"    Qty: {qty}  Avg: ₹{float(avg):.2f}  LTP: ₹{float(ltp):.2f}"
            f"  P&L: {_fmt_inr(pnl)}  [{prod}]"
        )

    lines.append(f"\nTotal unrealized P&L: {_fmt_inr(total_pnl)}")
    return "\n".join(lines)


@mcp.tool()
def get_orders() -> str:
    """
    Get today's order book — all orders with status, fill price, and type.
    Use this to check if a BUY/SL/TP order was filled, pending, or rejected.
    """
    data = _get("orders")
    if "error" in data:
        return data["error"]

    orders = data if isinstance(data, list) else data.get("data", [])
    if not orders:
        return "No orders today."

    lines = [f"Orders for {date.today().strftime('%d %b %Y')}:\n"]
    for o in orders:
        sym    = o.get("tradingSymbol", o.get("securityId", "?"))
        side   = o.get("transactionType", "?")
        qty    = o.get("quantity", 0)
        otype  = o.get("orderType", "?")
        status = o.get("orderStatus", o.get("status", "?"))
        price  = o.get("price",        0)
        avg    = o.get("averageTradedPrice", o.get("filledQty", ""))
        oid    = o.get("orderId", "")

        lines.append(
            f"  [{status}] {side} {qty}× {sym}  {otype}  ₹{float(price):.0f}"
            + (f"  filled@₹{float(avg):.0f}" if avg else "")
            + f"  ({oid})"
        )

    return "\n".join(lines)


@mcp.tool()
def get_daily_pnl() -> str:
    """
    Get today's total realized P&L from the trade book.
    Shows per-trade breakdown and total net P&L for the day.
    """
    data = _get("tradeBook")
    if "error" in data:
        return data["error"]

    trades = data if isinstance(data, list) else data.get("data", [])
    today_str = date.today().isoformat()

    today_trades = [
        t for t in trades
        if str(t.get("exchangeTime", t.get("createTime", ""))).startswith(today_str[:10])
    ]

    if not today_trades:
        return f"No trades executed today ({date.today().strftime('%d %b %Y')})."

    lines = [f"Trades on {date.today().strftime('%d %b %Y')}:\n"]
    total_buy  = 0.0
    total_sell = 0.0

    for t in today_trades:
        sym   = t.get("tradingSymbol", "?")
        side  = t.get("transactionType", "?")
        qty   = t.get("tradedQuantity", t.get("quantity", 0))
        price = t.get("tradedPrice",    t.get("price", 0))
        val   = float(qty) * float(price)

        lines.append(f"  {side} {qty}× {sym} @ ₹{float(price):.2f}  = ₹{val:,.2f}")

        if side == "BUY":
            total_buy += val
        else:
            total_sell += val

    net = total_sell - total_buy
    lines.append(f"\nBuy value : ₹{total_buy:,.2f}")
    lines.append(f"Sell value: ₹{total_sell:,.2f}")
    lines.append(f"Net P&L   : {_fmt_inr(net)}  (approximate — excludes charges)")

    return "\n".join(lines)


@mcp.tool()
def get_funds() -> str:
    """
    Get available margin, used margin, and withdrawable balance from Dhan.
    Use this before trading to confirm sufficient funds are available.
    """
    data = _get("fundlimit")
    if "error" in data:
        return data["error"]

    available   = data.get("availabelBalance",   data.get("availableBalance",   0))
    sod         = data.get("sodLimit",            0)
    collateral  = data.get("collateralAmount",    0)
    used        = data.get("utilizedAmount",      0)
    withdrawable= data.get("withdrawableBalance", 0)

    return (
        f"Dhan Fund Summary — {date.today().strftime('%d %b %Y')}\n\n"
        f"Available balance  : ₹{float(available):>12,.2f}\n"
        f"SOD limit          : ₹{float(sod):>12,.2f}\n"
        f"Collateral         : ₹{float(collateral):>12,.2f}\n"
        f"Utilized margin    : ₹{float(used):>12,.2f}\n"
        f"Withdrawable       : ₹{float(withdrawable):>12,.2f}"
    )


@mcp.tool()
def get_trade_summary() -> str:
    """
    Full summary: today's trade outcome from auto_trader, current positions,
    and funds. One-stop overview of the day's activity.
    """
    funds     = get_funds()
    positions = get_positions()
    pnl       = get_daily_pnl()

    return f"{funds}\n\n{'─'*40}\n\n{positions}\n\n{'─'*40}\n\n{pnl}"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
