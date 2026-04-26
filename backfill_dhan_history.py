#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
backfill_dhan_history.py — Re-build CSV rows from Dhan historical tradebook.

Replaces the older one-shot backfill_open_trades.py. Difference:
old script used hand-typed P&L numbers; this one fetches every fill from
Dhan /v2/trades/{from-date}/{to-date}/{page} and reconstructs the row from
real BUY/SELL prices and real per-fill charges. No estimates.

Usage:
  python3 backfill_dhan_history.py --date 2026-04-22
  python3 backfill_dhan_history.py --date 2026-04-22 --strategy ic
  python3 backfill_dhan_history.py --range 2026-04-22:2026-04-24
  python3 backfill_dhan_history.py --range 2026-04-22:2026-04-24 --apply

Without --apply this is a dry run: prints what would be written, makes no
changes. With --apply the matching CSV row (live_ic_trades.csv,
live_spread_trades.csv, or live_straddle_trades.csv) is upserted in place
using the same _upsert_csv_row helper trade_journal.py uses.

Strategy detection:
  Scan the trade fills for the date. If 4 distinct NF option SIDs traded
  (2 CE + 2 PE) → IC. If 2 SIDs (1 short + 1 long, same option type) → spread.
  If 2 SIDs (1 CE + 1 PE) → straddle. If 1 SID → naked option.
  Override with --strategy {ic|spread|straddle|naked}.
"""
import argparse
import csv
import json
import os
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import dhan_journal
from trade_journal import (
    IC_CSV, SPREAD_CSV, STRADDLE_CSV,
    IC_FIELDS, SPREAD_FIELDS, STRADDLE_FIELDS,
    _upsert_csv_row,
)

_IST   = timezone(timedelta(hours=5, minutes=30))
_DATA  = Path(__file__).parent / "data"


# ─── helpers ─────────────────────────────────────────────────────────────────

def _detect_strategy(legs: dict) -> str:
    """Infer strategy from the set of leg fills.

    Each leg dict has 'fills' from Dhan; each fill has drvOptionType + drvStrikePrice.
    """
    n = len(legs)
    if n == 4:
        return "ic"
    if n == 2:
        # Same option type both legs (CE+CE or PE+PE) → vertical spread
        # One CE + one PE → straddle
        opts = set()
        for sid, leg in legs.items():
            sample = leg.get("fills", [{}])[0]
            opts.add(str(sample.get("drvOptionType", "")).upper())
        if opts == {"CE", "PE"} or opts == {"CALL", "PUT"}:
            return "straddle"
        return "spread"
    if n == 1:
        return "naked"
    return "unknown"


def _enrich_legs_with_fills(date_str: str, sids_or_none) -> dict:
    """Run trade_pnl_for_date, then re-attach raw fill list per leg for inspection."""
    raw = dhan_journal.fetch_trade_history(date_str, date_str)
    nf  = dhan_journal.filter_nf_options(raw)
    grouped = dhan_journal.trades_by_sid(nf)
    if sids_or_none:
        s = {str(x) for x in sids_or_none}
        grouped = {k: v for k, v in grouped.items() if k in s}

    legs = {}
    for sid, fills in grouped.items():
        leg = dhan_journal.leg_pnl_from_fills(fills)
        leg["fills"] = fills
        sample = fills[0]
        leg["drv_strike"]      = float(sample.get("drvStrikePrice", 0) or 0)
        leg["drv_option_type"] = str(sample.get("drvOptionType", "")).upper().replace("CALL", "CE").replace("PUT", "PE")
        leg["trading_symbol"]  = str(sample.get("tradingSymbol") or sample.get("customSymbol") or "")
        legs[sid] = leg
    return legs


def _signal_from_archive(date_str: str) -> dict:
    """Look for archived today_trade.json snapshot for this date.

    auto_trader.py overwrites today_trade.json daily, but if the user kept any
    backups (e.g. data/today_trade_2026-04-22.json) read those. Otherwise return {}.
    """
    candidates = [
        _DATA / f"today_trade_{date_str}.json",
        _DATA / "archive" / f"today_trade_{date_str}.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


# ─── per-strategy row builders ──────────────────────────────────────────────

def _build_ic_row(date_str: str, legs: dict, archive: dict) -> dict:
    """Build a live_ic_trades.csv row from 4 leg dicts.

    IC layout:
      SHORT CE = leg with positionType SHORT (sell_qty>0 first), CE option type
                 → highest strike if 2 CEs (it's the lower-strike short, ATM)
      LONG  CE = the other CE (higher strike, ATM+150)
      SHORT PE = PE leg with sell-first pattern (lower-strike short means ATM)

    Simpler heuristic: short leg = sell_avg > buy_avg context but actually we
    can read drv_strike. SHORT CE = lower-strike CE (ATM). LONG CE = higher-strike
    CE (ATM+150). SHORT PE = higher-strike PE (ATM). LONG PE = lower-strike PE
    (ATM-150).
    """
    ces = sorted(
        [(sid, l) for sid, l in legs.items() if l["drv_option_type"] == "CE"],
        key=lambda x: x[1]["drv_strike"],
    )
    pes = sorted(
        [(sid, l) for sid, l in legs.items() if l["drv_option_type"] == "PE"],
        key=lambda x: x[1]["drv_strike"],
    )
    if len(ces) != 2 or len(pes) != 2:
        raise ValueError(f"IC needs 2 CEs + 2 PEs, got {len(ces)} CEs + {len(pes)} PEs")

    ce_short_sid, ce_short = ces[0]   # lower-strike CE = SHORT (ATM)
    ce_long_sid,  ce_long  = ces[1]   # higher-strike CE = LONG (ATM+150)
    pe_short_sid, pe_short = pes[1]   # higher-strike PE = SHORT (ATM)
    pe_long_sid,  pe_long  = pes[0]   # lower-strike  PE = LONG (ATM-150)

    pnl_inr = sum(l["net_pnl"] for l in legs.values())
    ce_credit = ce_short["sell_avg"] - ce_long["buy_avg"]
    pe_credit = pe_short["sell_avg"] - pe_long["buy_avg"]
    net_credit = ce_credit + pe_credit
    qty   = ce_short.get("qty") or 65
    lots  = max(1, qty // 65)
    spread_width = abs(ce_long["drv_strike"] - ce_short["drv_strike"])

    # exit_time = latest sell across all legs (closing trades)
    exit_times = [l["sell_time"] for l in legs.values() if l["sell_time"]]
    exit_time  = max(exit_times)[-8:] if exit_times else ""

    pnl_pct = (pnl_inr / (net_credit * qty) * 100) if net_credit > 0 else 0

    return {
        "date":              date_str,
        "strategy":          "nf_iron_condor",
        "signal":            archive.get("signal", "CALL"),
        "ce_short_strike":   ce_short["drv_strike"],
        "ce_long_strike":    ce_long["drv_strike"],
        "pe_short_strike":   pe_short["drv_strike"],
        "pe_long_strike":    pe_long["drv_strike"],
        "spread_width":      spread_width,
        "lots":              lots,
        "lot_size":          65,
        "dte":               archive.get("dte", 0),
        "spot_at_signal":    archive.get("spot_at_signal", 0),
        "ce_short_entry":    ce_short["sell_avg"],
        "ce_long_entry":     ce_long["buy_avg"],
        "ce_net_credit":     round(ce_credit, 2),
        "pe_short_entry":    pe_short["sell_avg"],
        "pe_long_entry":     pe_long["buy_avg"],
        "pe_net_credit":     round(pe_credit, 2),
        "net_credit":        round(net_credit, 2),
        "exit_reason":       "EOD",
        "exit_time":         exit_time,
        "pnl_inr":           round(pnl_inr, 2),
        "pnl_pct_of_credit": round(pnl_pct, 1),
        "oracle_correct":    "true" if pnl_inr > 0 else "false",
        "signal_score":      archive.get("signal_score", 0),
        "ml_conf":           archive.get("ml_conf", 0),
    }


def _build_spread_row(date_str: str, legs: dict, archive: dict) -> dict:
    """Build a live_spread_trades.csv row from 2 leg dicts (Bull Put / Bear Call)."""
    items = sorted(legs.items(), key=lambda x: x[1]["drv_strike"])
    if len(items) != 2:
        raise ValueError(f"Spread needs 2 legs, got {len(items)}")

    opt_type = items[0][1]["drv_option_type"]
    if opt_type == "PE":
        # Bull Put: SHORT = ATM (higher), LONG = ATM-150 (lower)
        long_sid,  long_leg  = items[0]
        short_sid, short_leg = items[1]
        strategy = "bull_put_credit"
    else:
        # Bear Call: SHORT = ATM (lower), LONG = ATM+150 (higher)
        short_sid, short_leg = items[0]
        long_sid,  long_leg  = items[1]
        strategy = "bear_call_credit"

    pnl_inr     = sum(l["net_pnl"] for l in legs.values())
    short_entry = short_leg["sell_avg"]
    long_entry  = long_leg["buy_avg"]
    short_exit  = short_leg["buy_avg"]
    long_exit   = long_leg["sell_avg"]
    net_credit  = short_entry - long_entry
    exit_spread = round(short_exit - long_exit, 2)
    qty   = short_leg.get("qty") or long_leg.get("qty") or 65
    lots  = max(1, qty // 65)
    spread_width = abs(long_leg["drv_strike"] - short_leg["drv_strike"])

    exit_times = [l["sell_time"] for l in legs.values() if l["sell_time"]]
    exit_time  = max(exit_times)[-8:] if exit_times else ""

    pnl_pct = (pnl_inr / (net_credit * qty) * 100) if net_credit > 0 else 0
    signal  = archive.get("signal") or ("PUT" if strategy == "bull_put_credit" else "CALL")

    return {
        "date":              date_str,
        "strategy":          strategy,
        "signal":            signal,
        "short_strike":      short_leg["drv_strike"],
        "long_strike":       long_leg["drv_strike"],
        "spread_width":      spread_width,
        "lots":              lots,
        "lot_size":          65,
        "dte":               archive.get("dte", 0),
        "spot_at_signal":    archive.get("spot_at_signal", 0),
        "short_entry":       short_entry,
        "long_entry":        long_entry,
        "net_credit":        round(net_credit, 2),
        "short_exit":        short_exit,
        "long_exit":         long_exit,
        "exit_spread":       exit_spread,
        "exit_reason":       "EOD",
        "exit_time":         exit_time,
        "pnl_inr":           round(pnl_inr, 2),
        "pnl_pct_of_credit": round(pnl_pct, 1),
        "oracle_correct":    "true" if pnl_inr > 0 else "false",
        "signal_score":      archive.get("signal_score", 0),
        "ml_conf":           archive.get("ml_conf", 0),
    }


def _build_straddle_row(date_str: str, legs: dict, archive: dict) -> dict:
    """Build live_straddle_trades.csv row from CE + PE legs."""
    ce_leg = next((l for l in legs.values() if l["drv_option_type"] == "CE"), None)
    pe_leg = next((l for l in legs.values() if l["drv_option_type"] == "PE"), None)
    if not (ce_leg and pe_leg):
        raise ValueError("Straddle needs one CE + one PE")

    pnl_inr    = sum(l["net_pnl"] for l in legs.values())
    ce_entry   = ce_leg["sell_avg"]
    pe_entry   = pe_leg["sell_avg"]
    ce_exit    = ce_leg["buy_avg"]
    pe_exit    = pe_leg["buy_avg"]
    net_credit = ce_entry + pe_entry
    exit_cost  = round(ce_exit + pe_exit, 2)
    qty   = ce_leg.get("qty") or 65
    lots  = max(1, qty // 65)
    pnl_pct = (pnl_inr / (net_credit * qty) * 100) if net_credit > 0 else 0

    exit_times = [l["sell_time"] for l in legs.values() if l["sell_time"]]
    exit_time  = max(exit_times)[-8:] if exit_times else ""

    return {
        "date":              date_str,
        "strategy":          "nf_short_straddle",
        "signal":            archive.get("signal", "CALL"),
        "atm_strike":        ce_leg["drv_strike"],
        "lots":              lots,
        "lot_size":          65,
        "dte":               archive.get("dte", 0),
        "spot_at_signal":    archive.get("spot_at_signal", 0),
        "ce_entry":          ce_entry,
        "pe_entry":          pe_entry,
        "net_credit":        round(net_credit, 2),
        "exit_cost":         exit_cost,
        "exit_reason":       "EOD",
        "exit_time":         exit_time,
        "pnl_inr":           round(pnl_inr, 2),
        "pnl_pct_of_credit": round(pnl_pct, 1),
        "oracle_correct":    "true" if pnl_inr > 0 else "false",
        "signal_score":      archive.get("signal_score", 0),
        "ml_conf":           archive.get("ml_conf", 0),
    }


# ─── main ────────────────────────────────────────────────────────────────────

def backfill_one_date(date_str: str, strategy_override: str | None,
                      apply: bool) -> tuple[str, str | None, dict | None]:
    """Backfill a single date. Returns (status_msg, csv_path, row_built)."""
    print(f"\n─── {date_str} ─────────────────────────────────────────")

    legs = _enrich_legs_with_fills(date_str, sids_or_none=None)
    if not legs:
        return f"No NF F&O fills found on {date_str}", None, None

    print(f"Found {len(legs)} leg(s) with {sum(len(l['fills']) for l in legs.values())} fills:")
    for sid, leg in legs.items():
        print(f"  sid={sid}  {leg['drv_option_type']} {leg['drv_strike']:.0f}  "
              f"buy={leg['buy_avg']:>7.2f}  sell={leg['sell_avg']:>7.2f}  "
              f"qty={leg['qty']:>3d}  net=₹{leg['net_pnl']:>9,.2f}")
    total_pnl = sum(l["net_pnl"] for l in legs.values())
    total_chg = sum(l["charges"] for l in legs.values())
    print(f"  → total net P&L ₹{total_pnl:,.2f}  (charges ₹{total_chg:,.2f})")

    strategy = strategy_override or _detect_strategy(legs)
    print(f"  strategy: {strategy}")
    archive = _signal_from_archive(date_str)
    if archive:
        print(f"  signal from archive: {archive.get('signal','?')} score={archive.get('signal_score','?')}")

    if strategy == "ic":
        row, csv_path, fields = _build_ic_row(date_str, legs, archive), IC_CSV, IC_FIELDS
    elif strategy == "spread":
        row, csv_path, fields = _build_spread_row(date_str, legs, archive), SPREAD_CSV, SPREAD_FIELDS
    elif strategy == "straddle":
        row, csv_path, fields = _build_straddle_row(date_str, legs, archive), STRADDLE_CSV, STRADDLE_FIELDS
    else:
        return f"Unsupported strategy: {strategy}", None, None

    print("  row preview:")
    for k, v in row.items():
        print(f"    {k:>24s} = {v}")

    if apply:
        bak = csv_path + ".pre-dhan-backfill.bak"
        if not os.path.exists(bak) and os.path.exists(csv_path):
            shutil.copy(csv_path, bak)
            print(f"  💾 backup → {bak}")
        _upsert_csv_row(csv_path, fields, row)
        print(f"  ✅ wrote row → {csv_path}")
    else:
        print("  (dry-run — pass --apply to write)")

    return "OK", csv_path, row


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="Single date YYYY-MM-DD")
    p.add_argument("--range", help="Date range FROM:TO (YYYY-MM-DD:YYYY-MM-DD)")
    p.add_argument("--strategy", choices=("ic", "spread", "straddle"),
                   help="Override auto-detected strategy")
    p.add_argument("--apply", action="store_true",
                   help="Actually write to CSV (default: dry run)")
    args = p.parse_args()

    if not args.date and not args.range:
        p.print_help()
        sys.exit(1)

    if args.date:
        dates = [args.date]
    else:
        a, b = args.range.split(":")
        d0 = datetime.strptime(a, "%Y-%m-%d").date()
        d1 = datetime.strptime(b, "%Y-%m-%d").date()
        dates = []
        cur = d0
        while cur <= d1:
            if cur.weekday() < 5:  # Mon–Fri only
                dates.append(cur.isoformat())
            cur += timedelta(days=1)

    print(f"═══ DHAN HISTORICAL BACKFILL — {datetime.now(_IST).strftime('%Y-%m-%d %H:%M:%S IST')} ═══")
    print(f"Dates: {len(dates)}  apply={args.apply}")
    for d in dates:
        try:
            backfill_one_date(d, args.strategy, args.apply)
        except Exception as e:
            print(f"  ❌ {d}: {e}")
    print("\n═══ DONE ═══")
    if not args.apply:
        print("Re-run with --apply to write to CSVs.\n")


if __name__ == "__main__":
    main()
