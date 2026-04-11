import pandas as pd
import numpy as np
import os
import math as _math
import calendar as _cal
from datetime import date as _date, timedelta
from math import floor

DATA_DIR         = "data"
LOT_SIZE         = 30
RISK_PCT         = 0.05
SL_PCT           = 0.15   # 15% SL → TP=30% at RR=2.0x (final strategy)
STARTING_CAPITAL = 30_000
MONTHLY_TOPUP    = 10_000
PREMIUM_K        = 0.004
MAX_LOTS         = 20
ITM_WALK_MAX     = 2    # probe up to 200pt ITM when capital is flush
OTM_WALK_MAX     = 10   # probe up to 1000pt OTM when capital is thin
_DEFAULT_IV      = 0.20 # assumed annualized IV for premium/delta approximation

# BankNifty expiry timeline (4 phases):
# Phase 1: Sep 2021 – Feb 2024    → weekly, every Thursday
# Phase 2: Mar 2024 – Nov 19 2024 → weekly, every Wednesday
# Phase 3: Nov 20 2024 – Aug 2025 → monthly, last Wednesday (weekly discontinued)
# Phase 4: Sep 2025 onwards       → monthly, last Tuesday  (NSE revised)
WEDNESDAY_WEEKLY_START = _date(2024,  3,  1)   # weekly shifted Thu → Wed
WEEKLY_DISCONTINUED    = _date(2024, 11, 20)   # SEBI: weekly BN options removed
TUESDAY_EXPIRY_FROM    = _date(2025,  9,  1)   # NSE: monthly shifted Wed → Tue

# ── Historical lot sizes ──────────────────────────────────────────────────────
# SEBI mandate (min contract value ₹15L) drove these changes.
# Source: NSE circulars / PL Capital research
#
# Timeline:
#   Before Nov 20 2024         → lot = 15
#   Nov 20 2024 – Jun 25 2025  → lot = 30
#     (Apr/May/Jun 2025 monthly contracts pre-existed the Apr 24 mandate,
#      so they kept lot=30 through their June 25 2025 expiry)
#   Jun 26 2025 – Jan 26 2026  → lot = 35
#     (Jul 2025 was first monthly contract created after Apr 24 → lot=35)
#   Jan 27 2026 onwards        → lot = 30  (current)

_LOT_15_UNTIL = _date(2024, 11, 20)   # SEBI mandate: 15 → 30
_LOT_35_FROM  = _date(2025,  6, 26)   # day after Jun 2025 expiry: 30 → 35
_LOT_30B_FROM = _date(2026,  1, 27)   # first Jan 2026 monthly expiry: 35 → 30

def _baseline_lot_size(d):
    """Hardcoded baseline timeline — canonical source of truth."""
    if d < _LOT_15_UNTIL:
        return 15   # Sep 2021 – Nov 2024
    elif d < _LOT_35_FROM:
        return 30   # Nov 2024 – Jun 2025
    elif d < _LOT_30B_FROM:
        return 35   # Jul 2025 – Jan 2026
    else:
        return 30   # Jan 2026 onwards


def get_lot_size(d):
    """
    Return the correct BankNifty lot size for a historical trade date.

    Priority:
      1. Active overrides from data/lot_size_overrides.json (written by
         lot_expiry_scanner.py on mismatch detection)
      2. Hardcoded baseline (_baseline_lot_size)

    The scanner runs monthly and updates the override file automatically
    when NSE publishes lot size changes.
    """
    if isinstance(d, pd.Timestamp):
        d = d.date()

    # Check override file (only if it exists and is readable)
    try:
        import json as _json
        import os as _os
        ov_path = _os.path.join(DATA_DIR, "lot_size_overrides.json")
        if _os.path.exists(ov_path):
            with open(ov_path) as _f:
                ov = _json.load(_f)
            best = _baseline_lot_size(d)
            best_eff = _date(1900, 1, 1)
            for entry in ov.get("active", []):
                try:
                    eff = _date.fromisoformat(entry["effective_date"])
                except Exception:
                    continue
                if eff <= d and eff >= best_eff:
                    best = int(entry["lot_size"])
                    best_eff = eff
            return best
    except Exception:
        pass

    return _baseline_lot_size(d)


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


# Legacy weekday DTE dict kept ONLY for --compare / old modes that don't pass dte_override.
# Phase 4 (Sep 2025+): monthly last-Tuesday expiry — Wednesday is a normal trade day.
# use_actual_dte=True (default for all backtests) ignores this dict entirely.
DAY_DTE = {
    "Monday": 2, "Tuesday": 1, "Wednesday": 6, "Thursday": 6, "Friday": 5,
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

def calculate_charges(premium, lots, lot_size=None, breakdown=False):
    """
    Return total round-trip transaction cost for one trade.
    If breakdown=True, returns (total, dict_of_components).
    lot_size defaults to current LOT_SIZE (30) if not provided.
    """
    if lot_size is None:
        lot_size = LOT_SIZE
    pv = lots * lot_size * premium          # total premium value

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

def load_signals(ml=False):
    fname = f"{DATA_DIR}/signals_ml.csv" if ml else f"{DATA_DIR}/signals.csv"
    df = pd.read_csv(fname, parse_dates=["date"])
    if "threshold" in df.columns:
        df = df.drop(columns=["threshold"])
    return df[df["signal"].isin(["CALL", "PUT"])].reset_index(drop=True)


def load_all_signals(ml=False):
    """Load full signals including NONE rows — used for summary stats."""
    fname = f"{DATA_DIR}/signals_ml.csv" if ml else f"{DATA_DIR}/signals.csv"
    return pd.read_csv(fname, parse_dates=["date"])


def load_bn_ohlcv():
    df = pd.read_csv(f"{DATA_DIR}/banknifty.csv", parse_dates=["date"])
    return df.set_index("date")


def load_real_premiums():
    """
    Load real ATM option premiums from data/options_atm_daily.csv
    (fetched from Dhan /charts/rollingoption).

    Returns dict keyed by pd.Timestamp → {"call_premium": float, "put_premium": float}
    or empty dict if file doesn't exist.

    Coverage is shown at backtest start. Days not in the file fall back to
    the BN × PREMIUM_K × √DTE approximation automatically.
    """
    path = f"{DATA_DIR}/options_atm_daily.csv"
    if not os.path.exists(path):
        return {}
    try:
        df  = pd.read_csv(path, parse_dates=["date"])
        out = {}
        for _, row in df.iterrows():
            out[row["date"]] = {
                "call_premium": row["call_premium"] if pd.notna(row.get("call_premium")) else None,
                "put_premium":  row["put_premium"]  if pd.notna(row.get("put_premium"))  else None,
            }
        return out
    except Exception as e:
        print(f"  Warning: could not load options_atm_daily.csv — {e}")
        return {}


# ── Trade simulator ───────────────────────────────────────────────────────────

def _norm_cdf(x):
    """Standard normal CDF — Abramowitz & Stegun approximation (error < 1e-5)."""
    a = abs(x)
    t = 1.0 / (1.0 + 0.2316419 * a)
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
           + t * (-1.821255978 + t * 1.330274429))))
    p = 1.0 - _math.exp(-0.5 * x * x) / _math.sqrt(2 * _math.pi) * poly
    return p if x >= 0 else 1.0 - p


def _otm_params(dist_100pts, bn_open, dte_days):
    """
    Return (premium_factor, delta) for a strike dist_100pts × 100 pts from ATM.

    dist_100pts convention (matches auto_trader.py otm_distance):
      negative  = ITM  (-1 = 100pt ITM for CALL, strike below spot)
      0         = ATM
      positive  = OTM  (+3 = 300pt OTM for CALL, strike above spot)

    Uses Bachelier / Black-Scholes approximation:
      • time-value decays as Gaussian around ATM (extrinsic value)
      • ITM premium = extrinsic + intrinsic/ATM_premium ratio
      • delta = N(-z) where z = dist_pts / (σ√T × spot)
        CALL delta: ATM→0.50, deep-OTM→0.05, deep-ITM→0.95
    """
    if dist_100pts == 0:
        return 1.0, 0.50

    dist_pts    = dist_100pts * 100
    t_years     = max(0.001, dte_days / 252.0)
    sigma_t_pts = _DEFAULT_IV * bn_open * _math.sqrt(t_years)   # 1σ in BN pts

    if sigma_t_pts == 0:
        return 1.0, 0.50

    z = dist_pts / sigma_t_pts   # + = OTM for CALL, − = ITM for CALL

    # Extrinsic (time) value decays symmetrically around ATM
    extrinsic_factor = _math.exp(-0.5 * z * z)

    if dist_pts < 0:
        # ITM: extrinsic + intrinsic. Intrinsic scaled to ATM premium.
        atm_premium  = bn_open * PREMIUM_K * _math.sqrt(max(0.25, dte_days))
        intrinsic    = abs(dist_pts)
        pf = extrinsic_factor + (intrinsic / atm_premium if atm_premium > 0 else 0)
        pf = min(3.0, max(0.05, pf))
    else:
        pf = max(0.05, extrinsic_factor)

    # CALL delta: N(−z) → 0.50 at ATM, <0.50 OTM, >0.50 ITM
    delta = _norm_cdf(-z)
    delta = max(0.05, min(0.95, delta))
    return pf, delta


def _select_strike(bn_open, capital, dte_days, lot_size, real_atm_premium=None):
    """
    Simulate the live ATM/OTM/ITM strike selection for a given capital level.

    Mirrors the logic in auto_trader._find_affordable_strike_in_chain:
      Phase 1 — OTM scan:  try dist=0 (ATM), 1, 2 … OTM_WALK_MAX.
                           Return first OTM that fits dual-guard sizing.
                           If ATM itself fits, stop and go to Phase 2.
      Phase 2 — ITM probe: ATM fits → try deepest ITM first (2, 1).
                           Return first ITM the capital supports.
                           Fall back to ATM if nothing deeper fits.

    real_atm_premium: actual ATM open premium from Dhan rollingoption data.
                      If provided, used as the ATM anchor instead of the
                      BN × PREMIUM_K × √DTE formula. OTM/ITM strikes are
                      scaled relative to this real anchor.

    Returns (dist_100pts, adj_premium, lots, delta) or None if nothing fits.
    dist_100pts: negative=ITM, 0=ATM, positive=OTM
    """
    # Real ATM premium as anchor (exact IV-priced); fall back to formula approximation
    atm_premium = (real_atm_premium if real_atm_premium and real_atm_premium > 0
                   else bn_open * PREMIUM_K * (dte_days ** 0.5))
    atm_result  = None

    for dist in range(0, OTM_WALK_MAX + 1):
        pf, delta = _otm_params(dist, bn_open, dte_days)
        premium   = atm_premium * pf
        loss_1lot = lot_size * premium * SL_PCT
        marg_1lot = lot_size * premium
        if loss_1lot <= 0 or marg_1lot <= 0:
            continue
        lots_r = floor(capital * RISK_PCT / loss_1lot)
        lots_m = floor(capital * 0.85    / marg_1lot)
        lots   = min(MAX_LOTS, lots_r, lots_m)
        if lots >= 1:
            if dist == 0:
                atm_result = (0, premium, lots, delta)
                break   # ATM fits — try ITM in Phase 2
            else:
                return (dist, premium, lots, delta)   # OTM fallback, return now

    if atm_result is None:
        return None   # nothing affordable in OTM window

    # Phase 2 — ITM probe: deepest first (200pt, then 100pt)
    for itm in range(ITM_WALK_MAX, 0, -1):
        pf, delta = _otm_params(-itm, bn_open, dte_days)
        premium   = atm_premium * pf
        loss_1lot = lot_size * premium * SL_PCT
        marg_1lot = lot_size * premium
        if loss_1lot <= 0 or marg_1lot <= 0:
            continue
        lots_r = floor(capital * RISK_PCT / loss_1lot)
        lots_m = floor(capital * 0.85    / marg_1lot)
        lots   = min(MAX_LOTS, lots_r, lots_m)
        if lots >= 1:
            return (-itm, premium, lots, delta)

    return atm_result   # ATM is the best achievable


def simulate_trade(row, bn_ohlcv, capital, trail_jump_opt=0, sl_pct=None,
                   flat_rr=None, day_rr_override=None,
                   bn_tp_pts=None, bn_sl_pts=None, dte_override=None,
                   lot_size=None, real_atm_premium=None):
    """
    Simulate one trade using same-day OHLCV to approximate intraday exit.

    trail_jump_opt   : trailing stop in option-price rupees (Dhan trailingJump). 0 = off.
    sl_pct           : override SL_PCT global (e.g. 0.20 for 20% SL). None = use global.
    flat_rr          : use this RR for every day instead of DAY_RR dict. None = use per-day.
    day_rr_override  : dict like DAY_RR to use instead of the module-level DAY_RR.
    bn_tp_pts        : fixed BN-point target (e.g. 500). Overrides premium% TP if set.
    bn_sl_pts        : fixed BN-point stop-loss (e.g. 150). Overrides premium% SL if set.
    dte_override     : actual DTE calculated from real expiry date. Overrides DAY_DTE dict.
    real_atm_premium : actual 9:15 AM ATM open from Dhan rollingoption data.
                       When provided replaces the BN × PREMIUM_K × √DTE formula.

    Returns (pnl, result, lots, premium, charges_total, charges_breakdown, strike_dist).
      strike_dist: negative=ITM, 0=ATM, positive=OTM (0=unknown for skipped)
    """
    date    = row["date"]
    weekday = row["weekday"]
    signal  = row["signal"]

    zero_breakdown = {k: 0.0 for k in
                      ["c_brokerage","c_stt","c_exchange","c_clearing",
                       "c_gst","c_stamp_duty","c_sebi"]}

    if date not in bn_ohlcv.index:
        return 0.0, "SKIPPED", 0, 0.0, 0.0, zero_breakdown, 0

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
    sl = sl_pct if sl_pct is not None else SL_PCT
    ls = lot_size if lot_size is not None else LOT_SIZE   # historical lot size

    # ── Strike / lot selection (simulates live OTM→ATM→ITM walk) ─────────────
    # Mirrors auto_trader._find_affordable_strike_in_chain logic:
    #   capital thin  → walk OTM until affordable (cheaper premium, lower delta)
    #   capital OK    → ATM (default)
    #   capital flush → probe ITM for better delta (higher payoff on trend days)
    if bn_tp_pts is not None and bn_sl_pts is not None:
        # BN-point mode: strike selection still determines premium/lots
        sel = _select_strike(bn_open, capital, dte, ls, real_atm_premium)
        if sel is None:
            return 0.0, "SKIPPED_LOW_CAPITAL", 0, 0.0, 0.0, zero_breakdown
        _dist, premium, lots, delta = sel
        sl_pts = bn_sl_pts
        tp_pts = bn_tp_pts
        max_loss_1lot = ls * bn_sl_pts * delta   # delta scales option loss per BN pt
    else:
        sel = _select_strike(bn_open, capital, dte, ls, real_atm_premium)
        if sel is None:
            return 0.0, "SKIPPED_LOW_CAPITAL", 0, 0.0, 0.0, zero_breakdown
        _dist, premium, lots, delta = sel
        # SL/TP in BN points — use actual delta, not hardcoded 0.5
        sl_pts = (sl * premium) / delta
        tp_pts = (rr * sl * premium) / delta
        max_loss_1lot = ls * premium * sl

    # ── Trailing SL helpers ───────────────────────────────────────────────────
    trail_jump_bn = (trail_jump_opt / delta) if trail_jump_opt > 0 else 0

    def trail_steps(favorable_bn_move):
        return int(favorable_bn_move / trail_jump_bn) if trail_jump_bn > 0 else 0

    def trail_exit_pnl(favorable_bn_move, n_steps):
        """P&L when trailing SL fires. Returns (gross_pnl, label).
        No breakeven cap — trail SL can exit profitably when price moves far enough.
        """
        opt_exit = premium * (1 - sl) + n_steps * trail_jump_opt
        gross    = (opt_exit - premium) * lots * ls
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
                charges, bd  = calculate_charges(premium, lots, lot_size=ls, breakdown=True)
                return round(gross - charges, 2), label, lots, round(premium, 2), charges, bd, _dist
            result = "LOSS"
        else:
            gross = (bn_close - bn_open) * delta * lots * ls
            charges, bd = calculate_charges(premium, lots, lot_size=ls, breakdown=True)
            return round(gross - charges, 2), "PARTIAL", lots, round(premium, 2), charges, bd, _dist

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
                charges, bd  = calculate_charges(premium, lots, lot_size=ls, breakdown=True)
                return round(gross - charges, 2), label, lots, round(premium, 2), charges, bd, _dist
            result = "LOSS"
        else:
            gross = (bn_open - bn_close) * delta * lots * ls
            charges, bd = calculate_charges(premium, lots, lot_size=ls, breakdown=True)
            return round(gross - charges, 2), "PARTIAL", lots, round(premium, 2), charges, bd, _dist

    charges, bd = calculate_charges(premium, lots, lot_size=ls, breakdown=True)
    if bn_tp_pts is not None and bn_sl_pts is not None:
        # BN-point mode: P&L based on fixed BN-point SL/TP, actual delta
        if result == "WIN":
            pnl =  lots * ls * bn_tp_pts * delta - charges
        else:
            pnl = -lots * ls * bn_sl_pts * delta - charges
    else:
        if result == "WIN":
            pnl =  lots * ls * premium * rr * sl - charges
        else:
            pnl = -lots * ls * premium * sl - charges

    return round(pnl, 2), result, lots, round(premium, 2), charges, bd, _dist


# ── Backtest loop ─────────────────────────────────────────────────────────────

def run_backtest(trail_jump_opt=0, sl_pct=None, flat_rr=None, day_rr_override=None,
                 bn_tp_pts=None, bn_sl_pts=None, use_actual_dte=True, ml=False,
                 use_real_premiums=True):
    signals      = load_signals(ml=ml)
    bn_ohlcv     = load_bn_ohlcv()
    opt_premiums = load_real_premiums() if use_real_premiums else {}

    if opt_premiums:
        covered = sum(1 for d in signals["date"] if d in opt_premiums and
                      opt_premiums[d].get("call_premium"))
        pct     = covered / len(signals) * 100 if len(signals) else 0
        print(f"  Real option premiums: {covered}/{len(signals)} trade days "
              f"({pct:.0f}% coverage) — remainder use BN×K×√DTE approx")
    else:
        print("  Real option premiums: not found — using BN×PREMIUM_K×√DTE for all days")
        print("  Run: python3 data_fetcher.py --fetch-options  to fetch real premiums")

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

        # Look up real ATM premium for this date + direction
        signal     = str(row.get("signal", "")).upper()
        real_prem  = None
        prem_src   = "approx"
        if opt_premiums:
            opt_row = opt_premiums.get(date)
            if opt_row:
                key = "call_premium" if signal == "CALL" else "put_premium"
                real_prem = opt_row.get(key)
                if real_prem and real_prem > 0:
                    prem_src = "real"

        capital_before = capital
        actual_dte  = get_dte(date) if use_actual_dte else None
        actual_lots = get_lot_size(date)
        pnl, result, lots, premium, charges, charges_bd, strike_dist = simulate_trade(
            row, bn_ohlcv, capital,
            trail_jump_opt=trail_jump_opt,
            sl_pct=sl_pct,
            flat_rr=flat_rr,
            day_rr_override=day_rr_override,
            bn_tp_pts=bn_tp_pts,
            bn_sl_pts=bn_sl_pts,
            real_atm_premium=real_prem,
            dte_override=actual_dte,
            lot_size=actual_lots)
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
            "premium_source": prem_src,      # "real" = rollingoption, "approx" = formula
            "lots":           lots,
            "lot_size":       actual_lots,
            "strike_dist":    strike_dist,   # <0=ITM, 0=ATM, >0=OTM in 100pt units
            "risk_amt":       round(lots * actual_lots * premium * SL_PCT, 2),
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
    sl_options = [0.15, 0.20, 0.25, 0.30]
    rr_options = [1.5, 2.0, 2.5]
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
    print(f"  Live config: SL=15%, flat RR=2.0×, trail=₹5  (TP=+30% of premium)")
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


def print_summary(trade_df, monthly, threshold=None, ml=False):
    active   = trade_df[trade_df["result"].isin(["WIN", "LOSS", "PARTIAL", "TRAIL_SL"])]
    wins     = (active["result"] == "WIN").sum()
    losses   = (active["result"] == "LOSS").sum()
    partial  = (active["result"] == "PARTIAL").sum()
    trail_sl = (active["result"] == "TRAIL_SL").sum()
    total    = len(active)
    skipped = (trade_df["result"].str.startswith("SKIPPED")).sum()

    # Event-skipped days — read from signals csv if available
    event_skipped = 0
    try:
        all_sig = load_all_signals(ml=ml)
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
    print(f"{'─'*60}")
    if "premium_source" in trade_df.columns:
        real_days  = (trade_df["premium_source"] == "real").sum()
        total_days = len(trade_df)
        print(f"  Premium source (real): {real_days}/{total_days} trade days  "
              f"({real_days/total_days*100:.0f}%  from Dhan rollingoption)")
        print(f"  Premium source (approx): {total_days-real_days}/{total_days} days "
              f" (BN×K×√DTE formula)")
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


def run_phase_analysis():
    """
    Month-on-month breakdown of trades tagged by expiry regime phase.
    Reads the saved trade_log.csv — run default backtest first.

    Phase 1: Sep 2021 – Feb 2024   weekly Thursday
    Phase 2: Mar 2024 – Nov 2024   weekly Wednesday
    Phase 3: Nov 2024 – Aug 2025   monthly last Wednesday
    Phase 4: Sep 2025 onwards      monthly last Tuesday
    """
    try:
        df = pd.read_csv(f"{DATA_DIR}/trade_log.csv", parse_dates=["date"])
    except FileNotFoundError:
        print("trade_log.csv not found — run: python3 backtest_engine.py  first.")
        return

    active = df[df["result"].isin(["WIN", "LOSS", "PARTIAL", "TRAIL_SL"])].copy()
    active["date"] = pd.to_datetime(active["date"])

    def phase_label(d):
        if d < pd.Timestamp(WEDNESDAY_WEEKLY_START):
            return "Ph1 weekly-Thu"
        elif d < pd.Timestamp(WEEKLY_DISCONTINUED):
            return "Ph2 weekly-Wed"
        elif d < pd.Timestamp(TUESDAY_EXPIRY_FROM):
            return "Ph3 monthly-Wed"
        else:
            return "Ph4 monthly-Tue"

    active["phase"]  = active["date"].apply(phase_label)
    active["month"]  = active["date"].dt.to_period("M")

    rows = []
    for month, grp in active.groupby("month"):
        wins     = (grp["result"] == "WIN").sum()
        losses   = (grp["result"] == "LOSS").sum()
        trail    = (grp["result"] == "TRAIL_SL").sum()
        total    = len(grp)
        wr       = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
        net_pnl  = grp["pnl"].sum()
        cum_cap  = grp["capital_after"].iloc[-1]
        phase    = grp["phase"].iloc[-1]   # phase of last trade in the month
        rows.append({
            "month":   str(month),
            "phase":   phase,
            "trades":  total,
            "W":       wins,
            "L":       losses,
            "T":       trail,
            "WR%":     f"{wr:.0f}%",
            "net_pnl": net_pnl,
            "P&L":     fmt_inr(net_pnl),
            "cap_end":  fmt_inr(cum_cap),
        })

    result = pd.DataFrame(rows)

    # ── Per-phase summary ─────────────────────────────────────────────────────
    phase_order = ["Ph1 weekly-Thu", "Ph2 weekly-Wed",
                   "Ph3 monthly-Wed", "Ph4 monthly-Tue"]
    print(f"\n{'='*95}")
    print(f"  PHASE SUMMARY")
    print(f"{'='*95}")
    print(f"  {'Phase':<18} {'Months':>6} {'Trades':>7} {'Wins':>5} {'Loss':>5}"
          f" {'Trail':>6} {'WR%':>6} {'Net P&L':>12}")
    print(f"  {'─'*85}")
    for ph in phase_order:
        sub = active[active["phase"] == ph]
        if len(sub) == 0:
            continue
        pw = (sub["result"] == "WIN").sum()
        pl = (sub["result"] == "LOSS").sum()
        pt = (sub["result"] == "TRAIL_SL").sum()
        pwr = pw / (pw + pl) * 100 if (pw + pl) > 0 else 0
        months = sub["month"].nunique()
        pnl = sub["pnl"].sum()
        print(f"  {ph:<18} {months:>6} {len(sub):>7} {pw:>5} {pl:>5}"
              f" {pt:>6} {pwr:>5.0f}% {fmt_inr(pnl):>12}")

    # ── Month-on-month detail ─────────────────────────────────────────────────
    print(f"\n{'='*95}")
    print(f"  MONTH-ON-MONTH  (phase tag = regime active at end of month)")
    print(f"{'='*95}")
    print(f"  {'Month':<9} {'Phase':<16} {'Tr':>3} {'W':>4} {'L':>4} {'T':>4}"
          f" {'WR%':>5} {'P&L':>10} {'Cap':>10}")
    print(f"  {'─'*83}")

    prev_phase = None
    for _, r in result.iterrows():
        sep = "  " if r["phase"] == prev_phase else "──"
        print(f"{sep} {r['month']:<9} {r['phase']:<16} {r['trades']:>3}"
              f" {r['W']:>4} {r['L']:>4} {r['T']:>4}"
              f" {r['WR%']:>5} {r['P&L']:>10} {r['cap_end']:>10}")
        prev_phase = r["phase"]

    print(f"{'='*95}\n")


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

    if len(_sys.argv) >= 2 and _sys.argv[1] == "--phases":
        print("Month-on-month breakdown by expiry regime phase...")
        run_phase_analysis()
        return

    if len(_sys.argv) >= 2 and _sys.argv[1] == "--real-premium":
        # ── Real-premium backtest (rule-based signals + Dhan option prices) ──
        print("Running backtest with REAL option premiums [SL=15%, TP=37.5% (RR=2.5x), trail=₹5]")
        print("Premium source: Dhan /charts/rollingoption  (falls back to formula if missing)")
        trade_df, monthly = run_backtest(
            trail_jump_opt=5, sl_pct=0.15, flat_rr=2.5,
            use_actual_dte=True, ml=False, use_real_premiums=True,
        )
        trade_df.to_csv(f"{DATA_DIR}/trade_log_real.csv",    index=False)
        monthly.to_csv( f"{DATA_DIR}/equity_curve_real.csv", index=False)
        try:
            threshold = int(pd.read_csv(f"{DATA_DIR}/signals.csv", nrows=1)
                            .get("threshold", [None]).iloc[0] or 0) or None
        except Exception:
            threshold = None
        print_summary(trade_df, monthly, threshold=threshold, ml=False)
        print(f"\nSaved → {DATA_DIR}/trade_log_real.csv")
        print(f"Saved → {DATA_DIR}/equity_curve_real.csv")
        return

    if len(_sys.argv) >= 2 and _sys.argv[1] == "--real-premium-ml":
        # ── Real-premium ML backtest ──────────────────────────────────────────
        sig_file = f"{DATA_DIR}/signals_ml.csv"
        if not os.path.exists(sig_file):
            print("signals_ml.csv not found. Run: python3 ml_engine.py first.")
            return
        print("Running ML backtest with REAL option premiums [SL=15%, TP=37.5% (RR=2.5x), trail=₹5]")
        trade_df, monthly = run_backtest(
            trail_jump_opt=5, sl_pct=0.15, flat_rr=2.5,
            use_actual_dte=True, ml=True, use_real_premiums=True,
        )
        trade_df.to_csv(f"{DATA_DIR}/trade_log_ml_real.csv",    index=False)
        monthly.to_csv( f"{DATA_DIR}/equity_curve_ml_real.csv", index=False)
        print_summary(trade_df, monthly, threshold=None, ml=True)
        print(f"\nSaved → {DATA_DIR}/trade_log_ml_real.csv")
        print(f"Saved → {DATA_DIR}/equity_curve_ml_real.csv")
        return

    if len(_sys.argv) >= 2 and _sys.argv[1] == "--ml":
        # ── ML-enhanced backtest ──────────────────────────────────────────────
        sig_file = f"{DATA_DIR}/signals_ml.csv"
        if not os.path.exists(sig_file):
            print("signals_ml.csv not found.")
            print("Run:  python3 ml_engine.py   to generate ML signals first.")
            return
        try:
            sig_df    = pd.read_csv(sig_file, nrows=1)
            threshold = int(sig_df["threshold"].iloc[0]) if "threshold" in sig_df.columns else None
            ml_mode   = "ML"
            if "rule_signal" in sig_df.columns and "signal" in sig_df.columns:
                # Detect combined mode: if signal column differs from ml_signal column
                if "ml_signal" in sig_df.columns:
                    full  = pd.read_csv(sig_file)
                    agree = (full["signal"] == full["ml_signal"]).all()
                    ml_mode = "ML-only" if agree else "COMBINED (rule+ML)"
        except Exception:
            threshold = None
            ml_mode   = "ML"

        print(f"Running ML backtest...  [{ml_mode} | SL=15%, TP=30%, RR=2.0x, trail=₹5, actual DTE]")
        trade_df, monthly = run_backtest(trail_jump_opt=5, sl_pct=0.15, flat_rr=2.0,
                                         use_actual_dte=True, ml=True)

        trade_df.to_csv(f"{DATA_DIR}/trade_log_ml.csv",    index=False)
        monthly.to_csv( f"{DATA_DIR}/equity_curve_ml.csv", index=False)

        print_summary(trade_df, monthly, threshold=threshold, ml=True)
        print(f"\nSaved → {DATA_DIR}/trade_log_ml.csv")
        print(f"Saved → {DATA_DIR}/equity_curve_ml.csv")
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
