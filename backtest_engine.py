import pandas as pd
import numpy as np
import os

DATA_DIR         = "data"
LOT_SIZE         = 30
RISK_PCT         = 0.05     # 5% of capital risked per trade
SL_PCT           = 0.20     # stop-loss = 20% of premium
STARTING_CAPITAL = 30_000
MONTHLY_TOPUP    = 10_000

# ATM premium as % of spot using sqrt(DTE) scaling.
# Calibrated from: Tuesday (1 DTE) = 0.4%, Friday (5 DTE) = 0.9%
# Formula: premium = spot × PREMIUM_K × sqrt(DTE)
# Verification: 0.004 × √1 = 0.4% ✓   0.004 × √5 = 0.894% ≈ 0.9% ✓
PREMIUM_K = 0.004
MAX_LOTS  = 20      # cap to keep backtest realistic (liquidity + margin constraints)

# Days to expiry per weekday (BankNifty expires Wednesday)
# Wednesday = 0 DTE (expiry day): ~6 hours of trading at open ≈ 0.25 of a day
DAY_DTE = {
    "Monday":    2,      # Mon → Wed = 2 days
    "Tuesday":   1,      # Tue → Wed = 1 day
    "Wednesday": 0.25,   # expiry day — ~6 hrs trading left at open
    "Thursday":  6,      # Thu → next Wed = 6 days
    "Friday":    5,      # Fri → next Wed = 5 days
}

# Reward-to-risk ratios per day — flat 2.0x across all days
# Chosen from SL%×RR grid backtest (trail=₹5): SL=20%, RR=2.0x gives best net P&L
# TP = entry_premium × (1 + SL_PCT × RR) = +40% of premium
DAY_RR = {
    "Monday":    2.0,
    "Tuesday":   2.0,
    "Wednesday": 2.0,
    "Thursday":  2.0,
    "Friday":    2.0,
}

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

def calculate_charges(premium, lots, breakdown=False):
    """
    Return total round-trip transaction cost for one trade.
    If breakdown=True, returns (total, dict_of_components).
    """
    pv = lots * LOT_SIZE * premium          # total premium value

    brokerage  = 40.0                       # ₹20 × 2 orders
    stt        = 0.000625 * pv              # 0.0625% on sell side
    exchange   = 0.00053  * pv * 2         # 0.053% per side
    clearing   = 0.000005 * pv * 2         # 0.0005% per side
    gst        = 0.18 * (brokerage + exchange + clearing)
    stamp_duty = 0.00003  * pv             # 0.003% on buy side
    sebi       = 0.000001 * pv * 2         # negligible

    total = brokerage + stt + exchange + clearing + gst + stamp_duty + sebi

    if breakdown:
        return round(total, 2), {
            "c_brokerage":  round(brokerage,  2),
            "c_stt":        round(stt,         2),
            "c_exchange":   round(exchange,    2),
            "c_clearing":   round(clearing,    2),
            "c_gst":        round(gst,         2),
            "c_stamp_duty": round(stamp_duty,  2),
            "c_sebi":       round(sebi,        2),
        }
    return round(total, 2)


# ── Loaders ──────────────────────────────────────────────────────────────────

def load_signals():
    df = pd.read_csv(f"{DATA_DIR}/signals.csv", parse_dates=["date"])
    if "threshold" in df.columns:
        df = df.drop(columns=["threshold"])
    return df[df["signal"].isin(["CALL", "PUT"])].reset_index(drop=True)


def load_all_signals():
    """Load full signals including NONE rows — used for summary stats."""
    return pd.read_csv(f"{DATA_DIR}/signals.csv", parse_dates=["date"])


def load_bn_ohlcv():
    df = pd.read_csv(f"{DATA_DIR}/banknifty.csv", parse_dates=["date"])
    return df.set_index("date")


# ── Trade simulator ───────────────────────────────────────────────────────────

def simulate_trade(row, bn_ohlcv, capital, trail_jump_opt=0, sl_pct=None,
                   flat_rr=None, day_rr_override=None):
    """
    Simulate one trade using same-day OHLCV to approximate intraday exit.

    trail_jump_opt  : trailing stop in option-price rupees (Dhan trailingJump). 0 = off.
    sl_pct          : override SL_PCT global (e.g. 0.20 for 20% SL). None = use global.
    flat_rr         : use this RR for every day instead of DAY_RR dict. None = use per-day.
    day_rr_override : dict like DAY_RR to use instead of the module-level DAY_RR.

    Returns (pnl, result, lots, premium, charges_total, charges_breakdown).
    """
    date    = row["date"]
    weekday = row["weekday"]
    signal  = row["signal"]

    zero_breakdown = {k: 0.0 for k in
                      ["c_brokerage","c_stt","c_exchange","c_clearing",
                       "c_gst","c_stamp_duty","c_sebi"]}

    if date not in bn_ohlcv.index:
        return 0.0, "SKIPPED", 0, 0.0, 0.0, zero_breakdown

    bar      = bn_ohlcv.loc[date]
    bn_open  = bar["open"]
    bn_high  = bar["high"]
    bn_low   = bar["low"]
    bn_close = bar["close"]

    dte  = DAY_DTE.get(weekday, 1)
    if flat_rr is not None:
        rr = flat_rr
    elif day_rr_override is not None:
        rr = day_rr_override.get(weekday, 1.4)
    else:
        rr = DAY_RR.get(weekday, 1.4)
    sl   = sl_pct  if sl_pct  is not None else SL_PCT
    premium = bn_open * PREMIUM_K * (dte ** 0.5)

    # ── Lot sizing ────────────────────────────────────────────────────────────
    max_loss_1lot = LOT_SIZE * premium * sl
    if max_loss_1lot > capital * 0.15:
        return 0.0, "SKIPPED_LOW_CAPITAL", 0, premium, 0.0, zero_breakdown

    lots = min(MAX_LOTS, max(1, int((capital * RISK_PCT) / max_loss_1lot)))

    # ── SL / TP levels in BN points (ATM delta ≈ 0.5) ────────────────────────
    sl_pts = (sl  * premium) / 0.5
    tp_pts = (rr * sl * premium) / 0.5

    # ── Trailing SL helpers ───────────────────────────────────────────────────
    trail_jump_bn = (trail_jump_opt / 0.5) if trail_jump_opt > 0 else 0

    def trail_steps(favorable_bn_move):
        return int(favorable_bn_move / trail_jump_bn) if trail_jump_bn > 0 else 0

    def trail_exit_pnl(favorable_bn_move, n_steps):
        """P&L when trailing SL fires. Returns (gross_pnl, label).
        No breakeven cap — trail SL can exit profitably when price moves far enough.
        """
        opt_exit = premium * (1 - sl) + n_steps * trail_jump_opt
        gross    = (opt_exit - premium) * lots * LOT_SIZE
        label    = "TRAIL_SL" if opt_exit > premium * (1 - sl) else "LOSS"
        return gross, label

    # ── CALL exit logic ───────────────────────────────────────────────────────
    if signal == "CALL":
        orig_sl  = bn_open - sl_pts
        tp_level = bn_open + tp_pts
        fav      = max(0.0, bn_high - bn_open)
        steps    = trail_steps(fav)
        # Trail SL ratchets up in BN pts — no cap at bn_open (trail can go profitable)
        sl_level = (orig_sl + steps * trail_jump_bn) if trail_jump_bn > 0 else orig_sl

        sl_hit = bn_low  <= sl_level
        tp_hit = bn_high >= tp_level

        if sl_hit and tp_hit:
            result = "WIN" if bn_close > bn_open else "LOSS"
        elif tp_hit:
            result = "WIN"
        elif sl_hit:
            if trail_jump_opt > 0 and steps > 0:
                gross, label = trail_exit_pnl(fav, steps)
                charges, bd  = calculate_charges(premium, lots, breakdown=True)
                return round(gross - charges, 2), label, lots, round(premium, 2), charges, bd
            result = "LOSS"
        else:
            gross = (bn_close - bn_open) * 0.5 * lots * LOT_SIZE
            charges, bd = calculate_charges(premium, lots, breakdown=True)
            return round(gross - charges, 2), "PARTIAL", lots, round(premium, 2), charges, bd

    # ── PUT exit logic ────────────────────────────────────────────────────────
    else:
        orig_sl  = bn_open + sl_pts
        tp_level = bn_open - tp_pts
        fav      = max(0.0, bn_open - bn_low)
        steps    = trail_steps(fav)
        # Trail SL ratchets down in BN pts — no floor at bn_open (trail can go profitable)
        sl_level = (orig_sl - steps * trail_jump_bn) if trail_jump_bn > 0 else orig_sl

        sl_hit = bn_high >= sl_level
        tp_hit = bn_low  <= tp_level

        if sl_hit and tp_hit:
            result = "WIN" if bn_close < bn_open else "LOSS"
        elif tp_hit:
            result = "WIN"
        elif sl_hit:
            if trail_jump_opt > 0 and steps > 0:
                gross, label = trail_exit_pnl(fav, steps)
                charges, bd  = calculate_charges(premium, lots, breakdown=True)
                return round(gross - charges, 2), label, lots, round(premium, 2), charges, bd
            result = "LOSS"
        else:
            gross = (bn_open - bn_close) * 0.5 * lots * LOT_SIZE
            charges, bd = calculate_charges(premium, lots, breakdown=True)
            return round(gross - charges, 2), "PARTIAL", lots, round(premium, 2), charges, bd

    charges, bd = calculate_charges(premium, lots, breakdown=True)
    if result == "WIN":
        pnl =  lots * LOT_SIZE * premium * rr  * sl - charges
    else:
        pnl = -lots * LOT_SIZE * premium * sl - charges

    return round(pnl, 2), result, lots, round(premium, 2), charges, bd


# ── Backtest loop ─────────────────────────────────────────────────────────────

def run_backtest(trail_jump_opt=0, sl_pct=None, flat_rr=None, day_rr_override=None):
    signals  = load_signals()
    bn_ohlcv = load_bn_ohlcv()

    capital       = STARTING_CAPITAL
    current_month = None
    trade_log     = []

    for _, row in signals.iterrows():
        date      = row["date"]
        month_key = (date.year, date.month)

        if current_month is None:
            current_month = month_key
        elif month_key != current_month:
            capital      += MONTHLY_TOPUP
            current_month = month_key

        capital_before = capital
        pnl, result, lots, premium, charges, charges_bd = simulate_trade(
            row, bn_ohlcv, capital,
            trail_jump_opt=trail_jump_opt,
            sl_pct=sl_pct,
            flat_rr=flat_rr,
            day_rr_override=day_rr_override)
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
            **charges_bd,
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

def run_sl_tp_grid(trail_jump_opt=5):
    """
    Grid search: SL% × flat RR, all with trail=₹5.
    Shows net P&L, win rate, max drawdown, and ending capital.
    """
    sl_options = [0.20, 0.25, 0.30, 0.35]
    rr_options = [1.0, 1.5, 2.0]
    rows = []

    for sl in sl_options:
        for rr in rr_options:
            trade_df, _ = run_backtest(trail_jump_opt=trail_jump_opt, sl_pct=sl, flat_rr=rr)
            active   = trade_df[trade_df["result"].isin(["WIN","LOSS","PARTIAL","TRAIL_SL"])]
            wins     = (active["result"] == "WIN").sum()
            losses   = (active["result"] == "LOSS").sum()
            trail_sl = (active["result"] == "TRAIL_SL").sum()
            total    = len(active)
            net_pnl  = active["pnl"].sum()
            end_cap  = trade_df["capital_after"].iloc[-1]
            wr       = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

            cap_series = trade_df["capital_after"]
            max_dd     = ((cap_series - cap_series.cummax()) / cap_series.cummax() * 100).min()

            rows.append({
                "SL%":      f"{int(sl*100)}%",
                "RR":       f"{rr:.1f}x",
                "TP%":      f"{int(sl*rr*100)}%",
                "wins":     wins,
                "losses":   losses,
                "trail_sl": trail_sl,
                "WR%":      f"{wr:.0f}%",
                "net_pnl":  net_pnl,
                "end_cap":  end_cap,
                "max_dd":   f"{max_dd:.1f}%",
            })

    df = pd.DataFrame(rows)

    # Sort by net P&L descending for easy reading
    df = df.sort_values("net_pnl", ascending=False).reset_index(drop=True)

    # Format for display
    df_display = df.copy()
    df_display["net_pnl"] = df_display["net_pnl"].apply(lambda x: f"₹{x:,.0f}")
    df_display["end_cap"] = df_display["end_cap"].apply(lambda x: f"₹{x:,.0f}")

    print(f"\n{'='*90}")
    print(f"  SL% × RR GRID  —  trail=₹{trail_jump_opt}, ranked by net P&L")
    print(f"  Live config: SL=20%, flat RR=2.0×, trail=₹5  (TP=+40% of premium)")
    print(f"{'='*90}")
    print(df_display.drop(columns=["net_pnl_raw"] if "net_pnl_raw" in df_display else [])
          .to_string(index=True))
    print(f"{'='*90}")
    print(f"\n  trail_sl = losses converted to smaller exits by trailing SL")
    print(f"  WR% = wins / (wins + full losses) — excludes trail_sl and partials\n")


def run_rr_comparison(sl_pct=0.20, trail_jump_opt=5):
    """
    Compare per-day RR configs vs flat RR, all at a fixed SL% and trail.
    Tests: original per-day mix, flat 1.0x, 1.5x, 2.0x, 2.5x, 3.0x
    """
    ORIGINAL_PER_DAY = {
        "Monday":    1.6,
        "Tuesday":   1.4,
        "Wednesday": 1.0,
        "Thursday":  2.0,
        "Friday":    2.0,
    }

    configs = [
        ("per-day  Mon1.6 Tue1.4 Wed1.0 Thu2.0 Fri2.0", None,  ORIGINAL_PER_DAY),
        ("flat 1.0×  (TP=+20%)",                          1.0,  None),
        ("flat 1.5×  (TP=+30%)",                          1.5,  None),
        ("flat 2.0×  (TP=+40%)",                          2.0,  None),
        ("flat 2.5×  (TP=+50%)",                          2.5,  None),
        ("flat 3.0×  (TP=+60%)",                          3.0,  None),
    ]

    rows = []
    for label, flat_rr, day_override in configs:
        trade_df, _ = run_backtest(
            trail_jump_opt=trail_jump_opt,
            sl_pct=sl_pct,
            flat_rr=flat_rr,
            day_rr_override=day_override,
        )
        active   = trade_df[trade_df["result"].isin(["WIN","LOSS","PARTIAL","TRAIL_SL"])]
        wins     = (active["result"] == "WIN").sum()
        losses   = (active["result"] == "LOSS").sum()
        trail_sl = (active["result"] == "TRAIL_SL").sum()
        net_pnl  = active["pnl"].sum()
        end_cap  = trade_df["capital_after"].iloc[-1]
        wr       = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

        cap_series = trade_df["capital_after"]
        max_dd     = ((cap_series - cap_series.cummax()) / cap_series.cummax() * 100).min()

        rows.append({
            "RR config":  label,
            "wins":       wins,
            "losses":     losses,
            "trail_sl":   trail_sl,
            "WR%":        f"{wr:.0f}%",
            "net_pnl":    net_pnl,
            "net_pnl_fmt":f"₹{net_pnl:,.0f}",
            "end_cap":    f"₹{end_cap:,.0f}",
            "max_dd":     f"{max_dd:.1f}%",
        })

    df = pd.DataFrame(rows).sort_values("net_pnl", ascending=False).reset_index(drop=True)

    print(f"\n{'='*95}")
    print(f"  RR COMPARISON  —  SL={int(sl_pct*100)}%, trail=₹{trail_jump_opt}, ranked by net P&L")
    print(f"{'='*95}")
    print(df.drop(columns=["net_pnl"]).to_string(index=True))
    print(f"{'='*95}\n")


def run_trail_comparison():
    """
    Compare fixed SL (trailingJump=0) vs trailing SL at ₹5, ₹10, ₹20 in option price.
    These map directly to Dhan super order trailingJump field values.
    """
    trail_values = [0, 5, 10, 20]
    rows = []

    for trail in trail_values:
        trade_df, _ = run_backtest(trail_jump_opt=trail)
        active = trade_df[trade_df["result"].isin(["WIN", "LOSS", "PARTIAL", "TRAIL_SL"])]
        wins      = (active["result"] == "WIN").sum()
        losses    = (active["result"] == "LOSS").sum()
        trail_sl  = (active["result"] == "TRAIL_SL").sum()
        partial   = (active["result"] == "PARTIAL").sum()
        total     = len(active)
        net_pnl   = active["pnl"].sum()
        end_cap   = trade_df["capital_after"].iloc[-1]
        wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

        cap_series  = trade_df["capital_after"]
        rolling_max = cap_series.cummax()
        max_dd      = ((cap_series - rolling_max) / rolling_max * 100).min()

        label = f"trail=₹{trail}" if trail > 0 else "no trail (current)"
        rows.append({
            "config":    label,
            "trades":    total,
            "wins":      wins,
            "losses":    losses,
            "trail_sl":  trail_sl,
            "partial":   partial,
            "win_rate":  f"{wr:.1f}%",
            "net_pnl":   f"₹{net_pnl:,.0f}",
            "end_cap":   f"₹{end_cap:,.0f}",
            "max_dd":    f"{max_dd:.1f}%",
        })
        print(f"  trail=₹{trail:>2}: trades={total} | W={wins} L={losses}"
              f" T={trail_sl} | WR={wr:.1f}% | Net=₹{net_pnl:,.0f}"
              f" | Cap=₹{end_cap:,.0f} | DD={max_dd:.1f}%")

    print(f"\n{'='*110}")
    print(f"  TRAIL JUMP COMPARISON  (trailingJump = Dhan super order field, in option ₹)")
    print(f"{'='*110}")
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))
    print(f"{'='*110}")
    print(f"\n  trail_sl = trades exited by trailing SL (better than full loss)")
    print(f"  Recommended: use the trail value that gives highest net P&L without")
    print(f"  significantly increasing max drawdown vs the no-trail baseline.\n")


def print_summary(trade_df, monthly, threshold=None):
    active   = trade_df[trade_df["result"].isin(["WIN", "LOSS", "PARTIAL", "TRAIL_SL"])]
    wins     = (active["result"] == "WIN").sum()
    losses   = (active["result"] == "LOSS").sum()
    partial  = (active["result"] == "PARTIAL").sum()
    trail_sl = (active["result"] == "TRAIL_SL").sum()
    total    = len(active)
    skipped = (trade_df["result"].str.startswith("SKIPPED")).sum()

    # Event-skipped days — read from signals.csv if available
    event_skipped = 0
    try:
        all_sig = load_all_signals()
        if "event_day" in all_sig.columns:
            event_skipped = int(all_sig["event_day"].sum())
    except Exception:
        pass

    start_cap     = STARTING_CAPITAL
    end_cap       = trade_df["capital_after"].iloc[-1]
    total_pnl     = active["pnl"].sum()
    total_charges = trade_df["charges"].sum() if "charges" in trade_df.columns else 0
    gross_pnl     = total_pnl + total_charges
    actual_topups = round((end_cap - start_cap - total_pnl) / MONTHLY_TOPUP)
    topups        = max(0, actual_topups)

    # Max drawdown on capital series
    cap_series  = trade_df["capital_after"]
    rolling_max = cap_series.cummax()
    drawdown    = (cap_series - rolling_max) / rolling_max * 100
    max_dd      = drawdown.min()

    # ── Cost breakdown components ─────────────────────────────────────────────
    cost_cols = ["c_brokerage", "c_stt", "c_exchange", "c_clearing",
                 "c_gst", "c_stamp_duty", "c_sebi"]
    costs = {c: trade_df[c].sum() if c in trade_df.columns else 0.0 for c in cost_cols}

    thr_label = f"±{threshold}" if threshold is not None else "±?"

    print(f"\n{'='*60}")
    print(f"   BANKNIFTY OPTIONS BACKTEST — Sep 2021 to Apr 2026"
          f"  [threshold {thr_label}]")
    print(f"{'='*60}")
    print(f"  Starting capital    : ₹{start_cap:>10,.0f}")
    print(f"  Monthly top-ups     : ₹10,000 × {topups} months = ₹{topups*10000:,.0f}")
    print(f"  Total injected      : ₹{start_cap + topups*10000:>10,.0f}")
    print(f"  Ending capital      : ₹{end_cap:>10,.2f}")
    print(f"{'─'*60}")
    print(f"  Gross trading P&L   : ₹{gross_pnl:>10,.2f}")
    print(f"  Net trading P&L     : ₹{total_pnl:>10,.2f}  ← after all costs")
    verdict = "PROFITABLE ✓" if total_pnl > 0 else "NOT PROFITABLE ✗"
    print(f"  Verdict             : {verdict}")
    print(f"{'─'*60}")
    print(f"  TRANSACTION COSTS BREAKDOWN  ({total} trades)")
    print(f"  {'Brokerage':<20}: ₹{costs['c_brokerage']:>8,.2f}  (₹40 flat × {total} trades)")
    print(f"  {'STT':<20}: ₹{costs['c_stt']:>8,.2f}  (0.0625% of sell premium)")
    print(f"  {'NSE exchange':<20}: ₹{costs['c_exchange']:>8,.2f}  (0.053% × both sides)")
    print(f"  {'NSCCL clearing':<20}: ₹{costs['c_clearing']:>8,.2f}  (0.0005% × both sides)")
    print(f"  {'GST (18%)':<20}: ₹{costs['c_gst']:>8,.2f}  (on brokerage+exchange)")
    print(f"  {'Stamp duty':<20}: ₹{costs['c_stamp_duty']:>8,.2f}  (0.003% on buy side)")
    print(f"  {'SEBI turnover':<20}: ₹{costs['c_sebi']:>8,.2f}  (0.0001% on turnover)")
    print(f"  {'─'*42}")
    charges_pct = (total_charges / gross_pnl * 100) if gross_pnl != 0 else 0
    print(f"  {'TOTAL CHARGES':<20}: ₹{total_charges:>8,.2f}  ({charges_pct:.1f}% of gross P&L)")
    print(f"  {'Avg cost/trade':<20}: ₹{total_charges/total:>8,.2f}")
    print(f"{'─'*60}")
    total_signals = total + skipped + event_skipped
    print(f"  Trading days total  : {total_signals}  (Mon/Tue/Thu/Fri)")
    print(f"  Trades taken        : {total}  ({total/total_signals*100:.1f}%)")
    print(f"  Skipped (low cap)   : {skipped}")
    if event_skipped:
        print(f"  Skipped (event day) : {event_skipped}  ← RBI MPC / Budget")
    print(f"{'─'*60}")
    print(f"  Wins                : {wins}  ({wins/total*100:.1f}%)")
    print(f"  Losses              : {losses}  ({losses/total*100:.1f}%)")
    print(f"  Trailing SL exits   : {trail_sl}  ({trail_sl/total*100:.1f}%)")
    print(f"  Partial exits       : {partial}  ({partial/total*100:.1f}%)")
    wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
    print(f"  Win rate (W vs L)   : {wr:.1f}%")
    print(f"{'─'*60}")
    # Per-day breakdown
    if "weekday" in active.columns:
        print(f"  PER-DAY BREAKDOWN")
        day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        for day in day_order:
            d = active[active["weekday"] == day]
            if len(d) == 0:
                continue
            dw = (d["result"] == "WIN").sum()
            dl = (d["result"] == "LOSS").sum()
            dwr = dw / (dw + dl) * 100 if (dw + dl) > 0 else 0
            dte = DAY_DTE.get(day, 1)
            print(f"  {day:<10}: {len(d):>3} trades | WR {dwr:.0f}%"
                  f" | Net ₹{d['pnl'].sum():>9,.0f} | DTE={dte}")
    print(f"{'─'*60}")
    print(f"  Best trade          : ₹{active['pnl'].max():>10,.2f}")
    print(f"  Worst trade         : ₹{active['pnl'].min():>10,.2f}")
    print(f"  Avg trade P&L       : ₹{active['pnl'].mean():>10,.2f}")
    print(f"  Max drawdown        : {max_dd:.1f}%")
    print(f"{'='*60}")

    print(f"\nMonthly breakdown (first 8 months):")
    cols = ["month", "trades", "wins", "losses", "monthly_pnl", "end_capital"]
    print(monthly[cols].head(8).to_string(index=False))


# ── Entry point ───────────────────────────────────────────────────────────────

def run_comparison():
    """
    Run backtest for all thresholds (1, 2, 3, 4) side-by-side using
    whatever signals.csv is present, but re-generates signals for each threshold.
    Prints a comparison table at the end.
    """
    import subprocess, sys as _sys

    thresholds = [1, 2, 3, 4]
    summary_rows = []

    # Pass optional days argument through: backtest_engine.py --compare [days]
    days_arg = _sys.argv[2] if len(_sys.argv) > 2 else None

    for thr in thresholds:
        print(f"\n{'─'*60}")
        print(f"  Generating signals at threshold ±{thr}...")
        cmd = [_sys.executable, "signal_engine.py", str(thr)]
        if days_arg:
            cmd.append(days_arg)
        subprocess.run(cmd, capture_output=True)   # quiet — we just need signals.csv

        trade_df, monthly = run_backtest()
        active = trade_df[trade_df["result"].isin(["WIN", "LOSS", "PARTIAL"])]
        total  = len(active)
        wins   = (active["result"] == "WIN").sum()
        losses = (active["result"] == "LOSS").sum()
        total_signals = len(trade_df)
        skipped = (trade_df["result"].str.startswith("SKIPPED")).sum()

        gross_pnl     = active["pnl"].sum() + trade_df["charges"].sum()
        net_pnl       = active["pnl"].sum()
        total_charges = trade_df["charges"].sum()
        end_cap       = trade_df["capital_after"].iloc[-1]
        wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

        cap_series  = trade_df["capital_after"]
        rolling_max = cap_series.cummax()
        drawdown    = (cap_series - rolling_max) / rolling_max * 100
        max_dd      = drawdown.min()

        summary_rows.append({
            "threshold":   f"±{thr}",
            "signals":     total + skipped,
            "trades":      total,
            "trade_rate":  f"{total/(total+skipped)*100:.0f}%",
            "wins":        wins,
            "losses":      losses,
            "win_rate":    f"{wr:.1f}%",
            "gross_pnl":   f"₹{gross_pnl:,.0f}",
            "charges":     f"₹{total_charges:,.0f}",
            "net_pnl":     f"₹{net_pnl:,.0f}",
            "end_capital": f"₹{end_cap:,.0f}",
            "max_dd":      f"{max_dd:.1f}%",
        })

        # Save individual results
        trade_df.to_csv(f"{DATA_DIR}/trade_log_t{thr}.csv",    index=False)
        monthly.to_csv( f"{DATA_DIR}/equity_curve_t{thr}.csv", index=False)
        print(f"  Threshold ±{thr}: {total} trades | WR {wr:.1f}% | Net P&L ₹{net_pnl:,.0f} | Charges ₹{total_charges:,.0f}")

    # Final comparison table
    print(f"\n{'='*100}")
    print(f"  THRESHOLD COMPARISON — all costs included")
    print(f"{'='*100}")
    df_cmp = pd.DataFrame(summary_rows)
    print(df_cmp.to_string(index=False))
    print(f"{'='*100}")
    print(f"\nNote: ±1 is effectively 'no threshold' — trades on any Mon/Tue/Thu/Fri")
    print(f"      with even 1 net bullish/bearish indicator (score = ±1 to ±10).")
    print(f"      Score = 0 (perfect tie) still gets no trade — no directional edge.")
    print(f"      Lot cap: {MAX_LOTS} lots per trade (liquidity/margin constraint).")


def main():
    import sys as _sys

    if len(_sys.argv) >= 2 and _sys.argv[1] == "--compare":
        print("Running full threshold comparison (±1 through ±4)...")
        run_comparison()
        return

    if len(_sys.argv) >= 2 and _sys.argv[1] == "--rr":
        print("Running RR comparison (per-day original vs flat 1.0–3.0×, SL=20%, trail=₹5)...")
        run_rr_comparison()
        return

    if len(_sys.argv) >= 2 and _sys.argv[1] == "--trail":
        print("Running trail jump comparison (₹0 / ₹5 / ₹10 / ₹20)...")
        run_trail_comparison()
        return

    if len(_sys.argv) >= 2 and _sys.argv[1] == "--grid":
        trail = float(_sys.argv[2]) if len(_sys.argv) > 2 else 5
        print(f"Running SL% × RR grid with trail=₹{trail:.0f}...")
        run_sl_tp_grid(trail_jump_opt=trail)
        return

    # Read threshold embedded in signals.csv
    try:
        sig_df    = pd.read_csv(f"{DATA_DIR}/signals.csv", nrows=1)
        threshold = int(sig_df["threshold"].iloc[0]) if "threshold" in sig_df.columns else None
    except Exception:
        threshold = None

    print("Running backtest...")
    trade_df, monthly = run_backtest()

    trade_df.to_csv(f"{DATA_DIR}/trade_log.csv",    index=False)
    monthly.to_csv( f"{DATA_DIR}/equity_curve.csv", index=False)

    print_summary(trade_df, monthly, threshold=threshold)

    print(f"\nSaved → {DATA_DIR}/trade_log.csv")
    print(f"Saved → {DATA_DIR}/equity_curve.csv")


if __name__ == "__main__":
    main()
