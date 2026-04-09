import pandas as pd
import numpy as np
import os

DATA_DIR         = "data"
LOT_SIZE         = 30
RISK_PCT         = 0.05     # 5% of capital risked per trade
SL_PCT           = 0.30     # stop-loss = 30% of premium
STARTING_CAPITAL = 30_000
MONTHLY_TOPUP    = 10_000

# ATM premium as % of spot (approximation: no live options data)
PREMIUM_FACTOR = {"Tuesday": 0.004, "Friday": 0.009}

# Reward-to-risk ratios
RR = {"Tuesday": 1.4, "Friday": 2.0}

# ── Dhan brokerage + statutory charges (per round-trip trade) ─────────────────
# Source: dhan.co/pricing + NSE circulars (as of 2024-25)
#
#   Brokerage      : ₹20 per order × 2 (buy + sell) = ₹40 flat
#   STT            : 0.0625% of premium on SELL side (Budget 2023 rate)
#   Exchange (NSE) : 0.053% per side on premium (index F&O)
#   Clearing (NSCCL): 0.0005% per side
#   GST            : 18% on (brokerage + exchange + clearing charges)
#   Stamp duty     : 0.003% on buy-side premium
#   SEBI turnover  : 0.0001% on total turnover (both sides) — negligible

def calculate_charges(premium, lots):
    """Return total round-trip transaction cost for one trade."""
    pv = lots * LOT_SIZE * premium          # total premium value

    brokerage       = 40.0                  # ₹20 × 2 orders
    stt             = 0.000625 * pv         # 0.0625% on sell side
    exchange        = 0.00053  * pv * 2    # 0.053% per side
    clearing        = 0.000005 * pv * 2    # 0.0005% per side
    gst             = 0.18 * (brokerage + exchange + clearing)
    stamp_duty      = 0.00003  * pv        # 0.003% on buy side
    sebi            = 0.000001 * pv * 2    # negligible

    total = brokerage + stt + exchange + clearing + gst + stamp_duty + sebi
    return round(total, 2)


# ── Loaders ──────────────────────────────────────────────────────────────────

def load_signals():
    df = pd.read_csv(f"{DATA_DIR}/signals.csv", parse_dates=["date"])
    return df[df["signal"].isin(["CALL", "PUT"])].reset_index(drop=True)


def load_bn_ohlcv():
    df = pd.read_csv(f"{DATA_DIR}/banknifty.csv", parse_dates=["date"])
    return df.set_index("date")


# ── Trade simulator ───────────────────────────────────────────────────────────

def simulate_trade(row, bn_ohlcv, capital):
    """
    Simulate one trade using same-day OHLCV to approximate intraday exit.
    Returns (pnl, result, lots, premium).
    """
    date    = row["date"]
    weekday = row["weekday"]
    signal  = row["signal"]

    if date not in bn_ohlcv.index:
        return 0.0, "SKIPPED", 0, 0.0

    bar     = bn_ohlcv.loc[date]
    bn_open  = bar["open"]
    bn_high  = bar["high"]
    bn_low   = bar["low"]
    bn_close = bar["close"]

    pf      = PREMIUM_FACTOR[weekday]
    rr      = RR[weekday]
    premium = bn_open * pf

    # ── Lot sizing ────────────────────────────────────────────────────────────
    max_loss_1lot = LOT_SIZE * premium * SL_PCT
    if max_loss_1lot > capital * 0.15:          # even 1 lot is too expensive
        return 0.0, "SKIPPED_LOW_CAPITAL", 0, premium

    lots = max(1, int((capital * RISK_PCT) / max_loss_1lot))

    # ── SL / TP levels in underlying points (delta ≈ 0.5 for ATM) ───────────
    sl_pts = (SL_PCT  * premium) / 0.5          # underlying drop to lose 30% option
    tp_pts = (rr * SL_PCT * premium) / 0.5      # underlying move to hit target

    # ── Exit logic ────────────────────────────────────────────────────────────
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
        else:                                    # neither hit — exit at close
            gross = (bn_close - bn_open) * 0.5 * lots * LOT_SIZE
            charges = calculate_charges(premium, lots)
            return round(gross - charges, 2), "PARTIAL", lots, round(premium, 2)

    else:  # PUT
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
            gross = (bn_open - bn_close) * 0.5 * lots * LOT_SIZE
            charges = calculate_charges(premium, lots)
            return round(gross - charges, 2), "PARTIAL", lots, round(premium, 2)

    charges = calculate_charges(premium, lots)
    if result == "WIN":
        pnl =  lots * LOT_SIZE * premium * rr  * SL_PCT - charges
    else:
        pnl = -lots * LOT_SIZE * premium * SL_PCT - charges

    return round(pnl, 2), result, lots, round(premium, 2)


# ── Backtest loop ─────────────────────────────────────────────────────────────

def run_backtest():
    signals  = load_signals()
    bn_ohlcv = load_bn_ohlcv()

    capital       = STARTING_CAPITAL
    current_month = None
    trade_log     = []

    for _, row in signals.iterrows():
        date      = row["date"]
        month_key = (date.year, date.month)

        # Monthly top-up at the first trade of each new calendar month
        if current_month is None:
            current_month = month_key
        elif month_key != current_month:
            capital      += MONTHLY_TOPUP
            current_month = month_key

        capital_before = capital
        pnl, result, lots, premium = simulate_trade(row, bn_ohlcv, capital)
        charges = calculate_charges(premium, lots) if lots > 0 else 0
        capital += pnl

        bn_open = (bn_ohlcv.loc[date, "open"]
                   if date in bn_ohlcv.index else None)

        trade_log.append({
            "date":           date.date(),
            "weekday":        row["weekday"],
            "signal":         row["signal"],
            "score":          row["score"],
            "bn_open":        round(bn_open, 2) if bn_open else None,
            "premium":        premium,
            "lots":           lots,
            "risk_amt":       round(lots * LOT_SIZE * premium * SL_PCT, 2),
            "charges":        charges,
            "result":         result,
            "pnl":            pnl,
            "capital_before": round(capital_before, 2),
            "capital_after":  round(capital, 2),
        })

    trade_df = pd.DataFrame(trade_log)

    # Monthly equity curve
    active = trade_df[trade_df["result"].isin(["WIN", "LOSS", "PARTIAL"])].copy()
    active["month"] = pd.to_datetime(active["date"]).dt.to_period("M")
    monthly = (active.groupby("month")
                     .agg(
                         trades     =("result", "count"),
                         wins       =("result", lambda x: (x == "WIN").sum()),
                         losses     =("result", lambda x: (x == "LOSS").sum()),
                         monthly_pnl=("pnl", "sum"),
                         end_capital=("capital_after", "last"),
                     )
                     .reset_index())

    return trade_df, monthly


# ── Summary printer ───────────────────────────────────────────────────────────

def print_summary(trade_df, monthly):
    active  = trade_df[trade_df["result"].isin(["WIN", "LOSS", "PARTIAL"])]
    wins    = (active["result"] == "WIN").sum()
    losses  = (active["result"] == "LOSS").sum()
    partial = (active["result"] == "PARTIAL").sum()
    total   = len(active)
    skipped = (trade_df["result"].str.startswith("SKIPPED")).sum()

    start_cap     = STARTING_CAPITAL
    end_cap       = trade_df["capital_after"].iloc[-1]
    total_pnl     = active["pnl"].sum()
    total_charges = trade_df["charges"].sum() if "charges" in trade_df.columns else 0
    gross_pnl     = total_pnl + total_charges
    topups        = (trade_df["capital_before"].diff() > 5000).sum()

    # Max drawdown on capital series
    cap_series  = trade_df["capital_after"]
    rolling_max = cap_series.cummax()
    drawdown    = (cap_series - rolling_max) / rolling_max * 100
    max_dd      = drawdown.min()

    print(f"\n{'='*56}")
    print(f"   BANKNIFTY OPTIONS BACKTEST — Sep 2021 to Apr 2026")
    print(f"{'='*56}")
    print(f"  Starting capital    : ₹{start_cap:>10,.0f}")
    print(f"  Monthly top-ups     : ₹10,000 × {topups} months = ₹{topups*10000:,.0f}")
    print(f"  Total injected      : ₹{start_cap + topups*10000:>10,.0f}")
    print(f"  Ending capital      : ₹{end_cap:>10,.2f}")
    print(f"{'─'*56}")
    print(f"  Gross trading P&L   : ₹{gross_pnl:>10,.2f}")
    print(f"  Total charges paid  : ₹{total_charges:>10,.2f}  ← brokerage+STT+GST etc.")
    print(f"  Net trading P&L     : ₹{total_pnl:>10,.2f}  ← after all costs")
    verdict = "PROFITABLE ✓" if total_pnl > 0 else "NOT PROFITABLE ✗"
    print(f"  Verdict             : {verdict}")
    print(f"{'─'*56}")
    print(f"  Signals generated   : {total + skipped}")
    print(f"  Trades taken        : {total}")
    print(f"  Skipped (low cap)   : {skipped}")
    print(f"{'─'*56}")
    print(f"  Wins                : {wins}  ({wins/total*100:.1f}%)")
    print(f"  Losses              : {losses}  ({losses/total*100:.1f}%)")
    print(f"  Partial exits       : {partial}  ({partial/total*100:.1f}%)")
    wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
    print(f"  Win rate (W vs L)   : {wr:.1f}%")
    print(f"{'─'*56}")
    print(f"  Best trade          : ₹{active['pnl'].max():>10,.2f}")
    print(f"  Worst trade         : ₹{active['pnl'].min():>10,.2f}")
    print(f"  Avg trade P&L       : ₹{active['pnl'].mean():>10,.2f}")
    print(f"  Max drawdown        : {max_dd:.1f}%")
    print(f"{'='*56}")

    print(f"\nMonthly breakdown (first 8 months):")
    cols = ["month", "trades", "wins", "losses", "monthly_pnl", "end_capital"]
    print(monthly[cols].head(8).to_string(index=False))


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print("Running backtest...")
    trade_df, monthly = run_backtest()

    trade_df.to_csv(f"{DATA_DIR}/trade_log.csv",    index=False)
    monthly.to_csv( f"{DATA_DIR}/equity_curve.csv", index=False)

    print_summary(trade_df, monthly)

    print(f"\nSaved → {DATA_DIR}/trade_log.csv")
    print(f"Saved → {DATA_DIR}/equity_curve.csv")


if __name__ == "__main__":
    main()
