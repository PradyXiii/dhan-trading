#!/usr/bin/env python3
"""
Multi-day holding period backtest for NF options strategies.

Tests: does holding positions 1-5 days improve or worsen P&L vs same-day EOD exit?
Uses Black-Scholes pricing calibrated from actual ATM premiums.

Compares all strategies: credit spreads, debit spreads, straddles, naked options.
Shows: WR%, avg P&L, total P&L, trade count, annual charges.

Usage:
  python3 backtest_hold_periods.py
  python3 backtest_hold_periods.py --ml                    # use ML signals
  python3 backtest_hold_periods.py --strategy nf_bear_call_credit
  python3 backtest_hold_periods.py --start 2025-09-01      # new regime (Tue expiry)
"""

import argparse
import os
import sys
from datetime import date as _date, timedelta
import numpy as np
import pandas as pd
from scipy import stats
from scipy.optimize import brentq

DATA_DIR = "data"
NF_STRIKE_STEP = 50
SPREAD_WIDTH = 150
RISK_FREE = 0.065
NF_TUESDAY_FROM = _date(2025, 9, 1)
LOT_CHANGE_DATE = _date(2026, 1, 6)

# ─────────────────────────────────────────────────────────────────────────────
# NF Expiry & DTE
# ─────────────────────────────────────────────────────────────────────────────

def get_nf_expiry(d):
    """NF weekly expiry: Thursday before Sep 2025, Tuesday from Sep 2025."""
    if isinstance(d, pd.Timestamp):
        d = d.date()
    if d < NF_TUESDAY_FROM:
        days_ahead = (3 - d.weekday()) % 7
        return d + timedelta(days=days_ahead)
    else:
        days_ahead = (1 - d.weekday()) % 7
        return d + timedelta(days=days_ahead)

def get_nf_dte(d):
    """Calendar days to NF expiry (min 0.25 for intraday on expiry day)."""
    expiry = get_nf_expiry(d)
    days = (expiry - (d.date() if isinstance(d, pd.Timestamp) else d)).days
    return max(0.25, float(days) + 1)

def get_nf_lot_size(d):
    """NF lot size: 75 before Jan 6 2026, 65 from that date."""
    if isinstance(d, pd.Timestamp):
        d = d.date()
    return 65 if d >= LOT_CHANGE_DATE else 75

# ─────────────────────────────────────────────────────────────────────────────
# Black-Scholes Pricing
# ─────────────────────────────────────────────────────────────────────────────

def bs_price(S, K, T, r, sigma, opt_type="CE"):
    """Black-Scholes option price."""
    if T <= 0:
        return max(S - K, 0.0) if opt_type == "CE" else max(K - S, 0.0)
    sigma = max(sigma, 0.01)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if opt_type == "CE":
        return float(S * stats.norm.cdf(d1) - K * np.exp(-r * T) * stats.norm.cdf(d2))
    return float(K * np.exp(-r * T) * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1))

def implied_vol(premium, S, K, T, r=RISK_FREE, opt_type="CE"):
    """Back out IV from observed premium."""
    if premium <= 0 or T <= 0 or S <= 0:
        return 0.20
    intrinsic = max(S - K, 0.0) if opt_type == "CE" else max(K - S, 0.0)
    if premium <= intrinsic:
        return 0.05
    try:
        def f(sigma):
            return bs_price(S, K, T, r, sigma, opt_type) - premium
        return float(brentq(f, 0.01, 5.0, xtol=1e-4))
    except Exception:
        return max(0.10, (premium / S) * np.sqrt(252 / max(T * 365, 1)))

# ─────────────────────────────────────────────────────────────────────────────
# Charges
# ─────────────────────────────────────────────────────────────────────────────

def calc_charges(premium, lots, lot_size, n_legs=2):
    """Round-trip charges for n_legs."""
    pv = lots * lot_size * premium
    brok = 40.0 * n_legs
    stt = 0.000625 * pv
    exch = 0.00053 * pv * 2
    clear = 0.000005 * pv * 2
    gst = 0.18 * (brok + exch + clear)
    stamp = 0.00003 * pv
    return round(brok + stt + exch + clear + gst + stamp, 2)

# ─────────────────────────────────────────────────────────────────────────────
# Data Loaders
# ─────────────────────────────────────────────────────────────────────────────

def load_signals(ml=False):
    path = f"{DATA_DIR}/signals_ml.csv" if ml else f"{DATA_DIR}/signals.csv"
    if not os.path.exists(path) and ml:
        path = f"{DATA_DIR}/signals.csv"
    df = pd.read_csv(path, parse_dates=["date"])
    return df[df["signal"].isin(["CALL", "PUT"])].sort_values("date").reset_index(drop=True)

def load_nf_ohlcv():
    path = f"{DATA_DIR}/nifty50.csv"
    if not os.path.exists(path):
        sys.exit(f"Missing {path}")
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    return df

def load_vix():
    path = f"{DATA_DIR}/india_vix.csv"
    if not os.path.exists(path):
        return None
    return pd.read_csv(path, parse_dates=["date"]).set_index("date")

def load_opts_daily():
    path = f"{DATA_DIR}/options_atm_daily.csv"
    if not os.path.exists(path):
        return None
    return pd.read_csv(path, parse_dates=["date"]).set_index("date")

# ─────────────────────────────────────────────────────────────────────────────
# Strategies
# ─────────────────────────────────────────────────────────────────────────────

STRATEGIES = {
    "nf_bear_call_credit": {
        "name": "Bear Call Credit", "signal": "CALL",
        "legs": [("CE", 0, "SELL"), ("CE", +150, "BUY")],
        "credit": True, "sl_frac": 0.50, "tp_frac": 0.65, "max_lots": 2,
    },
    "nf_bull_put_credit": {
        "name": "Bull Put Credit", "signal": "PUT",
        "legs": [("PE", 0, "SELL"), ("PE", -150, "BUY")],
        "credit": True, "sl_frac": 0.50, "tp_frac": 0.65, "max_lots": 2,
    },
    "nf_iron_condor": {
        "name": "Iron Condor", "signal": "BOTH",
        "legs": [("CE", 0, "SELL"), ("CE", +150, "BUY"), ("PE", 0, "SELL"), ("PE", -150, "BUY")],
        "credit": True, "sl_frac": 0.50, "tp_frac": 999, "max_lots": 1,
    },
    "nf_short_straddle": {
        "name": "Short Straddle", "signal": "BOTH",
        "legs": [("CE", 0, "SELL"), ("PE", 0, "SELL")],
        "credit": True, "sl_frac": 0.50, "tp_frac": 999, "max_lots": 1,
    },
    "nf_short_strangle": {
        "name": "Short Strangle ±150", "signal": "BOTH",
        "legs": [("CE", +150, "SELL"), ("PE", -150, "SELL")],
        "credit": True, "sl_frac": 0.50, "tp_frac": 999, "max_lots": 1,
    },
    "nf_bull_call_spread": {
        "name": "Bull Call Debit", "signal": "CALL",
        "legs": [("CE", 0, "BUY"), ("CE", +150, "SELL")],
        "credit": False, "sl_frac": 0.60, "tp_frac": 0.50, "max_lots": 2,
    },
    "nf_bear_put_spread": {
        "name": "Bear Put Debit", "signal": "PUT",
        "legs": [("PE", 0, "BUY"), ("PE", -150, "SELL")],
        "credit": False, "sl_frac": 0.60, "tp_frac": 0.50, "max_lots": 2,
    },
    "nf_long_straddle": {
        "name": "Long Straddle Debit", "signal": "BOTH",
        "legs": [("CE", 0, "BUY"), ("PE", 0, "BUY")],
        "credit": False, "sl_frac": 0.50, "tp_frac": 1.00, "max_lots": 1,
    },
    "nf_long_strangle": {
        "name": "Long Strangle ±150", "signal": "BOTH",
        "legs": [("CE", +150, "BUY"), ("PE", -150, "BUY")],
        "credit": False, "sl_frac": 0.50, "tp_frac": 1.00, "max_lots": 1,
    },
    "nf_naked_call": {
        "name": "Naked Call BUY", "signal": "CALL",
        "legs": [("CE", 0, "BUY")],
        "credit": False, "sl_frac": 0.50, "tp_frac": 1.00, "max_lots": 2,
    },
    "nf_naked_put": {
        "name": "Naked Put BUY", "signal": "PUT",
        "legs": [("PE", 0, "BUY")],
        "credit": False, "sl_frac": 0.50, "tp_frac": 1.00, "max_lots": 2,
    },
    # ── HYBRID STRATEGIES (route by signal) ───────────────────────────────────
    "nf_hybrid_ic_bullput": {
        "name": "IC(CALL) + BullPut(PUT) ★",
        "signal": "HYBRID",
        "call_key": "nf_iron_condor",
        "put_key":  "nf_bull_put_credit",
    },
    "nf_hybrid_bullcall_bullput": {
        "name": "BullCall(CALL) + BullPut(PUT)",
        "signal": "HYBRID",
        "call_key": "nf_bull_call_spread",
        "put_key":  "nf_bull_put_credit",
    },
    "nf_hybrid_ic_both": {
        "name": "IC(CALL) + IC(PUT) = pure IC",
        "signal": "HYBRID",
        "call_key": "nf_iron_condor",
        "put_key":  "nf_iron_condor",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Simulation
# ─────────────────────────────────────────────────────────────────────────────

def simulate_trade(entry_date, signal, strategy, hold_days, ohlcv, vix_df, opts_daily):
    """Simulate one trade held for hold_days trading days."""
    strat_sig = strategy["signal"]
    if strat_sig not in ("BOTH", signal):
        return None

    if not isinstance(entry_date, pd.Timestamp):
        entry_date = pd.Timestamp(entry_date)

    if entry_date not in ohlcv.index:
        return None

    entry_row = ohlcv.loc[entry_date]
    spot_entry = float(entry_row.get("open", entry_row.get("close", 0)))
    if spot_entry <= 0:
        return None

    atm = round(spot_entry / NF_STRIKE_STEP) * NF_STRIKE_STEP
    expiry = get_nf_expiry(entry_date.date())
    dte_entry = get_nf_dte(entry_date.date())
    T_entry = dte_entry / 365.0

    vix_val = 15.0
    if vix_df is not None and entry_date in vix_df.index:
        vix_val = float(vix_df.loc[entry_date, "close"])
    iv_entry = vix_val / 100.0

    # Calibrate IV from actual premiums
    if opts_daily is not None and entry_date in opts_daily.index:
        opt_row = opts_daily.loc[entry_date]
        if signal == "CALL" and "call_premium" in opt_row:
            px = float(opt_row.get("call_premium", 0) or 0)
            if px > 0:
                iv_cal = implied_vol(px, spot_entry, atm, T_entry, opt_type="CE")
                if 0.05 < iv_cal < 2.0:
                    iv_entry = iv_cal
        elif signal == "PUT" and "put_premium" in opt_row:
            px = float(opt_row.get("put_premium", 0) or 0)
            if px > 0:
                iv_cal = implied_vol(px, spot_entry, atm, T_entry, opt_type="PE")
                if 0.05 < iv_cal < 2.0:
                    iv_entry = iv_cal

    lot_size = get_nf_lot_size(entry_date.date())
    lots = strategy["max_lots"]

    # Entry net value
    net_entry = 0.0
    entry_legs = []
    for opt_type, offset, action in strategy["legs"]:
        K = atm + offset
        px = bs_price(spot_entry, K, T_entry, RISK_FREE, iv_entry, opt_type)
        sign = +1 if action == "BUY" else -1
        net_entry += sign * px
        entry_legs.append((opt_type, K, action, px))

    # Exit date
    all_dates = ohlcv.index[ohlcv.index > entry_date].sort_values()
    expiry_ts = pd.Timestamp(expiry)

    if hold_days == 0:
        exit_date = entry_date
    else:
        if len(all_dates) < hold_days:
            return None
        exit_date = all_dates[hold_days - 1]
        if exit_date > expiry_ts:
            valid = ohlcv.index[ohlcv.index <= expiry_ts]
            if len(valid) == 0:
                return None
            exit_date = valid[-1]

    dte_exit = get_nf_dte(exit_date.date() if hasattr(exit_date, "date") else exit_date)
    T_exit = max(dte_exit / 365.0, 0.0)

    vix_exit = vix_val
    if vix_df is not None and exit_date in vix_df.index:
        vix_exit = float(vix_df.loc[exit_date, "close"])
    iv_exit = vix_exit / 100.0

    # SL/TP check across holding period
    result = "EOD"
    exit_value = None
    sl_frac = strategy["sl_frac"]
    tp_frac = strategy["tp_frac"]

    if strategy["credit"]:
        sl_threshold = net_entry * (1.0 + sl_frac)
        tp_threshold = net_entry * (1.0 - tp_frac)
    else:
        sl_threshold = net_entry * (1.0 - sl_frac)
        tp_threshold = net_entry * (1.0 + tp_frac) if tp_frac < 10 else float("inf")

    # Check each day
    check_dates = [d for d in ohlcv.index if entry_date <= d <= exit_date]

    for check_date in check_dates:
        if check_date not in ohlcv.index:
            continue

        row_check = ohlcv.loc[check_date]
        # For SL check: use high for long positions (risk is up move), low for short (risk is down move)
        # Simplification: use high for all (worst case for shorts)
        spot_check = float(row_check.get("high", row_check.get("close", 0)))

        dte_check = get_nf_dte(check_date.date() if hasattr(check_date, "date") else check_date)
        T_check = max(dte_check / 365.0, 0.0)

        vix_check = vix_val
        if vix_df is not None and check_date in vix_df.index:
            vix_check = float(vix_df.loc[check_date, "close"])
        iv_check = vix_check / 100.0

        # Compute net value
        net_check = sum(
            (+1 if a == "BUY" else -1) * bs_price(spot_check, K, T_check, RISK_FREE, iv_check, ot)
            for ot, K, a, _ in entry_legs
        )

        # Check SL/TP
        if strategy["credit"]:
            if net_check <= sl_threshold:
                exit_value = net_check
                result = "SL"
                exit_date = check_date
                break
            if tp_frac < 100 and net_check >= tp_threshold:
                exit_value = net_check
                result = "TP"
                exit_date = check_date
                break
        else:
            if net_check <= sl_threshold:
                exit_value = net_check
                result = "SL"
                exit_date = check_date
                break
            if tp_frac < 100 and net_check >= tp_threshold:
                exit_value = net_check
                result = "TP"
                exit_date = check_date
                break

        if check_date == exit_date:
            exit_value = net_check

    if exit_value is None:
        if exit_date in ohlcv.index:
            spot_exit = float(ohlcv.loc[exit_date, "close"])
            exit_value = sum(
                (+1 if a == "BUY" else -1) * bs_price(spot_exit, K, T_exit, RISK_FREE, iv_exit, ot)
                for ot, K, a, _ in entry_legs
            )
        else:
            return None

    # P&L
    pnl_per_unit = exit_value - net_entry
    gross_pnl = pnl_per_unit * lots * lot_size
    max_px = max(px for _, _, _, px in entry_legs)
    charges = calc_charges(max_px, lots, lot_size, n_legs=len(entry_legs))
    net_pnl = gross_pnl - charges

    return {
        "entry_date": entry_date,
        "exit_date": exit_date,
        "signal": signal,
        "result": result,
        "net_pnl": round(net_pnl, 2),
        "win": net_pnl > 0,
        "lots": lots,
        "dow": entry_date.strftime("%a"),
    }

def run_strategy(strategy_key, hold_days, signals, ohlcv, vix_df, opts_daily, start_date=None):
    """Run backtest for one strategy × one hold period."""
    strategy = STRATEGIES[strategy_key]
    trades = []

    for _, row in signals.iterrows():
        d = row["date"]
        if start_date is not None and d < pd.Timestamp(start_date):
            continue
        sig = str(row["signal"]).upper()

        if strategy.get("signal") == "HYBRID":
            # Route by signal: CALL days use call_key, PUT days use put_key
            sub_key = strategy["call_key"] if sig == "CALL" else strategy["put_key"]
            sub_strat = STRATEGIES[sub_key]
            result = simulate_trade(d, sig, sub_strat, hold_days, ohlcv, vix_df, opts_daily)
        else:
            result = simulate_trade(d, sig, strategy, hold_days, ohlcv, vix_df, opts_daily)

        if result:
            trades.append(result)

    return trades

def aggregate(trades):
    """Compute stats from trades."""
    if not trades:
        return None
    df = pd.DataFrame(trades)
    n = len(df)
    wins = df["win"].sum()
    pnl_total = df["net_pnl"].sum()
    pnl_avg = df["net_pnl"].mean()
    return {
        "trades": n,
        "wr_pct": round(100 * wins / n, 1),
        "avg_pnl": round(pnl_avg, 0),
        "total_pnl": round(pnl_total, 0),
    }

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

DOW_MAP = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4,
           "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4,
           "0": 0, "1": 1, "2": 2, "3": 3, "4": 4}
DOW_NAMES = {0: "Mon(DTE1)", 1: "Tue(DTE0)", 2: "Wed(DTE6)", 3: "Thu(DTE5)", 4: "Fri(DTE4)"}


def aggregate_dow(trades):
    """Stats per day-of-week."""
    if not trades:
        return {}
    df = pd.DataFrame(trades)
    out = {}
    for day_num, day_name in DOW_NAMES.items():
        sub = df[df["dow"] == ["Mon","Tue","Wed","Thu","Fri"][day_num]]
        if len(sub) == 0:
            continue
        wins = sub["win"].sum()
        out[day_num] = {
            "day": day_name,
            "trades": len(sub),
            "wr_pct": round(100 * wins / len(sub), 1),
            "avg_pnl": round(sub["net_pnl"].mean(), 0),
            "total_pnl": round(sub["net_pnl"].sum(), 0),
        }
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", default=None, help="single strategy (all if omitted)")
    parser.add_argument("--ml", action="store_true", help="use ML signals")
    parser.add_argument("--start", default=None, help="start date (YYYY-MM-DD)")
    parser.add_argument("--dow", default=None,
                        help="filter to specific days e.g. --dow Mon,Tue or --dow Thu,Fri")
    parser.add_argument("--dow-breakdown", action="store_true",
                        help="show WR/P&L per day-of-week (hold=0d, all strategies)")
    args = parser.parse_args()

    # Parse DOW filter
    dow_filter = None
    if args.dow:
        parts = [p.strip() for p in args.dow.split(",")]
        dow_filter = set()
        for p in parts:
            if p in DOW_MAP:
                dow_filter.add(DOW_MAP[p])
            else:
                print(f"Unknown day '{p}'. Use Mon/Tue/Wed/Thu/Fri.")
                sys.exit(1)

    signals = load_signals(ml=args.ml)
    ohlcv = load_nf_ohlcv()
    vix_df = load_vix()
    opts_daily = load_opts_daily()

    # Filter signals by DOW
    if dow_filter is not None:
        signals = signals[signals["date"].dt.weekday.isin(dow_filter)].reset_index(drop=True)
        dow_label = "+".join(DOW_NAMES[d] for d in sorted(dow_filter))
    else:
        dow_label = "All days"

    strategies = {args.strategy: STRATEGIES[args.strategy]} if args.strategy else STRATEGIES

    # ── DOW BREAKDOWN MODE ────────────────────────────────────────────────────
    if args.dow_breakdown:
        print("\n" + "="*100)
        print(f"DOW BREAKDOWN — hold=0d, Sep 2025+ regime (Tue expiry)")
        print(f"{'Strategy':<24} {'Mon(DTE1)':>14} {'Tue(DTE0)':>14} "
              f"{'Wed(DTE6)':>14} {'Thu(DTE5)':>14} {'Fri(DTE4)':>14}")
        print(f"{'':24} {'WR%  TotalP&L':>14} {'WR%  TotalP&L':>14} "
              f"{'WR%  TotalP&L':>14} {'WR%  TotalP&L':>14} {'WR%  TotalP&L':>14}")
        print("-"*100)

        for strat_key, strat_def in sorted(strategies.items()):
            trades = run_strategy(strat_key, 0, signals, ohlcv, vix_df, opts_daily, args.start)
            dow_stats = aggregate_dow(trades)

            row = f"{strat_def['name']:<24}"
            for day_num in [0, 1, 2, 3, 4]:
                if day_num in dow_stats:
                    s = dow_stats[day_num]
                    pnl_k = s['total_pnl'] / 1000
                    sign = "+" if pnl_k >= 0 else ""
                    cell = f"{s['wr_pct']}% {sign}{pnl_k:.1f}K"
                else:
                    cell = "—"
                row += f"{cell:>14}"
            print(row)

        print("="*100)
        print("P&L in ₹K (thousands). WR% = win rate. hold=0d = same-day EOD exit.")
        print("="*100)
        return

    # ── HOLD PERIOD TABLE MODE ─────────────────────────────────────────────────
    print("\n" + "="*100)
    print(f"MULTI-DAY HOLDING PERIOD BACKTEST — Nifty50 Options  [{dow_label}]")
    print("="*100)

    for strat_key, strat_def in sorted(strategies.items()):
        print(f"\n{strat_def['name'].upper()}")
        print("-" * 100)
        print(f"{'Hold':<6} {'Trades':<8} {'WR%':<8} {'Avg P&L':<12} {'Total P&L':<12}")
        print("-" * 100)

        for hold_days in range(6):
            trades = run_strategy(strat_key, hold_days, signals, ohlcv, vix_df, opts_daily, args.start)
            stats = aggregate(trades)

            if stats:
                print(
                    f"{hold_days}d    {stats['trades']:<8} {stats['wr_pct']:<8} "
                    f"₹{stats['avg_pnl']:>10,.0f}  ₹{stats['total_pnl']:>10,.0f}"
                )

    print("\n" + "="*100)

if __name__ == "__main__":
    main()
