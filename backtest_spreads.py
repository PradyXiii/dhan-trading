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
Multi-leg options spread backtester for Nifty50.

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
    _otm_params, load_signals, load_nf_ohlcv, get_lot_size, get_dte,
    calculate_charges, fmt_inr,
    NIFTY_CACHE_DIR, get_nifty_lot_size, load_nifty_ohlcv,
)

NF_CACHE_DIR = NIFTY_CACHE_DIR

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
    # ── Credit spreads: SELL ATM + BUY OTM wing — theta-positive ──────────────
    # These trade AGAINST the signal direction (premium fade).
    # On a CALL signal day, BN is expected to rise — but ATM CE premium is rich.
    # Selling it (Bear Call) profits if BN stays below ATM+300pts by EOD.
    # Historical WR target: >71% (breakeven at credit=87, max_loss=213, 300pt spread).
    "bear_call_credit": {
        "name":          "Bear Call Spread (credit)",
        "direction":     "FADE_CALL",
        "signal_match":  ["CALL"],
        "spread_width":  300,
        "legs": [
            ("CE",  0, "SELL",  None, None),      # Short ATM CE  (collect premium)
            ("CE", +3, "BUY",   "p3", None),      # Long ATM+3 CE (protection wing)
        ],
        "entry_debit":   False,                   # net credit (we receive money)
        "sl_frac":       0.50,                    # stop if loss = 50% of credit received
        "tp_frac":       0.65,                    # take profit at 65% of credit captured (1.3× RR)
    },
    "bull_put_credit": {
        "name":          "Bull Put Spread (credit)",
        "direction":     "FADE_PUT",
        "signal_match":  ["PUT"],
        "spread_width":  300,
        "legs": [
            ("PE",  0, "SELL",  None, None),      # Short ATM PE  (collect premium)
            ("PE", -3, "BUY",   None, "m3"),      # Long ATM-3 PE (protection wing)
        ],
        "entry_debit":   False,
        "sl_frac":       0.50,
        "tp_frac":       0.65,
    },
    # ── Iron Condor: sell premium on BOTH sides simultaneously ────────────────
    # Collect Bear Call credit + Bull Put credit in one trade.
    # Profit zone: BN stays between (ATM-300) and (ATM+300) by EOD.
    # Net credit ≈ 2× single-side credit; max-loss ≈ max(each side's net loss)
    # because BN can only blow through one side (not both simultaneously).
    # Requires cache files: CE.csv, CE_p3.csv, PE_straddle.csv, PE_m3_straddle.csv
    #   on CALL days; CE_straddle.csv, CE_p3_straddle.csv, PE.csv, PE_m3.csv on PUT days.
    # Fetch: python3 fetch_intraday_options.py --spreads --start 2021-08-01
    "iron_condor": {
        "name":          "Iron Condor",
        "direction":     "NEUTRAL",
        "signal_match":  ["CALL", "PUT"],
        "spread_width":  300,
        "legs": [
            # Bear Call side (upper wing)
            ("CE",  0, "SELL",  None,           "straddle"),      # Short ATM CE
            ("CE", +3, "BUY",   "p3",           "p3_straddle"),   # Long  ATM+3 CE
            # Bull Put side (lower wing)
            ("PE",  0, "SELL",  "straddle",     None),            # Short ATM PE
            ("PE", -3, "BUY",   "m3_straddle",  "m3"),            # Long  ATM-3 PE
        ],
        "entry_debit":   False,
        "sl_frac":       0.50,   # SL when either wing's loss = 50% of total credit
        "tp_frac":       0.65,   # TP at 65% of total credit captured
        "max_lots":      10,     # half max_lots — IC ties up margin on both sides
    },
    # ── Naked (single-leg) BNF options — for comparison baseline ─────────────
    "bnf_naked_call": {
        "name":          "BNF Naked Call",
        "direction":     "BULLISH",
        "signal_match":  ["CALL"],
        "spread_width":  None,
        "legs": [
            ("CE", 0, "BUY", None, None),
        ],
        "entry_debit":   True,
        "sl_frac":       0.50,   # exit at 50% premium loss
        "tp_frac":       1.00,   # exit at 2× premium (100% gain)
    },
    "bnf_naked_put": {
        "name":          "BNF Naked Put",
        "direction":     "BEARISH",
        "signal_match":  ["PUT"],
        "spread_width":  None,
        "legs": [
            ("PE", 0, "BUY", None, None),
        ],
        "entry_debit":   True,
        "sl_frac":       0.50,
        "tp_frac":       1.00,
    },
}

# ── Nifty50 strategy registry ─────────────────────────────────────────────────
# NF strike spacing = 50pts → ATM±3 = ±150pts (spread_width=150).
# Cache naming is identical to BNF ("p3", "m3", "straddle") but files live
# in NF_CACHE_DIR (data/nifty_options_cache/).
# Fetch: python3 fetch_intraday_options.py --instrument NF --spreads --start 2021-08-01

NIFTY_STRATEGIES = {
    "nf_naked_call": {
        "name":          "NF Naked Call",
        "direction":     "BULLISH",
        "signal_match":  ["CALL"],
        "spread_width":  None,
        "legs": [
            ("CE", 0, "BUY", None, None),
        ],
        "entry_debit":   True,
        "sl_frac":       0.50,
        "tp_frac":       1.00,
    },
    "nf_naked_put": {
        "name":          "NF Naked Put",
        "direction":     "BEARISH",
        "signal_match":  ["PUT"],
        "spread_width":  None,
        "legs": [
            ("PE", 0, "BUY", None, None),
        ],
        "entry_debit":   True,
        "sl_frac":       0.50,
        "tp_frac":       1.00,
    },
    "nf_bull_call_spread": {
        "name":          "NF Bull Call Spread",
        "direction":     "BULLISH",
        "signal_match":  ["CALL"],
        "spread_width":  150,
        "legs": [
            ("CE",  0, "BUY",   None, None),
            ("CE", +3, "SELL",  "p3", None),
        ],
        "vix_min":       10.0,
        "vix_max":       20.0,
        "entry_debit":   True,
        "sl_frac":       0.60,
        "tp_frac":       0.50,
    },
    "nf_bear_put_spread": {
        "name":          "NF Bear Put Spread",
        "direction":     "BEARISH",
        "signal_match":  ["PUT"],
        "spread_width":  150,
        "legs": [
            ("PE",  0, "BUY",   None, None),
            ("PE", -3, "SELL",  None, "m3"),
        ],
        "vix_min":       10.0,
        "vix_max":       20.0,
        "entry_debit":   True,
        "sl_frac":       0.60,
        "tp_frac":       0.50,
    },
    "nf_bear_call_credit": {
        "name":          "NF Bear Call Spread (credit)",
        "direction":     "FADE_CALL",
        "signal_match":  ["CALL"],
        "spread_width":  150,
        "legs": [
            ("CE",  0, "SELL",  None, None),
            ("CE", +3, "BUY",   "p3", None),
        ],
        "entry_debit":   False,
        "sl_frac":       0.50,
        "tp_frac":       0.65,
    },
    "nf_bull_put_credit": {
        "name":          "NF Bull Put Spread (credit)",
        "direction":     "FADE_PUT",
        "signal_match":  ["PUT"],
        "spread_width":  150,
        "legs": [
            ("PE",  0, "SELL",  None, None),
            ("PE", -3, "BUY",   None, "m3"),
        ],
        "entry_debit":   False,
        "sl_frac":       0.50,
        "tp_frac":       0.65,
    },
    "nf_long_straddle": {
        "name":          "NF Long Straddle",
        "direction":     "VOLATILE",
        "signal_match":  ["CALL", "PUT"],
        "spread_width":  None,
        "legs": [
            ("CE",  0, "BUY",   None,       "straddle"),
            ("PE",  0, "BUY",   "straddle", None),
        ],
        "vix_min":       20.0,
        "vix_max":       99.0,
        "entry_debit":   True,
        "sl_frac":       0.50,
        "tp_frac":       1.00,
    },
    "nf_iron_condor": {
        "name":          "NF Iron Condor",
        "direction":     "NEUTRAL",
        "signal_match":  ["CALL", "PUT"],
        "spread_width":  150,
        "legs": [
            ("CE",  0, "SELL",  None,           "straddle"),
            ("CE", +3, "BUY",   "p3",           "p3_straddle"),
            ("PE",  0, "SELL",  "straddle",     None),
            ("PE", -3, "BUY",   "m3_straddle",  "m3"),
        ],
        "entry_debit":   False,
        "sl_frac":       0.50,
        "tp_frac":       0.65,
        "max_lots":      10,
    },
}


# ── Leg loader ────────────────────────────────────────────────────────────────

def _leg_cache_path(date, signal, opt_type, cache_suffix_call, cache_suffix_put,
                    cache_dir=None):
    """
    Resolve cache file path for one leg on a given signal day.
    cache_suffix_call applies on CALL signal days; cache_suffix_put on PUT days.
    Returns None if suffix undefined for this signal (leg doesn't exist that day).
    """
    if cache_dir is None:
        cache_dir = INTRADAY_CACHE_DIR
    if signal == "CALL":
        suffix = cache_suffix_call
    else:
        suffix = cache_suffix_put

    date_str = f"{date:%Y-%m-%d}"
    if suffix is None:
        return os.path.join(cache_dir, f"{date_str}_{opt_type}.csv")
    return os.path.join(cache_dir, f"{date_str}_{opt_type}_{suffix}.csv")

_LEG_BAR_CACHE: dict = {}   # path → DataFrame (or None); persists for process lifetime
_DATA_CACHE:    dict = {}   # keyed by ("signals", ml) / "nf_ohlcv" / "vix_df"


def _load_leg_bars(path):
    """Load 1-min bars for one leg. Returns DataFrame or None if missing."""
    if not path:
        return None
    if path in _LEG_BAR_CACHE:
        return _LEG_BAR_CACHE[path]
    result = None
    if os.path.exists(path):
        try:
            df = pd.read_csv(path, parse_dates=["dt"])
            result = df if not df.empty else None
        except Exception:
            pass
    _LEG_BAR_CACHE[path] = result
    return result


def _estimate_leg_from_atm(atm_bars, offset_100pts, nf_open, dte_days):
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
    pf, _ = _otm_params(effective_offset, nf_open, dte_days)

    est = atm_bars.copy()
    for col in ("open", "high", "low", "close"):
        est[col] = est[col] * pf
    est["_estimated"] = True
    return est


# ── Spread simulator ──────────────────────────────────────────────────────────

def simulate_spread_trade(row, nf_ohlcv, capital, strategy,
                          entry_time="09:30", exit_time="15:15",
                          lot_size=None, allow_estimate=False, vix_val=None,
                          cache_dir=None, lot_size_fn=None):
    """
    Simulate one spread trade using real 1-min bars for each leg (or
    delta-scaled estimates when OTM cache missing).

    cache_dir: override default INTRADAY_CACHE_DIR (pass NF_CACHE_DIR for Nifty).
    lot_size_fn: callable(date) → int; defaults to get_lot_size (Nifty50).
    Returns dict with trade results. On skip: result="SKIPPED".
    """
    if cache_dir is None:
        cache_dir = INTRADAY_CACHE_DIR
    if lot_size_fn is None:
        lot_size_fn = get_lot_size
    date     = row["date"]
    signal   = str(row.get("signal", "")).upper()
    ls       = lot_size if lot_size is not None else lot_size_fn(date)
    dte      = get_dte(date)
    nf_open  = nf_ohlcv.loc[date, "open"] if date in nf_ohlcv.index else None

    zero_result = {
        "date": date.date(), "signal": signal, "strategy": strategy["name"],
        "result": "SKIPPED", "entry_debit": 0.0, "exit_value": 0.0,
        "pnl": 0.0, "lots": 0, "nf_open": nf_open,
        "legs_real": 0, "legs_estimated": 0,
        "long_entry": 0.0, "short_entry": 0.0,
    }

    if signal not in strategy["signal_match"] or nf_open is None:
        return zero_result

    # VIX regime filter (if configured on strategy)
    vix_min = strategy.get("vix_min")
    vix_max = strategy.get("vix_max")
    if (vix_min is not None or vix_max is not None) and vix_val is not None:
        if vix_min is not None and vix_val < vix_min:
            return {**zero_result, "result": "SKIPPED_VIX_LOW"}
        if vix_max is not None and vix_val > vix_max:
            return {**zero_result, "result": "SKIPPED_VIX_HIGH"}

    # DTE filter (if configured on strategy)
    dte_min = strategy.get("dte_min")
    dte_max = strategy.get("dte_max")
    if dte_min is not None and dte < dte_min:
        return {**zero_result, "result": "SKIPPED_DTE_LOW"}
    if dte_max is not None and dte > dte_max:
        return {**zero_result, "result": "SKIPPED_DTE_HIGH"}

    # Load each leg's bars
    leg_bars  = []
    n_real    = 0
    n_est     = 0
    leg_specs = strategy["legs"]

    # Load ATM CE/PE once — needed as anchor for delta-scaled estimation fallback
    atm_ce_path = os.path.join(cache_dir,
                               f"{date:%Y-%m-%d}_CE.csv") if signal == "CALL" \
                  else os.path.join(cache_dir,
                                    f"{date:%Y-%m-%d}_CE_straddle.csv")
    atm_pe_path = os.path.join(cache_dir,
                               f"{date:%Y-%m-%d}_PE.csv") if signal == "PUT" \
                  else os.path.join(cache_dir,
                                    f"{date:%Y-%m-%d}_PE_straddle.csv")
    atm_ce_bars = _load_leg_bars(atm_ce_path)
    atm_pe_bars = _load_leg_bars(atm_pe_path)

    for opt_type, offset, action, suffix_call, suffix_put in leg_specs:
        path = _leg_cache_path(date, signal, opt_type, suffix_call, suffix_put,
                               cache_dir=cache_dir)
        bars = _load_leg_bars(path)

        if bars is not None:
            n_real += 1
        elif allow_estimate:
            # Estimate from real ATM anchor — UNRELIABLE.
            # BS formula understates skew; debit comes out 10-20× too low.
            # Only used when --allow-estimate explicitly passed.
            anchor = atm_ce_bars if opt_type == "CE" else atm_pe_bars
            bars   = _estimate_leg_from_atm(anchor, offset, nf_open, dte)
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

    # SL / TP on spread value — debit vs credit logic differs
    sl_frac      = strategy.get("sl_frac", 0.60)
    tp_frac      = strategy.get("tp_frac", 0.50)
    spread_width = strategy.get("spread_width")
    is_credit    = (not strategy.get("entry_debit", True)) and (net_debit < 0)

    if is_credit:
        # Credit spread: we received |net_debit| as premium. Theta helps us.
        # SL fires if unrealized loss exceeds sl_frac × credit (spread expanded).
        # TP fires if we've retained (1 - tp_frac) × credit or less of exposure.
        net_credit       = abs(net_debit)
        max_loss_per_lot = net_credit * sl_frac * ls
        if max_loss_per_lot <= 0:
            return {**zero_result, "result": "SKIPPED_ZERO_RISK"}
        max_lots_cap = strategy.get("max_lots", MAX_LOTS)
        lots = min(int((capital * RISK_PCT) / max_loss_per_lot), max_lots_cap)
        if lots < 1:
            if max_loss_per_lot <= capital * 0.85:
                lots = 1
            else:
                return {**zero_result, "result": "SKIPPED_LOW_CAPITAL",
                        "entry_debit": round(net_debit, 2),
                        "long_entry": round(entry_prices[0], 2),
                        "short_entry": (round(entry_prices[1], 2) if len(entry_prices) > 1 else 0),
                        "legs_real": n_real, "legs_estimated": n_est}
        # net_debit is negative; more-negative = larger loss; less-negative = profit
        sl_value = net_debit * (1.0 + sl_frac)   # more negative threshold (worse)
        tp_value = net_debit * (1.0 - tp_frac)   # less negative threshold (profit)

    elif net_debit <= 0:
        # Debit spread where legs are inverted (short more than long) — data issue
        return {**zero_result, "result": "SKIPPED_INVERTED_DEBIT"}

    else:
        # Debit spread: we paid net_debit. Max loss = full debit. Theta works against.
        max_loss_per_lot = net_debit * ls
        if max_loss_per_lot <= 0:
            return {**zero_result, "result": "SKIPPED_ZERO_RISK"}
        max_lots_cap = strategy.get("max_lots", MAX_LOTS)
        lots = min(int((capital * RISK_PCT) / max_loss_per_lot), max_lots_cap)
        if lots < 1:
            if max_loss_per_lot <= capital * 0.85:
                lots = 1
            else:
                return {**zero_result, "result": "SKIPPED_LOW_CAPITAL",
                        "entry_debit": round(net_debit, 2),
                        "long_entry": round(entry_prices[0], 2),
                        "short_entry": (round(entry_prices[1], 2) if len(entry_prices) > 1 else 0),
                        "legs_real": n_real, "legs_estimated": n_est}
        sl_value = net_debit * (1.0 - sl_frac)
        if spread_width is not None:
            max_profit = spread_width - net_debit
            tp_value   = net_debit + max_profit * tp_frac
        else:
            tp_value   = net_debit * (1.0 + tp_frac)

    # Walk bars: compute spread value each minute, check SL/TP
    exit_value = None
    result     = "EOD"
    # Vectorized SL/TP scan (replaces iterrows loop — 100× faster)
    bar_values = pd.Series(0.0, index=aligned.index)
    for i, leg in enumerate(leg_bars):
        sign = +1 if leg["action"] == "BUY" else -1
        bar_values += sign * aligned[f"c{i}"]

    sl_mask = bar_values <= sl_value
    tp_mask = bar_values >= tp_value

    first_sl = int(sl_mask.idxmax()) if sl_mask.any() else None
    first_tp = int(tp_mask.idxmax()) if tp_mask.any() else None

    if first_sl is not None and (first_tp is None or first_sl <= first_tp):
        exit_value = float(bar_values.iloc[first_sl])
        result     = "SL"
    elif first_tp is not None:
        exit_value = float(bar_values.iloc[first_tp])
        result     = "TP"
    else:
        exit_value = float(bar_values.iloc[-1])
        result     = "EOD"

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
        "nf_open":        round(nf_open, 2),
        "long_entry":     round(entry_prices[0], 2),
        "short_entry":    round(entry_prices[1], 2) if len(entry_prices) > 1 else 0.0,
        "legs_real":      n_real,
        "legs_estimated": n_est,
        "charges":        round(charges, 2),
    }


# ── Backtest loop ─────────────────────────────────────────────────────────────

def run_spread_backtest(strategy_key, ml=False, adaptive=False,
                        entry_time="09:30", exit_time="15:15",
                        allow_estimate=False, max_dte=None, instrument="BNF",
                        entry_dow=None, start_date=None, end_date=None):
    """
    Run spread backtest for ONE strategy (or adaptive routing).

    instrument: "BNF" (default) or "NF" (Nifty50 weekly options).
    adaptive=True ignores strategy_key and dispatches each day to the
    strategy matching its signal + VIX regime.
    """
    # ── Instrument dispatch ────────────────────────────────────────────────────
    if instrument == "NF":
        all_strategies  = NIFTY_STRATEGIES
        inst_cache_dir  = NF_CACHE_DIR
        inst_lot_fn     = get_nifty_lot_size
        ohlcv_key       = "nf_ohlcv"
        ohlcv_loader    = load_nifty_ohlcv
        route_fn        = _route_nifty_strategy
    else:
        all_strategies  = STRATEGIES
        inst_cache_dir  = INTRADAY_CACHE_DIR
        inst_lot_fn     = get_lot_size
        ohlcv_key       = "nf_ohlcv"
        ohlcv_loader    = load_nf_ohlcv
        route_fn        = _route_strategy

    if ("signals", ml) not in _DATA_CACHE:
        _DATA_CACHE[("signals", ml)] = load_signals(ml=ml)
    signals = _DATA_CACHE[("signals", ml)]
    if ohlcv_key not in _DATA_CACHE:
        _DATA_CACHE[ohlcv_key] = ohlcv_loader()
    ohlcv = _DATA_CACHE[ohlcv_key]

    vix_path = f"{DATA_DIR}/india_vix.csv"
    if "vix_df" not in _DATA_CACHE:
        _DATA_CACHE["vix_df"] = (pd.read_csv(vix_path, parse_dates=["date"])
                                 .set_index("date") if os.path.exists(vix_path) else None)
    vix_df = _DATA_CACHE["vix_df"]

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

        sig = str(row.get("signal", "")).upper()

        # Always look up VIX — needed for trade metadata and ex-post filtering
        vix_val = (float(vix_df.loc[date, "close"])
                   if vix_df is not None and date in vix_df.index
                   else 15.0)

        # Fast path: skip signal mismatches WITHOUT simulate call
        if not adaptive:
            strategy = all_strategies[strategy_key]
            if sig not in strategy["signal_match"]:
                continue
        else:
            strategy = route_fn(sig, vix_val)
            if strategy is None:
                continue

        # Date range filter
        if start_date is not None and date < start_date:
            continue
        if end_date is not None and date > end_date:
            continue

        # Day-of-week filter (0=Mon … 4=Fri)
        if entry_dow is not None and date.weekday() != entry_dow:
            continue

        # Apply DTE cap if requested (copy strategy to avoid mutating global)
        strat_use = strategy
        if max_dte is not None:
            strat_use = {**strategy, "dte_max": max_dte}

        trade = simulate_spread_trade(
            row, ohlcv, capital, strat_use,
            entry_time=entry_time, exit_time=exit_time,
            allow_estimate=allow_estimate, vix_val=vix_val,
            cache_dir=inst_cache_dir, lot_size_fn=inst_lot_fn,
        )
        trade["ml_conf"]        = round(float(row.get("ml_conf", 0.5)), 4)
        trade["vix_at_entry"]   = round(vix_val, 2)
        trade["capital_before"] = round(capital, 2)
        capital += trade["pnl"]
        trade["capital_after"]  = round(capital, 2)
        trades.append(trade)

    return pd.DataFrame(trades)


def _route_strategy(signal, vix_val):
    """
    Adaptive regime router v2: credit-first, IC preferred.

    Regime → strategy:
      VIX < 12              → skip (no premium worth collecting)
      VIX ∈ [12, 22)        → Iron Condor (collect both sides, wider profit zone)
                              Falls back to single-side credit if IC cache missing.
      VIX ≥ 22              → Long Straddle (capture big directional move)
      no signal (NONE)      → skip

    Previous router used directional debit spreads (Bull Call / Bear Put) —
    these showed 14.7% WR, -₹7.8L over 5 years. IC-first replaces them.
    """
    sig = str(signal).upper()
    if sig not in ("CALL", "PUT"):
        return None
    if vix_val < 12.0:
        return None   # insufficient premium across all strikes
    if vix_val >= 22.0:
        return STRATEGIES["long_straddle"]
    # Sweet spot [12, 22): Iron Condor preferred
    return STRATEGIES["iron_condor"]


def _route_nifty_strategy(signal, vix_val):
    """
    Adaptive regime router for Nifty50 (always weekly — no DTE filter needed).
    Same VIX regime logic as BNF router but dispatches to NIFTY_STRATEGIES.
    """
    sig = str(signal).upper()
    if sig not in ("CALL", "PUT"):
        return None
    if vix_val < 12.0:
        return None
    if vix_val >= 22.0:
        return NIFTY_STRATEGIES["nf_long_straddle"]
    return NIFTY_STRATEGIES["nf_iron_condor"]


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
    is_credit_strat = avg_debit < 0
    if is_credit_strat:
        print(f"  Avg credit/trade: ₹{abs(avg_debit):.1f}  ×  {avg_lots:.1f} lots avg")
    else:
        print(f"  Avg debit/trade:  ₹{avg_debit:.1f}  ×  {avg_lots:.1f} lots avg")
    print(f"  Real-leg coverage: {real_pct:.0f}%  ({total_real} real / {total_est} estimated)")
    if total_est > 0:
        print(f"  ⚠️  ESTIMATED LEGS BIAS RESULTS — BS understates debit ~10-20×")
        print(f"     Real run: drop --allow-estimate, build OTM cache first:")
        print(f"     python3 fetch_intraday_options.py --spreads --start 2021-08-01")
    if n_no_otm > 0:
        print(f"  Skipped (no OTM cache): {n_no_otm} days — fetch in progress?")

    # Per day-of-week breakdown
    if "date" in active.columns:
        dow_map = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
        active = active.copy()
        active["_dow"] = pd.to_datetime(active["date"]).dt.weekday
        print(f"\n  Day-of-week breakdown:")
        print(f"  {'Day':<5} {'Trades':>6} {'WR%':>6} {'AvgPnL':>9} {'TP':>4} {'SL':>4} {'EOD':>4}")
        for d in range(5):
            grp = active[active["_dow"] == d]
            if grp.empty:
                continue
            g_wins = (grp["pnl"] > 0).sum()
            g_wr   = g_wins / len(grp) * 100
            g_avg  = grp["pnl"].mean()
            g_tp   = (grp["result"] == "TP").sum()
            g_sl   = (grp["result"] == "SL").sum()
            g_eod  = (grp["result"] == "EOD").sum()
            print(f"  {dow_map[d]:<5} {len(grp):>6} {g_wr:>5.1f}% {g_avg:>9,.0f} {g_tp:>4} {g_sl:>4} {g_eod:>4}")

    print(f"{'='*80}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", choices=["BNF", "NF"], default="BNF",
                    help="BNF=BankNifty legacy, NF=Nifty50 weekly options (default)")
    ap.add_argument("--strategy", default="all",
                    help="Strategy key, or: all / credit / debit / naked. "
                         "BNF keys: " + ", ".join(STRATEGIES.keys()) + ". "
                         "NF keys: " + ", ".join(NIFTY_STRATEGIES.keys()) + ".")
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
    ap.add_argument("--max-dte", type=int, default=None,
                    help="Skip days where DTE > N. Use 7 to replicate weekly-expiry "
                         "conditions with monthly contracts (last week before expiry).")
    ap.add_argument("--entry-day", type=int, default=None,
                    help="Only trade on this weekday: 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri")
    ap.add_argument("--start-date", default=None,
                    help="Only include trades on or after YYYY-MM-DD")
    ap.add_argument("--end-date", default=None,
                    help="Only include trades on or before YYYY-MM-DD")
    args = ap.parse_args()

    inst = args.instrument
    all_strategies = NIFTY_STRATEGIES if inst == "NF" else STRATEGIES
    fetch_cmd = (f"python3 fetch_intraday_options.py --instrument {inst} "
                 f"--spreads --start 2021-08-01")

    print(f"\nSpread backtest  |  instrument={inst}  "
          f"|  signals={'ML' if args.ml else 'rule'}  "
          f"|  entry={args.entry}  exit={args.exit}")

    if args.allow_estimate:
        print("⚠️  --allow-estimate ON: OTM legs filled by BS formula when missing.")
        print("    These results UNDERSTATE debit ~10-20× due to vol skew. Trust real-only runs.\n")

    from datetime import date as _date
    start_dt = _date.fromisoformat(args.start_date) if args.start_date else None
    end_dt   = _date.fromisoformat(args.end_date)   if args.end_date   else None

    if args.max_dte:
        print(f"DTE filter: only trading days with DTE ≤ {args.max_dte}")
    dow_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
    if args.entry_day is not None:
        print(f"Entry-day filter: {dow_names.get(args.entry_day, args.entry_day)} only")
    if start_dt:
        print(f"Start date: {start_dt}")
    if end_dt:
        print(f"End date:   {end_dt}")

    if args.adaptive:
        print(f"Adaptive regime router: signal+VIX → strategy")
        df = run_spread_backtest(None, ml=args.ml, adaptive=True,
                                 entry_time=args.entry, exit_time=args.exit,
                                 allow_estimate=args.allow_estimate,
                                 max_dte=args.max_dte, instrument=inst,
                                 entry_dow=args.entry_day,
                                 start_date=start_dt, end_date=end_dt)
        label = f"Adaptive ({inst} regime router)"
        print_spread_summary(df, label)
        if args.save:
            df.to_csv(args.save, index=False)
            print(f"  Trade log: {args.save}")
        return

    DEBIT_KEYS  = [k for k, v in all_strategies.items() if v.get("entry_debit", True)]
    CREDIT_KEYS = [k for k, v in all_strategies.items() if not v.get("entry_debit", True)]
    NAKED_KEYS  = [k for k, v in all_strategies.items()
                   if v.get("entry_debit", True) and len(v["legs"]) == 1]

    strat_arg = args.strategy
    if strat_arg == "all":
        strategies = list(all_strategies.keys())
    elif strat_arg == "credit":
        strategies = CREDIT_KEYS
    elif strat_arg == "debit":
        strategies = DEBIT_KEYS
    elif strat_arg == "naked":
        strategies = NAKED_KEYS
    else:
        if strat_arg not in all_strategies:
            print(f"Unknown strategy '{strat_arg}' for instrument {inst}.")
            print(f"Valid keys: {', '.join(all_strategies.keys())}")
            sys.exit(1)
        strategies = [strat_arg]

    for key in strategies:
        df = run_spread_backtest(key, ml=args.ml,
                                 entry_time=args.entry, exit_time=args.exit,
                                 allow_estimate=args.allow_estimate,
                                 max_dte=args.max_dte, instrument=inst,
                                 entry_dow=args.entry_day,
                                 start_date=start_dt, end_date=end_dt)
        print_spread_summary(df, all_strategies[key]["name"])
        if args.save and len(strategies) == 1:
            df.to_csv(args.save, index=False)
            print(f"  Trade log: {args.save}")


if __name__ == "__main__":
    main()
