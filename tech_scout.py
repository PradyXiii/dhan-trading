#!/usr/bin/env python3
"""
tech_scout.py — Autonomous tech scouting for NF IC ML system.

Weekly Sunday cron. Scans 3 sources for new ML/trading innovations:
  1. GitHub Search API  — new repos (last 30 days, sorted by stars)
  2. arXiv cs.LG + q-fin — recent papers on tabular ML / options
  3. Hacker News         — Algolia search: algo trading + ML

Claude API scores each find 1–10 for relevance to THIS system.
  - Score >= 7  → queued in data/scout_queue.json (autoloop picks up as experiment hints)
  - Score < 7   → archived in data/scout_discoveries.json as evaluated/discarded
  - Already tried → skipped

Telegram weekly digest: top finds + count discarded.

Cron: Sunday 18:30 UTC (midnight IST Monday).
Manual: python3 tech_scout.py [--dry-run] [--sources github,arxiv,hn]
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

_HERE = Path(__file__).parent
_IST  = timezone(timedelta(hours=5, minutes=30))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN      = os.getenv("GITHUB_TOKEN", "")   # optional — 60 req/hr anon, 5000 with token

SCOUT_DISCOVERIES = _HERE / "data" / "scout_discoveries.json"
SCOUT_QUEUE       = _HERE / "data" / "scout_queue.json"

SCORE_THRESHOLD   = 7    # >= this → queue for autoloop
MAX_CANDIDATES    = 15   # per source

# Our system description sent to Claude for scoring
_SYSTEM_CONTEXT = """
Our system: Nifty50 Iron Condor options auto-trader on NSE India.
- ML task: binary classification (CALL vs PUT day), tabular daily data (~1500 rows)
- 4-model ensemble: RandomForest + XGBoost + LightGBM + CatBoost
- 70 features: technical, macro, options flow, IV skew, OI surface, breadth
- Metric: composite = 0.5×accuracy + 0.25×recall_CALL + 0.25×recall_PUT (target: >0.6484)
- Infrastructure: CPU-only GCP VM, Python 3.11, cron-driven, no GPU
- Strategy: sell weekly spreads, ~84% win rate, ₹1.17Cr over 5 years on backtest
- Pain points: directional accuracy plateaued ~67%, regime changes, concept drift

ONLY interested in: ML reasoning, feature engineering, model architectures, ensembling,
regime detection, drift detection, calibration, feature selection, hyperparameter tuning,
backtest methodology, statistical inference improvements.

HARD REJECT (auto-score 1) — never queue these, regardless of quality:
- Order placement / execution / OMS / smart order routing wrappers
- Broker SDK wrappers, broker adapters, broker UIs (Zerodha, Upstox, Dhan, Angel, IB, etc.)
- Position management, P&L trackers, portfolio dashboards
- Backtest engines (we have our own), trading bots, exchange connectors
- Algo trading platforms (openalgo, freqtrade, vectorbt, backtrader, etc.)
- News scrapers, sentiment dashboards, Telegram/Discord bots
RATIONALE: Our broker layer is locked to Dhan via `docs/DHAN_API_V2_REFERENCE.md` —
the ONLY trusted external Dhan reference is `dhan-oss/DhanHQ-py` (official). All
other broker / order / execution code is forbidden territory regardless of stars.

Reject also: GPU-only, NLP-only, web scraping, infra tools, non-Python,
already standard (pandas/numpy/sklearn).
""".strip()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    from atomic_io import write_atomic_json
    write_atomic_json(str(path), data)


# Reasons that mean "this evaluation failed; retry next run" — don't bury in archive
_RETRYABLE_REASONS = ("error", "chunk error", "no match", "no api key", "api error")


def _is_retryable_failure(item: dict) -> bool:
    """True if a stored item was a failed eval (score=0 + error reason). These should
    not block re-evaluation in future runs."""
    if int(item.get("score", 0) or 0) > 0:
        return False
    reason = str(item.get("reason", "")).lower()
    return any(r in reason for r in _RETRYABLE_REASONS)


def _already_seen(name: str) -> bool:
    """True if name has been EVALUATED CLEANLY (success or genuine reject) before.
    Failed-API attempts are NOT considered seen — they should retry."""
    discoveries = _load_json(SCOUT_DISCOVERIES, [])
    queue       = _load_json(SCOUT_QUEUE, [])
    nm = name.lower()
    for d in discoveries + queue:
        if d.get("name", "").lower() != nm:
            continue
        if _is_retryable_failure(d):
            continue   # failed eval — let it retry
        return True
    return False


def _archive(item: dict):
    """Archive a candidate. Failed evals (api error etc.) are skipped — we want
    them retried on next run."""
    if _is_retryable_failure(item):
        return  # don't pollute discoveries with errors
    discoveries = _load_json(SCOUT_DISCOVERIES, [])
    discoveries.append(item)
    _save_json(SCOUT_DISCOVERIES, discoveries)


def _purge_failed_evals():
    """One-shot cleanup: remove score=0+error entries from discoveries.json so they
    can be re-evaluated next run."""
    discoveries = _load_json(SCOUT_DISCOVERIES, [])
    if not discoveries:
        print("  scout_discoveries.json empty or missing — nothing to purge")
        return
    before = len(discoveries)
    cleaned = [d for d in discoveries if not _is_retryable_failure(d)]
    after = len(cleaned)
    removed = before - after
    if removed > 0:
        _save_json(SCOUT_DISCOVERIES, cleaned)
        print(f"  purged {removed} failed-eval entries (kept {after} clean evaluations)")
    else:
        print(f"  no failed-eval entries to purge ({before} entries all clean)")


def _enqueue(item: dict):
    queue = _load_json(SCOUT_QUEUE, [])
    queue.append(item)
    _save_json(SCOUT_QUEUE, queue)


# ── Source scrapers ───────────────────────────────────────────────────────────

def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def fetch_github(days_back: int = 30) -> list[dict]:
    """GitHub Search: new ML/tabular/trading repos, sorted by stars."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    queries = [
        f"tabular machine learning created:>{cutoff}",
        f"options trading python machine learning created:>{cutoff}",
        f"time series classification tabular created:>{cutoff}",
        f"feature engineering financial time series created:>{cutoff}",
    ]
    seen, results = set(), []
    for q in queries:
        try:
            r = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": q, "sort": "stars", "order": "desc", "per_page": 8},
                headers=_gh_headers(),
                timeout=15,
            )
            if r.status_code == 403:
                print("  GitHub API rate limit hit — skipping remaining GH queries")
                break
            if r.status_code != 200:
                continue
            for repo in r.json().get("items", []):
                name = repo.get("full_name", "")
                if name in seen:
                    continue
                seen.add(name)
                results.append({
                    "source":      "github",
                    "name":        name,
                    "url":         repo.get("html_url", ""),
                    "description": repo.get("description", "") or "",
                    "stars":       repo.get("stargazers_count", 0),
                    "language":    repo.get("language", ""),
                    "topics":      repo.get("topics", []),
                })
            time.sleep(1)  # be polite
        except Exception as e:
            print(f"  GitHub fetch error: {e}")
    return results[:MAX_CANDIDATES]


def fetch_github_topics() -> list[dict]:
    """GitHub Search: repos with ML/trading/time-series TOPICS, updated in last 14 days, high stars."""
    queries = [
        "topic:machine-learning topic:tabular pushed:>2026-01-01 stars:>50",
        "topic:time-series topic:python pushed:>2026-01-01 stars:>30",
        "topic:algorithmic-trading topic:python stars:>20",
        "topic:feature-engineering topic:machine-learning stars:>30",
        "topic:gradient-boosting pushed:>2026-01-01 stars:>20",
    ]
    seen, results = set(), []
    for q in queries:
        try:
            r = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": q, "sort": "updated", "order": "desc", "per_page": 6},
                headers=_gh_headers(),
                timeout=15,
            )
            if r.status_code == 403:
                print("  GitHub API rate limit hit — skipping remaining topic queries")
                break
            if r.status_code != 200:
                continue
            for repo in r.json().get("items", []):
                name = repo.get("full_name", "")
                if name in seen:
                    continue
                seen.add(name)
                results.append({
                    "source":      "github_topics",
                    "name":        name,
                    "url":         repo.get("html_url", ""),
                    "description": repo.get("description", "") or "",
                    "stars":       repo.get("stargazers_count", 0),
                    "language":    repo.get("language", ""),
                    "topics":      repo.get("topics", []),
                })
            time.sleep(1)
        except Exception as e:
            print(f"  GitHub topics fetch error: {e}")
    return results[:MAX_CANDIDATES]


# Keywords that flag a PyPI package as relevant to our system
_PYPI_KEYWORDS = [
    "tabular", "time series", "timeseries", "trading", "options", "financial",
    "gradient boost", "gradient-boost", "ensemble", "feature", "classification",
    "forecasting", "drift", "calibration", "xgboost", "lightgbm",
]


def fetch_pypi_new() -> list[dict]:
    """PyPI new packages RSS — filter for ML/tabular/trading relevance."""
    try:
        r = requests.get("https://pypi.org/rss/packages.xml", timeout=15)
        if r.status_code != 200:
            return []
        items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
        results = []
        seen = set()
        for item in items:
            title_m = re.search(r"<title>(.*?)</title>", item)
            link_m  = re.search(r"<link>(.*?)</link>", item)
            desc_m  = re.search(r"<description>(.*?)</description>", item)
            if not title_m:
                continue
            raw_title = title_m.group(1).strip()
            # Format: "pkg_name added to PyPI"
            name  = raw_title.split(" added to PyPI")[0].strip()
            url   = link_m.group(1).strip() if link_m else f"https://pypi.org/project/{name}/"
            desc  = desc_m.group(1).strip() if desc_m else ""
            if name in seen:
                continue
            # Filter: description must mention at least one relevant keyword
            combined = (name + " " + desc).lower()
            if not any(kw in combined for kw in _PYPI_KEYWORDS):
                continue
            seen.add(name)
            results.append({
                "source":      "pypi",
                "name":        f"PyPI: {name}",
                "url":         url,
                "description": desc[:300],
                "stars":       0,
                "language":    "Python",
                "topics":      [],
            })
        return results[:MAX_CANDIDATES]
    except Exception as e:
        print(f"  PyPI RSS fetch error: {e}")
        return []


# ── Claude evaluation ─────────────────────────────────────────────────────────

_BATCH_SIZE = 8   # tokens-safe; 30 candidates → 4 calls


def _score_one_batch(candidates: list[dict]) -> list[dict]:
    """Single Claude call for up to _BATCH_SIZE candidates. Returns scored copies."""
    batch_text = "\n\n".join(
        f"--- Candidate {i+1} ---\n"
        f"Source: {c['source']}\n"
        f"Name: {c['name']}\n"
        f"URL: {c['url']}\n"
        f"Description: {c['description'][:200]}\n"
        f"Stars/Points: {c['stars']}\n"
        f"Language: {c['language']}"
        for i, c in enumerate(candidates)
    )

    prompt = f"""{_SYSTEM_CONTEXT}

Below are {len(candidates)} tech finds. Score each 1-10 for relevance to our system.

{batch_text}

Return a JSON array (one object per candidate, same order, max one short sentence per field):
[
  {{
    "name": "...",
    "score": 8,
    "reason": "one short sentence",
    "integration_idea": "one short sentence (empty if score < 7)",
    "pip_install": "pip package name (empty if not pip-installable)",
    "risk": "cpu_only_ok | gpu_required | no_python | already_have | broker_or_execution | irrelevant"
  }}
]

JSON only. No commentary. No code fences. No trailing text. Keep every string under 120 chars."""

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":          ANTHROPIC_API_KEY,
            "anthropic-version":  "2023-06-01",
            "content-type":       "application/json",
        },
        json={
            "model":      "claude-sonnet-4-6",
            "max_tokens": 3000,
            "messages":   [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

    raw = r.json()["content"][0]["text"].strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```\w*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)

    # Try strict parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Trim to last "]" that closes the array — Claude sometimes appends junk after
        last = raw.rfind("]")
        if last != -1:
            candidate = raw[:last+1]
            try:
                return json.loads(candidate)
            except Exception:
                pass
        # Last resort: extract whatever objects parsed cleanly so we don't lose them
        salvaged = []
        for m in re.finditer(r"\{[^{}]*\}", raw, re.DOTALL):
            try:
                salvaged.append(json.loads(m.group(0)))
            except Exception:
                continue
        if salvaged:
            return salvaged
        raise


def _evaluate_batch(candidates: list[dict], dry_run: bool) -> list[dict]:
    """Score each candidate 1–10 via Claude API. Chunks into _BATCH_SIZE-sized calls."""
    if not candidates:
        return []
    if not ANTHROPIC_API_KEY:
        print("  ANTHROPIC_API_KEY not set — skipping Claude scoring, tagging all as score=0")
        for c in candidates:
            c.update({"score": 0, "reason": "no api key", "integration_idea": "", "pip_install": "", "risk": ""})
        return candidates

    n_chunks = (len(candidates) + _BATCH_SIZE - 1) // _BATCH_SIZE
    for chunk_i in range(n_chunks):
        chunk = candidates[chunk_i*_BATCH_SIZE : (chunk_i+1)*_BATCH_SIZE]
        print(f"  Scoring chunk {chunk_i+1}/{n_chunks} ({len(chunk)} candidates)...")
        try:
            scored = _score_one_batch(chunk)
            # Merge scores back by name match (more robust than position)
            scored_by_name = {s.get("name", "").lower(): s for s in scored if isinstance(s, dict)}
            for c in chunk:
                key = c["name"].lower()
                # Try exact, then prefix match (Claude sometimes truncates names)
                hit = scored_by_name.get(key)
                if not hit:
                    for sn, sv in scored_by_name.items():
                        if sn and (sn in key or key.startswith(sn) or sn.startswith(key[:30])):
                            hit = sv
                            break
                if hit:
                    c.update({
                        "score":            int(hit.get("score", 0) or 0),
                        "reason":           str(hit.get("reason", ""))[:200],
                        "integration_idea": str(hit.get("integration_idea", ""))[:200],
                        "pip_install":      str(hit.get("pip_install", ""))[:80],
                        "risk":             str(hit.get("risk", ""))[:40],
                    })
                else:
                    c.update({"score": 0, "reason": "no match in claude response", "integration_idea": "", "pip_install": "", "risk": ""})
        except Exception as e:
            print(f"    chunk {chunk_i+1} failed: {e}")
            for c in chunk:
                if "score" not in c:
                    c.update({"score": 0, "reason": f"chunk error: {str(e)[:80]}", "integration_idea": "", "pip_install": "", "risk": ""})
        time.sleep(1)  # be nice to Claude API

    return candidates


# ── Main ──────────────────────────────────────────────────────────────────────

def run_scout(sources: list[str], dry_run: bool):
    import notify

    now_str = datetime.now(_IST).strftime("%Y-%m-%d %H:%M IST")
    print(f"\n=== tech_scout — {now_str} {'[DRY RUN]' if dry_run else ''} ===")

    # ── Fetch candidates ──────────────────────────────────────────────────────
    raw_candidates = []
    if "github" in sources:
        print("  Fetching GitHub (new repos)...")
        raw_candidates += fetch_github()
        n_gh = len([c for c in raw_candidates if c["source"] == "github"])
        print(f"    {n_gh} candidates")

    if "github_topics" in sources or "github" in sources:
        print("  Fetching GitHub (topic-tagged repos)...")
        raw_candidates += fetch_github_topics()
        n_ght = len([c for c in raw_candidates if c["source"] == "github_topics"])
        print(f"    {n_ght} candidates")

    if "pypi" in sources:
        print("  Fetching PyPI new packages...")
        raw_candidates += fetch_pypi_new()
        n_pypi = len([c for c in raw_candidates if c["source"] == "pypi"])
        print(f"    {n_pypi} candidates")

    # ── Filter already seen ───────────────────────────────────────────────────
    fresh = [c for c in raw_candidates if not _already_seen(c["name"])]
    print(f"  {len(fresh)} fresh (never evaluated before) of {len(raw_candidates)} total")

    if not fresh:
        print("  Nothing new this week. Exiting.")
        msg = "🔭 <b>Tech Scout</b> — nothing new this week. All candidates already evaluated."
        if not dry_run:
            notify.send(msg)
        else:
            print(f"  [dry-run] Would send: {msg}")
        return

    # ── Score via Claude ──────────────────────────────────────────────────────
    print(f"  Scoring {len(fresh)} candidates via Claude API...")
    if not dry_run:
        scored = _evaluate_batch(fresh, dry_run=False)
    else:
        print("  [dry-run] Skipping Claude API — assigning score=5 to all")
        for c in fresh:
            c.update({"score": 5, "reason": "dry-run", "integration_idea": "", "pip_install": "", "risk": "dry_run"})
        scored = fresh

    # ── Route: queue or archive ───────────────────────────────────────────────
    queued, discarded = [], []
    now_iso = datetime.now(_IST).isoformat()

    for c in scored:
        c["evaluated_at"] = now_iso
        c["tried"]        = False
        score = c.get("score", 0)
        if score >= SCORE_THRESHOLD and c.get("integration_idea"):
            queued.append(c)
            if not dry_run:
                _enqueue(c)
            print(f"  ✅ QUEUED  [{score}/10] {c['name'][:60]}")
            print(f"            → {c['integration_idea'][:80]}")
        else:
            discarded.append(c)
            if not dry_run:
                _archive(c)
            print(f"  ✗ discard [{score}/10] {c['name'][:50]} — {c.get('reason','')[:60]}")

    # ── Telegram digest ───────────────────────────────────────────────────────
    if queued:
        lines = []
        for c in queued[:5]:
            lines.append(
                f"  [{c['score']}/10] <b>{c['name'][:55]}</b>\n"
                f"  {c['reason'][:80]}\n"
                f"  💡 {c['integration_idea'][:90]}\n"
                f"  📦 {c.get('pip_install','?')}  |  🔗 {c['source']}"
            )
        top_str = "\n\n".join(lines)
        msg = (
            f"🔭 <b>Tech Scout — {datetime.now(_IST).strftime('%d %b %Y')}</b>\n\n"
            f"Found <b>{len(queued)}</b> promising finds (queued for autoloop experiments):\n\n"
            f"{top_str}\n\n"
            f"({len(discarded)} evaluated and discarded this week)"
        )
    else:
        msg = (
            f"🔭 <b>Tech Scout — {datetime.now(_IST).strftime('%d %b %Y')}</b>\n\n"
            f"Nothing scored ≥{SCORE_THRESHOLD}/10 this week.\n"
            f"{len(discarded)} candidates evaluated and discarded.\n"
            f"System already well-equipped for current problem."
        )

    print(f"\n  Queued: {len(queued)}  |  Discarded: {len(discarded)}")
    if not dry_run:
        notify.send(msg)
    else:
        print(f"\n[dry-run] Would send:\n{msg}")


def main():
    parser = argparse.ArgumentParser(description="Autonomous tech scout for NF IC ML system")
    parser.add_argument("--dry-run", action="store_true", help="Fetch + score but don't write or Telegram")
    parser.add_argument("--sources", default="github,pypi",
                        help="Comma-separated sources (default: github,pypi). "
                             "Available: github, github_topics, pypi")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Purge prior failed-API entries from discoveries.json, then run normally")
    args = parser.parse_args()
    if args.retry_failed:
        print("=== Purging failed evals from scout_discoveries.json ===")
        _purge_failed_evals()
    sources = [s.strip() for s in args.sources.split(",")]
    run_scout(sources=sources, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
