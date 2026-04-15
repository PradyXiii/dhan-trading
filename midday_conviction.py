#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
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
    """Current BankNifty spot via Dhan option chain, with API structure unwrap."""
    try:
        r = requests.post(
            "https://api.dhan.co/v2/optionchain/expirylist",
            headers=HEADERS,
            json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I"},
            timeout=10,
        )
        expiries = r.json().get("data", [])
        if not expiries:
            _log("expirylist returned empty — market closed or API issue")
            return None
        expiry = expiries[0]
        r2 = requests.post(
            "https://api.dhan.co/v2/optionchain",
            headers=HEADERS,
            json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I", "Expiry": expiry},
            timeout=10,
        )
        inner = r2.json().get("data") or {}
        # Dhan wraps chain in {"<id>": {"last_price": ..., "oc": {...}}}
        if isinstance(inner, dict) and "last_price" not in inner:
            inner = next(iter(inner.values()), {})
        spot = float(inner.get("last_price") or inner.get("underlyingPrice") or 0)
        return spot if spot > 0 else None
    except Exception as e:
        _log(f"BN spot unavailable: {e}")
        return None


def _get_ltp_from_marketfeed(security_id: str) -> float | None:
    """Fetch current option LTP from Dhan marketfeed/ltp using our security_id.
    This is the most direct route — no strike-key guessing needed."""
    try:
        sid_int = int(security_id)
        resp = requests.post(
            "https://api.dhan.co/v2/marketfeed/ltp",
            headers=HEADERS,
            json={"NSE_FNO": [sid_int]},
            timeout=10,
        )
        if resp.status_code != 200:
            _log(f"marketfeed/ltp {resp.status_code}: {resp.text[:80]}")
            return None
        d = resp.json()
        # Response: {"data": {"NSE_FNO": {<sid>: {"last_price": ...}}}} or similar
        fno_data = (d.get("data") or {}).get("NSE_FNO") or d.get("NSE_FNO") or {}
        entry = fno_data.get(sid_int) or fno_data.get(str(sid_int)) or fno_data.get(security_id) or {}
        ltp = float(entry.get("last_price") or entry.get("ltp") or entry.get("lastTradedPrice") or 0)
        if ltp > 0:
            _log(f"marketfeed/ltp: ₹{ltp:.0f}  [SID {security_id}]")
            return ltp
        _log(f"marketfeed/ltp returned 0 for SID {security_id}. Payload: {str(d)[:120]}")
    except Exception as e:
        _log(f"marketfeed/ltp failed: {e}")
    return None


def _get_ltp_from_option_chain(trade: dict) -> float | None:
    """Fetch current option premium from Dhan option chain at our known strike.

    Strategy:
      1. marketfeed/ltp by security_id — direct, reliable, no key-format guessing.
      2. Option chain scan by security_id — handles any oc key format.
      3. Option chain strike key lookup — last resort.
    """
    our_sid = str(trade.get("security_id", ""))

    # ── Strategy 1: marketfeed/ltp (fastest, most direct) ─────────────────────
    if our_sid:
        ltp = _get_ltp_from_marketfeed(our_sid)
        if ltp:
            return ltp

    # ── Strategy 2 & 3: option chain ──────────────────────────────────────────
    try:
        strike      = float(trade.get("strike", 0))
        opt_type_lc = "ce" if trade.get("signal", "CALL") == "CALL" else "pe"
        opt_type_uc = opt_type_lc.upper()

        # Use stored expiry from today_trade.json if available; else fetch nearest
        if trade.get("expiry"):
            expiry = trade["expiry"]   # already a string "YYYY-MM-DD"
        else:
            r = requests.post(
                "https://api.dhan.co/v2/optionchain/expirylist",
                headers=HEADERS,
                json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I"},
                timeout=10,
            )
            expiry = r.json()["data"][0]

        # Fetch option chain
        r2 = requests.post(
            "https://api.dhan.co/v2/optionchain",
            headers=HEADERS,
            json={"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I", "Expiry": expiry},
            timeout=15,
        )
        data  = r2.json()
        inner = data.get("data") or {}

        # Dhan wraps chain in a single-key dict (instrument/scrip id → actual data)
        # e.g. {"805": {"last_price": ..., "oc": {...}}}
        if isinstance(inner, dict) and "oc" not in inner and "last_price" not in inner:
            inner = next(iter(inner.values()), {})
            _log(f"Unwrapped option chain nesting → inner keys: {list(inner.keys())[:5]}")

        oc = (inner.get("oc") if isinstance(inner, dict) else None) or {}

        if not oc:
            _log(f"Option chain oc still empty after unwrap (expiry {expiry}). "
                 f"inner keys: {list(inner.keys()) if isinstance(inner, dict) else type(inner)}")
            return None

        _log(f"Option chain: {len(oc)} strikes  (expiry {expiry}). "
             f"Sample keys: {list(oc.keys())[:3]}")

        # Strategy 2: scan by security_id (format-agnostic)
        if our_sid:
            for strike_key, opts in oc.items():
                for otk in (opt_type_lc, opt_type_uc):
                    sub = opts.get(otk) if isinstance(opts, dict) else None
                    if not sub:
                        continue
                    sid_in_chain = str(sub.get("security_id") or sub.get("securityId") or "")
                    if sid_in_chain == our_sid:
                        ltp = float(sub.get("last_price") or sub.get("ltp")
                                    or sub.get("lastPrice") or 0)
                        _log(f"Option chain matched SID {our_sid}: ₹{ltp:.0f}  [{strike_key} {otk.upper()}]")
                        if ltp > 0:
                            return ltp
                        _log("Option chain SID match but LTP=0")

        # Strategy 3: strike key lookup
        key = (f"{strike:.6f}"  if f"{strike:.6f}"  in oc else
               str(int(strike)) if str(int(strike)) in oc else None)
        if key is None:
            _log(f"Strike {strike:.0f} not in oc keys. Sample: {list(oc.keys())[:5]}")
            return None
        sub = oc[key].get(opt_type_lc) or oc[key].get(opt_type_uc) or {}
        ltp = float(sub.get("last_price") or sub.get("ltp") or sub.get("lastPrice") or 0)
        if ltp > 0:
            _log(f"Option chain LTP by strike: ₹{ltp:.0f}  [{strike:.0f} {opt_type_uc}]")
            return ltp
        _log(f"Option chain strike key also returned 0 for {strike:.0f} {opt_type_uc}")
    except Exception as e:
        _log(f"Option chain LTP fallback failed: {e}")
    return None


def get_option_ltp(security_id: str, trade: dict | None = None) -> float | None:
    """Current LTP of our open BN option.
    1. Tries positions API (fastest, matches by security_id then any open BN pos).
    2. If LTP=0, falls back to option chain at our known strike (most accurate).
    """
    try:
        r = requests.get("https://api.dhan.co/v2/positions", headers=HEADERS, timeout=10)
        if r.status_code != 200:
            _log(f"Positions API {r.status_code}: {r.text[:80]}")
        else:
            data  = r.json()
            items = data if isinstance(data, list) else data.get("data", [])
            bn_positions = [
                p for p in items
                if int(p.get("netQty", 0)) > 0
                and p.get("exchangeSegment", "") == "NSE_FNO"
                and "BANKNIFTY" in str(p.get("tradingSymbol", p.get("securityId", ""))).upper()
            ]
            # Prefer exact security_id match
            ltp_from_pos = None
            for p in bn_positions:
                if str(p.get("securityId", p.get("security_id", ""))) == str(security_id):
                    ltp = float(p.get("lastTradedPrice", p.get("ltp", 0)))
                    _log(f"Positions matched security_id: ₹{ltp:.0f}  [{p.get('tradingSymbol','')}]")
                    ltp_from_pos = ltp
                    break
            # Fallback: any open BN position
            if ltp_from_pos is None and bn_positions:
                p   = bn_positions[0]
                ltp = float(p.get("lastTradedPrice", p.get("ltp", 0)))
                _log(f"Positions fallback match: ₹{ltp:.0f}  [{p.get('tradingSymbol','')}]")
                ltp_from_pos = ltp
            if ltp_from_pos and ltp_from_pos > 0:
                return ltp_from_pos
            if bn_positions:
                _log("LTP=0 from positions — trying option chain...")
            else:
                _log("No open BN positions found — trying option chain...")
    except Exception as e:
        _log(f"Positions API error: {e}")

    # Option chain fallback — uses known strike+type from trade dict
    if trade:
        return _get_ltp_from_option_chain(trade)
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
    option_ltp = get_option_ltp(security_id, trade)
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
