#!/usr/bin/env python3
"""
Entry-time grid backtest for BankNifty options.

Question: would entering earlier (or later) than 9:30 AM produce better P&L?
Method:   use cached 1-min ATM option bars from Dhan rollingoption, simulate
          SL/TP/trailing-stop exit forward from each candidate entry time.

Rules mirrored from auto_trader.py:
  SL_PCT           = 15%       (stop-loss on option premium)
  RR               = 2.5×      → TP = +37.5% of entry premium
  TRAIL_JUMP_OPT   = ₹5        (Dhan trailing jump on option price)
  Exit by 15:15 if neither SL nor TP hit → PARTIAL at that minute's close.

Usage:
  python3 backtest_entry_time.py                   # use whatever cache exists
  python3 backtest_entry_time.py --times 09:15,09:20,09:25,09:30,09:45,10:00
  python3 backtest_entry_time.py --start 2024-10-01 --end 2025-04-20
  python3 backtest_entry_time.py --no-trail        # disable trailing stop
  python3 backtest_entry_time.py --lot-size 30     # override current lot size

Caveat: uses rolling ATM (contract rolls as spot drifts). On big-move days this
understates the edge of earlier entry, so results are a LOWER BOUND on
the real benefit of moving entry earlier.
"""
import argparse
import glob
import os
from datetime import datetime, time as dtime

import pandas as pd

DATA_DIR     = "data"
CACHE_DIR    = os.path.join(DATA_DIR, "intraday_options_cache")
SIGNALS_PATH = os.path.join(DATA_DIR, "signals_ml.csv")

# Defaults mirror auto_trader.py
DEFAULT_SL_PCT      = 0.15
DEFAULT_RR          = 2.5
DEFAULT_TRAIL_JUMP  = 5.0          # Dhan trailingJump in ₹ on option price
DEFAULT_LOT_SIZE    = 30           # current BN lot size (Jan 2026+)
DEFAULT_EXIT_TIME   = "15:15"
DEFAULT_TIMES       = "09:15,09:20,09:25,09:30,09:35,09:45,10:00,10:30,11:00"


def _hhmm_to_time(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


def _simulate_day(bars: pd.DataFrame, entry_time: dtime, exit_time: dtime,
                  sl_pct: float, rr: float, trail_jump: float,
                  lot_size: int) -> dict:
    """
    Simulate ONE trading day, ONE entry time.
    Returns dict: {entry_price, exit_price, result, pnl_per_lot, exit_reason}.
    result ∈ {"WIN", "LOSS", "TRAIL_SL", "PARTIAL", "NO_ENTRY", "NO_EXIT_DATA"}.
    """
    bars = bars.sort_values("dt").reset_index(drop=True)
    t    = bars["dt"].dt.time

    entry_rows = bars[t >= entry_time]
    if entry_rows.empty:
        return {"result": "NO_ENTRY", "pnl_per_lot": 0.0}

    entry_bar   = entry_rows.iloc[0]
    entry_price = float(entry_bar["open"])
    if entry_price <= 0 or pd.isna(entry_price):
        return {"result": "NO_ENTRY", "pnl_per_lot": 0.0}

    # Absolute SL / TP on option premium (long-only — buying CE or PE)
    orig_sl_lvl = entry_price * (1.0 - sl_pct)
    tp_lvl      = entry_price * (1.0 + sl_pct * rr)

    # Walk forward bars (after entry, up to exit_time)
    fwd_mask = (bars["dt"] >= entry_bar["dt"]) & (t <= exit_time)
    path     = bars[fwd_mask].reset_index(drop=True)
    if len(path) <= 1:
        return {"result": "NO_EXIT_DATA", "entry_price": entry_price,
                "pnl_per_lot": 0.0}

    running_high = entry_price
    sl_level     = orig_sl_lvl

    # The entry bar itself: check TP/SL from its high/low too
    for i, bar in path.iterrows():
        high = float(bar["high"]); low = float(bar["low"])

        # Update trailing stop BEFORE checking SL.
        # Dhan trailing = STEPPED ratchet: SL moves up by `trail_jump` for every
        # `trail_jump` of favorable move from entry (not continuous).
        if trail_jump > 0 and high > running_high:
            running_high = high
            favorable    = running_high - entry_price
            steps        = int(favorable / trail_jump) if favorable > 0 else 0
            sl_level     = orig_sl_lvl + steps * trail_jump

        # TP check (priority order: if both hit in the same bar, we conservatively assume SL)
        tp_hit = high >= tp_lvl
        sl_hit = low  <= sl_level

        if tp_hit and sl_hit:
            # Conservative: both hit in same bar → assume SL first
            exit_price  = sl_level
            exit_reason = "BOTH_HIT_TAKE_SL"
            result      = "TRAIL_SL" if sl_level > orig_sl_lvl else "LOSS"
            pnl         = (exit_price - entry_price) * lot_size
            return {"result": result, "entry_price": entry_price,
                    "exit_price": exit_price, "pnl_per_lot": pnl,
                    "exit_reason": exit_reason, "exit_time": str(bar["dt"])}
        if tp_hit:
            exit_price  = tp_lvl
            pnl         = (exit_price - entry_price) * lot_size
            return {"result": "WIN", "entry_price": entry_price,
                    "exit_price": exit_price, "pnl_per_lot": pnl,
                    "exit_reason": "TP", "exit_time": str(bar["dt"])}
        if sl_hit:
            exit_price  = sl_level
            pnl         = (exit_price - entry_price) * lot_size
            result      = "TRAIL_SL" if sl_level > orig_sl_lvl else "LOSS"
            return {"result": result, "entry_price": entry_price,
                    "exit_price": exit_price, "pnl_per_lot": pnl,
                    "exit_reason": "SL",  "exit_time": str(bar["dt"])}

    # Ran out of bars (reached exit_time without SL or TP) → close at last bar close
    last_close = float(path.iloc[-1]["close"])
    pnl        = (last_close - entry_price) * lot_size
    return {"result": "PARTIAL", "entry_price": entry_price,
            "exit_price": last_close, "pnl_per_lot": pnl,
            "exit_reason": "EOD", "exit_time": str(path.iloc[-1]["dt"])}


def run_grid(start: str, end: str, times_list, sl_pct: float, rr: float,
             trail_jump: float, lot_size: int, exit_time_str: str) -> pd.DataFrame:
    if not os.path.exists(SIGNALS_PATH):
        raise FileNotFoundError(f"{SIGNALS_PATH} missing")
    signals = pd.read_csv(SIGNALS_PATH, parse_dates=["date"])
    signals["date"] = signals["date"].dt.date
    lo = datetime.strptime(start, "%Y-%m-%d").date()
    hi = datetime.strptime(end,   "%Y-%m-%d").date()
    days = signals[(signals["date"] >= lo) & (signals["date"] <= hi)
                   & (signals["signal"].isin(["CALL", "PUT"]))][["date", "signal"]]

    exit_time = _hhmm_to_time(exit_time_str)
    entries   = [_hhmm_to_time(t) for t in times_list]
    per_trade = []

    n_total = len(days)
    n_cache_miss = 0
    for _, r in days.iterrows():
        opt_code  = "CE" if r["signal"] == "CALL" else "PE"
        path      = os.path.join(CACHE_DIR, f"{r['date'].strftime('%Y-%m-%d')}_{opt_code}.csv")
        if not os.path.exists(path):
            n_cache_miss += 1
            continue
        bars = pd.read_csv(path, parse_dates=["dt"])
        if bars.empty:
            continue
        for entry_t in entries:
            res = _simulate_day(bars, entry_t, exit_time, sl_pct, rr,
                                trail_jump, lot_size)
            res["date"]       = r["date"]
            res["signal"]     = r["signal"]
            res["entry_time"] = entry_t.strftime("%H:%M")
            per_trade.append(res)

    print(f"\nDays evaluated:  {n_total - n_cache_miss}  "
          f"(cache miss: {n_cache_miss})")
    if n_cache_miss:
        print(f"  → run: python3 fetch_intraday_options.py --start {start} --end {end}")

    df = pd.DataFrame(per_trade)
    if df.empty:
        print("No trades simulated — check cache and signals_ml.csv.")
        return df

    # ── Aggregate by entry_time ─────────────────────────────────────────────
    rows = []
    for entry_t in entries:
        et = entry_t.strftime("%H:%M")
        sub = df[(df["entry_time"] == et)
                 & (df["result"].isin(["WIN", "LOSS", "TRAIL_SL", "PARTIAL"]))]
        if sub.empty:
            continue
        wins       = (sub["result"] == "WIN").sum()
        losses     = (sub["result"] == "LOSS").sum()
        trail_exits= (sub["result"] == "TRAIL_SL").sum()
        partials   = (sub["result"] == "PARTIAL").sum()
        total      = len(sub)
        total_pnl  = sub["pnl_per_lot"].sum()
        avg_pnl    = sub["pnl_per_lot"].mean()
        wr         = 100.0 * wins / (wins + losses) if (wins + losses) else 0.0
        rows.append({
            "entry_time": et,
            "trades":     total,
            "wins":       wins,
            "losses":     losses,
            "trail_sl":   trail_exits,
            "partial":    partials,
            "WR%":        round(wr, 1),
            "avg_pnl":    round(avg_pnl, 0),
            "total_pnl":  round(total_pnl, 0),
        })
    summary = pd.DataFrame(rows)
    return summary, df   # (summary, per_trade_detail)


def _print_table(summary: pd.DataFrame, lot_size: int) -> None:
    print("\n" + "═" * 82)
    print(f"  ENTRY TIME GRID  (lot size = {lot_size}, P&L per lot)")
    print("═" * 82)
    print(f"  {'time':<7}{'trades':>8}{'wins':>6}{'loss':>6}{'trail':>7}"
          f"{'part':>6}{'WR%':>7}{'avg ₹':>10}{'total ₹':>12}")
    print("  " + "─" * 78)
    for _, r in summary.iterrows():
        print(f"  {r['entry_time']:<7}{int(r['trades']):>8}{int(r['wins']):>6}"
              f"{int(r['losses']):>6}{int(r['trail_sl']):>7}{int(r['partial']):>6}"
              f"{r['WR%']:>7.1f}{int(r['avg_pnl']):>10}{int(r['total_pnl']):>12}")
    print("═" * 82)

    baseline = summary[summary["entry_time"] == "09:30"]
    if not baseline.empty:
        base_total = float(baseline.iloc[0]["total_pnl"])
        print(f"\n  Δ vs 9:30 baseline (total P&L per lot):")
        for _, r in summary.iterrows():
            delta = int(r["total_pnl"] - base_total)
            sign  = "+" if delta >= 0 else ""
            print(f"    {r['entry_time']}  {sign}{delta:>10} ₹")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None,  help="YYYY-MM-DD (default: 1 year ago)")
    ap.add_argument("--end",   default=None,  help="YYYY-MM-DD (default: today)")
    ap.add_argument("--times", default=DEFAULT_TIMES,
                    help="comma-separated HH:MM entry times")
    ap.add_argument("--sl-pct",     type=float, default=DEFAULT_SL_PCT)
    ap.add_argument("--rr",         type=float, default=DEFAULT_RR)
    ap.add_argument("--trail-jump", type=float, default=DEFAULT_TRAIL_JUMP)
    ap.add_argument("--no-trail",   action="store_true", help="disable trailing stop")
    ap.add_argument("--lot-size",   type=int,   default=DEFAULT_LOT_SIZE)
    ap.add_argument("--exit-time",  default=DEFAULT_EXIT_TIME, help="HH:MM (force-close time)")
    ap.add_argument("--out",        default=None,  help="optional CSV path for per-trade detail")
    args = ap.parse_args()

    end   = args.end   or datetime.today().strftime("%Y-%m-%d")
    start = args.start or datetime.today().replace(year=datetime.today().year - 1).strftime("%Y-%m-%d")
    trail_jump = 0.0 if args.no_trail else args.trail_jump
    times_list = [t.strip() for t in args.times.split(",")]

    print(f"Entry-time backtest  {start} → {end}")
    print(f"SL={args.sl_pct*100:.0f}%  RR={args.rr}×  (TP=+{args.sl_pct*args.rr*100:.1f}%)  "
          f"trail_jump=₹{trail_jump}  lot={args.lot_size}  exit=≤{args.exit_time}")
    print(f"Candidate entry times: {', '.join(times_list)}")

    n_cache = len(glob.glob(os.path.join(CACHE_DIR, "*.csv")))
    print(f"Cache files present: {n_cache}")

    summary, detail = run_grid(start, end, times_list,
                                sl_pct=args.sl_pct, rr=args.rr,
                                trail_jump=trail_jump, lot_size=args.lot_size,
                                exit_time_str=args.exit_time)
    if summary is None or summary.empty:
        return
    _print_table(summary, args.lot_size)
    if args.out:
        detail.to_csv(args.out, index=False)
        print(f"\nPer-trade detail → {args.out}  ({len(detail)} rows)")


if __name__ == "__main__":
    main()
