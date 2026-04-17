#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
morning_brief.py — Pre-market BankNifty intelligence brief
===========================================================
Runs at 9:15 AM IST (15 min before auto_trader).

1. Fetches live BN spot from Dhan + S&P futures from yfinance → actual gap data
2. Fetches BANKING-SPECIFIC headlines from ET Banking / Google News (last 20h)
3. Calls Claude to identify event-driven catalysts only (ignores price descriptions)
4. Writes data/news_sentiment.json — auto_trader.py reads this as extra vote

Cron (9:15 AM IST = 3:45 AM UTC, Mon–Fri):
  45 3 * * 1-5 cd ~/dhan-trading && python3 morning_brief.py >> logs/morning_brief.log 2>&1
"""

import os
import json
import email.utils
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone, timedelta

import requests
import pandas as pd
from dotenv import load_dotenv
load_dotenv()

DATA_DIR    = "data"
OUTPUT_FILE = f"{DATA_DIR}/news_sentiment.json"
MAX_AGE_H   = 20      # only headlines from last 20 hours (overnight catalysts)
MAX_LINES   = 10      # cap headlines fed to Claude

# Banking-sector-specific RSS feeds only — no generic Sensex/Nifty noise
NEWS_FEEDS = [
    # ET Markets — Banking & Finance section
    "https://economictimes.indiatimes.com/industry/banking/finance/rssfeeds/13358575.cms",
    # Google News — BankNifty + RBI + banking events (not generic market moves)
    ("https://news.google.com/rss/search?q=%22BankNifty%22+OR+%22RBI%22+OR"
     "+%22HDFC+Bank%22+OR+%22SBI%22+OR+%22ICICI+Bank%22+OR+%22Axis+Bank%22"
     "+OR+%22banking+sector%22+OR+%22bank+results%22"
     "&hl=en-IN&gl=IN&ceid=IN:en"),
]


# ─── Live price fetch ────────────────────────────────────────────────────────

def _fetch_live_bn_spot() -> float | None:
    """Get BankNifty current spot from Dhan LTP endpoint."""
    try:
        headers = {
            "access-token": os.getenv("DHAN_ACCESS_TOKEN", ""),
            "client-id":    os.getenv("DHAN_CLIENT_ID", ""),
            "Content-Type": "application/json",
        }
        resp = requests.post(
            "https://api.dhan.co/v2/marketfeed/ltp",
            headers=headers, json={"IDX_I": [25]}, timeout=10,
        )
        if resp.status_code == 200:
            d        = resp.json()
            idx_data = (d.get("data") or {}).get("IDX_I") or d.get("IDX_I") or {}
            ltp      = (idx_data.get(25) or idx_data.get("25") or {}).get("last_price")
            if ltp and float(ltp) > 10000:
                return float(ltp)
    except Exception as e:
        print(f"  BN spot fetch failed: {e}")
    return None


def _fetch_spf_overnight() -> float | None:
    """Get S&P 500 futures overnight change % from yfinance (ES=F 5m bars)."""
    try:
        import yfinance as yf
        hist = yf.Ticker("ES=F").history(period="2d", interval="5m")
        if hist.empty:
            return None
        # Compare latest bar vs yesterday's close bar
        today_open  = float(hist["Close"].iloc[0])
        current     = float(hist["Close"].iloc[-1])
        return round((current / today_open - 1) * 100, 2)
    except Exception as e:
        print(f"  S&P futures fetch failed: {e}")
    return None


def _build_live_context() -> str:
    """Combine live + CSV data into a factual context block for Claude."""
    lines = []

    # 1. BN live spot vs yesterday's close
    bn_spot = _fetch_live_bn_spot()
    try:
        bn_csv   = pd.read_csv(f"{DATA_DIR}/banknifty.csv", parse_dates=["date"])
        bn_prev  = float(bn_csv["close"].iloc[-1])
        if bn_spot:
            gap_pct = (bn_spot / bn_prev - 1) * 100
            lines.append(
                f"BankNifty NOW: {bn_spot:,.0f}  |  Yesterday close: {bn_prev:,.0f}  "
                f"|  Gap: {gap_pct:+.2f}%"
            )
        else:
            chg = (bn_csv["close"].iloc[-1] / bn_csv["close"].iloc[-2] - 1) * 100
            lines.append(f"BankNifty yesterday: {bn_prev:,.0f} ({chg:+.2f}%)")
    except Exception:
        if bn_spot:
            lines.append(f"BankNifty NOW: {bn_spot:,.0f}")

    # 2. India VIX from CSV (overnight VIX not available pre-market)
    try:
        vix_csv  = pd.read_csv(f"{DATA_DIR}/india_vix.csv", parse_dates=["date"])
        vix_last = float(vix_csv["close"].iloc[-1])
        vix_chg  = vix_last - float(vix_csv["close"].iloc[-2])
        lines.append(f"India VIX: {vix_last:.1f}  ({vix_chg:+.2f} pts yesterday)")
    except Exception:
        pass

    # 3. S&P 500 futures overnight
    spf_chg = _fetch_spf_overnight()
    if spf_chg is not None:
        lines.append(f"S&P 500 futures overnight: {spf_chg:+.2f}%")
    else:
        try:
            spf = pd.read_csv(f"{DATA_DIR}/sp500_futures.csv", parse_dates=["date"])
            spf_chg_csv = (spf["close"].iloc[-1] / spf["close"].iloc[-2] - 1) * 100
            lines.append(f"S&P 500 futures yesterday: {spf_chg_csv:+.2f}%")
        except Exception:
            pass

    return "\n".join(lines) if lines else "Market context unavailable."


# ─── News fetch ──────────────────────────────────────────────────────────────

def _fetch_rss(url: str, max_items: int = 8) -> list[str]:
    """Fetch RSS, return headlines published within MAX_AGE_H only."""
    try:
        req    = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            xml_bytes = resp.read()
        root   = ET.fromstring(xml_bytes)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_H)
        titles, skipped = [], 0
        for item in root.findall(".//item"):
            title   = (item.findtext("title") or "").strip()
            pub_str = item.findtext("pubDate") or ""
            if not title:
                continue
            if pub_str:
                try:
                    if email.utils.parsedate_to_datetime(pub_str) < cutoff:
                        skipped += 1
                        continue
                except Exception:
                    pass
            titles.append(title)
            if len(titles) >= max_items:
                break
        if skipped:
            print(f"    (dropped {skipped} stale)")
        return titles
    except Exception as e:
        print(f"  RSS error ({url[:55]}…): {e}")
        return []


# ─── Claude call ─────────────────────────────────────────────────────────────

def _call_claude(headlines: list[str], live_ctx: str) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"direction": "NEUTRAL", "confidence": "LOW",
                "reason": "ANTHROPIC_API_KEY not set", "error": True}

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    news_block = "\n".join(f"- {h}" for h in headlines) if headlines else "(no fresh banking news)"

    prompt = f"""You are a pre-market analyst for BankNifty options trading.

LIVE MARKET DATA (as of 9:15 AM IST today):
{live_ctx}

BANKING-SPECIFIC NEWS (last 20 hours):
{news_block}

TASK: Decide if there is an EVENT-DRIVEN catalyst that would push BankNifty strongly UP or DOWN today.

RULES:
1. Focus ONLY on: RBI decisions, banking regulations, major bank earnings/results, FII flows, credit events, geopolitical shocks, US Fed policy, India CPI/GDP surprises.
2. IGNORE headlines that simply describe today's price movement (e.g. "Sensex up 500 pts", "markets open flat") — those are already captured by the live price data above.
3. If the live gap data already shows strong direction (e.g. BankNifty gap +0.8%), weight that heavily.
4. If news is generic or no clear banking catalyst exists → return NEUTRAL with LOW confidence.

Reply with ONLY this JSON (one line, no markdown):
{{"direction": "BULLISH|BEARISH|NEUTRAL", "confidence": "HIGH|MEDIUM|LOW", "reason": "one sentence citing specific catalyst or 'no clear catalyst'"}}"""

    try:
        msg  = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=140,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1].strip().lstrip("json").strip()
        result = json.loads(text)
        return {
            "direction":  result.get("direction", "NEUTRAL"),
            "confidence": result.get("confidence", "LOW"),
            "reason":     result.get("reason", ""),
        }
    except Exception as e:
        return {"direction": "NEUTRAL", "confidence": "LOW",
                "reason": f"API error: {e}", "error": True}


# ─── Main ────────────────────────────────────────────────────────────────────

def run() -> dict:
    print(f"[{datetime.now().strftime('%H:%M:%S')} IST] Morning brief starting...")

    # Live prices
    live_ctx = _build_live_context()
    print(f"  Live context:\n" + "\n".join(f"    {l}" for l in live_ctx.splitlines()))

    # Banking news
    headlines = []
    for url in NEWS_FEEDS:
        items = _fetch_rss(url, max_items=8)
        headlines.extend(items)
        print(f"  {len(items)} fresh banking headlines")

    # Deduplicate
    seen, unique = set(), []
    for h in headlines:
        k = h[:60].lower()
        if k not in seen:
            seen.add(k)
            unique.append(h)
    headlines = unique[:MAX_LINES]

    if headlines:
        print(f"  {len(headlines)} unique headlines:")
        for h in headlines:
            print(f"    • {h[:95]}")

    # Claude
    print("  Calling Claude for event-driven sentiment...")
    result = _call_claude(headlines, live_ctx)

    output = {
        "date":        date.today().isoformat(),
        "generated":   datetime.now(timezone.utc).isoformat(),
        "direction":   result["direction"],
        "confidence":  result["confidence"],
        "reason":      result.get("reason", ""),
        "n_headlines": len(headlines),
        "headlines":   headlines[:5],
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    emoji = "📈" if output["direction"] == "BULLISH" else (
            "📉" if output["direction"] == "BEARISH" else "➡️")
    print(f"  {emoji} {output['direction']} ({output['confidence']}) — {output['reason']}")
    print(f"  Written → {OUTPUT_FILE}")
    return output


if __name__ == "__main__":
    run()
