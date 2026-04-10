#!/usr/bin/env python3
"""
multi_entry_backtest.py — Re-entry strategy backtest on BankNifty options
===========================================================================
Tests up to 3 intraday entries per signal day using 5-min BankNifty candles.

Entry logic:
  Entry 1 : market open (ENTRY_TIME, default 09:15)
  Entry 2 : if Entry 1 hits SL → re-enter at next candle's open (same direction)
  Entry 3 : if Entry 2 hits SL → re-enter at next candle's open
  No re-entry after a TP or EOD (partial) exit.
  No re-entry started after NO_REENTRY_AFTER (14:30) — too close to close.

Data required:
  data/banknifty_5min.csv  — python3 fetch_intraday.py
  data/signals.csv         — python3 signal_engine.py

Usage:
  python3 multi_entry_backtest.py               # compare 1/2/3 entries (default)
  python3 multi_entry_backtest.py --entries 2   # fixed: max 2 entries/day
  python3 multi_entry_backtest.py --entry-time 09:20
"""

import os
import sys
import pandas as pd
from math import sqrt

DATA_DIR          = "data"
LOT_SIZE          = 30
SL_PCT            = 0.30         # stop-loss = 30% of premium
RISK_PCT          = 0.05         # risk 5% of capital per leg
MAX_LOTS          = 20
PREMIUM_K         = 0.004        # ATM premium calibration constant
DELTA             = 0.5          # ATM delta approximation
NO_REENTRY_AFTER  = "14:30"      # don't start a fresh leg after this time

STARTING_CAPITAL  = 30_000
MONTHLY_TOPUP     = 10_000

# Days-to-expiry per weekday (BankNifty weekly expiry on Wednesday)
DAY_DTE = {"Monday": 2, "Tuesday": 1, "Wednesday": 0.25, "Thursday": 6, "Friday": 5}
# Reward-to-risk ratios per day (higher DTE = richer premium)
DAY_RR  = {"Monday": 1.6, "Tuesday": 1.4, "Wednesday": 1.0, "Thursday": 2.0, "Friday": 2.0}


# ── Transaction cost calculator (mirrors backtest_engine.py) ──────────────────

def calculate_charges(premium, lots):
    pv         = lots * LOT_SIZE * premium
    brokerage  = 40.0
    stt        = 0.000625 * pv
    exchange   = 0.00053  * pv * 2
    clearing   = 0.000005 * pv * 2
    gst        = 0.18 * (brokerage + exchange + clearing)
    stamp_duty = 0.00003  * pv
    sebi       = 0.000001 * pv * 2
    return round(brokerage + stt + exchange + clearing + gst + stamp_duty + sebi, 2)


def calc_lots(capital, premium):
    """Return lot count based on 5% capital risk; 0 if capital too low."""
    max_loss_1lot = LOT_SIZE * premium * SL_PCT
    if max_loss_1lot <= 0 or max_loss_1lot > capital * 0.15:
        return 0
    return min(MAX_LOTS, max(1, int((capital * RISK_PCT) / max_loss_1lot)))


# ── Single-leg simulator (candle-by-candle exit) ──────────────────────────────

def simulate_leg(day_candles, signal, entry_candle_idx, entry_bn, dte, rr, capital):
    """
    Simulate one options leg entering at entry_bn (open of candle after entry_candle_idx).
    Scans every subsequent 5-min candle for SL/TP; exits at 15:30 if neither hit.

    Returns a dict:
      outcome        : 'WIN' | 'LOSS' | 'PARTIAL' | 'SKIP_CAP'
      pnl            : net P&L (after charges) in ₹
      charges        : brokerage+STT+exchange+... in ₹
      lots           : number of lots traded
      premium        : estimated option premium in ₹
      exit_time      : HH:MM when position closed
      next_entry_bn  : open of the candle AFTER SL hit (for re-entry), or None
      sl_candle_idx  : DataFrame index of SL candle (to continue scan for re-entry)
    """
    premium = entry_bn * PREMIUM_K * sqrt(dte)
    lots    = calc_lots(capital, premium)

    if lots == 0:
        return {
            "outcome": "SKIP_CAP", "pnl": 0.0, "charges": 0.0,
            "lots": 0, "premium": round(premium, 2), "exit_time": None,
            "next_entry_bn": None, "sl_candle_idx": None,
        }

    sl_pts = (premium * SL_PCT) / DELTA     # BN-index points to hit SL
    tp_pts = (premium * SL_PCT * rr) / DELTA

    if signal == "CALL":
        sl_level = entry_bn - sl_pts
        tp_level = entry_bn + tp_pts
    else:
        sl_level = entry_bn + sl_pts
        tp_level = entry_bn - tp_pts

    # Candles AFTER the entry candle (we entered at the open of entry_candle_idx+1)
    after = day_candles[day_candles.index > entry_candle_idx].copy()
    after = after[after["time"] <= "15:15"]   # don't extend past 15:15

    outcome       = None
    sl_candle_idx = None
    next_entry_bn = None
    exit_time     = None

    for idx, c in after.iterrows():
        if signal == "CALL":
            tp_hit = float(c["high"]) >= tp_level
            sl_hit = float(c["low"])  <= sl_level
        else:
            tp_hit = float(c["low"])  <= tp_level
            sl_hit = float(c["high"]) >= sl_level

        if tp_hit and sl_hit:
            # Both hit in the same candle — direction of the candle decides
            if signal == "CALL":
                outcome = "WIN" if float(c["close"]) >= float(c["open"]) else "LOSS"
            else:
                outcome = "WIN" if float(c["close"]) <= float(c["open"]) else "LOSS"
            exit_time = str(c["time"])
            if outcome == "LOSS":
                sl_candle_idx = idx
                remaining = after[after.index > idx]
                if not remaining.empty and str(remaining.iloc[0]["time"]) <= NO_REENTRY_AFTER:
                    next_entry_bn = float(remaining.iloc[0]["open"])
            break

        elif tp_hit:
            outcome   = "WIN"
            exit_time = str(c["time"])
            break

        elif sl_hit:
            outcome       = "LOSS"
            exit_time     = str(c["time"])
            sl_candle_idx = idx
            remaining = after[after.index > idx]
            if not remaining.empty and str(remaining.iloc[0]["time"]) <= NO_REENTRY_AFTER:
                next_entry_bn = float(remaining.iloc[0]["open"])
            break

    if outcome is None:
        # Neither SL nor TP hit — exit at 15:30 close
        eod = day_candles[day_candles["time"] <= "15:30"].iloc[-1]
        exit_bn   = float(eod["close"])
        exit_time = str(eod["time"])
        bn_move   = (exit_bn - entry_bn) if signal == "CALL" else (entry_bn - exit_bn)
        gross     = LOT_SIZE * bn_move * DELTA * lots
        charges   = calculate_charges(premium, lots)
        return {
            "outcome": "PARTIAL", "pnl": round(gross - charges, 2), "charges": charges,
            "lots": lots, "premium": round(premium, 2), "exit_time": exit_time,
            "next_entry_bn": None, "sl_candle_idx": None,
        }

    charges = calculate_charges(premium, lots)
    pnl = (lots * LOT_SIZE * premium * rr * SL_PCT - charges) if outcome == "WIN" \
          else -(lots * LOT_SIZE * premium * SL_PCT) - charges

    return {
        "outcome": outcome, "pnl": round(pnl, 2), "charges": charges,
        "lots": lots, "premium": round(premium, 2), "exit_time": exit_time,
        "next_entry_bn": next_entry_bn, "sl_candle_idx": sl_candle_idx,
    }


# ── Day simulator (up to max_entries legs) ────────────────────────────────────

def simulate_day(day_candles, signal, weekday, entry_time, max_entries, capital_start):
    """
    Simulate up to max_entries legs on one signal day.
    Each re-entry is taken at the candle AFTER the SL candle.
    Capital is updated leg-by-leg (loss reduces capital for next lot sizing).
    Returns list of leg result dicts (augmented with 'capital_before'/'capital_after').
    """
    dte = DAY_DTE.get(weekday, 1)
    rr  = DAY_RR.get(weekday, 1.4)

    # Find Entry 1 candle
    entry_row = day_candles[day_candles["time"] == entry_time]
    if entry_row.empty:
        return []

    entry_candle_idx = int(entry_row.index[0])
    entry_bn         = float(entry_row.iloc[0]["open"])

    legs    = []
    capital = capital_start

    for attempt in range(max_entries):
        if entry_bn is None:
            break

        cap_before = capital
        leg        = simulate_leg(day_candles, signal, entry_candle_idx, entry_bn,
                                  dte, rr, capital)
        capital   += leg["pnl"]

        legs.append({
            **leg,
            "entry_num":      attempt + 1,
            "capital_before": round(cap_before, 2),
            "capital_after":  round(capital, 2),
        })

        # Re-enter only if SL hit AND a valid next candle exists
        if leg["outcome"] != "LOSS" or leg["next_entry_bn"] is None:
            break

        # Set up next leg
        entry_candle_idx = leg["sl_candle_idx"]
        entry_bn         = leg["next_entry_bn"]

    return legs


# ── Main backtest loop ────────────────────────────────────────────────────────

def run_backtest(max_entries=3, entry_time="09:15"):
    """
    Run the multi-entry backtest.
    Returns a DataFrame with one row per trade leg.
    """
    intraday_path = f"{DATA_DIR}/banknifty_5min.csv"
    signals_path  = f"{DATA_DIR}/signals.csv"

    if not os.path.exists(intraday_path):
        print(f"\nERROR: {intraday_path} not found.")
        print("Fetch intraday data first:  python3 fetch_intraday.py")
        sys.exit(1)
    if not os.path.exists(signals_path):
        print(f"\nERROR: {signals_path} not found.")
        print("Generate signals first:     python3 signal_engine.py")
        sys.exit(1)

    # Load 5-min data
    df5 = pd.read_csv(intraday_path, parse_dates=["datetime"])
    df5["date"] = df5["datetime"].dt.date
    df5["time"] = df5["datetime"].dt.strftime("%H:%M")
    df5 = df5[(df5["time"] >= "09:15") & (df5["time"] <= "15:30")].copy()

    # Load signals — filter to CALL/PUT only and to days covered by 5-min data
    sigs = pd.read_csv(signals_path, parse_dates=["date"])
    sigs = sigs.drop(columns=["threshold"], errors="ignore")
    sigs = sigs[sigs["signal"].isin(["CALL", "PUT"])].reset_index(drop=True)
    sigs["date_py"] = sigs["date"].dt.date

    intraday_dates = set(df5["date"].unique())
    trade_sigs = sigs[sigs["date_py"].isin(intraday_dates)].reset_index(drop=True)

    if trade_sigs.empty:
        print("\nNo signal days overlap with the 5-min data window.")
        print("Run:  python3 signal_engine.py   then retry.")
        sys.exit(1)

    # Index 5-min data by date for fast lookup
    by_date = {d: grp.reset_index(drop=True) for d, grp in df5.groupby("date")}

    # Simulate
    capital       = STARTING_CAPITAL
    current_month = None
    rows          = []

    for _, sig in trade_sigs.iterrows():
        d         = sig["date_py"]
        weekday   = sig["weekday"]
        signal    = sig["signal"]
        score     = sig.get("score", None)
        month_key = (d.year, d.month)

        # Monthly top-up at first trade of new month
        if current_month is None:
            current_month = month_key
        elif month_key != current_month:
            capital      += MONTHLY_TOPUP
            current_month = month_key

        if d not in by_date:
            continue

        cap_day_start = capital
        legs = simulate_day(by_date[d], signal, weekday, entry_time,
                            max_entries, capital)

        for leg in legs:
            capital = leg["capital_after"]    # already updated inside simulate_day
            rows.append({
                "date":           d,
                "weekday":        weekday,
                "signal":         signal,
                "score":          score,
                "entry_num":      leg["entry_num"],
                "lots":           leg["lots"],
                "premium":        leg["premium"],
                "outcome":        leg["outcome"],
                "pnl":            leg["pnl"],
                "charges":        leg["charges"],
                "exit_time":      leg["exit_time"],
                "capital_before": leg["capital_before"],
                "capital_after":  leg["capital_after"],
            })

    return pd.DataFrame(rows)


# ── Summary printer ───────────────────────────────────────────────────────────

def print_summary(df, max_entries, entry_time):
    if df.empty:
        print("No trades to show.")
        return

    active = df[df["outcome"].isin(["WIN", "LOSS", "PARTIAL"])]
    window = f"{df['date'].min()} → {df['date'].max()}"

    print(f"\n{'='*70}")
    print(f"  MULTI-ENTRY BACKTEST  (max {max_entries} entries/day, entry @ {entry_time} AM)")
    print(f"  {window}   |   {df['date'].nunique()} signal days")
    print(f"{'='*70}")

    # ── By entry number ───────────────────────────────────────────────────────
    print(f"\n  P&L BY ENTRY NUMBER:")
    print(f"  {'Entry':>12}  {'Trades':>7}  {'W':>4}  {'L':>4}  {'P':>4}  {'WR':>7}  {'Net P&L':>12}")
    print(f"  {'─'*58}")

    for n in sorted(active["entry_num"].unique()):
        d  = active[active["entry_num"] == n]
        w  = (d["outcome"] == "WIN").sum()
        l  = (d["outcome"] == "LOSS").sum()
        p  = (d["outcome"] == "PARTIAL").sum()
        wr = w / (w + l) * 100 if (w + l) > 0 else 0
        net = d["pnl"].sum()
        label = "Initial" if n == 1 else f"Re-entry {n-1}"
        print(f"  {label:>12}  {len(d):>7}  {w:>4}  {l:>4}  {p:>4}  {wr:>6.1f}%  ₹{net:>10,.0f}")

    print(f"  {'─'*58}")
    w  = (active["outcome"] == "WIN").sum()
    l  = (active["outcome"] == "LOSS").sum()
    p  = (active["outcome"] == "PARTIAL").sum()
    wr = w / (w + l) * 100 if (w + l) > 0 else 0
    net = active["pnl"].sum()
    print(f"  {'TOTAL':>12}  {len(active):>7}  {w:>4}  {l:>4}  {p:>4}  {wr:>6.1f}%  ₹{net:>10,.0f}")

    # ── Re-entry contribution ─────────────────────────────────────────────────
    e1_pnl = active[active["entry_num"] == 1]["pnl"].sum()
    re_pnl = active[active["entry_num"] >  1]["pnl"].sum()
    days_reentry = df[df["entry_num"] > 1]["date"].nunique()
    total_days   = df["date"].nunique()
    print(f"\n  Re-entry contribution:")
    print(f"    Entry 1 (initial)  : ₹{e1_pnl:>10,.0f}")
    print(f"    Re-entries (2-{max_entries})   : ₹{re_pnl:>10,.0f}  "
          f"({'adds value' if re_pnl > 0 else 'drag on P&L'})")
    print(f"    SL→re-entry taken  : {days_reentry}/{total_days} signal days "
          f"({days_reentry/total_days*100:.0f}% of days)")

    # ── Weekday breakdown ─────────────────────────────────────────────────────
    print(f"\n  WEEKDAY BREAKDOWN  (all entries combined):")
    print(f"  {'Day':>12}  {'Trades':>7}  {'WR':>7}  {'Net P&L':>12}")
    print(f"  {'─'*42}")
    for day in ["Monday","Tuesday","Wednesday","Thursday","Friday"]:
        d = active[active["weekday"] == day]
        if d.empty:
            continue
        dw  = (d["outcome"] == "WIN").sum()
        dl  = (d["outcome"] == "LOSS").sum()
        dwr = dw / (dw + dl) * 100 if (dw + dl) > 0 else 0
        print(f"  {day:>12}  {len(d):>7}  {dwr:>6.0f}%  ₹{d['pnl'].sum():>10,.0f}")

    # ── Capital summary ───────────────────────────────────────────────────────
    end_cap       = df["capital_after"].iloc[-1]
    total_charges = df["charges"].sum()
    cap_series    = df["capital_after"]
    max_dd        = ((cap_series - cap_series.cummax()) / cap_series.cummax() * 100).min()

    print(f"\n  CAPITAL SUMMARY:")
    print(f"    Starting capital    : ₹{STARTING_CAPITAL:>10,.0f}")
    print(f"    Ending capital      : ₹{end_cap:>10,.2f}")
    print(f"    Total charges       : ₹{total_charges:>10,.2f}")
    print(f"    Max drawdown        : {max_dd:.1f}%")
    print(f"{'='*70}")


# ── Strategy comparison ───────────────────────────────────────────────────────

def compare_strategies(entry_time="09:15"):
    """Run 1 / 2 / 3 max entries and print side-by-side comparison."""
    print(f"\n{'═'*70}")
    print(f"  MULTI-ENTRY COMPARISON  (entry @ {entry_time} AM)")
    print(f"{'═'*70}")
    print(f"  {'Strategy':>16}  {'Trades':>7}  {'WR':>7}  {'Net P&L':>12}  "
          f"{'End Cap':>12}  {'Max DD':>8}")
    print(f"  {'─'*68}")

    results = []
    for me in [1, 2, 3]:
        df     = run_backtest(max_entries=me, entry_time=entry_time)
        active = df[df["outcome"].isin(["WIN","LOSS","PARTIAL"])]
        if active.empty:
            continue

        w   = (active["outcome"] == "WIN").sum()
        l   = (active["outcome"] == "LOSS").sum()
        wr  = w / (w + l) * 100 if (w + l) > 0 else 0
        net = active["pnl"].sum()
        end = df["capital_after"].iloc[-1]
        cs  = df["capital_after"]
        dd  = ((cs - cs.cummax()) / cs.cummax() * 100).min()

        label = "Single entry" if me == 1 else f"Max {me} entries"
        results.append({"label": label, "me": me, "trades": len(active),
                        "wr": wr, "net": net, "end": end, "dd": dd, "df": df})
        print(f"  {label:>16}  {len(active):>7}  {wr:>6.1f}%  ₹{net:>10,.0f}  "
              f"₹{end:>10,.0f}  {dd:>7.1f}%")

    if not results:
        print("  No results to compare.")
        return

    # Delta vs single-entry baseline
    base = results[0]["net"]
    print(f"  {'─'*68}")
    for r in results[1:]:
        delta = r["net"] - base
        sign  = "+" if delta >= 0 else ""
        verdict = "BETTER" if delta > 0 else "WORSE"
        print(f"  {r['label']:>16} vs single: {sign}₹{delta:,.0f} net P&L  [{verdict}]")

    print(f"{'═'*70}")

    # Detailed breakdown for the best-performing variant
    best = max(results, key=lambda r: r["net"])
    print(f"\n  → Best: {best['label']}")
    print_summary(best["df"], best["me"], entry_time)

    # Save best result
    out = f"{DATA_DIR}/multi_entry_log.csv"
    best["df"].to_csv(out, index=False)
    print(f"\n  Saved → {out}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args        = sys.argv[1:]
    entry_time  = "09:15"
    max_entries = None    # None = run comparison

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--entries" and i + 1 < len(args):
            try:
                max_entries = int(args[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if a == "--entry-time" and i + 1 < len(args):
            entry_time = args[i + 1]
            i += 2
            continue
        i += 1

    print("=" * 70)
    print("  BankNifty Options — Multi-Entry Re-entry Backtest")
    print("  Strategy: if SL hit → re-enter same direction (same day)")
    print("=" * 70)

    if max_entries is None:
        # Default: compare 1 / 2 / 3 entries
        compare_strategies(entry_time=entry_time)
    else:
        df = run_backtest(max_entries=max_entries, entry_time=entry_time)
        print_summary(df, max_entries, entry_time)
        out = f"{DATA_DIR}/multi_entry_log.csv"
        df.to_csv(out, index=False)
        print(f"\n  Saved → {out}")


if __name__ == "__main__":
    main()
