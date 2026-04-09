#!/usr/bin/env python3
"""
strangle_backtest.py — Straddle vs Directional Long comparison
==============================================================
Tests three strategies on the full 4.5-year BankNifty dataset:

  A) Directional Long  : BUY ATM CE or PE based on signal (current strategy)
  B) Straddle Always   : BUY ATM CE + PE every day (no signal needed)
  C) Signal Straddle   : BUY ATM CE + PE only on signal days (skips NONE days)
  D) Best-of combos    : pre-market signal drives straddle on select days

Straddle P&L model:
  - Buy both legs at ATM premium at open
  - TP if intraday max excursion hits TP level on EITHER side
    → winner leg = +tp_pts, loser leg = -(delta × excursion) approximated
    → net modeled as: +tp_pts - delta × excursion_on_losing_side
  - EOD exit: |close - open| × delta × lots × lot_size - 2 × premium
  - Max loss = 2 × premium (both options expire nearly worthless on flat day)
  - Lot sizing: 5% of capital / (2 × premium × lot_size × 0.5)
    i.e., sized as if max loss = 50% of total premium (not full 100%)

Usage:
    python3 strangle_backtest.py
"""

import os, sys
import pandas as pd
import numpy as np
from math import sqrt, floor

DATA_DIR         = "data"
LOT_SIZE         = 30
SL_PCT           = 0.30       # directional leg stop-loss
RISK_PCT         = 0.05       # 5% of capital at risk
MAX_LOTS         = 20
PREMIUM_K        = 0.004
DELTA            = 0.5
STARTING_CAPITAL = 30_000
MONTHLY_TOPUP    = 10_000

DAY_DTE = {"Monday": 2, "Tuesday": 1, "Wednesday": 0.25, "Thursday": 6, "Friday": 5}
DAY_RR  = {"Monday": 1.6, "Tuesday": 1.4, "Wednesday": 1.0, "Thursday": 2.0, "Friday": 2.0}


# ── Charges ────────────────────────────────────────────────────────────────────

def charges(premium, lots):
    pv = lots * LOT_SIZE * premium
    b  = 40.0
    s  = 0.000625 * pv
    ex = 0.00053  * pv * 2
    cl = 0.000005 * pv * 2
    g  = 0.18 * (b + ex + cl)
    sd = 0.00003  * pv
    sb = 0.000001 * pv * 2
    return round(b + s + ex + cl + g + sd + sb, 2)


# ── Strategy A: Directional Long (current strategy) ───────────────────────────

def sim_directional(row, bn_ohlcv, capital):
    """Buy CE or PUT based on signal. Returns (pnl, result, lots)."""
    d       = row["date"]
    signal  = row["signal"]
    weekday = row["weekday"]

    if signal not in ("CALL", "PUT"):
        return 0.0, "SKIP", 0

    if d not in bn_ohlcv.index:
        return 0.0, "SKIP", 0

    bar = bn_ohlcv.loc[d]
    bn_open, bn_high, bn_low, bn_close = bar["open"], bar["high"], bar["low"], bar["close"]

    dte     = DAY_DTE.get(weekday, 1)
    rr      = DAY_RR.get(weekday, 1.4)
    premium = bn_open * PREMIUM_K * sqrt(dte)

    max_loss_1lot = LOT_SIZE * premium * SL_PCT
    if max_loss_1lot > capital * 0.15:
        return 0.0, "SKIP", 0

    lots   = min(MAX_LOTS, max(1, int((capital * RISK_PCT) / max_loss_1lot)))
    sl_pts = (SL_PCT * premium) / DELTA
    tp_pts = (rr * SL_PCT * premium) / DELTA

    if signal == "CALL":
        sl_hit = bn_low  <= bn_open - sl_pts
        tp_hit = bn_high >= bn_open + tp_pts
        if sl_hit and tp_hit:
            result = "WIN" if bn_close > bn_open else "LOSS"
        elif tp_hit:
            result = "WIN"
        elif sl_hit:
            result = "LOSS"
        else:
            gross  = (bn_close - bn_open) * DELTA * lots * LOT_SIZE
            return round(gross - charges(premium, lots), 2), "PARTIAL", lots
    else:
        sl_hit = bn_high >= bn_open + sl_pts
        tp_hit = bn_low  <= bn_open - tp_pts
        if sl_hit and tp_hit:
            result = "WIN" if bn_close < bn_open else "LOSS"
        elif tp_hit:
            result = "WIN"
        elif sl_hit:
            result = "LOSS"
        else:
            gross  = (bn_open - bn_close) * DELTA * lots * LOT_SIZE
            return round(gross - charges(premium, lots), 2), "PARTIAL", lots

    c = charges(premium, lots)
    if result == "WIN":
        return round(lots * LOT_SIZE * premium * rr * SL_PCT - c, 2), "WIN", lots
    else:
        return round(-(lots * LOT_SIZE * premium * SL_PCT) - c, 2), "LOSS", lots


# ── Strategy B/C: Straddle (buy both CE + PE) ─────────────────────────────────

def sim_straddle(row, bn_ohlcv, capital):
    """
    Buy ATM CE + PE at open. Profits from large moves either way.

    TP logic (intraday):
      If BN high exceeds CALL TP level:  CALL wins, PUT is nearly worthless
      If BN low  drops below PUT  TP level: PUT wins, CALL is nearly worthless
      If both:  both legs hit (whipsaw) → double winner

    Approximate option P&L (delta model):
      Winning leg:  +tp_pts per option unit
      Losing leg:   -(delta × excursion_on_losing_side) per option unit
      For single TP hit: net ≈ +tp_pts - tp_pts = ~0 (see below why straddle is hard)
      For double TP hit: net ≈ 2 × tp_pts
      For EOD exit: max(|close-open|, 0) × delta - 2 × premium

    P&L formula per lot for straddle:
      WIN_one_side:  1 lot × lot_size × (tp_pts - tp_pts) - 2 × charges ≈ -charges only
      WIN_both_sides: 1 lot × lot_size × 2 × tp_pts - 2 × charges (rare, big winner)
      EOD exit:      1 lot × lot_size × (|close-open| × delta - 2 × premium) - 2 × charges
    """
    d       = row["date"]
    weekday = row["weekday"]

    if d not in bn_ohlcv.index:
        return 0.0, "SKIP", 0

    bar = bn_ohlcv.loc[d]
    bn_open, bn_high, bn_low, bn_close = bar["open"], bar["high"], bar["low"], bar["close"]

    dte     = DAY_DTE.get(weekday, 1)
    rr      = DAY_RR.get(weekday, 1.4)
    premium = bn_open * PREMIUM_K * sqrt(dte)

    # Lots: size on 2× premium since both legs can lose (max loss ≈ 2 × premium)
    # Use 5% of capital / (2 × premium × lot_size)
    max_loss_1lot = LOT_SIZE * 2 * premium    # both legs total risk per 1 lot
    if max_loss_1lot > capital * 0.15:
        return 0.0, "SKIP", 0

    lots   = min(MAX_LOTS, max(1, int((capital * RISK_PCT) / max_loss_1lot)))
    tp_pts = (rr * SL_PCT * premium) / DELTA   # BN move needed to hit TP

    # Check TP hit for each side intraday
    call_tp_hit = (bn_high - bn_open) >= tp_pts
    put_tp_hit  = (bn_open - bn_low)  >= tp_pts

    c = charges(premium, lots) * 2   # two legs, two round-trips

    if call_tp_hit and put_tp_hit:
        # Whipsaw: both sides TP — big winner
        # Net ≈ tp_pts (CALL winner) + tp_pts (PUT winner) - 2×premium (cost of both) - charges
        gross = lots * LOT_SIZE * (2 * rr * SL_PCT * premium - 2 * premium)
        result = "WIN_BOTH"

    elif call_tp_hit:
        # CALL TP hit. PUT lost most of its value (BN moved up by tp_pts/delta).
        # PUT value at exit ≈ max(0, premium - tp_pts) = premium × max(0, 1 - rr×SL_PCT)
        put_residual = max(0.0, premium * (1 - rr * SL_PCT))
        gross = lots * LOT_SIZE * (rr * SL_PCT * premium + put_residual - 2 * premium)
        result = "WIN_CALL"

    elif put_tp_hit:
        call_residual = max(0.0, premium * (1 - rr * SL_PCT))
        gross = lots * LOT_SIZE * (rr * SL_PCT * premium + call_residual - 2 * premium)
        result = "WIN_PUT"

    else:
        # Neither TP hit — exit at close
        # Net move in better direction
        net_move = max(abs(bn_close - bn_open), 0)
        gross = lots * LOT_SIZE * (net_move * DELTA - 2 * premium)
        result = "PARTIAL"

    return round(gross - c, 2), result, lots


# ── Backtest loop ──────────────────────────────────────────────────────────────

def run_backtest(signal_df, bn_ohlcv, mode="directional"):
    capital       = STARTING_CAPITAL
    current_month = None
    all_pnl       = []
    all_cap       = []

    results_count = {"WIN":0,"LOSS":0,"PARTIAL":0,"WIN_CALL":0,"WIN_PUT":0,"WIN_BOTH":0,"SKIP":0}

    for _, row in signal_df.iterrows():
        d         = row["date"]
        month_key = (d.year, d.month)

        if current_month is None:
            current_month = month_key
        elif month_key != current_month:
            capital      += MONTHLY_TOPUP
            current_month = month_key

        if mode == "directional":
            pnl, result, lots = sim_directional(row, bn_ohlcv, capital)
        else:  # straddle
            pnl, result, lots = sim_straddle(row, bn_ohlcv, capital)

        capital += pnl
        all_pnl.append(pnl)
        all_cap.append(capital)
        results_count[result] = results_count.get(result, 0) + 1

    wins   = results_count.get("WIN",0)
    losses = results_count.get("LOSS",0)
    partials = results_count.get("PARTIAL",0)
    straddle_wins = (results_count.get("WIN_CALL",0) +
                     results_count.get("WIN_PUT",0)  +
                     results_count.get("WIN_BOTH",0))
    total  = wins + losses + partials + straddle_wins

    wr     = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0.0
    if mode == "straddle":
        wr = straddle_wins / total * 100 if total > 0 else 0.0

    cap_s  = pd.Series(all_cap) if all_cap else pd.Series([STARTING_CAPITAL])
    max_dd = ((cap_s - cap_s.cummax()) / cap_s.cummax() * 100).min()

    # net P&L
    if signal_df.empty:
        return {"trades":0,"wr":0,"net_pnl":0,"end_cap":STARTING_CAPITAL,"max_dd":0,"results":results_count}

    first = signal_df.iloc[0]["date"]
    last  = signal_df.iloc[-1]["date"]
    months= (last.year * 12 + last.month) - (first.year * 12 + first.month)
    injected = STARTING_CAPITAL + months * MONTHLY_TOPUP
    net_pnl  = capital - injected

    return {
        "trades":  total,
        "wr":      wr,
        "net_pnl": net_pnl,
        "end_cap": capital,
        "max_dd":  max_dd,
        "results": results_count,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    try:
        sig_df = pd.read_csv(f"{DATA_DIR}/signals.csv", parse_dates=["date"])
        sig_df = sig_df.drop(columns=["threshold"], errors="ignore")
    except FileNotFoundError:
        print("ERROR: data/signals.csv not found. Run: python3 signal_engine.py")
        sys.exit(1)

    try:
        bn = pd.read_csv(f"{DATA_DIR}/banknifty.csv", parse_dates=["date"])
        bn_ohlcv = bn.set_index("date")
    except FileNotFoundError:
        print("ERROR: data/banknifty.csv not found.")
        sys.exit(1)

    date_range = f"{sig_df['date'].min().date()}  →  {sig_df['date'].max().date()}"
    trade_sigs = sig_df[sig_df["signal"].isin(["CALL", "PUT"])].copy()
    all_sigs   = sig_df.copy()    # includes NONE rows (for straddle_always)

    # Make a fake "CALL" signal for all days (straddle buys both — direction irrelevant)
    all_as_call = all_sigs.copy()
    all_as_call["signal"] = "CALL"

    trade_as_call = trade_sigs.copy()
    trade_as_call["signal"] = "CALL"

    print(f"\n{'═'*76}")
    print(f"  STRADDLE vs DIRECTIONAL LONG — Full Comparison")
    print(f"  {date_range}")
    print(f"{'═'*76}")
    print(f"  {'Strategy':<35} {'Days':>6} {'WR':>7} {'Net P&L':>13} {'End Cap':>13} {'MaxDD':>7}")
    print(f"  {'─'*74}")

    scenarios = [
        ("Directional Long (current) ★",  trade_sigs,    "directional"),
        ("Straddle — every day",            all_as_call,   "straddle"),
        ("Straddle — signal days only",     trade_as_call, "straddle"),
    ]

    # Also per-day straddle vs directional
    day_results = {}
    for day in ["Monday","Tuesday","Wednesday","Thursday","Friday"]:
        day_dir = trade_sigs[trade_sigs["weekday"] == day].copy()
        day_all = all_sigs[all_sigs["weekday"] == day].copy()
        day_all["signal"] = "CALL"
        day_results[day] = {
            "dir_df": day_dir,
            "str_df": day_all,
        }

    summary_rows = []
    for label, df, mode in scenarios:
        r = run_backtest(df, bn_ohlcv, mode)
        marker = " ◀" if "★" in label else ""
        print(f"  {label:<35} {r['trades']:>6} {r['wr']:>6.1f}%  "
              f"₹{r['net_pnl']:>11,.0f}  ₹{r['end_cap']:>11,.0f}  {r['max_dd']:>6.1f}%{marker}")
        summary_rows.append((label, r))

    # Per-day comparison
    print(f"\n  {'─'*74}")
    print(f"  PER-DAY BREAKDOWN  (Directional vs Straddle, net P&L per 1 lot basis)")
    print(f"  {'Day':<12} {'DTE':>4} {'Dir P&L':>12} {'Dir WR':>7} │ {'Str P&L':>12} {'Str WR':>7}  {'Winner':>12}")
    print(f"  {'─'*74}")

    for day in ["Monday","Tuesday","Wednesday","Thursday","Friday"]:
        dte = DAY_DTE.get(day, 1)
        dr  = run_backtest(day_results[day]["dir_df"], bn_ohlcv, "directional")
        sr  = run_backtest(day_results[day]["str_df"], bn_ohlcv, "straddle")
        winner = "Directional" if dr["net_pnl"] > sr["net_pnl"] else "Straddle"
        print(f"  {day:<12} {dte:>4}  ₹{dr['net_pnl']:>10,.0f}  {dr['wr']:>5.1f}%  │  "
              f"₹{sr['net_pnl']:>10,.0f}  {sr['wr']:>5.1f}%   {winner}")

    print(f"\n{'═'*76}")

    # Key analysis
    dir_r = summary_rows[0][1]
    str_always_r = summary_rows[1][1]
    str_signal_r = summary_rows[2][1]

    print(f"\n  KEY FINDINGS")
    print(f"  {'─'*60}")
    print(f"  Directional Long P&L  : ₹{dir_r['net_pnl']:>12,.0f}  (current strategy)")
    print(f"  Straddle (every day)  : ₹{str_always_r['net_pnl']:>12,.0f}  (no signal needed)")
    print(f"  Straddle (signal days): ₹{str_signal_r['net_pnl']:>12,.0f}  (only on signal days)")
    print()
    print(f"  WHY STRADDLES ARE HARD for daily BankNifty options:")
    print(f"  ┌─────────┬──────────┬──────────────┬──────────────────┐")
    print(f"  │   Day   │ 1L prem  │ Breakeven BN │ Actual avg range │")
    print(f"  ├─────────┼──────────┼──────────────┼──────────────────┤")
    avg_range = (bn_ohlcv["high"] - bn_ohlcv["low"]).mean()
    for day, dte, spot_proxy in [
        ("Mon",  2,    50000),
        ("Tue",  1,    50000),
        ("Wed",  0.25, 50000),
        ("Thu",  6,    50000),
        ("Fri",  5,    50000),
    ]:
        prem = spot_proxy * PREMIUM_K * sqrt(dte)
        bkeven = 2 * prem / DELTA    # BN move needed to break even on straddle
        print(f"  │ {day:<7} │ ₹{prem:>6.0f}  │  {bkeven:>6.0f} pts  │  ~{avg_range:.0f} pts avg    │")
    print(f"  └─────────┴──────────┴──────────────┴──────────────────┘")
    print(f"\n  Breakeven = move needed for straddle to profit after paying 2× premium.")
    print(f"  If actual range < breakeven, straddle loses money on that day type.")
    print(f"{'═'*76}\n")


if __name__ == "__main__":
    main()
