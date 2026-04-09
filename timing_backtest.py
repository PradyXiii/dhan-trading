#!/usr/bin/env python3
"""
timing_backtest.py — Entry timing + slippage sensitivity analysis
=================================================================
Part A  (intraday)   : 9:15 vs 9:20 vs 9:25 vs 9:30 AM
                       Uses real 5-min candles from data/banknifty_5min.csv (~90 days)
                       Exit also tracked candle-by-candle (not OHLCV approximation)

Part B  (slippage)   : What if every fill is X% worse than estimated premium?
                       0% / 0.5% / 1% / 2% / 3% / 5%
                       Runs on full 4.5-year daily backtest

Usage:
    python3 fetch_intraday.py       # one-time: fetch 5-min data
    python3 timing_backtest.py      # run both parts
    python3 timing_backtest.py --timing-only
    python3 timing_backtest.py --slippage-only
"""

import os
import sys
import pandas as pd
from math import sqrt, floor

DATA_DIR  = "data"
LOT_SIZE  = 30
SL_PCT    = 0.30
RISK_PCT  = 0.05
MAX_LOTS  = 20
PREMIUM_K = 0.004
DELTA     = 0.5        # ATM option delta approximation

STARTING_CAPITAL = 30_000
MONTHLY_TOPUP    = 10_000

DAY_DTE = {"Monday": 2, "Tuesday": 1, "Wednesday": 0.25, "Thursday": 6, "Friday": 5}
DAY_RR  = {"Monday": 1.6, "Tuesday": 1.4, "Wednesday": 1.0, "Thursday": 2.0, "Friday": 2.0}

ENTRY_TIMES = ["09:15", "09:20", "09:25", "09:30"]


# ═══════════════════════════════════════════════════════════════════════════════
# PART A — Intraday timing test (5-min candles, ~90 days)
# ═══════════════════════════════════════════════════════════════════════════════

def load_intraday():
    path = f"{DATA_DIR}/banknifty_5min.csv"
    if not os.path.exists(path):
        print(f"ERROR: {path} not found.")
        print("Run:   python3 fetch_intraday.py")
        sys.exit(1)

    df = pd.read_csv(path, parse_dates=["datetime"])
    df["date"] = df["datetime"].dt.date
    df["time"] = df["datetime"].dt.strftime("%H:%M")
    # Market hours only
    df = df[(df["time"] >= "09:15") & (df["time"] <= "15:30")].copy()
    return df


def load_signals():
    df = pd.read_csv(f"{DATA_DIR}/signals.csv", parse_dates=["date"])
    return df.drop(columns=["threshold"], errors="ignore")


def simulate_entry(day_candles, signal, entry_time, weekday):
    """
    Simulate one trade entering at entry_time using 5-min candles.
    Returns dict with outcome, pnl_1lot, entry_bn, premium — or None if no candle.
    """
    dte = DAY_DTE.get(weekday, 1)
    rr  = DAY_RR.get(weekday, 1.4)

    entry_row = day_candles[day_candles["time"] == entry_time]
    if entry_row.empty:
        return None

    entry_idx = int(entry_row.index[0])
    entry_bn  = float(entry_row.iloc[0]["open"])     # enter at the candle's open

    premium   = entry_bn * PREMIUM_K * sqrt(dte)
    sl_pts    = premium * SL_PCT                      # option SL in ₹
    tp_pts    = premium * SL_PCT * rr                 # option TP in ₹

    # Convert to BankNifty index points (delta ≈ 0.5)
    sl_bn = sl_pts / DELTA
    tp_bn = tp_pts / DELTA

    if signal == "CALL":
        sl_level = entry_bn - sl_bn
        tp_level = entry_bn + tp_bn
    else:
        sl_level = entry_bn + sl_bn
        tp_level = entry_bn - tp_bn

    # Scan candles after entry for SL/TP hit
    after = day_candles[day_candles.index > entry_idx]
    after = after[after["time"] <= "15:15"]

    outcome = None
    for _, c in after.iterrows():
        if signal == "CALL":
            tp_hit = c["high"] >= tp_level
            sl_hit = c["low"]  <= sl_level
        else:
            tp_hit = c["low"]  <= tp_level
            sl_hit = c["high"] >= sl_level

        if tp_hit and sl_hit:
            # Both in same candle — direction decides
            if signal == "CALL":
                outcome = "WIN" if c["close"] >= c["open"] else "LOSS"
            else:
                outcome = "WIN" if c["close"] <= c["open"] else "LOSS"
            break
        elif tp_hit:
            outcome = "WIN"
            break
        elif sl_hit:
            outcome = "LOSS"
            break

    if outcome is None:
        # Neither hit — exit at last available candle close
        last_row = day_candles[day_candles["time"] <= "15:30"].iloc[-1]
        exit_bn  = float(last_row["close"])
        bn_move  = (exit_bn - entry_bn) if signal == "CALL" else (entry_bn - exit_bn)
        pnl_1lot = round(LOT_SIZE * bn_move * DELTA, 2)
        outcome  = "PARTIAL"
    elif outcome == "WIN":
        pnl_1lot = round(LOT_SIZE * tp_pts, 2)
    else:
        pnl_1lot = round(-LOT_SIZE * sl_pts, 2)

    return {
        "outcome":   outcome,
        "pnl_1lot":  pnl_1lot,
        "entry_bn":  entry_bn,
        "premium":   round(premium, 2),
    }


def run_timing_analysis():
    print("\nLoading intraday data and signals...")
    df5  = load_intraday()
    sigs = load_signals()

    intraday_dates = set(df5["date"].unique())
    sigs["date_py"] = sigs["date"].dt.date
    trade_sigs = sigs[
        sigs["date_py"].isin(intraday_dates) &
        sigs["signal"].isin(["CALL", "PUT"])
    ].reset_index(drop=True)

    if trade_sigs.empty:
        print("\nNo overlapping trade signals found in the 5-min data window.")
        print("signals.csv may not cover the same recent period.")
        print("Run: python3 signal_engine.py   then retry.")
        return

    all_dates_in_window = sigs[sigs["date_py"].isin(intraday_dates)]
    total_days = len(intraday_dates)
    total_signal_days = len(all_dates_in_window)

    # Pre-group candles by date
    by_date = {}
    for d, grp in df5.groupby("date"):
        by_date[d] = grp.reset_index(drop=True)

    # Run for each entry time
    results = {t: [] for t in ENTRY_TIMES}

    for _, row in trade_sigs.iterrows():
        d       = row["date_py"]
        weekday = row["weekday"]
        signal  = row["signal"]

        if d not in by_date:
            continue

        day_candles = by_date[d]

        for t in ENTRY_TIMES:
            res = simulate_entry(day_candles, signal, t, weekday)
            if res is None:
                continue
            results[t].append({
                "date":    d,
                "weekday": weekday,
                "signal":  signal,
                **res,
            })

    # ── Print summary table ──────────────────────────────────────────────────
    window_start = min(intraday_dates)
    window_end   = max(intraday_dates)

    print(f"\n{'═'*74}")
    print(f"  PART A — INTRADAY ENTRY TIMING  "
          f"({window_start} → {window_end})")
    print(f"  {total_days} trading days  |  {len(trade_sigs)} trade signals")
    print(f"  P&L shown per 1 lot ({LOT_SIZE} qty), no capital compounding")
    print(f"{'═'*74}")
    print(f"  {'Time':<10} {'Trades':>6} {'W':>4} {'L':>4} {'P':>4}  {'WR':>6}  {'AvgP&L':>8}  {'TotalP&L':>10}")
    print(f"  {'─'*68}")

    summary = {}
    for t in ENTRY_TIMES:
        rows = results[t]
        if not rows:
            print(f"  {t+' AM':<10}  — no data")
            continue
        dfr    = pd.DataFrame(rows)
        trades = len(dfr)
        wins   = (dfr["outcome"] == "WIN").sum()
        losses = (dfr["outcome"] == "LOSS").sum()
        parts  = (dfr["outcome"] == "PARTIAL").sum()
        wr     = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
        total  = dfr["pnl_1lot"].sum()
        avg    = dfr["pnl_1lot"].mean()
        summary[t] = {"total": total, "wr": wr, "trades": trades,
                      "wins": wins, "losses": losses, "parts": parts,
                      "avg": avg, "df": dfr}
        print(f"  {t+' AM':<10} {trades:>6} {wins:>4} {losses:>4} {parts:>4}  "
              f"{wr:>5.1f}%  {avg:>+7.0f}  {total:>+10.0f}")

    if not summary:
        return

    best_time = max(summary, key=lambda t: summary[t]["total"])
    best_pnl  = summary[best_time]["total"]
    worst_time = min(summary, key=lambda t: summary[t]["total"])
    worst_pnl  = summary[worst_time]["total"]

    print(f"  {'─'*68}")
    print(f"  Best  entry: {best_time} AM  →  ₹{best_pnl:+,.0f} total / lot")
    print(f"  Worst entry: {worst_time} AM  →  ₹{worst_pnl:+,.0f} total / lot")
    diff = best_pnl - worst_pnl
    diff_per_trade = diff / summary[best_time]["trades"] if summary[best_time]["trades"] else 0
    print(f"  Spread:      ₹{diff:,.0f} over {len(trade_sigs)} trades "
          f"(≈ ₹{diff_per_trade:+.0f}/trade)")
    print(f"{'═'*74}")

    # Per-day breakdown for best time
    dfb = summary[best_time]["df"]
    print(f"\n  Per-day breakdown @ {best_time} AM (best timing):")
    print(f"  {'Day':<12} {'Trades':>6} {'WR':>7} {'P&L/lot':>10}")
    print(f"  {'─'*38}")
    for day in ["Monday","Tuesday","Wednesday","Thursday","Friday"]:
        d = dfb[dfb["weekday"] == day]
        if len(d) == 0:
            continue
        dw  = (d["outcome"] == "WIN").sum()
        dl  = (d["outcome"] == "LOSS").sum()
        dwr = dw / (dw + dl) * 100 if (dw + dl) > 0 else 0
        print(f"  {day:<12} {len(d):>6} {dwr:>6.0f}%  {d['pnl_1lot'].sum():>+9,.0f}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# PART B — Slippage sensitivity (full 4.5-year backtest)
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_charges(premium, lots):
    """Round-trip transaction cost (mirrors backtest_engine.py)."""
    pv         = lots * LOT_SIZE * premium
    brokerage  = 40.0
    stt        = 0.000625 * pv
    exchange   = 0.00053  * pv * 2
    clearing   = 0.000005 * pv * 2
    gst        = 0.18 * (brokerage + exchange + clearing)
    stamp_duty = 0.00003  * pv
    sebi       = 0.000001 * pv * 2
    return round(brokerage + stt + exchange + clearing + gst + stamp_duty + sebi, 2)


def simulate_trade_with_slip(row, bn_ohlcv, capital, slip_pct):
    """
    Same logic as backtest_engine.simulate_trade() but with an extra slippage cost.
    slip_pct: e.g. 0.5 means you pay 0.5% more than estimated premium.
    """
    d       = row["date"]
    weekday = row["weekday"]
    signal  = row["signal"]

    if d not in bn_ohlcv.index:
        return 0.0, "SKIPPED", 0, 0.0

    bar     = bn_ohlcv.loc[d]
    bn_open = bar["open"]
    bn_high = bar["high"]
    bn_low  = bar["low"]
    bn_close= bar["close"]

    dte     = DAY_DTE.get(weekday, 1)
    rr      = DAY_RR.get(weekday, 1.4)
    premium = bn_open * PREMIUM_K * (dte ** 0.5)

    max_loss_1lot = LOT_SIZE * premium * SL_PCT
    if max_loss_1lot > capital * 0.15:
        return 0.0, "SKIPPED_LOW_CAPITAL", 0, premium

    lots = min(MAX_LOTS, max(1, int((capital * RISK_PCT) / max_loss_1lot)))

    sl_pts = (SL_PCT * premium) / DELTA
    tp_pts = (rr * SL_PCT * premium) / DELTA

    if signal == "CALL":
        sl_level = bn_open - sl_pts
        tp_level = bn_open + tp_pts
        sl_hit   = bn_low  <= sl_level
        tp_hit   = bn_high >= tp_level
        if sl_hit and tp_hit:
            result = "WIN" if bn_close > bn_open else "LOSS"
        elif tp_hit:
            result = "WIN"
        elif sl_hit:
            result = "LOSS"
        else:
            gross = (bn_close - bn_open) * DELTA * lots * LOT_SIZE
            charges = calculate_charges(premium, lots)
            slip_cost = lots * LOT_SIZE * premium * slip_pct / 100
            return round(gross - charges - slip_cost, 2), "PARTIAL", lots, premium
    else:
        sl_level = bn_open + sl_pts
        tp_level = bn_open - tp_pts
        sl_hit   = bn_high >= sl_level
        tp_hit   = bn_low  <= tp_level
        if sl_hit and tp_hit:
            result = "WIN" if bn_close < bn_open else "LOSS"
        elif tp_hit:
            result = "WIN"
        elif sl_hit:
            result = "LOSS"
        else:
            gross = (bn_open - bn_close) * DELTA * lots * LOT_SIZE
            charges = calculate_charges(premium, lots)
            slip_cost = lots * LOT_SIZE * premium * slip_pct / 100
            return round(gross - charges - slip_cost, 2), "PARTIAL", lots, premium

    charges   = calculate_charges(premium, lots)
    slip_cost = lots * LOT_SIZE * premium * slip_pct / 100

    if result == "WIN":
        pnl = lots * LOT_SIZE * premium * rr * SL_PCT - charges - slip_cost
    else:
        pnl = -lots * LOT_SIZE * premium * SL_PCT - charges - slip_cost

    return round(pnl, 2), result, lots, premium


def run_slippage_sensitivity():
    print(f"\n{'═'*74}")
    print(f"  PART B — SLIPPAGE SENSITIVITY  (full Sep 2021 → Apr 2026 backtest)")
    print(f"  Models getting a worse option fill at entry than estimated premium.")
    print(f"  SL / TP levels unchanged — slippage just raises your break-even.")
    print(f"{'═'*74}")
    print(f"  {'Slippage':>10}  {'Trades':>7}  {'WR':>6}  {'Net P&L':>12}  "
          f"{'vs 0%':>10}  {'End Cap':>12}  {'Max DD':>8}")
    print(f"  {'─'*72}")

    try:
        sigs = pd.read_csv(f"{DATA_DIR}/signals.csv", parse_dates=["date"])
        sigs = sigs.drop(columns=["threshold"], errors="ignore")
        sigs = sigs[sigs["signal"].isin(["CALL", "PUT"])].reset_index(drop=True)
    except FileNotFoundError:
        print("  ERROR: data/signals.csv not found.")
        return

    try:
        bn = pd.read_csv(f"{DATA_DIR}/banknifty.csv", parse_dates=["date"])
        bn_ohlcv = bn.set_index("date")
    except FileNotFoundError:
        print("  ERROR: data/banknifty.csv not found.")
        return

    slippage_pcts = [0.0, 0.5, 1.0, 2.0, 3.0, 5.0]
    base_pnl = None

    for slip_pct in slippage_pcts:
        capital       = STARTING_CAPITAL
        current_month = None
        all_caps      = []
        wins = losses  = 0

        for _, row in sigs.iterrows():
            d         = row["date"]
            month_key = (d.year, d.month)

            if current_month is None:
                current_month = month_key
            elif month_key != current_month:
                capital      += MONTHLY_TOPUP
                current_month = month_key

            pnl, result, lots, _ = simulate_trade_with_slip(row, bn_ohlcv, capital, slip_pct)
            capital += pnl
            all_caps.append(capital)

            if result == "WIN":    wins   += 1
            elif result == "LOSS": losses += 1

        total   = wins + losses
        wr      = wins / total * 100 if total > 0 else 0
        net_pnl = capital - STARTING_CAPITAL  # rough proxy

        # Recalculate net P&L properly
        # (capital - starting - topups)
        # topups = MONTHLY_TOPUP × months elapsed
        months_elapsed = (sigs.iloc[-1]["date"].year * 12 + sigs.iloc[-1]["date"].month) - \
                         (sigs.iloc[0]["date"].year  * 12 + sigs.iloc[0]["date"].month)
        injected = STARTING_CAPITAL + months_elapsed * MONTHLY_TOPUP
        net_pnl  = capital - injected

        cap_series = pd.Series(all_caps)
        max_dd     = ((cap_series - cap_series.cummax()) / cap_series.cummax() * 100).min()

        if base_pnl is None:
            base_pnl = net_pnl
            vs_base  = "  base"
        else:
            delta    = base_pnl - net_pnl
            vs_base  = f"−₹{delta:,.0f}"

        slip_label = f"{slip_pct:.1f}%"
        print(f"  {slip_label:>10}  {total:>7}  {wr:>5.1f}%  "
              f"₹{net_pnl:>10,.0f}  {vs_base:>10}  ₹{capital:>10,.0f}  {max_dd:>7.1f}%")

    print(f"  {'─'*72}")
    print(f"  Breakeven slippage = the % at which strategy stops being profitable.")
    print(f"  In practice, ATM option spread at open is usually 0.5–1.5% of premium.")
    print(f"{'═'*74}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    args = sys.argv[1:]

    timing_only  = "--timing-only"  in args
    slippage_only= "--slippage-only" in args

    if not timing_only and not slippage_only:
        timing_only = slippage_only = True   # run both by default

    print("=" * 74)
    print("  BankNifty Options — Entry Timing & Slippage Analysis")
    print("=" * 74)

    if timing_only:
        run_timing_analysis()

    if slippage_only:
        run_slippage_sensitivity()
