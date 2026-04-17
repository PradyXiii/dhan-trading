#!/usr/bin/env python3
# DHAN API: always read docs/DHAN_API_V2_REFERENCE.md before any API work.
"""
morning_brief.py — Pre-market news sentiment via Claude API
===========================================================
Runs at 9:15 AM IST (15 min before auto_trader).

1. Fetches top headlines from Google News RSS for BankNifty / Indian markets
2. Adds macro context from yesterday's CSVs (BN close, VIX, S&P futures)
3. Calls Claude API for sentiment: BULLISH / BEARISH / NEUTRAL
4. Writes data/news_sentiment.json — auto_trader.py reads this as extra vote

Cron (9:15 AM IST = 3:45 AM UTC, Mon–Fri):
  45 3 * * 1-5 cd ~/dhan-trading && python3 morning_brief.py >> logs/morning_brief.log 2>&1
"""

import os
import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone, timedelta

import pandas as pd
from dotenv import load_dotenv
load_dotenv()

DATA_DIR      = "data"
OUTPUT_FILE   = f"{DATA_DIR}/news_sentiment.json"
STALE_HOURS   = 6          # ignore JSON older than this at trade time
MAX_HEADLINES = 12

# ─────────────────────────────────────────────────────────────────────────────

NEWS_FEEDS = [
    # Google News RSS — BankNifty + Indian banking
    "https://news.google.com/rss/search?q=BankNifty+OR+%22Bank+Nifty%22+India&hl=en-IN&gl=IN&ceid=IN:en",
    # Google News — Nifty / Sensex broader market
    "https://news.google.com/rss/search?q=Nifty+OR+Sensex+market+India&hl=en-IN&gl=IN&ceid=IN:en",
]


def _fetch_rss(url: str, max_items: int = 8) -> list[str]:
    """Fetch RSS feed, return list of headline strings."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_bytes = resp.read()
        root  = ET.fromstring(xml_bytes)
        items = root.findall(".//item")
        titles = []
        for item in items[:max_items]:
            title = item.findtext("title") or ""
            title = title.strip()
            if title:
                titles.append(title)
        return titles
    except Exception as e:
        print(f"  RSS fetch failed ({url[:60]}...): {e}")
        return []


def _macro_context() -> str:
    """Build a short macro summary from yesterday's CSV data."""
    lines = []
    try:
        bn  = pd.read_csv(f"{DATA_DIR}/banknifty.csv",  parse_dates=["date"])
        nf  = pd.read_csv(f"{DATA_DIR}/nifty50.csv",    parse_dates=["date"])
        vix = pd.read_csv(f"{DATA_DIR}/india_vix.csv",  parse_dates=["date"])
        spf = pd.read_csv(f"{DATA_DIR}/sp500_futures.csv", parse_dates=["date"])

        bn_close  = float(bn["close"].iloc[-1])
        bn_chg    = (bn["close"].iloc[-1] / bn["close"].iloc[-2] - 1) * 100
        nf_chg    = (nf["close"].iloc[-1] / nf["close"].iloc[-2] - 1) * 100
        vix_close = float(vix["close"].iloc[-1])
        vix_chg   = float(vix["close"].iloc[-1]) - float(vix["close"].iloc[-2])
        spf_chg   = (spf["close"].iloc[-1] / spf["close"].iloc[-2] - 1) * 100

        lines.append(f"BankNifty yesterday: {bn_close:,.0f} ({bn_chg:+.2f}%)")
        lines.append(f"Nifty50 yesterday: {nf_chg:+.2f}%")
        lines.append(f"India VIX: {vix_close:.1f} (change: {vix_chg:+.2f} pts)")
        lines.append(f"S&P 500 futures yesterday: {spf_chg:+.2f}%")
    except Exception as e:
        lines.append(f"[macro context unavailable: {e}]")
    return "\n".join(lines)


def _call_claude(headlines: list[str], macro: str) -> dict:
    """Call Anthropic API. Returns {direction, confidence, reason}."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"direction": "NEUTRAL", "confidence": "LOW",
                "reason": "ANTHROPIC_API_KEY not set", "error": True}

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    prompt = (
        "You are analyzing pre-market conditions for BankNifty (Indian Banking Index).\n\n"
        f"MARKET CONTEXT (yesterday's data):\n{macro}\n\n"
        f"TODAY'S HEADLINES (as of 9:15 AM IST):\n"
        + "\n".join(f"- {h}" for h in headlines) +
        "\n\nBased on these headlines and market context, predict whether BankNifty "
        "will likely go UP or DOWN significantly enough for an option trade to hit "
        "its take-profit (roughly 37% premium gain) before stop-loss (15% loss).\n\n"
        "Reply with ONLY a JSON object on one line:\n"
        "{\"direction\": \"BULLISH|BEARISH|NEUTRAL\", "
        "\"confidence\": \"HIGH|MEDIUM|LOW\", "
        "\"reason\": \"one sentence max\"}"
    )

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",   # fast + cheap for this simple task
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # Parse JSON — handle if Claude wraps in markdown
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


def run() -> dict:
    print(f"[{datetime.now().strftime('%H:%M:%S')} IST] Morning brief starting...")

    # 1. Fetch headlines from all RSS feeds
    headlines = []
    for feed_url in NEWS_FEEDS:
        items = _fetch_rss(feed_url, max_items=8)
        headlines.extend(items)
        print(f"  {len(items)} headlines from feed")

    # Deduplicate and cap
    seen = set()
    unique = []
    for h in headlines:
        key = h[:60].lower()
        if key not in seen:
            seen.add(key)
            unique.append(h)
    headlines = unique[:MAX_HEADLINES]

    if not headlines:
        print("  No headlines fetched — writing NEUTRAL")
        result = {"direction": "NEUTRAL", "confidence": "LOW",
                  "reason": "no headlines available"}
    else:
        print(f"  {len(headlines)} unique headlines gathered")
        for h in headlines:
            print(f"    • {h[:90]}")

        # 2. Macro context
        macro = _macro_context()

        # 3. Claude API call
        print("  Calling Claude for sentiment...")
        result = _call_claude(headlines, macro)

    # 4. Write output
    output = {
        "date":       date.today().isoformat(),
        "generated":  datetime.now(timezone.utc).isoformat(),
        "direction":  result["direction"],    # BULLISH / BEARISH / NEUTRAL
        "confidence": result["confidence"],   # HIGH / MEDIUM / LOW
        "reason":     result.get("reason", ""),
        "n_headlines": len(headlines),
        "headlines":  headlines[:5],           # store first 5 for reference
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    dir_emoji = "📈" if output["direction"] == "BULLISH" else (
                "📉" if output["direction"] == "BEARISH" else "➡️")
    print(f"  {dir_emoji} {output['direction']} ({output['confidence']}) — {output['reason']}")
    print(f"  Written → {OUTPUT_FILE}")
    return output


if __name__ == "__main__":
    run()
