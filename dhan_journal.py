#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
dhan_journal.py — Dhan API as the single source of truth for trade outcomes.

Replaces today_trade.json fragility. Trade journal + backfill both use this module.

Endpoints used:
  GET /v2/positions               — today's CLOSED positions, with realizedProfit per leg
  GET /v2/trades/{from}/{to}/0    — historical trade history (paginated, NF F&O fills)

The realizedProfit field on /v2/positions is Dhan's own booked P&L per leg
(after charges). For today's journal we sum these — no computation, no proxy.

For historical backfill (past dates) we use /v2/trades for raw fills,
group by securityId, pair the BUY+SELL, and net out the explicit charges
fields the API returns (brokerage, stt, sebiTax, exchangeTransactionCharges,
serviceTax, stampDuty). All numbers come from Dhan, none are estimated.
"""
import os
import time
import requests
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

_TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
_CLIENT_ID = os.getenv("DHAN_CLIENT_ID",    "")
_HEADERS   = {
    "access-token": _TOKEN,
    "client-id":    _CLIENT_ID,
    "Content-Type": "application/json",
    "Accept":       "application/json",
}

_BASE = "https://api.dhan.co/v2"


# ─── /v2/positions — today's positions (live journal) ────────────────────────

def get_positions() -> list[dict]:
    """Today's positions (LONG / SHORT / CLOSED). After EOD squareoff every
    leg is CLOSED with realizedProfit = booked P&L per leg.

    Returns full list (caller filters by NF F&O / segment).
    Empty list on any error so callers can fall back gracefully.
    """
    try:
        r = requests.get(f"{_BASE}/positions", headers=_HEADERS, timeout=10)
    except Exception as e:
        print(f"[dhan_journal] /v2/positions unreachable: {e}")
        return []
    if r.status_code != 200:
        print(f"[dhan_journal] /v2/positions HTTP {r.status_code}: {r.text[:120]}")
        return []
    data = r.json()
    return data if isinstance(data, list) else data.get("data", [])


def positions_by_sid(positions: list[dict]) -> dict[str, dict]:
    """Index positions by securityId for O(1) lookup."""
    return {str(p.get("securityId", "")): p for p in positions if p.get("securityId")}


def realized_pnl(positions: list[dict], sids: list[str]) -> float:
    """Sum realizedProfit (Dhan's booked P&L) across the listed leg SIDs."""
    by_sid = positions_by_sid(positions)
    total = 0.0
    for sid in sids:
        pos = by_sid.get(str(sid))
        if pos:
            total += float(pos.get("realizedProfit", 0) or 0)
    return round(total, 2)


def leg_avgs(positions: list[dict], sid: str) -> dict:
    """Per-leg entry, exit, P&L from /v2/positions for one securityId.

    Returns dict with: buy_avg, sell_avg, day_buy_qty, day_sell_qty,
    realized, unrealized, position_type, drv_strike, drv_option_type.
    Empty dict if SID not found.
    """
    by_sid = positions_by_sid(positions)
    p = by_sid.get(str(sid))
    if not p:
        return {}
    return {
        "buy_avg":         float(p.get("buyAvg",          0) or 0),
        "sell_avg":        float(p.get("sellAvg",         0) or 0),
        "day_buy_qty":     int(p.get("dayBuyQty",         0) or 0),
        "day_sell_qty":    int(p.get("daySellQty",        0) or 0),
        "day_buy_value":   float(p.get("dayBuyValue",     0) or 0),
        "day_sell_value":  float(p.get("daySellValue",    0) or 0),
        "realized":        float(p.get("realizedProfit",  0) or 0),
        "unrealized":      float(p.get("unrealizedProfit",0) or 0),
        "position_type":   p.get("positionType", ""),
        "drv_strike":      float(p.get("drvStrikePrice",  0) or 0),
        "drv_option_type": p.get("drvOptionType", ""),
    }


# ─── /v2/trades/{from}/{to}/{page} — historical fills (backfill) ─────────────

_CHARGE_FIELDS = (
    "brokerageCharges", "stt", "sebiTax",
    "exchangeTransactionCharges", "serviceTax", "stampDuty",
)


def _to_float(v) -> float:
    try:
        return float(v) if v not in (None, "", "NA") else 0.0
    except (ValueError, TypeError):
        return 0.0


def fetch_trade_history(from_date: str, to_date: str) -> list[dict]:
    """Historical trade fills for a date range (YYYY-MM-DD).

    Walks paginated endpoint until empty page. Returns combined list of trades
    (each trade = one fill with transactionType BUY/SELL, tradedPrice, qty,
    plus full charges breakdown).
    """
    all_trades: list[dict] = []
    page = 0
    while True:
        url = f"{_BASE}/trades/{from_date}/{to_date}/{page}"
        try:
            r = requests.get(url, headers=_HEADERS, timeout=15)
        except Exception as e:
            print(f"[dhan_journal] history page {page} unreachable: {e}")
            break
        if r.status_code != 200:
            print(f"[dhan_journal] history page {page} HTTP {r.status_code}: {r.text[:120]}")
            break
        data = r.json()
        trades = data if isinstance(data, list) else data.get("data", [])
        if not trades:
            break
        all_trades.extend(trades)
        page += 1
        time.sleep(0.4)  # 2 req/s pacing for Data API rate limit (10 req/s ceiling)
        if page > 50:    # safety cap (50 pages × ~50 trades = 2500 fills)
            break
    return all_trades


def filter_nf_options(trades: list[dict]) -> list[dict]:
    """Keep only NSE F&O option fills with NIFTY underlying."""
    out = []
    for t in trades:
        sym = str(t.get("tradingSymbol") or t.get("customSymbol") or "").upper()
        seg = str(t.get("exchangeSegment", "")).upper()
        if seg == "NSE_FNO" and "NIFTY" in sym:
            out.append(t)
    return out


def trades_by_sid(trades: list[dict]) -> dict[str, list[dict]]:
    """Group fills by securityId."""
    out = defaultdict(list)
    for t in trades:
        sid = str(t.get("securityId", ""))
        if sid:
            out[sid].append(t)
    return dict(out)


def leg_pnl_from_fills(fills: list[dict]) -> dict:
    """Compute one leg's net P&L from its BUY + SELL fills.

    Net = Σ(sell_price × qty) − Σ(buy_price × qty) − Σ(charges).
    Charges are Dhan-reported per-fill numbers, not estimates.

    Returns dict with: buy_avg, sell_avg, qty, gross_pnl, charges, net_pnl,
    sell_time (latest), buy_time (latest).
    """
    buys  = [f for f in fills if str(f.get("transactionType", "")).upper() == "BUY"]
    sells = [f for f in fills if str(f.get("transactionType", "")).upper() == "SELL"]

    def _wavg(fs):
        total_qty = sum(_to_float(f.get("tradedQuantity")) for f in fs)
        if total_qty == 0:
            return 0.0, 0
        px = sum(_to_float(f.get("tradedPrice")) * _to_float(f.get("tradedQuantity")) for f in fs)
        return round(px / total_qty, 2), int(total_qty)

    buy_avg,  buy_qty  = _wavg(buys)
    sell_avg, sell_qty = _wavg(sells)
    qty = min(buy_qty, sell_qty) if buy_qty and sell_qty else max(buy_qty, sell_qty)

    gross_pnl = (sell_avg * sell_qty) - (buy_avg * buy_qty)
    charges   = sum(_to_float(f.get(field)) for f in fills for field in _CHARGE_FIELDS)
    net_pnl   = round(gross_pnl - charges, 2)

    def _latest_time(fs, key="exchangeTime"):
        times = [f.get(key) or f.get("updateTime") or f.get("createTime") or "" for f in fs]
        times = [t for t in times if t and t != "NA"]
        return max(times) if times else ""

    return {
        "buy_avg":   buy_avg,
        "sell_avg":  sell_avg,
        "buy_qty":   buy_qty,
        "sell_qty":  sell_qty,
        "qty":       qty,
        "gross_pnl": round(gross_pnl, 2),
        "charges":   round(charges, 2),
        "net_pnl":   net_pnl,
        "buy_time":  _latest_time(buys),
        "sell_time": _latest_time(sells),
    }


def trade_pnl_for_date(date_str: str, sids: list[str] | None = None) -> dict:
    """Top-level helper: total NF options P&L for a date, optionally filtered to SIDs.

    Returns dict with:
      total_net_pnl: float
      total_charges: float
      legs: dict[sid] -> leg_pnl_from_fills() output
      n_fills: int
    """
    raw = fetch_trade_history(date_str, date_str)
    nf  = filter_nf_options(raw)
    grouped = trades_by_sid(nf)
    if sids:
        sids_set = {str(s) for s in sids}
        grouped = {k: v for k, v in grouped.items() if k in sids_set}

    legs: dict[str, dict] = {}
    total_net = 0.0
    total_charges = 0.0
    for sid, fills in grouped.items():
        leg = leg_pnl_from_fills(fills)
        legs[sid] = leg
        total_net     += leg["net_pnl"]
        total_charges += leg["charges"]

    return {
        "date":          date_str,
        "total_net_pnl": round(total_net, 2),
        "total_charges": round(total_charges, 2),
        "legs":          legs,
        "n_fills":       sum(len(v) for v in grouped.values()),
    }


# ─── self-test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--positions":
        ps = get_positions()
        print(f"{len(ps)} positions today.")
        for p in ps:
            print(f"  {p.get('tradingSymbol')} sid={p.get('securityId')} "
                  f"strike={p.get('drvStrikePrice')} {p.get('drvOptionType')} "
                  f"type={p.get('positionType')} realized={p.get('realizedProfit')} "
                  f"buy={p.get('buyAvg')} sell={p.get('sellAvg')}")
    elif len(sys.argv) > 1 and sys.argv[1] == "--history":
        d = sys.argv[2] if len(sys.argv) > 2 else None
        if not d:
            print("Usage: python3 dhan_journal.py --history YYYY-MM-DD")
            sys.exit(1)
        result = trade_pnl_for_date(d)
        print(f"Date: {result['date']}  fills: {result['n_fills']}  "
              f"net P&L: ₹{result['total_net_pnl']:,.2f}  "
              f"charges: ₹{result['total_charges']:,.2f}")
        for sid, leg in result["legs"].items():
            print(f"  sid={sid:>10s}  buy={leg['buy_avg']:>8.2f}  "
                  f"sell={leg['sell_avg']:>8.2f}  qty={leg['qty']:>4d}  "
                  f"net=₹{leg['net_pnl']:>10,.2f}")
    else:
        print("Usage: python3 dhan_journal.py --positions")
        print("       python3 dhan_journal.py --history YYYY-MM-DD")
