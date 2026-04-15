#!/usr/bin/env python3
"""
midday_conviction.py — Intraday trade conviction check
=======================================================
Fetches current BN spot + option LTP + intraday macro, reassesses
whether the morning signal thesis still holds, sends Telegram summary.

Usage:
  python3 midday_conviction.py          # live run
  python3 midday_conviction.py --dry-run # print without sending Telegram

Cron (11:00 AM IST = 5:30 AM UTC):
  30 5 * * 1-5 cd ~/dhan-trading && python3 midday_conviction.py >> logs/conviction.log 2>&1
"""

import os
import sys
import json
import requests
import pandas as pd
from datetime import date, datetime
from dotenv import load_dotenv
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

load_dotenv()

_HERE     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(_HERE, "data")
TOKEN     = os.getenv("DHAN_ACCESS_TOKEN", "")
CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
DRY_RUN   = "--dry-run" in sys.argv
IST       = ZoneInfo("Asia/Kolkata")

HEADERS = {
    "access-token": TOKEN,
    "client-id":    CLIENT_ID,
    "Content-Type": "application/json",
}

import notify


def _log(msg):
    ts = datetime.now(IST).strftime("%H:%M:%S IST")
    print(f"[{ts}] {msg}")


# ── Data loaders ──────────────────────────────────────────────────────────────

def load_trade() -> dict | None:
    """Load today's oracle intent from today_trade.json. Returns None if stale."""
    path = os.path.join(DATA_DIR, "today_trade.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        t = json.load(f)
    if str(t.get("date", "")) != str(date.today()):
        _log(f"today_trade.json dated {t.get('date')} — not today's trade.")
        return None
    return t


def get_bn_spot() -> float | None:
    """Current BankNifty spot via Dhan option chain."""
    try:
        r = requests.post(
            "https://api.dhan.co/v2/optionchain/expirylist",
            headers=HEADERS,
            json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I"},
            timeout=10,
        )
        expiry = r.json()["data"][0]
        r2 = requests.post(
            "https://api.dhan.co/v2/optionchain",
            headers=HEADERS,
            json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I", "Expiry": expiry},
            timeout=10,
        )
        return float(r2.json()["data"]["last_price"])
    except Exception as e:
        _log(f"BN spot unavailable: {e}")
        return None


def get_option_ltp(security_id: str) -> float | None:
    """Current LTP of our open BN option from Dhan positions API.
    Matches by security_id first; falls back to first open NSE_FNO BANKNIFTY pos."""
    try:
        r = requests.get("https://api.dhan.co/v2/positions", headers=HEADERS, timeout=10)
        if r.status_code != 200:
            _log(f"Positions API {r.status_code}: {r.text[:80]}")
            return None
        data  = r.json()
        items = data if isinstance(data, list) else data.get("data", [])
        bn_positions = [
            p for p in items
            if int(p.get("netQty", 0)) > 0
            and p.get("exchangeSegment", "") == "NSE_FNO"
            and "BANKNIFTY" in str(p.get("tradingSymbol", p.get("securityId", ""))).upper()
        ]
        # Prefer exact security_id match
        for p in bn_positions:
            if str(p.get("securityId", p.get("security_id", ""))) == str(security_id):
                ltp = float(p.get("lastTradedPrice", p.get("ltp", 0)))
                _log(f"Option LTP matched by security_id: ₹{ltp:.0f}  [{p.get('tradingSymbol','')}]")
                return ltp if ltp > 0 else None
        # Fallback: any open BN position (there should only be one per day)
        if bn_positions:
            p   = bn_positions[0]
            ltp = float(p.get("lastTradedPrice", p.get("ltp", 0)))
            _log(f"Option LTP (fallback match): ₹{ltp:.0f}  [{p.get('tradingSymbol','')}]")
            return ltp if ltp > 0 else None
        _log("No open BN positions found — SL/TP may have already fired.")
    except Exception as e:
        _log(f"Option LTP unavailable: {e}")
    return None


def get_macro() -> dict:
    """Intraday SP500 futures + DXY + India VIX via yfinance."""
    import yfinance as yf
    out = {}

    def _fetch(ticker, key_now, key_prev, interval, period):
        try:
            df = yf.download(ticker, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            close = df["Close"].dropna() if "Close" in df.columns else pd.Series(dtype=float)
            if len(close) < 2:
                _log(f"  macro {ticker}: only {len(close)} rows — skipping")
                return
            out[key_now]  = float(close.iloc[-1])
            out[key_prev] = float(close.iloc[0])
            _log(f"  macro {ticker}: {float(close.iloc[0]):.2f} → {float(close.iloc[-1]):.2f}")
        except Exception as e:
            _log(f"  macro {ticker} error: {e}")

    # SP500 futures — intraday (US markets open ~7:30 PM IST; use prior day if pre-open)
    _fetch("ES=F",      "sp500f_now",  "sp500f_open",  "5m",  "2d")
    # DXY — intraday
    _fetch("DX-Y.NYB",  "dxy_now",     "dxy_open",     "5m",  "2d")
    # India VIX — daily (no intraday available via yfinance)
    _fetch("^INDIAVIX", "vix_now",     "vix_prev",     "1d",  "5d")

    # Derived
    if "sp500f_now" in out and "sp500f_open" in out and out.get("sp500f_open", 0):
        out["sp500f_chg_pct"] = (out["sp500f_now"] - out["sp500f_open"]) / out["sp500f_open"] * 100
    if "dxy_now" in out and "dxy_open" in out and out.get("dxy_open", 0):
        out["dxy_chg_pct"] = (out["dxy_now"] - out["dxy_open"]) / out["dxy_open"] * 100
    if "vix_now" in out and "vix_prev" in out:
        out["vix_chg"] = out["vix_now"] - out["vix_prev"]
    return out


# ── Conviction engine ─────────────────────────────────────────────────────────

def reassess(trade, bn_spot, option_ltp, macro) -> tuple[int, list, str]:
    """
    Score the 4 conviction factors in real-time.
    Returns (score -4..+4, factor lines, verdict string).
    """
    direction  = trade.get("signal", "CALL")
    entry_prem = float(trade.get("oracle_premium", 0))
    sl_price   = float(trade.get("sl_price", 0))
    tp_price   = float(trade.get("tp_price", 0))
    entry_spot = float(trade.get("spot_at_signal", 0))

    bull = direction == "CALL"
    score = 0
    lines = []

    # ── Factor 1: Premium vs SL ───────────────────────────────────────────────
    if option_ltp and entry_prem > 0:
        prem_chg_pct = (option_ltp - entry_prem) / entry_prem * 100
        sl_buffer    = (option_ltp - sl_price) / entry_prem * 100   # % of entry left before SL
        if bull:
            if prem_chg_pct >= 0:
                lines.append(f"✅ Premium +{prem_chg_pct:.1f}%  ₹{option_ltp:.0f} (entry ₹{entry_prem:.0f})")
                score += 1
            elif sl_buffer > 7:
                lines.append(f"🟡 Premium {prem_chg_pct:+.1f}%  ₹{option_ltp:.0f} — {sl_buffer:.0f}% buffer to SL")
            else:
                lines.append(f"🔴 Premium {prem_chg_pct:+.1f}%  ₹{option_ltp:.0f} — only {sl_buffer:.0f}% to SL ₹{sl_price:.0f}")
                score -= 1
    else:
        lines.append("⬜ Option LTP unavailable (SL/TP may have already fired)")

    # ── Factor 2: BN spot trend ───────────────────────────────────────────────
    if bn_spot and entry_spot:
        spot_chg = (bn_spot - entry_spot) / entry_spot * 100
        if bull:
            if spot_chg > 0.2:
                lines.append(f"✅ BN spot +{spot_chg:.2f}%  ₹{bn_spot:,.0f} (entry ₹{entry_spot:,.0f})")
                score += 1
            elif spot_chg > -0.3:
                lines.append(f"➡️ BN spot flat {spot_chg:+.2f}%  ₹{bn_spot:,.0f}")
            else:
                lines.append(f"🔴 BN spot {spot_chg:+.2f}%  ₹{bn_spot:,.0f}")
                score -= 1
        else:
            if spot_chg < -0.2:
                lines.append(f"✅ BN spot {spot_chg:.2f}%  ₹{bn_spot:,.0f} (PUT thesis: BN falling)")
                score += 1
            elif spot_chg < 0.3:
                lines.append(f"➡️ BN spot flat {spot_chg:+.2f}%  ₹{bn_spot:,.0f}")
            else:
                lines.append(f"🔴 BN spot +{spot_chg:.2f}%  ₹{bn_spot:,.0f} (PUT headwind)")
                score -= 1
    else:
        lines.append("⬜ BN spot unavailable")

    # ── Factor 3: SP500 futures (global risk sentiment) ───────────────────────
    if "sp500f_chg_pct" in macro:
        sp = macro["sp500f_chg_pct"]
        if bull:
            if sp > 0.3:
                lines.append(f"✅ SP500 futures +{sp:.1f}% — risk-on supports CALL")
                score += 1
            elif sp > -0.3:
                lines.append(f"➡️ SP500 futures flat {sp:+.1f}%")
            else:
                lines.append(f"🔴 SP500 futures {sp:+.1f}% — risk-off headwind for CALL")
                score -= 1
        else:
            if sp < -0.3:
                lines.append(f"✅ SP500 futures {sp:+.1f}% — risk-off supports PUT")
                score += 1
            elif sp < 0.3:
                lines.append(f"➡️ SP500 futures flat {sp:+.1f}%")
            else:
                lines.append(f"🔴 SP500 futures +{sp:.1f}% — risk-on headwind for PUT")
                score -= 1
    else:
        lines.append("⬜ SP500 futures unavailable")

    # ── Factor 4: India VIX ───────────────────────────────────────────────────
    if "vix_chg" in macro:
        vchg = macro["vix_chg"]
        vnow = macro.get("vix_now", "?")
        if bull:
            if vchg < -0.5:
                lines.append(f"✅ India VIX {vchg:+.1f} → {vnow:.1f} — fear easing, CALL tailwind")
                score += 1
            elif vchg > 0.5:
                lines.append(f"🔴 India VIX {vchg:+.1f} → {vnow:.1f} — fear rising, CALL headwind")
                score -= 1
            else:
                lines.append(f"➡️ India VIX flat {vchg:+.1f} → {vnow:.1f}")
        else:
            if vchg > 0.5:
                lines.append(f"✅ India VIX {vchg:+.1f} → {vnow:.1f} — fear rising, PUT tailwind")
                score += 1
            elif vchg < -0.5:
                lines.append(f"🔴 India VIX {vchg:+.1f} → {vnow:.1f} — fear easing, PUT headwind")
                score -= 1
            else:
                lines.append(f"➡️ India VIX flat {vchg:+.1f} → {vnow:.1f}")
    else:
        lines.append("⬜ India VIX unavailable")

    # ── Verdict ───────────────────────────────────────────────────────────────
    if score >= 2:
        verdict = "🟢 HOLD — thesis intact"
    elif score >= 0:
        verdict = "🟡 HOLD with caution — mixed signals"
    elif score == -1:
        verdict = "🟠 WEAKENING — 1+ factors against thesis"
    else:
        verdict = "🔴 THESIS BROKEN — consider reviewing SL"

    return score, lines, verdict


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    _log("Midday conviction check starting...")

    trade = load_trade()
    if not trade:
        _log("No today_trade.json for today — no open trade to assess.")
        return

    direction  = trade.get("signal", "CALL")
    strike     = int(trade.get("strike", 0))
    lots       = int(trade.get("lots", 1))
    entry_prem = float(trade.get("oracle_premium", 0))
    sl_price   = float(trade.get("sl_price", 0))
    tp_price   = float(trade.get("tp_price", 0))
    dte        = int(trade.get("dte", 0))
    security_id = str(trade.get("security_id", ""))
    orig_score = int(trade.get("signal_score", 0))

    _log(f"Trade: {direction} {strike}  entry ₹{entry_prem:.0f}  SL ₹{sl_price:.0f}  TP ₹{tp_price:.0f}")

    # Fetch live data
    bn_spot    = get_bn_spot()
    option_ltp = get_option_ltp(security_id)
    macro      = get_macro()

    _log(f"BN spot: {bn_spot}  |  Option LTP: {option_ltp}  |  Macro keys: {list(macro.keys())}")

    # Reassess
    conv_score, factor_lines, verdict = reassess(trade, bn_spot, option_ltp, macro)

    # Build Telegram message
    opt_type = "CE" if direction == "CALL" else "PE"
    icon     = "📈" if direction == "CALL" else "📉"
    now_str  = datetime.now(IST).strftime("%I:%M %p IST")

    # Premium P&L line
    if option_ltp and entry_prem:
        pnl_pct  = (option_ltp - entry_prem) / entry_prem * 100
        pnl_rs   = (option_ltp - entry_prem) * lots * 30   # 30 = lot size
        prem_line = (f"₹{entry_prem:.0f} → ₹{option_ltp:.0f}  "
                     f"({pnl_pct:+.1f}%  {'+' if pnl_rs>=0 else ''}₹{pnl_rs:,.0f})")
    else:
        prem_line = f"Entry ₹{entry_prem:.0f}  (LTP unavailable — may be closed)"

    spot_str = f"₹{bn_spot:,.0f}" if bn_spot else "unavailable"

    macro_parts = []
    if "sp500f_chg_pct" in macro:
        macro_parts.append(f"SP500F {macro['sp500f_chg_pct']:+.1f}%")
    if "dxy_chg_pct" in macro:
        macro_parts.append(f"DXY {macro['dxy_chg_pct']:+.1f}%")
    if "vix_now" in macro:
        macro_parts.append(f"VIX {macro['vix_now']:.1f}")
    macro_str = "  ·  ".join(macro_parts) if macro_parts else "unavailable"

    factors_str = "\n".join(factor_lines)

    msg = (
        f"{icon}  <b>Midday Conviction  ·  {now_str}</b>\n\n"
        f"<b>BANKNIFTY 28Apr2026 {strike} {opt_type}</b>\n"
        f"Premium: {prem_line}\n"
        f"BN spot: {spot_str}\n"
        f"SL ₹{sl_price:.0f}  ·  TP ₹{tp_price:.0f}  ·  {dte}DTE\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Entry signal: {direction} score {orig_score:+d}/4\n\n"
        f"<b>Live factors:</b>\n"
        f"{factors_str}\n\n"
        f"<b>Macro:</b>  {macro_str}\n\n"
        f"<b>{verdict}</b>\n"
        f"<i>Conviction: {conv_score:+d}/4</i>"
    )

    _log(f"Verdict: {verdict}  (conviction {conv_score:+d}/4)")

    if DRY_RUN:
        print("\n── Telegram preview ──────────────────────")
        # Strip HTML tags for console
        import re
        print(re.sub(r"<[^>]+>", "", msg))
        print("──────────────────────────────────────────")
        return

    notify.send(msg)
    _log("Conviction message sent to Telegram.")


if __name__ == "__main__":
    main()
