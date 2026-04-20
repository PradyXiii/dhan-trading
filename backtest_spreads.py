#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
# ─── REAL-OPTIONS RULE (April 2026) ──────────────────────────────────────────
# This backtest uses REAL 1-min option bars for both legs when available
# (data/intraday_options_cache/{date}_{opt}_{offset}.csv). OTM legs missing
# from cache fall back to delta-scaled estimates from the real ATM anchor —
# that's better than OHLCV formula but flagged in output. Build OTM cache:
#   python3 fetch_intraday_options.py --spreads --start 2021-08-01
# ─────────────────────────────────────────────────────────────────────────────
"""
Multi-leg options spread backtester for BankNifty.

Why this exists:
  Naked option buying on monthly expiry loses structurally (theta + IV crush).
  Real 1-min backtest: WR 24%, -₹4.58L 1-lot normalized over 5 years.
  Spreads cap debit, limit theta bleed, enable defined-risk trading.

Strategies tested:
  - Bull Call Spread  : BUY ATM CE, SELL ATM+3 CE  (bullish, debit)
  - Bear Put Spread   : BUY ATM PE, SELL ATM-3 PE  (bearish, debit)
  - Long Straddle     : BUY ATM CE + BUY ATM PE    (volatile, debit)
  - Iron Condor       : BUY ATM-3 PE, SELL ATM-1 PE, SELL ATM+1 CE, BUY ATM+3 CE
                        (range-bound, credit — not yet implemented, needs ATM±1)

Adaptive regime routing:
  CALL signal + VIX ∈ [12, 20]  →  Bull Call Spread
  PUT  signal + VIX ∈ [12, 20]  →  Bear Put Spread
  Any  signal + VIX > 20        →  Long Straddle (high-vol play)
  No   signal + VIX ∈ [12, 18]  →  Iron Condor (neutral premium collection)

Trade placement order (for live deployment, not backtest):
  ALWAYS place BUY order first, then SELL order. Dhan hedge margin rules
  require the long leg on the books before the short leg to get margin benefit.

Usage:
  python3 backtest_spreads.py                                # run all strategies
  python3 backtest_spreads.py --strategy bull_call_spread    # single strategy
  python3 backtest_spreads.py --ml                           # use signals_ml.csv
  python3 backtest_spreads.py --adaptive                     # regime router
  python3 backtest_spreads.py --compare                      # spread vs naked
"""
import argparse
import os
import sys
from datetime import time as _dtime

import numpy as np
import pandas as pd

from backtest_engine import (
    DATA_DIR, INTRADAY_CACHE_DIR, LOT_SIZE, STARTING_CAPITAL, MONTHLY_TOPUP,
    PREMIUM_K, MAX_LOTS, RISK_PCT,
    _otm_params, load_signals, load_bn_ohlcv, get_lot_size, get_dte,
    calculate_charges, fmt_inr,
)

# ── Strategy registry ──────────────────────────────────────────────────────────
# Each strategy defines its legs (long/short) and the cache-file suffix to load.
# offset = strike offset in 100-pt units (0=ATM, +3=ATM+300, -3=ATM-300).
# file_suffix maps to fetch_intraday_options.py cache naming:
#   None        → "{date}_{opt_code}.csv"          (ATM, backward-compat)
#   "p3"        → "{date}_CE_p3.csv"               (ATM+3 CE)
#   "m3"        → "{date}_PE_m3.csv"               (ATM-3 PE)
#   "straddle"  → "{date}_PE_straddle.csv"         (ATM PE on CALL day, etc.)

STRATEGIES = {
    "bull_call_spread": {
        "name":          "Bull Call Spread",
        "direction":     "BULLISH",
        "signal_match":  ["CALL"],
        "spread_width":  300,                     # BN pts between strikes
        "legs": [
            # (opt_type, offset_100pts, action, cache_suffix_CALL, cache_suffix_PUT)
            ("CE",  0, "BUY",   None, None),      # Long ATM CE  (on CALL day)
            ("CE", +3, "SELL",  "p3", None),      # Short ATM+3 CE
        ],
        "vix_min":       10.0,
        "vix_max":       20.0,
        "entry_debit":   True,                    # net debit position
        "sl_frac":       0.60,                    # exit if lost 60% of debit
        "tp_frac":       0.50,                    # exit at 50% of max profit
    },
    "bear_put_spread": {
        "name":          "Bear Put Spread",
        "direction":     "BEARISH",
        "signal_match":  ["PUT"],
        "spread_width":  300,
        "legs": [
            ("PE",  0, "BUY",   None, None),      # Long ATM PE  (on PUT day)
            ("PE", -3, "SELL",  None, "m3"),      # Short ATM-3 PE
        ],
        "vix_min":       10.0,
        "vix_max":       20.0,
        "entry_debit":   True,
        "sl_frac":       0.60,
        "tp_frac":       0.50,
    },
    "long_straddle": {
        "name":          "Long Straddle",
        "direction":     "VOLATILE",
        "signal_match":  ["CALL", "PUT"],
        "spread_width":  None,
        "legs": [
            # Straddle: two longs (CE + PE). Suffix depends on signal day.
            ("CE",  0, "BUY",   None,       "straddle"),  # ATM CE
            ("PE",  0, "BUY",   "straddle", None),        # ATM PE
        ],
        "vix_min":       20.0,
        "vix_max":       99.0,
        "entry_debit":   True,
        "sl_frac":       0.50,
        "tp_frac":       1.00,                    # straddle has no defined max
    },
}


# ── Leg loader ────────────────────────────────────────────────────────────────

def _leg_cache_path(date, signal, opt_type, cache_suffix_call, cache_suffix_put):
    """
    Resolve cache file path for one leg on a given signal day.
    cache_suffix_call applies on CALL signal days; cache_suffix_put on PUT days.
    Returns None if suffix undefined for this signal (leg doesn't exist that day).
    """
    if signal == "CALL":
        suffix = cache_suffix_call
    else:
        suffix = cache_suffix_put

    date_str = f"{date:%Y-%m-%d}"
    if suffix is None:
        return os.path.join(INTRADAY_CACHE_DIR, f"{date_str}_{opt_type}.csv")
    return os.path.join(INTRADAY_CACHE_DIR, f"{date_str}_{opt_type}_{suffix}.csv")


def _load_leg_bars(path):
    """Load 1-min bars for one leg. Returns DataFrame or None if missing."""
    if not path or not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, parse_dates=["dt"])
        return df if not df.empty else None
    except Exception:
        return None


def _estimate_leg_from_atm(atm_bars, offset_100pts, bn_open, dte_days):
    """
    Fallback: estimate OTM leg bars from real ATM bars using Black-Scholes
    delta scaling. Each ATM bar's open/high/low/close is multiplied by the
    OTM premium factor (extrinsic ratio).

    offset_100pts: +3 = 300pts OTM for CE, -3 = 300pts OTM for PE.
                   The sign convention matches _otm_params (CE convention).
                   For PE OTM, caller passes -3 (PE below spot).

    Returns DataFrame with same columns as atm_bars, or None if atm_bars None.
    """
    if atm_bars is None or atm_bars.empty:
        return None

    # For PE legs: PE below spot is OTM. _otm_params uses CE convention
    # (positive=OTM for CE). For PE OTM (below spot, negative offset in our
    # convention), we flip sign so _otm_params returns the correct extrinsic.
    effective_offset = abs(offset_100pts) if offset_100pts != 0 else 0
    pf, _ = _otm_params(effective_offset, bn_open, dte_days)

    est = atm_bars.copy()
    for col in ("open", "high", "low", "close"):
        est[col] = est[col] * pf
    est["_estimated"] = True
    return est


# ── Spread simulator ──────────────────────────────────────────────────────────

def simulate_spread_trade(row, bn_ohlcv, capital, strategy,
                          entry_time="09:30", exit_time="15:15",
                          lot_size=None, allow_estimate=False):
    """
    Simulate one spread trade using real 1-min bars for each leg (or
    delta-scaled estimates when OTM cache missing).

    Returns dict with trade results. On skip: result="SKIPPED".
    """
    date     = row["date"]
    signal   = str(row.get("signal", "")).upper()
    ls       = lot_size if lot_size is not None else get_lot_size(date)
    dte      = get_dte(date)
    bn_open  = bn_ohlcv.loc[date, "open"] if date in bn_ohlcv.index else None

    zero_result = {
        "date": date.date(), "signal": signal, "strategy": strategy["name"],
        "result": "SKIPPED", "entry_debit": 0.0, "exit_value": 0.0,
        "pnl": 0.0, "lots": 0, "bn_open": bn_open,
        "legs_real": 0, "legs_estimated": 0,
        "long_entry": 0.0, "short_entry": 0.0,
    }

    if signal not in strategy["signal_match"] or bn_open is None:
        return zero_result

    # Load each leg's bars
    leg_bars  = []
    n_real    = 0
    n_est     = 0
    leg_specs = strategy["legs"]

    # Load ATM CE (for the day) once — needed as anchor for estimation
    atm_ce_path = os.path.join(INTRADAY_CACHE_DIR,
                               f"{date:%Y-%m-%d}_CE.csv") if signal == "CALL" \
                  else os.path.join(INTRADAY_CACHE_DIR,
                                    f"{date:%Y-%m-%d}_CE_straddle.csv")
    atm_pe_path = os.path.join(INTRADAY_CACHE_DIR,
                               f"{date:%Y-%m-%d}_PE.csv") if signal == "PUT" \
                  else os.path.join(INTRADAY_CACHE_DIR,
                                    f"{date:%Y-%m-%d}_PE_straddle.csv")
    atm_ce_bars = _load_leg_bars(atm_ce_path)
    atm_pe_bars = _load_leg_bars(atm_pe_path)

    for opt_type, offset, action, suffix_call, suffix_put in leg_specs:
        path = _leg_cache_path(date, signal, opt_type, suffix_call, suffix_put)
        bars = _load_leg_bars(path)

        if bars is not None:
            n_real += 1
        elif allow_estimate:
            # Estimate from real ATM anchor — UNRELIABLE.
            # BS formula understates skew; debit comes out 10-20× too low.
            # Only used when --allow-estimate explicitly passed.
            anchor = atm_ce_bars if opt_type == "CE" else atm_pe_bars
            bars   = _estimate_leg_from_atm(anchor, offset, bn_open, dte)
            if bars is not None:
                n_est += 1
        else:
            # Default: refuse to fabricate. Skip day until OTM cache built.
            return {**zero_result, "result": "SKIPPED_NO_OTM_CACHE",
                    "legs_real": n_real, "legs_estimated": n_est}

        if bars is None:
            return {**zero_result, "result": "SKIPPED_NO_CACHE"}

        leg_bars.append({
            "opt_type": opt_type, "offset": offset, "action": action,
            "bars": bars.sort_values("dt").reset_index(drop=True),
        })

    # Parse entry/exit clock times
    eh, em = [int(x) for x in entry_time.split(":")]
    xh, xm = [int(x) for x in exit_time.split(":")]
    entry_t = _dtime(eh, em)
    exit_t  = _dtime(xh, xm)

    # Align all legs to common time grid (inner join on dt)
    aligned = leg_bars[0]["bars"][["dt", "open", "close", "high", "low"]] \
        .rename(columns={"open": f"o0", "close": f"c0",
                         "high": f"h0", "low": f"l0"})
    for i in range(1, len(leg_bars)):
        other = leg_bars[i]["bars"][["dt", "open", "close", "high", "low"]] \
            .rename(columns={"open": f"o{i}", "close": f"c{i}",
                             "high":  f"h{i}", "low":   f"l{i}"})
        aligned = aligned.merge(other, on="dt", how="inner")

    if aligned.empty:
        return {**zero_result, "result": "SKIPPED_NO_OVERLAP"}

    # Filter to trading window [entry_time, exit_time]
    times      = aligned["dt"].dt.time
    entry_mask = times >= entry_t
    exit_mask  = times <= exit_t
    aligned    = aligned[entry_mask & exit_mask].reset_index(drop=True)
    if aligned.empty:
        return {**zero_result, "result": "SKIPPED_NO_BARS"}

    # ── Entry: net debit = sum of (sign × leg_open) where sign=+1 for BUY, -1 for SELL
    first = aligned.iloc[0]
    entry_prices = []
    for i, leg in enumerate(leg_bars):
        px = float(first[f"o{i}"])
        if px <= 0 or pd.isna(px):
            return {**zero_result, "result": "SKIPPED_BAD_ENTRY"}
        entry_prices.append(px)

    net_debit = 0.0   # positive = we pay (debit), negative = we receive (credit)
    for i, leg in enumerate(leg_bars):
        sign = +1 if leg["action"] == "BUY" else -1
        net_debit += sign * entry_prices[i]

    if net_debit <= 0 and strategy.get("entry_debit", True):
        return {**zero_result, "result": "SKIPPED_INVERTED_DEBIT"}

    # Position sizing: risk RISK_PCT of capital against max loss per lot
    # For debit spread: max loss = net_debit × lot_size
    max_loss_per_lot = net_debit * ls
    if max_loss_per_lot <= 0:
        return {**zero_result, "result": "SKIPPED_ZERO_RISK"}
    lots = min(int((capital * RISK_PCT) / max_loss_per_lot), MAX_LOTS)
    if lots < 1:
        # Fallback: 1 lot if margin-affordable (net_debit × ls < 85% of capital)
        if max_loss_per_lot <= capital * 0.85:
            lots = 1
        else:
            return {**zero_result, "result": "SKIPPED_LOW_CAPITAL",
                    "entry_debit": round(net_debit, 2),
                    "long_entry": round(entry_prices[0], 2),
                    "short_entry": (round(entry_prices[1], 2) if len(entry_prices) > 1 else 0),
                    "legs_real": n_real, "legs_estimated": n_est}

    # SL / TP on spread value
    sl_frac = strategy.get("sl_frac", 0.60)
    tp_frac = strategy.get("tp_frac", 0.50)
    spread_width = strategy.get("spread_width")

    sl_value = net_debit * (1.0 - sl_frac)   # exit if spread drops to this
    if spread_width is not None:
        max_profit = spread_width - net_debit
        tp_value   = net_debit + max_profit * tp_frac
    else:
        # Straddle: TP when spread gains tp_frac × net_debit (since no cap)
        tp_value   = net_debit * (1.0 + tp_frac)

    # Walk bars: compute spread value each minute, check SL/TP
    exit_value = None
    result     = "EOD"
    for _, bar in aligned.iterrows():
        # Intra-bar high/low of the spread — approximate as high-of-longs + low-of-shorts etc.
        # Simpler: use close-of-bar as spread value (conservative, 1-min granularity).
        bar_value = 0.0
        for i, leg in enumerate(leg_bars):
            sign = +1 if leg["action"] == "BUY" else -1
            bar_value += sign * float(bar[f"c{i}"])

        if bar_value <= sl_value:
            exit_value = bar_value
            result     = "SL"
            break
        if bar_value >= tp_value:
            exit_value = bar_value
            result     = "TP"
            break

    if exit_value is None:
        # EOD forced exit
        last = aligned.iloc[-1]
        exit_value = 0.0
        for i, leg in enumerate(leg_bars):
            sign = +1 if leg["action"] == "BUY" else -1
            exit_value += sign * float(last[f"c{i}"])
        result = "EOD"

    # P&L: (exit_value - entry_value) × lots × lot_size
    # For debit: entry_value > 0 (paid), exit_value > entry → profit, < entry → loss
    gross = (exit_value - net_debit) * lots * ls

    # Charges: 2 legs × 2 orders (entry + exit) = 4 orders total
    # Approximate using the largest leg premium for charges (conservative)
    max_leg_px = max(entry_prices)
    charges    = calculate_charges(max_leg_px, lots, lot_size=ls) * len(leg_bars)

    pnl = gross - charges

    return {
        "date":           date.date(),
        "signal":         signal,
        "strategy":       strategy["name"],
        "result":         result,      # SL / TP / EOD
        "entry_debit":    round(net_debit, 2),
        "exit_value":     round(exit_value, 2),
        "pnl":            round(pnl, 2),
        "lots":           lots,
        "lot_size":       ls,
        "bn_open":        round(bn_open, 2),
        "long_entry":     round(entry_prices[0], 2),
        "short_entry":    round(entry_prices[1], 2) if len(entry_prices) > 1 else 0.0,
        "legs_real":      n_real,
        "legs_estimated": n_est,
        "charges":        round(charges, 2),
    }


# ── Backtest loop ─────────────────────────────────────────────────────────────

def run_spread_backtest(strategy_key, ml=False, adaptive=False,
                        entry_time="09:30", exit_time="15:15",
                        allow_estimate=False):
    """
    Run spread backtest for ONE strategy (or adaptive routing).

    adaptive=True ignores strategy_key and dispatches each day to the
    strategy matching its signal + VIX regime.
    """
    signals  = load_signals(ml=ml)
    bn_ohlcv = load_bn_ohlcv()

    # VIX for regime routing
    vix_path = f"{DATA_DIR}/india_vix.csv"
    vix_df   = pd.read_csv(vix_path, parse_dates=["date"]) \
                 .set_index("date") if os.path.exists(vix_path) else None

    capital       = STARTING_CAPITAL
    current_month = None
    trades        = []

    for _, row in signals.iterrows():
        date  = row["date"]
        mkey  = (date.year, date.month)
        if current_month is None:
            current_month = mkey
        elif mkey != current_month:
            capital      += MONTHLY_TOPUP
            current_month = mkey

        # Select strategy
        if adaptive:
            vix_val = (float(vix_df.loc[date, "close"])
                       if vix_df is not None and date in vix_df.index
                       else 15.0)   # default mid-range if missing
            strategy = _route_strategy(row["signal"], vix_val)
            if strategy is None:
                continue            # no strategy for this regime — skip
        else:
            strategy = STRATEGIES[strategy_key]

        trade = simulate_spread_trade(
            row, bn_ohlcv, capital, strategy,
            entry_time=entry_time, exit_time=exit_time,
            allow_estimate=allow_estimate,
        )
        trade["capital_before"] = round(capital, 2)
        capital += trade["pnl"]
        trade["capital_after"]  = round(capital, 2)
        trades.append(trade)

    return pd.DataFrame(trades)


def _route_strategy(signal, vix_val):
    """
    Adaptive router: map (signal, VIX) → strategy.
    Returns None if no strategy fits (day is skipped).
    """
    sig = str(signal).upper()
    # High-vol → straddle regardless of direction
    if vix_val >= 20.0 and sig in ("CALL", "PUT"):
        return STRATEGIES["long_straddle"]
    # Directional signal + normal vol → directional spread
    if sig == "CALL" and 10.0 <= vix_val < 20.0:
        return STRATEGIES["bull_call_spread"]
    if sig == "PUT" and 10.0 <= vix_val < 20.0:
        return STRATEGIES["bear_put_spread"]
    return None


# ── Summary printer ───────────────────────────────────────────────────────────

def print_spread_summary(trade_df, strategy_name):
    active     = trade_df[trade_df["result"].isin(["SL", "TP", "EOD"])].copy()
    n_no_otm   = (trade_df["result"] == "SKIPPED_NO_OTM_CACHE").sum()
    n_no_cache = (trade_df["result"] == "SKIPPED_NO_CACHE").sum()
    if active.empty:
        print(f"\n{'='*80}")
        print(f"  {strategy_name}: no active trades")
        print(f"  Skipped: {len(trade_df)} total")
        if n_no_otm > 0:
            print(f"    - {n_no_otm} skipped: OTM cache missing")
            print(f"      Run: python3 fetch_intraday_options.py --spreads --start 2021-08-01")
        if n_no_cache > 0:
            print(f"    - {n_no_cache} skipped: ATM anchor missing")
        print(f"{'='*80}")
        return

    n         = len(active)
    wins      = active[active["pnl"] > 0]
    losses    = active[active["pnl"] <= 0]
    tp_count  = (active["result"] == "TP").sum()
    sl_count  = (active["result"] == "SL").sum()
    eod_count = (active["result"] == "EOD").sum()

    total_pnl = active["pnl"].sum()
    avg_win   = wins["pnl"].mean() if len(wins) else 0
    avg_loss  = losses["pnl"].mean() if len(losses) else 0
    wr        = len(wins) / n * 100 if n else 0

    avg_debit = active["entry_debit"].mean()
    avg_lots  = active["lots"].mean()
    total_real = active["legs_real"].sum()
    total_est  = active["legs_estimated"].sum()
    real_pct   = total_real / (total_real + total_est) * 100 if (total_real + total_est) else 0

    cap = trade_df["capital_after"]
    dd  = ((cap - cap.cummax()) / cap.cummax() * 100).min()

    # Normalize to 1-lot equivalent
    pnl_1lot = (active["pnl"] / active["lots"]).sum() if avg_lots > 0 else 0

    print(f"\n{'='*80}")
    print(f"  {strategy_name}")
    print(f"{'='*80}")
    print(f"  Active trades:    {n}  (skipped: {len(trade_df) - n})")
    print(f"  Outcomes:         TP={tp_count}  SL={sl_count}  EOD={eod_count}")
    print(f"  Win rate:         {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Avg win:          ₹{avg_win:,.0f}")
    print(f"  Avg loss:         ₹{avg_loss:,.0f}")
    print(f"  Total P&L:        {fmt_inr(total_pnl)}  (end cap: {fmt_inr(cap.iloc[-1])})")
    print(f"  1-lot P&L:        {fmt_inr(pnl_1lot)}  (sum of pnl/lots)")
    print(f"  Max drawdown:     {dd:.1f}%")
    print(f"  Avg debit/trade:  ₹{avg_debit:.1f}  ×  {avg_lots:.1f} lots avg")
    print(f"  Real-leg coverage: {real_pct:.0f}%  ({total_real} real / {total_est} estimated)")
    if total_est > 0:
        print(f"  ⚠️  ESTIMATED LEGS BIAS RESULTS — BS understates debit ~10-20×")
        print(f"     Real run: drop --allow-estimate, build OTM cache first:")
        print(f"     python3 fetch_intraday_options.py --spreads --start 2021-08-01")
    if n_no_otm > 0:
        print(f"  Skipped (no OTM cache): {n_no_otm} days — fetch in progress?")
    print(f"{'='*80}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=list(STRATEGIES.keys()) + ["all"],
                    default="all",
                    help="Strategy to backtest (default: all)")
    ap.add_argument("--ml", action="store_true",
                    help="Use signals_ml.csv instead of signals.csv")
    ap.add_argument("--adaptive", action="store_true",
                    help="Regime router: pick strategy per day from signal+VIX")
    ap.add_argument("--entry", default="09:30", help="Entry time HH:MM IST")
    ap.add_argument("--exit",  default="15:15", help="Exit time HH:MM IST")
    ap.add_argument("--save",  default=None,
                    help="CSV path to save trade log (default: don't save)")
    ap.add_argument("--allow-estimate", action="store_true",
                    help="Estimate OTM legs from ATM via Black-Scholes when "
                         "cache missing. UNRELIABLE — debit understated 10-20× "
                         "due to vol skew. Default: skip days without real OTM.")
    args = ap.parse_args()

    print(f"\nSpread backtest  |  signals={'ML' if args.ml else 'rule'}  "
          f"|  entry={args.entry}  exit={args.exit}")

    if args.allow_estimate:
        print("⚠️  --allow-estimate ON: OTM legs filled by BS formula when missing.")
        print("    These results UNDERSTATE debit ~10-20× due to vol skew. Trust real-only runs.\n")

    if args.adaptive:
        print(f"Adaptive regime router: signal+VIX → strategy")
        df = run_spread_backtest(None, ml=args.ml, adaptive=True,
                                 entry_time=args.entry, exit_time=args.exit,
                                 allow_estimate=args.allow_estimate)
        print_spread_summary(df, "Adaptive (regime router)")
        if args.save:
            df.to_csv(args.save, index=False)
            print(f"  Trade log: {args.save}")
        return

    strategies = (list(STRATEGIES.keys()) if args.strategy == "all"
                  else [args.strategy])
    for key in strategies:
        df = run_spread_backtest(key, ml=args.ml,
                                 entry_time=args.entry, exit_time=args.exit,
                                 allow_estimate=args.allow_estimate)
        print_spread_summary(df, STRATEGIES[key]["name"])
        if args.save and len(strategies) == 1:
            df.to_csv(args.save, index=False)
            print(f"  Trade log: {args.save}")


if __name__ == "__main__":
    main()
