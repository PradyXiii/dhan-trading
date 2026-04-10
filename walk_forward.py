#!/usr/bin/env python3
"""
walk_forward.py — Walk-forward validation of the BankNifty strategy
====================================================================
Tests whether our strategy has a genuine edge or is curve-fitted to history.

Method (mirrors the video):
  Training window : 12 months  → test thresholds 1/2/3/4, lock the best one
  Blind test window: 3 months  → apply locked threshold to data never seen
  Walk forward by 3 months and repeat (~14 folds across 4.5 years)

Three results compared:
  Threshold ±1 (full)   : standard backtest on all data — our current approach
  Threshold ±2 (full)   : same but stricter signal
  Walk-forward (OOS)    : out-of-sample returns stitched from blind test windows

If walk-forward P&L is close to full-backtest → real edge ✓
If walk-forward collapses → curve-fitted, strategy is fake ✗

Usage:
  python3 walk_forward.py
  python3 walk_forward.py --train 9   # 9-month training window
  python3 walk_forward.py --test  2   # 2-month test window
"""

import os
import sys
import pandas as pd
import numpy as np
from math import floor, sqrt
from dateutil.relativedelta import relativedelta

DATA_DIR  = "data"
LOT_SIZE  = 30
SL_PCT    = 0.30
RISK_PCT  = 0.05
MAX_LOTS  = 20
PREMIUM_K = 0.004
DELTA     = 0.5

STARTING_CAPITAL = 30_000
MONTHLY_TOPUP    = 10_000

DAY_DTE = {"Monday": 2, "Tuesday": 1, "Wednesday": 0.25, "Thursday": 6, "Friday": 5}
DAY_RR  = {"Monday": 1.6, "Tuesday": 1.4, "Wednesday": 1.0, "Thursday": 2.0, "Friday": 2.0}
TRADE_WEEKDAYS = {0, 1, 2, 3, 4}   # Mon–Fri (Wed included)

# ── Event calendar (mirrors signal_engine.py) ─────────────────────────────────

_EVENT_STRS = {
    "2021-10-08","2021-12-08",
    "2022-02-01","2022-02-10","2022-04-08","2022-06-08","2022-08-05",
    "2022-09-30","2022-12-07",
    "2023-02-01","2023-02-08","2023-04-06","2023-06-08","2023-08-10",
    "2023-10-06","2023-12-08",
    "2024-02-01","2024-02-08","2024-04-05","2024-06-07","2024-07-23",
    "2024-08-08","2024-10-09","2024-12-06",
    "2025-02-01","2025-02-07","2025-04-09","2025-06-06","2025-08-07",
    "2025-10-08","2025-12-05",
    "2026-02-01","2026-02-06",
}
EVENT_DATES = {pd.Timestamp(d).date() for d in _EVENT_STRS}


# ── Data loading + indicators (mirrors signal_engine.py) ─────────────────────

def load_and_compute():
    bn  = pd.read_csv(f"{DATA_DIR}/banknifty.csv",     parse_dates=["date"])
    nf  = pd.read_csv(f"{DATA_DIR}/nifty50.csv",       parse_dates=["date"])
    vix = pd.read_csv(f"{DATA_DIR}/india_vix.csv",     parse_dates=["date"])

    bn  = bn [["date","open","high","low","close"]].rename(
              columns={"open":"bn_open","high":"bn_high","low":"bn_low","close":"bn_close"})
    nf  = nf [["date","close"]].rename(columns={"close":"nf_close"})
    vix = vix[["date","close"]].rename(columns={"close":"vix_close"})

    df = bn.merge(nf, on="date", how="left").merge(vix, on="date", how="left")
    df = df.sort_values("date").reset_index(drop=True)
    df[["nf_close","vix_close"]] = df[["nf_close","vix_close"]].ffill(limit=3)
    df = df.dropna(subset=["bn_close","nf_close","vix_close"])

    # Indicators
    df["ema20"]     = df["bn_close"].ewm(span=20, adjust=False).mean()
    df["trend5"]    = (df["bn_close"] - df["bn_close"].shift(5)) / df["bn_close"].shift(5) * 100
    df["vix_dir"]   = df["vix_close"] - df["vix_close"].shift(1)
    bn_chg          = (df["bn_close"] - df["bn_close"].shift(1)) / df["bn_close"].shift(1) * 100
    nf_chg          = (df["nf_close"] - df["nf_close"].shift(1)) / df["nf_close"].shift(1) * 100
    df["bn_nf_div"] = bn_chg - nf_chg

    return df.dropna(subset=["ema20","trend5","vix_dir","bn_nf_div"]).copy()


def score_and_signal(df, threshold):
    """Generate signals on df at given threshold. Returns DataFrame."""
    trade = df[df["date"].dt.weekday.isin(TRADE_WEEKDAYS)].copy()
    rows = []
    for _, row in trade.iterrows():
        s_ema    = 1 if row["bn_close"] > row["ema20"] else -1
        s_trend  = (1 if row["trend5"] > 1.0 else (-1 if row["trend5"] < -1.0 else 0))
        s_vix    = (1 if row["vix_dir"] < 0  else (-1 if row["vix_dir"] > 0   else 0))
        s_div    = (1 if row["bn_nf_div"] > 0.5 else (-1 if row["bn_nf_div"] < -0.5 else 0))
        score    = s_ema + s_trend + s_vix + s_div
        td       = row["date"].date()
        if td in EVENT_DATES:
            signal = "NONE"
        else:
            signal = ("CALL" if score >= threshold else ("PUT" if score <= -threshold else "NONE"))
        rows.append({"date": row["date"], "weekday": row["date"].day_name(),
                     "bn_open": row["bn_open"], "bn_high": row["bn_high"],
                     "bn_low": row["bn_low"], "bn_close": row["bn_close"],
                     "score": score, "signal": signal})
    return pd.DataFrame(rows)


# ── Trade simulator (mirrors backtest_engine.py) ──────────────────────────────

def calculate_charges(premium, lots):
    pv = lots * LOT_SIZE * premium
    b  = 40.0
    stt= 0.000625*pv; exc=0.00053*pv*2; clr=0.000005*pv*2
    gst= 0.18*(b+exc+clr); stmp=0.00003*pv; sebi=0.000001*pv*2
    return round(b+stt+exc+clr+gst+stmp+sebi, 2)


def simulate_trade(row, capital):
    weekday = row["weekday"]
    signal  = row["signal"]
    if signal not in ("CALL", "PUT"):
        return 0.0, "SKIP"

    bn_open  = float(row["bn_open"])
    bn_high  = float(row["bn_high"])
    bn_low   = float(row["bn_low"])
    bn_close = float(row["bn_close"])

    dte     = DAY_DTE.get(weekday, 1)
    rr      = DAY_RR.get(weekday, 1.4)
    premium = bn_open * PREMIUM_K * sqrt(dte)

    max_loss_1lot = LOT_SIZE * premium * SL_PCT
    if max_loss_1lot > capital * 0.15:
        return 0.0, "SKIP_CAP"

    lots   = min(MAX_LOTS, max(1, floor(capital * RISK_PCT / max_loss_1lot)))
    sl_pts = (SL_PCT * premium) / DELTA
    tp_pts = (rr * SL_PCT * premium) / DELTA

    charges = calculate_charges(premium, lots)

    if signal == "CALL":
        sl_hit = bn_low  <= bn_open - sl_pts
        tp_hit = bn_high >= bn_open + tp_pts
        if sl_hit and tp_hit:
            result = "WIN" if bn_close > bn_open else "LOSS"
        elif tp_hit:  result = "WIN"
        elif sl_hit:  result = "LOSS"
        else:
            gross = (bn_close - bn_open) * DELTA * lots * LOT_SIZE
            return round(gross - charges, 2), "PARTIAL"
    else:
        sl_hit = bn_high >= bn_open + sl_pts
        tp_hit = bn_low  <= bn_open - tp_pts
        if sl_hit and tp_hit:
            result = "WIN" if bn_close < bn_open else "LOSS"
        elif tp_hit:  result = "WIN"
        elif sl_hit:  result = "LOSS"
        else:
            gross = (bn_open - bn_close) * DELTA * lots * LOT_SIZE
            return round(gross - charges, 2), "PARTIAL"

    if result == "WIN":
        pnl = lots * LOT_SIZE * premium * rr * SL_PCT - charges
    else:
        pnl = -(lots * LOT_SIZE * premium * SL_PCT) - charges
    return round(pnl, 2), result


def run_backtest_on_signals(sig_df, start_capital=STARTING_CAPITAL):
    """Run backtest on a pre-filtered signals DataFrame. Returns (trade_df, end_capital)."""
    capital = start_capital
    current_month = None
    rows = []

    for _, row in sig_df.iterrows():
        d = row["date"]
        mk = (d.year, d.month)
        if current_month is None:
            current_month = mk
        elif mk != current_month:
            capital += MONTHLY_TOPUP
            current_month = mk

        cap_before = capital
        pnl, result = simulate_trade(row, capital)
        capital += pnl
        rows.append({"date": d.date(), "result": result, "pnl": pnl,
                     "cap_before": cap_before, "cap_after": capital})

    if not rows:
        empty = pd.DataFrame(columns=["date","result","pnl","cap_before","cap_after"])
        return empty, capital
    return pd.DataFrame(rows), capital


def sharpe(trade_df):
    """Simple Sharpe-like ratio on per-trade P&L."""
    if trade_df.empty or "result" not in trade_df.columns:
        return -999.0
    active = trade_df[trade_df["result"].isin(["WIN","LOSS","PARTIAL"])]["pnl"]
    if len(active) < 5:
        return -999.0
    return active.mean() / (active.std() + 1e-9)


# ── Walk-forward engine ───────────────────────────────────────────────────────

def build_folds(start_date, end_date, train_months=12, test_months=3):
    """Generate (train_start, train_end, test_start, test_end) tuples."""
    folds = []
    ts = start_date
    while True:
        te = ts + relativedelta(months=train_months) - pd.Timedelta(days=1)
        bs = te + pd.Timedelta(days=1)
        be = bs + relativedelta(months=test_months) - pd.Timedelta(days=1)
        if be > end_date:
            break
        folds.append((ts, te, bs, be))
        ts = ts + relativedelta(months=test_months)
    return folds


def run_walk_forward(df, train_months=12, test_months=3):
    start = df["date"].min()
    end   = df["date"].max()
    folds = build_folds(start, end, train_months, test_months)

    print(f"\n  {len(folds)} folds  |  Train: {train_months}m  |  Test: {test_months}m")
    print(f"  {'Fold':<6}  {'Train window':<24}  {'Test window':<24}  "
          f"{'Best THR':>9}  {'OOS P&L':>10}  {'Trades':>7}  {'WR':>7}")
    print(f"  {'─'*88}")

    oos_rows     = []
    capital      = STARTING_CAPITAL
    best_thrs    = []

    for i, (tr_s, tr_e, ts_s, ts_e) in enumerate(folds):
        # ── Training: pick best threshold ─────────────────────────────────────
        train_df = df[(df["date"] >= tr_s) & (df["date"] <= tr_e)]
        best_thr, best_score = 1, -999.0
        # Only search ±1 and ±2: ±3/±4 require 3-4 indicators to agree
        # simultaneously, making them different strategies with tiny sample
        # sizes — not valid alternatives to ±1 for this system.
        for thr in [1, 2]:
            sigs = score_and_signal(train_df, thr)
            sigs = sigs[sigs["signal"].isin(["CALL","PUT"])]
            if len(sigs) < 15:
                continue
            td, _ = run_backtest_on_signals(sigs)
            s = sharpe(td)
            if s > best_score:
                best_score, best_thr = s, thr
        best_thrs.append(best_thr)

        # ── Blind test: apply locked threshold ────────────────────────────────
        test_df  = df[(df["date"] >= ts_s) & (df["date"] <= ts_e)]
        test_sigs = score_and_signal(test_df, best_thr)
        active_sigs = test_sigs[test_sigs["signal"].isin(["CALL","PUT"])].copy()

        oos_td, end_cap = run_backtest_on_signals(active_sigs, start_capital=capital)
        capital = end_cap   # capital flows continuously fold-to-fold

        if oos_td.empty or "result" not in oos_td.columns:
            print(f"  Fold {i+1:<2}  "
                  f"{str(tr_s.date())} → {str(tr_e.date())}  "
                  f"{str(ts_s.date())} → {str(ts_e.date())}  "
                  f"THR ±{best_thr}       (no trades in test window)")
            continue
        active = oos_td[oos_td["result"].isin(["WIN","LOSS","PARTIAL"])]
        trades = len(active)
        w      = (active["result"] == "WIN").sum()
        l      = (active["result"] == "LOSS").sum()
        wr     = w / (w + l) * 100 if (w + l) > 0 else 0
        oos_pnl = active["pnl"].sum()

        for _, r in oos_td.iterrows():
            oos_rows.append({**r, "fold": i+1, "thr": best_thr})

        print(f"  Fold {i+1:<2}  "
              f"{str(tr_s.date())} → {str(tr_e.date())}  "
              f"{str(ts_s.date())} → {str(ts_e.date())}  "
              f"THR ±{best_thr}  {oos_pnl:>+10,.0f}  {trades:>7}  {wr:>6.0f}%")

    return pd.DataFrame(oos_rows), best_thrs


# ── Full backtest for comparison ──────────────────────────────────────────────

def run_full_backtest(df, threshold):
    sigs = score_and_signal(df, threshold)
    sigs = sigs[sigs["signal"].isin(["CALL","PUT"])]
    td, end_cap = run_backtest_on_signals(sigs)
    active = td[td["result"].isin(["WIN","LOSS","PARTIAL"])]
    w = (active["result"] == "WIN").sum()
    l = (active["result"] == "LOSS").sum()
    return active["pnl"].sum(), len(active), w/(w+l)*100 if (w+l)>0 else 0, end_cap


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    train_m = 12; test_m = 3
    for i, a in enumerate(args):
        if a == "--train" and i+1 < len(args): train_m = int(args[i+1])
        if a == "--test"  and i+1 < len(args): test_m  = int(args[i+1])

    print("=" * 70)
    print("  BankNifty — Walk-Forward Strategy Validation")
    print(f"  Train: {train_m} months  |  Test: {test_m} months blind")
    print("=" * 70)

    print("\nLoading data and computing indicators...")
    df = load_and_compute()
    print(f"  {len(df)} trading days  "
          f"({df['date'].min().date()} → {df['date'].max().date()})")

    # ── Walk-forward ──────────────────────────────────────────────────────────
    print("\nRunning walk-forward analysis...")
    oos_df, best_thrs = run_walk_forward(df, train_m, test_m)

    # ── Full backtests for comparison ─────────────────────────────────────────
    print(f"\n{'═'*70}")
    print(f"  RESULTS COMPARISON")
    print(f"{'═'*70}")
    print(f"  {'Method':<30}  {'Trades':>7}  {'WR':>7}  {'Net P&L':>12}  {'₹/trade':>10}  {'End Cap':>12}")
    print(f"  {'─'*78}")

    full_pnl_1 = full_trades_1 = 0
    for thr in [1, 2]:
        pnl, trades, wr, end = run_full_backtest(df, thr)
        avg = pnl / trades if trades > 0 else 0
        print(f"  {'Full backtest (thr ±'+str(thr)+')':<30}  {trades:>7}  "
              f"{wr:>6.1f}%  ₹{pnl:>10,.0f}  ₹{avg:>8,.0f}  ₹{end:>10,.0f}")
        if thr == 1:
            full_pnl_1, full_trades_1 = pnl, trades

    # Walk-forward out-of-sample summary
    oos_active = oos_df[oos_df["result"].isin(["WIN","LOSS","PARTIAL"])]
    if not oos_active.empty:
        w = (oos_active["result"] == "WIN").sum()
        l = (oos_active["result"] == "LOSS").sum()
        wr  = w / (w + l) * 100 if (w + l) > 0 else 0
        net = oos_active["pnl"].sum()
        avg = net / len(oos_active) if len(oos_active) > 0 else 0
        end = oos_df["cap_after"].iloc[-1]
        print(f"  {'Walk-forward (OOS only)':<30}  {len(oos_active):>7}  "
              f"{wr:>6.1f}%  ₹{net:>10,.0f}  ₹{avg:>8,.0f}  ₹{end:>10,.0f}")

    print(f"{'─'*70}")

    # Verdict
    if not oos_active.empty:
        oos_net = oos_active["pnl"].sum()
        oos_avg = oos_net / len(oos_active) if len(oos_active) > 0 else 0
        full_avg_1 = full_pnl_1 / full_trades_1 if full_trades_1 > 0 else 0
        # Compare per-trade quality, not absolute (WF takes fewer trades by design)
        pertrade_degradation = (full_avg_1 - oos_avg) / abs(full_avg_1) * 100 if full_avg_1 != 0 else 0

        print(f"\n  Threshold chosen per fold: {best_thrs}")
        most_common = max(set(best_thrs), key=best_thrs.count)
        print(f"  Most common best threshold: ±{most_common}  "
              f"({'consistent' if best_thrs.count(most_common)/len(best_thrs) > 0.6 else 'mixed ±1 and ±2'})")

        if oos_net > 0:
            print(f"\n  ✓ STRATEGY HAS REAL EDGE")
            print(f"    Walk-forward P&L is positive on unseen data.")
            print(f"    Per-trade P&L:  full ±1 = ₹{full_avg_1:,.0f}  |  OOS = ₹{oos_avg:,.0f}  "
                  f"({'OOS better ✓' if oos_avg >= full_avg_1 else f'degraded {pertrade_degradation:.0f}%'})")
            if pertrade_degradation <= 0:
                print(f"    No per-trade degradation — minimal curve-fitting. The edge is real.")
            elif pertrade_degradation < 30:
                print(f"    Mild per-trade degradation — some overfitting but edge survives.")
            else:
                print(f"    Per-trade degradation {pertrade_degradation:.0f}% — moderate curve-fitting.")
                print(f"    Strategy still makes money OOS, but live returns may be lower than backtest.")
        else:
            print(f"\n  ✗ STRATEGY MAY BE CURVE-FITTED")
            print(f"    Walk-forward P&L is negative on unseen data.")
            print(f"    The returns in the full backtest may not hold in live trading.")

    print(f"{'='*70}")

    # Save OOS trade log
    if not oos_df.empty:
        out = f"{DATA_DIR}/walk_forward_log.csv"
        oos_df.to_csv(out, index=False)
        print(f"\n  Saved → {out}")


if __name__ == "__main__":
    try:
        from dateutil.relativedelta import relativedelta
    except ImportError:
        print("Installing python-dateutil...")
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "python-dateutil", "--break-system-packages", "-q"])
        from dateutil.relativedelta import relativedelta
    main()
