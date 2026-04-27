#!/usr/bin/env python3
"""
analyze_today_trade.py — Live post-mortem on today's trade.

Pulls actual position data from Dhan, today_trade.json intent, and ML feature
values, then prints a full breakdown:
  1. Entry context (signal, score, ML conf, capital, lot count)
  2. Live spread cost vs net credit (mid-day P&L)
  3. EOD exit projection (theta decay assumption)
  4. Why ML predicted PUT — top 5 features today + their value
  5. Historical analogs from live_spread_trades.csv (similar setups)

Usage:
  python3 analyze_today_trade.py
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from dhan_journal import get_positions, leg_avgs, realized_pnl
import auto_trader as AT

_IST = timezone(timedelta(hours=5, minutes=30))
HEADERS = {
    "access-token": os.getenv("DHAN_ACCESS_TOKEN", ""),
    "client-id":    os.getenv("DHAN_CLIENT_ID", ""),
    "Content-Type": "application/json",
    "Accept":       "application/json",
}


def _fmt_money(v):
    return f"Rs.{v:+,.0f}" if v is not None else "n/a"


def _fetch_ltps(security_ids: list) -> dict:
    """Fetch LTPs from Dhan /v2/marketfeed/ltp. Returns {sid_str: float}."""
    try:
        payload = {"NSE_FNO": [int(s) for s in security_ids if s]}
        resp = requests.post("https://api.dhan.co/v2/marketfeed/ltp",
                             headers=HEADERS, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"  LTP fetch HTTP {resp.status_code}: {resp.text[:100]}")
            return {}
        d   = resp.json()
        seg = (d.get("data") or {}).get("NSE_FNO") or {}
        out = {}
        for sid in security_ids:
            entry = seg.get(str(sid)) or seg.get(int(sid)) or {}
            out[str(sid)] = float(
                entry.get("last_price") or entry.get("lastTradedPrice") or 0
            )
        return out
    except Exception as e:
        print(f"  LTP fetch failed: {e}")
        return {}


def _section(title):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def fetch_optionchain_premiums(expiry_str, target_strikes_ce, target_strikes_pe):
    """Get CE/PE premiums for specific strikes from option chain."""
    payload = {
        "UnderlyingScrip": 13,
        "UnderlyingSeg":   "IDX_I",
        "Expiry":          expiry_str,
    }
    resp = requests.post(
        "https://api.dhan.co/v2/optionchain",
        headers=HEADERS, json=payload, timeout=15,
    )
    if resp.status_code != 200:
        print(f"  optionchain HTTP {resp.status_code}: {resp.text[:200]}")
        return None, {}, {}

    data  = resp.json().get("data") or {}
    spot  = float(data.get("last_price", 0))
    oc    = data.get("oc", {}) or {}
    ce_ltp, pe_ltp = {}, {}
    for strike_str, leg in oc.items():
        try:
            strike = int(float(strike_str))
        except Exception:
            continue
        ce = leg.get("ce") or {}
        pe = leg.get("pe") or {}
        if strike in target_strikes_ce and "last_price" in ce:
            ce_ltp[strike] = float(ce["last_price"])
        if strike in target_strikes_pe and "last_price" in pe:
            pe_ltp[strike] = float(pe["last_price"])
    return spot, ce_ltp, pe_ltp


def main():
    # ── 1. Today's trade intent ───────────────────────────────────────────────
    _section("1. ENTRY CONTEXT (today_trade.json)")
    tt_path = Path("data/today_trade.json")
    if not tt_path.exists():
        print("  No today_trade.json — auto_trader may not have run today.")
        return
    tt = json.loads(tt_path.read_text())

    today_date = tt.get("date", "")
    strategy   = tt.get("strategy", "?")
    signal     = tt.get("signal", "?")
    ml_conf    = tt.get("ml_conf")
    score      = tt.get("rule_score")
    lots       = tt.get("lots", 0)
    lot_size   = tt.get("lot_size", 65)
    qty        = lots * lot_size

    print(f"  Date:        {today_date}")
    print(f"  Strategy:    {strategy}")
    print(f"  Signal:      {signal}  (rule score: {score}, ML conf: {ml_conf})")
    print(f"  Lots:        {lots} × {lot_size} = {qty} qty")

    # ── 2. Leg structure + entry premiums ─────────────────────────────────────
    _section("2. LEG STRUCTURE + ENTRY PREMIUMS")
    if strategy == "bull_put_credit":
        short_sid = tt.get("short_sid")
        long_sid  = tt.get("long_sid")
        short_strike = tt.get("short_strike", 0)
        long_strike  = tt.get("long_strike", 0)
        short_entry  = float(tt.get("short_entry", 0))
        long_entry   = float(tt.get("long_entry", 0))
        net_credit_per_share = short_entry - long_entry
        net_credit_total     = net_credit_per_share * qty
        max_loss_per_share   = (short_strike - long_strike) - net_credit_per_share
        max_loss_total       = max_loss_per_share * qty

        print(f"  SHORT PUT {int(short_strike)}: entry @ Rs.{short_entry:.2f}  (sid {short_sid})")
        print(f"  LONG  PUT {int(long_strike)}: entry @ Rs.{long_entry:.2f}  (sid {long_sid})")
        print(f"  Net credit/share: Rs.{net_credit_per_share:.2f}")
        print(f"  Net credit total: {_fmt_money(net_credit_total)}")
        print(f"  Spread width:     {int(short_strike - long_strike)}pt")
        print(f"  Max loss:         {_fmt_money(-max_loss_total)}")
        print(f"  Breakeven NF:     {short_strike - net_credit_per_share:.0f}")

    elif strategy == "nf_iron_condor":
        print("  IC structure — implement IC-specific breakdown if needed")
    else:
        print(f"  Unknown strategy: {strategy}")
        return

    # ── 3. Live position from Dhan ────────────────────────────────────────────
    _section("3. LIVE POSITION (Dhan API /v2/positions)")
    try:
        positions = get_positions()
    except Exception as e:
        print(f"  ERROR fetching positions: {e}")
        return

    if not positions:
        print("  No open positions on Dhan — already squared off?")
        return

    short_avgs = leg_avgs(positions, str(short_sid))
    long_avgs  = leg_avgs(positions, str(long_sid))
    # Dhan-truth: pull LTPs from /v2/marketfeed/ltp directly. `leg_avgs()` does
    # NOT return an "ltp" key — earlier `.get("ltp", 0)` always read 0, making
    # every downstream LTP / spread-cost number garbage.
    ltp_map  = _fetch_ltps([short_sid, long_sid])
    short_ltp = ltp_map.get(str(short_sid), 0.0)
    long_ltp  = ltp_map.get(str(long_sid),  0.0)
    short_unrealized = (short_entry - short_ltp) * qty   # we sold, so loss if LTP > entry
    long_unrealized  = (long_ltp - long_entry) * qty     # we bought, so gain if LTP > entry
    net_unrealized   = short_unrealized + long_unrealized

    spread_cost_now  = short_ltp - long_ltp
    spread_cost_pct  = (spread_cost_now / net_credit_per_share * 100) if net_credit_per_share else 0

    print(f"  SHORT PUT {int(short_strike)} LTP: Rs.{short_ltp:.2f}  (entry Rs.{short_entry:.2f}, change {short_ltp-short_entry:+.2f})")
    print(f"  LONG  PUT {int(long_strike)} LTP: Rs.{long_ltp:.2f}  (entry Rs.{long_entry:.2f}, change {long_ltp-long_entry:+.2f})")
    print(f"  Spread cost now:  Rs.{spread_cost_now:.2f}/share  ({spread_cost_pct:.0f}% of net credit)")
    print(f"  SHORT leg P&L:    {_fmt_money(short_unrealized)}")
    print(f"  LONG  leg P&L:    {_fmt_money(long_unrealized)}")
    print(f"  Net unrealized:   {_fmt_money(net_unrealized)}  ({net_unrealized/net_credit_total*100 if net_credit_total else 0:+.1f}% of credit)")

    # SL / TP triggers
    sl_trigger_cost = net_credit_per_share * 1.5
    tp_trigger_cost = net_credit_per_share * 0.35
    print(f"\n  SL trigger: spread cost > Rs.{sl_trigger_cost:.2f}/share  ({'TRIGGERED' if spread_cost_now >= sl_trigger_cost else 'not yet'})")
    print(f"  TP trigger: spread cost < Rs.{tp_trigger_cost:.2f}/share  ({'TRIGGERED' if spread_cost_now <= tp_trigger_cost else 'not yet'})")

    # ── 4. NF spot + distance to short strike ─────────────────────────────────
    _section("4. NF SPOT + STRIKE DISTANCE")
    expiry_str = tt.get("expiry", "")
    spot = None
    if expiry_str:
        try:
            spot, _, _ = fetch_optionchain_premiums(
                expiry_str, set(), {int(short_strike), int(long_strike)},
            )
        except Exception as e:
            print(f"  optionchain fetch failed: {e}")
    if spot is None or spot == 0:
        print("  Spot fetch failed (token expired? run renew_token.py).")
        print("  Skipping spot-based sections — leg P&L still valid above.")
        return
    print(f"  NF spot:           {spot:.2f}")
    print(f"  Distance to short: {spot - short_strike:+.0f} pts ({'ITM by ' + str(int(short_strike-spot)) + 'pt' if spot < short_strike else 'OTM by ' + str(int(spot-short_strike)) + 'pt'})")
    if spot < short_strike:
        intrinsic_short = short_strike - spot
        print(f"  SHORT PUT intrinsic value: Rs.{intrinsic_short:.2f}")
        print(f"  SHORT PUT time value:      Rs.{short_ltp - intrinsic_short:.2f}")

    # ── 5. EOD exit projection ────────────────────────────────────────────────
    _section("5. EOD EXIT PROJECTION (3:15 PM)")
    # Theta assumption: time value decays linearly from now until 3:15 PM
    now      = datetime.now(_IST)
    eod      = now.replace(hour=15, minute=15, second=0, microsecond=0)
    minutes_left = max(0, (eod - now).total_seconds() / 60)
    market_minutes_total = 6 * 60 + 0  # 9:15 AM -> 3:15 PM = 6 hours = 360 min
    decay_fraction = minutes_left / market_minutes_total

    # Approximate theta drag for remaining minutes (only on time value portion)
    if spot < short_strike:
        time_value_short = short_ltp - (short_strike - spot)
    else:
        time_value_short = short_ltp
    if spot < long_strike:
        time_value_long = long_ltp - (long_strike - spot)
    else:
        time_value_long = long_ltp

    proj_short_ltp = (short_strike - spot if spot < short_strike else 0) + time_value_short * decay_fraction
    proj_long_ltp  = (long_strike  - spot if spot < long_strike  else 0) + time_value_long  * decay_fraction
    proj_spread    = proj_short_ltp - proj_long_ltp
    proj_pnl       = (net_credit_per_share - proj_spread) * qty

    print(f"  Time to 3:15 PM:   {minutes_left:.0f} min  ({decay_fraction*100:.0f}% time value remaining)")
    print(f"  Projected SHORT LTP @ EOD: Rs.{proj_short_ltp:.2f}")
    print(f"  Projected LONG  LTP @ EOD: Rs.{proj_long_ltp:.2f}")
    print(f"  Projected spread cost @ EOD: Rs.{proj_spread:.2f}/share")
    print(f"  Projected EOD P&L: {_fmt_money(proj_pnl)}  ({'PROFIT' if proj_pnl > 0 else 'LOSS'} if NF stays here)")

    # ── 6. Sensitivity table — P&L vs NF spot at EOD ──────────────────────────
    _section("6. EOD P&L SENSITIVITY (assuming time value → 0 at 3:15 PM)")
    print(f"  {'NF @ 3:15':>12}  {'Spread cost':>14}  {'P&L':>14}")
    for offset in [-150, -100, -50, -25, 0, 25, 50, 100]:
        nf_eod = spot + offset
        sc_short = max(0, short_strike - nf_eod)
        sc_long  = max(0, long_strike - nf_eod)
        sc       = sc_short - sc_long
        pnl      = (net_credit_per_share - sc) * qty
        marker = " ← current" if offset == 0 else ""
        print(f"  {nf_eod:>12.0f}  Rs.{sc:>10.2f}  {_fmt_money(pnl):>14}{marker}")

    # ── 7. Why ML predicted PUT — feature snapshot ────────────────────────────
    _section("7. WHY ML PREDICTED " + signal)
    try:
        from ml_engine import compute_features, load_all_data, FEATURE_COLS
        df = compute_features(load_all_data())
        today_ts = pd.Timestamp(today_date)
        match = df[df["date"] == today_ts]
        if match.empty:
            match = df.iloc[[-1]]
            print(f"  (today's row not in df — using last row {match.iloc[0]['date'].date()})")
        row = match.iloc[0]

        # Try to load champion + show top features by importance
        try:
            import joblib
            model = joblib.load("models/champion.pkl")
            with open("models/champion_meta.json") as f:
                meta = json.load(f)
            feat_cols = meta.get("feature_cols", FEATURE_COLS)
            if hasattr(model, "feature_importances_"):
                imps = list(zip(feat_cols, model.feature_importances_))
                imps.sort(key=lambda x: -x[1])
                print("  Top 8 features by importance + today's value:")
                for col, imp in imps[:8]:
                    val = row.get(col, "n/a")
                    val_str = f"{val:+.3f}" if isinstance(val, (int, float)) else str(val)
                    print(f"    {col:<22} imp={imp*100:5.1f}%   today={val_str}")
        except Exception as e:
            print(f"  (champion load failed: {e})")

    except Exception as e:
        print(f"  Feature analysis error: {e}")

    # ── 8. Historical analogs ─────────────────────────────────────────────────
    _section("8. HISTORICAL BULL PUT TRADES (live_spread_trades.csv)")
    sp_path = Path("data/live_spread_trades.csv")
    if sp_path.exists():
        try:
            sp = pd.read_csv(sp_path)
            sp_bp = sp[sp.get("strategy", "") == "bull_put_credit"] if "strategy" in sp.columns else sp
            if len(sp_bp):
                wins = (sp_bp["actual_pnl"].astype(float) > 0).sum() if "actual_pnl" in sp_bp.columns else 0
                losses = (sp_bp["actual_pnl"].astype(float) <= 0).sum() if "actual_pnl" in sp_bp.columns else 0
                tot_pnl = sp_bp["actual_pnl"].astype(float).sum() if "actual_pnl" in sp_bp.columns else 0
                print(f"  Total Bull Put trades: {len(sp_bp)}")
                print(f"  Wins / Losses:         {wins}W / {losses}L  ({wins/max(1,wins+losses)*100:.0f}%)")
                print(f"  Cumulative P&L:        {_fmt_money(tot_pnl)}")
                print(f"  Last 5 trades:")
                last5 = sp_bp.tail(5)
                for _, r in last5.iterrows():
                    pnl = float(r.get("actual_pnl", 0)) if r.get("actual_pnl") not in ("", None) else 0
                    print(f"    {r.get('date', '?')}  {r.get('signal', '?')}  {_fmt_money(pnl)}")
            else:
                print("  No Bull Put trades in history.")
        except Exception as e:
            print(f"  Read error: {e}")
    else:
        print("  data/live_spread_trades.csv not found.")

    print()


if __name__ == "__main__":
    main()
