import pandas as pd
import numpy as np
import os
import calendar as _cal
from datetime import date as _date, timedelta

DATA_DIR         = "data"
LOT_SIZE         = 30
RISK_PCT         = 0.05
SL_PCT           = 0.15   # 15% SL → TP=30% at RR=2.0x (final strategy)
STARTING_CAPITAL = 30_000
MONTHLY_TOPUP    = 10_000
PREMIUM_K        = 0.004
MAX_LOTS         = 20

# BankNifty expiry timeline (4 phases):
# Phase 1: Sep 2021 – Feb 2024    → weekly, every Thursday
# Phase 2: Mar 2024 – Nov 19 2024 → weekly, every Wednesday
# Phase 3: Nov 20 2024 – Aug 2025 → monthly, last Wednesday (weekly discontinued)
# Phase 4: Sep 2025 onwards       → monthly, last Tuesday  (NSE revised)
WEDNESDAY_WEEKLY_START = _date(2024,  3,  1)   # weekly shifted Thu → Wed
WEEKLY_DISCONTINUED    = _date(2024, 11, 20)   # SEBI: weekly BN options removed
TUESDAY_EXPIRY_FROM    = _date(2025,  9,  1)   # NSE: monthly shifted Wed → Tue


def last_wednesday(year, month):
    """Last Wednesday of the given year/month."""
    last_day = _cal.monthrange(year, month)[1]
    for d in range(last_day, last_day - 7, -1):
        if _date(year, month, d).weekday() == 2:   # 2 = Wednesday
            return _date(year, month, d)


def last_tuesday(year, month):
    """Last Tuesday of the given year/month."""
    last_day = _cal.monthrange(year, month)[1]
    for d in range(last_day, last_day - 7, -1):
        if _date(year, month, d).weekday() == 1:   # 1 = Tuesday
            return _date(year, month, d)


def get_expiry(d):
    """
    Returns the relevant BN expiry date for a given trading date.

    Phase 1 — before Mar 2024    : weekly Thursday (days_ahead=0 → expiry today)
    Phase 2 — Mar–Nov 2024       : weekly Wednesday (days_ahead=0 → expiry today)
    Phase 3 — Nov 2024–Aug 2025  : monthly, last Wednesday of month
    Phase 4 — Sep 2025 onwards   : monthly, last Tuesday of month

    NOTE: for weekly phases, days_ahead % 7 = 0 means TODAY is expiry —
    we return today (not next week) so get_dte gives DTE≈1, not DTE≈8.
    """
    if isinstance(d, pd.Timestamp):
        d = d.date()

    if d < WEDNESDAY_WEEKLY_START:
        # Phase 1: weekly Thursday (weekday 3)
        days_ahead = (3 - d.weekday()) % 7   # 0 if today is Thursday → returns today
        return d + timedelta(days=days_ahead)

    elif d < WEEKLY_DISCONTINUED:
        # Phase 2: weekly Wednesday (weekday 2)
        days_ahead = (2 - d.weekday()) % 7   # 0 if today is Wednesday → returns today
        return d + timedelta(days=days_ahead)

    elif d < TUESDAY_EXPIRY_FROM:
        # Phase 3: monthly, last Wednesday
        exp = last_wednesday(d.year, d.month)
        if d > exp:
            nxt = d.replace(day=1) + timedelta(days=32)
            exp = last_wednesday(nxt.year, nxt.month)
        return exp

    else:
        # Phase 4: monthly, last Tuesday
        exp = last_tuesday(d.year, d.month)
        if d > exp:
            nxt = d.replace(day=1) + timedelta(days=32)
            exp = last_tuesday(nxt.year, nxt.month)
        return exp


def get_dte(d):
    """
    Days to expiry for date d. Minimum 0.25 (expiry morning still has ~6h of trading).
    +1 because options are live on the expiry date itself until 3:30 PM.
    """
    expiry = get_expiry(d)
    days   = (expiry - (d.date() if isinstance(d, pd.Timestamp) else d)).days
    return max(0.25, float(days) + 1)


# Legacy weekday DTE dict kept ONLY for --compare / old modes that don't pass dte_override
DAY_DTE = {
    "Monday": 2, "Tuesday": 1, "Wednesday": 0.25, "Thursday": 6, "Friday": 5,
}
DAY_RR = {
    "Monday": 2.0, "Tuesday": 2.0, "Wednesday": 2.0, "Thursday": 2.0, "Friday": 2.0,
}


def fmt_inr(amount):
    """Format rupee amount in Indian crore/lakh notation."""
    if abs(amount) >= 1e7:
        return f"₹{amount/1e7:.2f}Cr"
    elif abs(amount) >= 1e5:
        return f"₹{amount/1e5:.1f}L"
    else:
        return f"₹{amount:,.0f}"

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
                   flat_rr=None, day_rr_override=None,
                   bn_tp_pts=None, bn_sl_pts=None, dte_override=None):
    """
    Simulate one trade using same-day OHLCV to approximate intraday exit.

    trail_jump_opt  : trailing stop in option-price rupees (Dhan trailingJump). 0 = off.
    sl_pct          : override SL_PCT global (e.g. 0.20 for 20% SL). None = use global.
    flat_rr         : use this RR for every day instead of DAY_RR dict. None = use per-day.
    day_rr_override : dict like DAY_RR to use instead of the module-level DAY_RR.
    bn_tp_pts       : fixed BN-point target (e.g. 500). Overrides premium% TP if set.
    bn_sl_pts       : fixed BN-point stop-loss (e.g. 150). Overrides premium% SL if set.
    dte_override    : actual DTE calculated from real expiry date. Overrides DAY_DTE dict.

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

    dte  = dte_override if dte_override is not None else DAY_DTE.get(weekday, 1)
    if flat_rr is not None:
        rr = flat_rr
    elif day_rr_override is not None:
        rr = day_rr_override.get(weekday, 1.4)
    else:
        rr = DAY_RR.get(weekday, 1.4)
    sl   = sl_pct  if sl_pct  is not None else SL_PCT
    premium = bn_open * PREMIUM_K * (dte ** 0.5)

    # ── SL / TP levels in BN points (ATM delta ≈ 0.5) ────────────────────────
    if bn_tp_pts is not None and bn_sl_pts is not None:
        # BN-point mode: SL/TP defined directly in BankNifty points
        sl_pts = bn_sl_pts
        tp_pts = bn_tp_pts
        option_sl_loss = bn_sl_pts * 0.5       # option price drop at SL (delta=0.5)
        max_loss_1lot  = LOT_SIZE * option_sl_loss
    else:
        # Premium% mode: SL/TP as % of option premium
        sl_pts = (sl * premium) / 0.5
        tp_pts = (rr * sl * premium) / 0.5
        max_loss_1lot = LOT_SIZE * premium * sl

    # ── Lot sizing ────────────────────────────────────────────────────────────
    if max_loss_1lot > capital * 0.15:
        return 0.0, "SKIPPED_LOW_CAPITAL", 0, premium, 0.0, zero_breakdown

    lots = min(MAX_LOTS, max(1, int((capital * RISK_PCT) / max_loss_1lot)))

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
    if bn_tp_pts is not None and bn_sl_pts is not None:
        # BN-point mode: P&L based on fixed BN-point SL/TP, delta=0.5
        if result == "WIN":
            pnl =  lots * LOT_SIZE * bn_tp_pts * 0.5 - charges
        else:
            pnl = -lots * LOT_SIZE * bn_sl_pts * 0.5 - charges
    else:
        if result == "WIN":
            pnl =  lots * LOT_SIZE * premium * rr * sl - charges
        else:
            pnl = -lots * LOT_SIZE * premium * sl - charges

    return round(pnl, 2), result, lots, round(premium, 2), charges, bd


# ── Backtest loop ─────────────────────────────────────────────────────────────

def run_backtest(trail_jump_opt=0, sl_pct=None, flat_rr=None, day_rr_override=None,
                 bn_tp_pts=None, bn_sl_pts=None, use_actual_dte=True):
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
        actual_dte = get_dte(date) if use_actual_dte else None
        pnl, result, lots, premium, charges, charges_bd = simulate_trade(
            row, bn_ohlcv, capital,
            trail_jump_opt=trail_jump_opt,
            sl_pct=sl_pct,
            flat_rr=flat_rr,
            day_rr_override=day_rr_override,
            bn_tp_pts=bn_tp_pts,
            bn_sl_pts=bn_sl_pts,
            dte_override=actual_dte)
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
                "net_pnl_fmt": fmt_inr(net_pnl),
                "end_cap":  fmt_inr(end_cap),
                "max_dd":   f"{max_dd:.1f}%",
            })

    df = pd.DataFrame(rows).sort_values("net_pnl", ascending=False).reset_index(drop=True)

    print(f"\n{'='*90}")
    print(f"  SL% × RR GRID  —  trail=₹{trail_jump_opt}, ranked by net P&L")
    print(f"  Live config: SL=20%, flat RR=2.0×, trail=₹5  (TP=+40% of premium)")
    print(f"{'='*90}")
    print(df.drop(columns=["net_pnl"]).to_string(index=True))
    print(f"{'='*90}")
    print(f"\n  trail_sl = losses converted to smaller exits by trailing SL")
    print(f"  WR% = wins / (wins + full losses) — excludes trail_sl and partials\n")


def run_range_validation():
    """
    Analyse historical BN daily moves to validate whether TP% targets are realistic.

    For each trading day:
      - favorable_call = High - Open  (BN pts BN moves UP from open)
      - favorable_put  = Open - Low   (BN pts BN moves DOWN from open)
      - Combined favorable = max of both (whichever side our signal is on)

    For each DTE bucket, compute ATM premium and check what % of days
    a given TP% would actually be hit by the favorable move.
    """
    bn = pd.read_csv(f"{DATA_DIR}/banknifty.csv", parse_dates=["date"])
    bn["date_dt"]  = bn["date"].dt.date
    bn["fav_call"] = bn["high"]  - bn["open"]   # BN pts if trade is CALL
    bn["fav_put"]  = bn["open"]  - bn["low"]    # BN pts if trade is PUT
    bn["fav_max"]  = bn[["fav_call","fav_put"]].max(axis=1)  # best case direction
    bn["range"]    = bn["high"]  - bn["low"]
    bn["dte"]      = bn["date_dt"].apply(get_dte)

    print(f"\n{'='*80}")
    print(f"  BANKNIFTY DAILY MOVE ANALYSIS — {len(bn)} trading days")
    print(f"  Source: data/banknifty.csv   (weekly expiry → Sep 2024, monthly after)")
    print(f"{'='*80}")

    # Overall directional move stats (CALL or PUT, whoever is right)
    for pct in [25, 50, 75, 90, 95]:
        print(f"  {pct:>2}th pct favorable move: {bn['fav_max'].quantile(pct/100):>6.0f} BN pts")

    print(f"\n  NOTE: 'favorable move' = move in trade direction (CALL = up from open)")
    print(f"  Median H-L range = {bn['range'].median():.0f} pts   Max = {bn['range'].max():.0f} pts\n")

    # DTE buckets — what premium and what TP BN-pts threshold at each bucket
    dte_buckets = [
        ("  0–2  DTE  (expiry week Tue/Wed)", 0,   2),
        ("  3–7  DTE  (expiry week Mon/Thu/Fri)", 3,  7),
        ("  8–15 DTE  (2 weeks out)", 8,  15),
        (" 16–25 DTE  (3–4 weeks out)", 16, 25),
    ]

    sl_vals = [0.15, 0.20, 0.25, 0.30]
    rr_vals = [1.5, 2.0, 2.5, 3.0]

    print(f"  {'DTE bucket':<35}  {'avg DTE':>7}  {'avg prem':>9}  "
          f"{'TP20%':>6} {'TP30%':>6} {'TP40%':>6} {'TP50%':>6}  ← BN pts needed")
    print(f"  {'-'*35}  {'-'*7}  {'-'*9}  {'-'*6} {'-'*6} {'-'*6} {'-'*6}")

    for label, lo, hi in dte_buckets:
        sub = bn[(bn["dte"] >= lo) & (bn["dte"] <= hi)]
        if sub.empty:
            continue
        avg_dte  = sub["dte"].mean()
        avg_spot = sub["open"].mean()
        avg_prem = avg_spot * PREMIUM_K * (avg_dte ** 0.5)

        # BN pts needed to hit TP at each % level (delta=0.5)
        tp_pts = {tp: avg_prem * tp / 0.5 for tp in [0.20, 0.30, 0.40, 0.50]}

        print(f"  {label:<35}  {avg_dte:>7.1f}  ₹{avg_prem:>7.0f}  "
              f"{tp_pts[0.20]:>6.0f} {tp_pts[0.30]:>6.0f} "
              f"{tp_pts[0.40]:>6.0f} {tp_pts[0.50]:>6.0f}")

    print(f"\n  {'DTE bucket':<35}  {'HIT%  TP=20%':>12} {'TP=30%':>8} {'TP=40%':>8} {'TP=50%':>8}")
    print(f"  {'-'*35}  {'-'*12} {'-'*8} {'-'*8} {'-'*8}")
    for label, lo, hi in dte_buckets:
        sub = bn[(bn["dte"] >= lo) & (bn["dte"] <= hi)]
        if sub.empty:
            continue
        avg_dte  = sub["dte"].mean()
        avg_spot = sub["open"].mean()
        avg_prem = avg_spot * PREMIUM_K * (avg_dte ** 0.5)

        hits = {}
        for tp in [0.20, 0.30, 0.40, 0.50]:
            needed = avg_prem * tp / 0.5
            hits[tp] = (sub["fav_max"] >= needed).mean() * 100

        print(f"  {label:<35}  {hits[0.20]:>12.0f}% {hits[0.30]:>7.0f}% "
              f"{hits[0.40]:>7.0f}% {hits[0.50]:>7.0f}%")

    print(f"\n  HIT% = % of days where BN moved enough in trade direction to hit that TP")
    print(f"  (uses average premium per bucket; actual will vary by spot price)")
    print(f"{'='*80}\n")


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
            "net_pnl_fmt":fmt_inr(net_pnl),
            "end_cap":    fmt_inr(end_cap),
            "max_dd":     f"{max_dd:.1f}%",
        })

    df = pd.DataFrame(rows).sort_values("net_pnl", ascending=False).reset_index(drop=True)

    print(f"\n{'='*95}")
    print(f"  RR COMPARISON  —  SL={int(sl_pct*100)}%, trail=₹{trail_jump_opt}, ranked by net P&L")
    print(f"{'='*95}")
    print(df.drop(columns=["net_pnl"]).to_string(index=True))
    print(f"{'='*95}\n")


def run_pts_grid(tp_pts=500, trail_jump_opt=5):
    """
    Fixed BN-point target grid: TP fires when BN moves tp_pts in one direction.
    Tests various SL values (in BN points), computes resulting RR.
    Shows what SL to use and what that means as % of premium per day.
    """
    sl_options = [100, 150, 200, 250, 300]
    rows = []

    # Representative premium per day at BN=54000 (for reference only)
    ref_spot = 54_000
    ref_prems = {d: ref_spot * PREMIUM_K * (dte ** 0.5)
                 for d, dte in DAY_DTE.items()}

    print(f"\n  Fixed TP = {tp_pts} BN pts  →  option gain = ₹{tp_pts*0.5:.0f}  (delta=0.5)")
    print(f"  Reference spot = ₹{ref_spot:,}. Premium per day:")
    for day, p in ref_prems.items():
        sl_pct_fri = (tp_pts * 0.5) / p
        print(f"    {day:<10}: premium ~₹{p:.0f}  →  TP is {tp_pts*0.5/p*100:.0f}% of premium")
    print()

    for sl_bn in sl_options:
        rr = tp_pts / sl_bn
        trade_df, _ = run_backtest(
            trail_jump_opt=trail_jump_opt,
            bn_tp_pts=tp_pts,
            bn_sl_pts=sl_bn,
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

        # SL as % of Friday premium (most common high-DTE trade)
        sl_opt_loss   = sl_bn * 0.5
        fri_prem      = ref_prems["Friday"]
        sl_pct_fri    = sl_opt_loss / fri_prem * 100
        tue_prem      = ref_prems["Tuesday"]
        sl_pct_tue    = sl_opt_loss / tue_prem * 100

        rows.append({
            "SL(BN pts)": sl_bn,
            "SL opt(₹)":  f"₹{sl_opt_loss:.0f}",
            "SL%(Fri)":   f"{sl_pct_fri:.0f}%",
            "SL%(Tue)":   f"{sl_pct_tue:.0f}%",
            "RR":         f"{rr:.1f}x",
            "wins":       wins,
            "losses":     losses,
            "trail_sl":   trail_sl,
            "WR%":        f"{wr:.0f}%",
            "net_pnl":    net_pnl,
            "net_pnl_fmt":fmt_inr(net_pnl),
            "end_cap":    fmt_inr(end_cap),
            "max_dd":     f"{max_dd:.1f}%",
        })

    df = pd.DataFrame(rows).sort_values("net_pnl", ascending=False).reset_index(drop=True)

    print(f"{'='*110}")
    print(f"  BN-POINT TARGET: TP fires when BN moves {tp_pts} pts  |  trail=₹{trail_jump_opt}")
    print(f"  SL%(Fri) = SL as % of Friday premium (~₹{ref_prems['Friday']:.0f})")
    print(f"  SL%(Tue) = SL as % of Tuesday premium (~₹{ref_prems['Tuesday']:.0f})")
    print(f"{'='*110}")
    print(df.drop(columns=["net_pnl"]).to_string(index=True))
    print(f"{'='*110}\n")


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


def run_tp_fixed_grid(tp_pct=0.30, trail_jump_opt=5):
    """
    Fix TP% and sweep SL% options — find the best SL for a given TP target.
    RR is computed as tp_pct / sl_pct for each row.
    All runs use trail=₹5 and actual calendar DTE (monthly expiry aware).
    """
    sl_options = [0.10, 0.15, 0.20, 0.25, 0.30]
    rows = []

    print(f"\n  Fixed TP = {tp_pct*100:.0f}%  |  trail=₹{trail_jump_opt}")
    print(f"  Testing SL: {[f'{s*100:.0f}%' for s in sl_options]}")
    print()

    for sl in sl_options:
        rr = tp_pct / sl
        trade_df, _ = run_backtest(
            trail_jump_opt=trail_jump_opt,
            sl_pct=sl,
            flat_rr=rr,
            use_actual_dte=True,
        )
        active   = trade_df[trade_df["result"].isin(["WIN", "LOSS", "PARTIAL", "TRAIL_SL"])]
        wins     = (active["result"] == "WIN").sum()
        losses   = (active["result"] == "LOSS").sum()
        trail_sl = (active["result"] == "TRAIL_SL").sum()
        partial  = (active["result"] == "PARTIAL").sum()
        total    = len(active)
        net_pnl  = active["pnl"].sum()
        end_cap  = trade_df["capital_after"].iloc[-1]
        wr       = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

        cap_series = trade_df["capital_after"]
        max_dd     = ((cap_series - cap_series.cummax()) / cap_series.cummax() * 100).min()

        rows.append({
            "SL%":      f"{sl*100:.0f}%",
            "RR":       f"{rr:.2f}x",
            "trades":   total,
            "wins":     wins,
            "losses":   losses,
            "trail_sl": trail_sl,
            "partial":  partial,
            "WR%":      f"{wr:.1f}%",
            "net_pnl":  net_pnl,
            "P&L":      fmt_inr(net_pnl),
            "end_cap":  fmt_inr(end_cap),
            "max_dd":   f"{max_dd:.1f}%",
        })
        print(f"  SL={sl*100:.0f}%  RR={rr:.2f}x: trades={total} | W={wins} L={losses}"
              f" T={trail_sl} | WR={wr:.1f}% | Net={fmt_inr(net_pnl)}"
              f" | Cap={fmt_inr(end_cap)} | DD={max_dd:.1f}%")

    df = pd.DataFrame(rows).sort_values("net_pnl", ascending=False).reset_index(drop=True)

    print(f"\n{'='*100}")
    print(f"  FIXED TP={tp_pct*100:.0f}%  —  SL SWEEP  |  trail=₹{trail_jump_opt}  |  actual calendar DTE")
    print(f"  Ranked by net P&L (best first)")
    print(f"{'='*100}")
    print(df.drop(columns=["net_pnl"]).to_string(index=False))
    print(f"{'='*100}")
    best = df.iloc[0]
    print(f"\n  BEST CONFIG:  SL={best['SL%']}  RR={best['RR']}  →  P&L={best['P&L']}  DD={best['max_dd']}  WR={best['WR%']}")
    print(f"\n  To update auto_trader.py:")
    print(f"    SL_PCT = {float(best['SL%'].strip('%'))/100:.2f}")
    print(f"    RR     = {best['RR']}")
    print()


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

    if len(_sys.argv) >= 2 and _sys.argv[1] == "--validate":
        print("Analysing historical BN daily moves vs TP% targets...")
        run_range_validation()
        return

    if len(_sys.argv) >= 2 and _sys.argv[1] == "--rr":
        print("Running RR comparison (per-day original vs flat 1.0–3.0×, SL=20%, trail=₹5)...")
        run_rr_comparison()
        return

    if len(_sys.argv) >= 2 and _sys.argv[1] == "--pts":
        tp = int(_sys.argv[2]) if len(_sys.argv) > 2 else 500
        print(f"Running BN-point target grid: TP={tp} pts, trail=₹5...")
        run_pts_grid(tp_pts=tp, trail_jump_opt=5)
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

    if len(_sys.argv) >= 2 and _sys.argv[1] == "--tp":
        tp = float(_sys.argv[2]) if len(_sys.argv) > 2 else 30
        print(f"Running fixed TP={tp:.0f}% SL sweep with trail=₹5 and actual DTE...")
        run_tp_fixed_grid(tp_pct=tp / 100, trail_jump_opt=5)
        return

    # Read threshold embedded in signals.csv
    try:
        sig_df    = pd.read_csv(f"{DATA_DIR}/signals.csv", nrows=1)
        threshold = int(sig_df["threshold"].iloc[0]) if "threshold" in sig_df.columns else None
    except Exception:
        threshold = None

    print("Running backtest...  [SL=15%, TP=30%, RR=2.0x, trail=₹5, actual DTE]")
    trade_df, monthly = run_backtest(trail_jump_opt=5, sl_pct=0.15, flat_rr=2.0, use_actual_dte=True)

    trade_df.to_csv(f"{DATA_DIR}/trade_log.csv",    index=False)
    monthly.to_csv( f"{DATA_DIR}/equity_curve.csv", index=False)

    print_summary(trade_df, monthly, threshold=threshold)

    print(f"\nSaved → {DATA_DIR}/trade_log.csv")
    print(f"Saved → {DATA_DIR}/equity_curve.csv")


if __name__ == "__main__":
    main()
