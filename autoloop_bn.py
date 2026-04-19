#!/usr/bin/env python3
"""
autoloop_bn.py — Daily midnight autoresearch loop for BankNifty ML system.

Paper-trading mode for ml_engine.py changes:
  - ml_engine.py proposals go to ml_engine_paper.py (not live)
  - Paper model beats live by ≥1.5% for 3 consecutive nights → auto-promote
  - signal_engine.py / auto_trader.py changes apply immediately

Usage:
    python3 autoloop_bn.py                   # 5 experiments, full run
    python3 autoloop_bn.py --experiments 3   # quick 3-experiment run
    python3 autoloop_bn.py --dry-run         # baseline only, no Claude API calls
    python3 autoloop_bn.py --no-evolver      # skip model_evolver.py at end

Cron (Mon–Fri midnight IST = 18:30 UTC):
    30 18 * * 1-5  cd /path && python3 autoloop_bn.py >> logs/autoloop_bn.log 2>&1
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

# ── Constants ─────────────────────────────────────────────────────────────────
N_EXPERIMENTS    = 5
MODEL            = "claude-opus-4-6"
MAX_TOKENS       = 2048
PNL_GUARD        = 0.90
PAPER_ADVANTAGE  = 0.015    # paper must beat live by ≥1.5% (combined score)
PAPER_WIN_STREAK = 3        # consecutive nights needed
PAPER_MIN_DAYS   = 3        # minimum days before eligible
LIVE_WEIGHT      = 0.6      # live-trade accuracy weight in combined promotion score
HOLDOUT_WEIGHT   = 0.4      # holdout composite weight in combined promotion score
MIN_LIVE_FOR_MIX = 3        # need ≥N labeled live trades before mixing in live accuracy

_IST             = timezone(timedelta(hours=5, minutes=30))
_HERE            = Path(__file__).parent.resolve()

_PAPER_FILES     = {"ml_engine.py"}
_IMMEDIATE_FILES = {"signal_engine.py", "auto_trader.py"}
_BACKTEST_FILES  = {"auto_trader.py"}
_ALLOWED_FILES   = _PAPER_FILES | _IMMEDIATE_FILES

_PAPER_FILE      = _HERE / "ml_engine_paper.py"
_PAPER_PERF_CSV  = _HERE / "data" / "paper_performance.csv"
_PAPER_CHANGES   = _HERE / "data" / "paper_changes.json"
_EXP_HISTORY     = _HERE / "data" / "experiment_history.json"  # never reset


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _send(message: str) -> bool:
    try:
        sys.path.insert(0, str(_HERE))
        import notify
        return notify.send(message)
    except Exception as e:
        print(f"[Telegram error] {e}")
        return False


# ── Git helpers ───────────────────────────────────────────────────────────────

def _git(*args: str) -> tuple[int, str]:
    # GIT_TERMINAL_PROMPT=0 makes git fail fast instead of blocking on a
    # credential prompt — critical for autoloop which runs unattended and
    # would otherwise hang on `git push` when no PAT/SSH key is configured.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(
        ["git", *args],
        cwd=str(_HERE),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _current_branch() -> str:
    rc, out = _git("rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if rc == 0 else "main"


def _revert_files(files: list[str]) -> None:
    _git("checkout", "--", *files)


def _commit(message: str, files: list[str]) -> bool:
    rc, out = _git("add", *files)
    if rc != 0:
        print(f"  git add failed: {out}")
        return False
    rc, out = _git("commit", "-m", message)
    if rc != 0:
        print(f"  git commit failed: {out}")
        return False
    return True


# ── Paper tracking helpers ────────────────────────────────────────────────────

def _ensure_paper_file(commit: bool = True) -> None:
    """Sync ml_engine_paper.py with ml_engine.py.

    Behavior:
      1. Paper file missing → copy from live.
      2. Paper file exists, no accumulated changes (paper_changes.json empty) → re-sync from live
         (handles the case where ml_engine.py was updated upstream while paper file is stale).
      3. Paper file exists, has accumulated changes → leave alone (preserves autoresearch work).
    """
    live_file = _HERE / "ml_engine.py"

    has_accumulated_changes = False
    if _PAPER_CHANGES.exists():
        try:
            changes = json.loads(_PAPER_CHANGES.read_text())
            has_accumulated_changes = bool(changes)
        except Exception:
            has_accumulated_changes = False

    if not _PAPER_FILE.exists():
        print("[Paper] ml_engine_paper.py not found — creating from live...")
        shutil.copy(live_file, _PAPER_FILE)
        action = "created"
    elif not has_accumulated_changes:
        live_content = live_file.read_text(encoding="utf-8")
        paper_content = _PAPER_FILE.read_text(encoding="utf-8")
        if live_content == paper_content:
            return
        print("[Paper] ml_engine_paper.py out of sync with live — re-syncing...")
        shutil.copy(live_file, _PAPER_FILE)
        action = "re-synced from live"
    else:
        return

    if commit:
        committed = _commit(f"autoloop: {action} ml_engine_paper.py", ["ml_engine_paper.py"])
        if committed:
            print(f"[Paper] ml_engine_paper.py {action} and committed.")
        else:
            print(f"[Paper] ml_engine_paper.py {action} (commit failed or no changes).")
    else:
        print(f"[Paper] ml_engine_paper.py {action} (dry-run — not committed).")


def _log_paper_performance(date_str: str, live_score: float, paper_score: float,
                           live_eval: dict | None = None) -> None:
    """Append one row to paper_performance.csv.
    live_eval: dict from _score_paper_on_live_trades() with paper_acc, live_acc, n_trades.
    combined_advantage = 60% live accuracy lead + 40% holdout lead (when ≥3 live trades).
    """
    _PAPER_PERF_CSV.parent.mkdir(exist_ok=True)
    holdout_adv = paper_score - live_score
    n_live = live_eval.get("n_trades", 0) if live_eval else 0

    if live_eval and n_live >= MIN_LIVE_FOR_MIX:
        live_adv = live_eval["paper_acc"] - live_eval["live_acc"]
        combined_adv = LIVE_WEIGHT * live_adv + HOLDOUT_WEIGHT * holdout_adv
    else:
        combined_adv = holdout_adv   # not enough live trades yet — holdout only

    header_needed = not _PAPER_PERF_CSV.exists()
    with open(_PAPER_PERF_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if header_needed:
            w.writerow(["date", "live_score", "paper_score", "holdout_advantage",
                        "live_paper_acc", "live_live_acc", "n_live_trades", "combined_advantage"])
        w.writerow([
            date_str,
            f"{live_score:.4f}",
            f"{paper_score:.4f}",
            f"{holdout_adv:.4f}",
            f"{live_eval.get('paper_acc', 0.0):.4f}" if live_eval else "0.0000",
            f"{live_eval.get('live_acc',  0.0):.4f}" if live_eval else "0.0000",
            n_live,
            f"{combined_adv:.4f}",
        ])


def _check_paper_promotion() -> tuple[bool, int, float]:
    """Check if paper model should be promoted to live.
    Uses combined_advantage when available (60% live trades + 40% holdout),
    falls back to holdout_advantage for older rows.
    """
    if not _PAPER_PERF_CSV.exists():
        return False, 0, 0.0
    try:
        with open(_PAPER_PERF_CSV, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return False, 0, 0.0

    if len(rows) < PAPER_MIN_DAYS:
        return False, 0, 0.0

    streak = 0
    latest_adv = 0.0
    for row in reversed(rows):
        # combined_advantage when available; fall back to holdout_advantage or legacy advantage
        adv_str = (row.get("combined_advantage") or
                   row.get("holdout_advantage") or
                   row.get("advantage", "0"))
        try:
            adv = float(adv_str)
        except (ValueError, TypeError):
            break
        if adv >= PAPER_ADVANTAGE:
            streak += 1
            if streak == 1:
                latest_adv = adv
        else:
            break

    return streak >= PAPER_WIN_STREAK, streak, latest_adv


def _score_paper_on_live_trades() -> dict:
    """
    Score the paper model on actual live trade dates vs real market outcomes.

    live_trades.csv records oracle_correct (True/False) + signal (CALL/PUT).
    We infer the true market direction from those two columns, then ask:
    "What would the paper model have predicted on that date?"
    Returns {'paper_acc': float, 'live_acc': float, 'n_trades': int}.
    """
    live_csv = _HERE / "data" / "live_trades.csv"
    empty = {"paper_acc": 0.0, "live_acc": 0.0, "n_trades": 0}

    if not live_csv.exists():
        return empty

    import csv as _csv
    try:
        with open(live_csv) as f:
            rows = list(_csv.DictReader(f))
    except Exception:
        return empty

    # Only rows with a definitive outcome
    labeled = [r for r in rows
               if str(r.get("oracle_correct", "")).lower() in ("true", "false")]
    if not labeled:
        return empty

    # Live model accuracy = fraction oracle was correct on actual trades
    live_correct = sum(1 for r in labeled if str(r["oracle_correct"]).lower() == "true")
    live_acc = live_correct / len(labeled)

    # Score the paper model on those same dates
    try:
        import importlib
        import pandas as pd
        from sklearn.ensemble import RandomForestClassifier

        sys.path.insert(0, str(_HERE))
        mle_paper = importlib.import_module("ml_engine_paper")

        df_all    = mle_paper.compute_features(mle_paper.load_all_data())
        df_labels = mle_paper.compute_labels(df_all)
        df        = df_all.merge(df_labels[["date", "label"]], on="date", how="inner")
        df["date"] = pd.to_datetime(df["date"])

        feat_cols = mle_paper.FEATURE_COLS
        missing   = [c for c in feat_cols if c not in df.columns]
        if missing:
            print(f"  [live-eval] paper model missing columns: {missing}")
            return {"paper_acc": 0.0, "live_acc": live_acc, "n_trades": len(labeled)}

        df_clean = df.dropna(subset=feat_cols + ["label"])

        # Build (date, true_market_direction) pairs
        trade_dates, true_dirs = [], []
        for r in labeled:
            dt  = pd.Timestamp(r["date"])
            sig = r["signal"].upper()
            correct = str(r["oracle_correct"]).lower() == "true"
            # True direction: CALL if (CALL+correct) or (PUT+wrong)
            true_dir = "CALL" if (sig == "CALL") == correct else "PUT"
            trade_dates.append(dt)
            true_dirs.append(true_dir)

        # Train paper model on all data strictly before the earliest live trade
        earliest  = min(trade_dates)
        train_df  = df_clean[df_clean["date"] < earliest]
        if len(train_df) < 100:
            print(f"  [live-eval] only {len(train_df)} training rows before first live trade — skipping")
            return {"paper_acc": 0.0, "live_acc": live_acc, "n_trades": len(labeled)}

        rf = RandomForestClassifier(
            n_estimators=200, max_depth=8, min_samples_leaf=3,
            max_features="sqrt", class_weight="balanced",
            random_state=42, n_jobs=-1,
        )
        rf.fit(train_df[feat_cols].values,
               (train_df["label"] == "CALL").astype(int).values)

        paper_correct, n_scored = 0, 0
        for dt, true_dir in zip(trade_dates, true_dirs):
            match = df_clean[df_clean["date"] == dt]
            if match.empty:
                continue
            pred_label = rf.predict(match[feat_cols].values[0:1])[0]
            pred_dir   = "CALL" if pred_label == 1 else "PUT"
            if pred_dir == true_dir:
                paper_correct += 1
            n_scored += 1

        if n_scored == 0:
            return {"paper_acc": 0.0, "live_acc": live_acc, "n_trades": len(labeled)}

        paper_acc = paper_correct / n_scored
        print(f"  [live-eval] paper {paper_acc:.2%} vs live {live_acc:.2%} on {n_scored} real trades")
        return {"paper_acc": paper_acc, "live_acc": live_acc, "n_trades": len(labeled)}

    except Exception as e:
        print(f"  [live-eval] Error scoring paper on live trades: {e}")
        return {"paper_acc": 0.0, "live_acc": live_acc, "n_trades": len(labeled)}


def _load_paper_changes() -> list[dict]:
    """Load accumulated paper changes from JSON."""
    if not _PAPER_CHANGES.exists():
        return []
    try:
        return json.loads(_PAPER_CHANGES.read_text())
    except Exception:
        return []


def _log_paper_change(description: str, plain_english: str, score_before: float, score_after: float) -> None:
    """Append a change entry to paper_changes.json."""
    _PAPER_CHANGES.parent.mkdir(exist_ok=True)
    changes = _load_paper_changes()
    changes.append({
        "date": datetime.now(_IST).strftime("%Y-%m-%d"),
        "description": description,
        "plain_english": plain_english,
        "score_before": round(score_before, 4),
        "score_after": round(score_after, 4),
    })
    _PAPER_CHANGES.write_text(json.dumps(changes, indent=2))


def _clear_paper_changes() -> None:
    """Reset paper changes and performance streak after promotion."""
    _PAPER_CHANGES.write_text("[]")
    # Also clear the performance streak so the system doesn't re-promote
    # on the next run (files are identical post-promotion, nothing to commit).
    if _PAPER_PERF_CSV.exists():
        _PAPER_PERF_CSV.unlink()


def _record_experiment(description: str, kept: bool, score_before: float, score_after: float) -> None:
    """Append every experiment (kept or discarded) to the lifetime history. Never reset."""
    _EXP_HISTORY.parent.mkdir(exist_ok=True)
    history = json.loads(_EXP_HISTORY.read_text()) if _EXP_HISTORY.exists() else []
    history.append({
        "date": datetime.now(_IST).strftime("%Y-%m-%d"),
        "description": description,
        "kept": kept,
        "before": round(score_before, 4),
        "after": round(score_after, 4),
    })
    _EXP_HISTORY.write_text(json.dumps(history, indent=2))


def _load_experiment_history(n: int = 40) -> str:
    """Return last n experiments as a prompt-ready string."""
    if not _EXP_HISTORY.exists():
        return ""
    history = json.loads(_EXP_HISTORY.read_text())
    if not history:
        return ""
    recent = history[-n:]
    lines = [
        f"  {e['date']}  {'✅ KEPT' if e['kept'] else '❌ DISCARD'}  "
        f"{e['before']:.4f}→{e['after']:.4f}  {e['description'][:120]}"
        for e in recent
    ]
    return (
        f"\n\n### Lifetime experiment history (last {len(recent)} — DO NOT repeat these):\n"
        + "\n".join(lines)
    )


def _promote_paper_to_live(streak: int, avg_advantage: float) -> None:
    """Copy ml_engine_paper.py → ml_engine.py, commit, send Telegram."""
    changes = _load_paper_changes()
    print(f"[Promote] Paper model outperformed for {streak} nights (+{avg_advantage:.2%} avg). Promoting...")

    # Copy paper → live
    shutil.copy(_PAPER_FILE, _HERE / "ml_engine.py")

    # Commit both files
    commit_msg = f"autoloop PROMOTE: paper→live after {streak} nights of +{avg_advantage:.1%} advantage"
    committed = _commit(commit_msg, ["ml_engine.py"])
    if not committed:
        print("[Promote] Warning: git commit failed.")

    # Push to remote so GitHub stays in sync
    branch = _current_branch()
    rc, out = _git("push", "origin", branch)
    if rc == 0:
        print(f"[Promote] Pushed to origin/{branch}.")
    else:
        print(f"[Promote] Warning: git push failed: {out}")

    # Build Telegram message
    if changes:
        change_lines = "\n".join(f"  {j+1}. {c['plain_english']}" for j, c in enumerate(changes))
        changes_block = f"\n\n<b>What changed (accumulated over {len(changes)} nights):</b>\n{change_lines}"
    else:
        changes_block = ""

    _send(
        f"🚀 <b>PAPER MODEL GOES LIVE!</b>\n\n"
        f"The paper (test) model outperformed the live model for <b>{streak} nights in a row</b> "
        f"by an average of <b>{avg_advantage:.1%}</b>.\n\n"
        f"It has been automatically promoted to live."
        + changes_block +
        f"\n\n✅ Live model updated. Used for tomorrow's trade."
    )

    _clear_paper_changes()
    print("[Promote] Paper changes log reset.")


# ── Claude API cost helper ────────────────────────────────────────────────────

_COST_LOG_DIR = _HERE / "data"

# Claude claude-opus-4-6 pricing (per 1M tokens, USD)
_PRICE_IN      = 5.00
_PRICE_OUT     = 25.00
_PRICE_CACHE_W = 6.25   # cache write
_PRICE_CACHE_R = 0.50   # cache read


def _record_api_usage(usage) -> None:
    """Append token counts from one API call to today's usage file."""
    try:
        _COST_LOG_DIR.mkdir(exist_ok=True)
        today = datetime.now(_IST).date().isoformat()
        path = _COST_LOG_DIR / f"claude_usage_{today}.json"
        data = {}
        if path.exists():
            with open(path) as f:
                data = json.load(f)
        for key in ("input_tokens", "output_tokens",
                    "cache_creation_input_tokens", "cache_read_input_tokens"):
            data[key] = data.get(key, 0) + getattr(usage, key, 0)
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _get_claude_cost_yesterday() -> str:
    """Return yesterday's Claude API spend from local token log."""
    try:
        today_ist = datetime.now(_IST).date()
        yesterday = (today_ist - timedelta(days=1)).isoformat()
        path = _COST_LOG_DIR / f"claude_usage_{yesterday}.json"
        if not path.exists():
            return "N/A (no log yet)"
        with open(path) as f:
            d = json.load(f)
        cost = (
            d.get("input_tokens", 0)                    / 1_000_000 * _PRICE_IN
            + d.get("output_tokens", 0)                 / 1_000_000 * _PRICE_OUT
            + d.get("cache_creation_input_tokens", 0)   / 1_000_000 * _PRICE_CACHE_W
            + d.get("cache_read_input_tokens", 0)       / 1_000_000 * _PRICE_CACHE_R
        )
        total_tok = sum(d.get(k, 0) for k in (
            "input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens"))
        return f"${cost:.3f}  ({total_tok:,} tokens)"
    except Exception:
        return "N/A"


# ── Experiment runners ────────────────────────────────────────────────────────

def _run_subprocess_with_args(script: str, extra_args: list[str] = None, timeout: int = 300) -> dict:
    """Run a script with optional args and return the last JSON line from stdout."""
    cmd = [sys.executable, script] + (extra_args or [])
    try:
        result = subprocess.run(cmd, cwd=str(_HERE), capture_output=True, text=True, timeout=timeout)
        stdout = result.stdout.strip()
        if not stdout:
            return {"composite": 0.0, "error": f"empty stdout"}
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        return {"composite": 0.0, "error": f"no JSON in stdout"}
    except subprocess.TimeoutExpired:
        return {"composite": 0.0, "error": f"timeout after {timeout}s"}
    except json.JSONDecodeError as e:
        return {"composite": 0.0, "error": f"JSON parse error: {e}"}
    except Exception as e:
        return {"composite": 0.0, "error": str(e)}


def _run_ml_experiment(module: str = "ml_engine") -> dict:
    return _run_subprocess_with_args("autoexperiment_bn.py", ["--module", module], timeout=300)


def _run_backtest_experiment() -> dict:
    return _run_subprocess_with_args("autoexperiment_backtest.py", timeout=600)


# ── File content helpers ──────────────────────────────────────────────────────

def _reversal_summary() -> str:
    """Summarise recent intraday reversals from midday_checkpoints.csv."""
    import pandas as pd
    from collections import Counter

    path = _HERE / "data" / "midday_checkpoints.csv"
    if not path.exists():
        return ""
    try:
        df = pd.read_csv(path, parse_dates=["date"])
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=30)
        df = df[df["date"] >= cutoff]
        revs = df[df["reversal_detected"].astype(str).str.lower() == "true"]
        if revs.empty:
            return ""
        codes = Counter()
        for rc in revs["reason_codes"].dropna():
            for c in str(rc).split("|"):
                c = c.strip()
                if c:
                    codes[c] += 1
        lines = [f"  {code}: {n}×" for code, n in codes.most_common(5)]
        return (
            f"\n\n### Recent intraday reversals (last 30 days)\n"
            f"{len(revs)} reversal(s) out of {len(df)} checks.\n"
            f"Top drivers:\n" + "\n".join(lines) + "\n"
            "Prefer features that guard against these patterns."
        )
    except Exception:
        return ""


def _read_file(filename: str) -> str:
    path = _HERE / filename
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _extract_section(content: str, section: str, lines_after: int = 80) -> str:
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if section in line:
            end = min(i + lines_after, len(lines))
            return "\n".join(lines[i:end])
    return content[:3000]


def _build_column_inventory() -> str:
    """Run load_all_data + compute_features and return a plain inventory of
    column names Claude can safely reference. Anything NOT in this list must
    be computed explicitly inside compute_features() before being referenced.

    This stops Claude from hallucinating column names like `iv_proxy` or
    `adx14` and wasting an experiment on a pre-flight failure.
    """
    try:
        import importlib, sys as _sys
        _MOD = "_ml_inventory_"
        if _MOD in _sys.modules:
            del _sys.modules[_MOD]
        import importlib.util
        src = _PAPER_FILE if _PAPER_FILE.exists() else (_HERE / "ml_engine.py")
        spec = importlib.util.spec_from_file_location(_MOD, src)
        mod  = importlib.util.module_from_spec(spec)
        _sys.modules[_MOD] = mod
        spec.loader.exec_module(mod)

        raw  = mod.load_all_data()
        feat = mod.compute_features(raw)
        raw_cols  = sorted(c for c in raw.columns if c != "date")
        feat_cols = sorted(c for c in feat.columns
                           if c != "date" and c not in raw.columns)
        del _sys.modules[_MOD]

        return (
            "Columns available in the raw dataframe `d` at the START of "
            "compute_features (from load_all_data):\n"
            f"  {', '.join(raw_cols)}\n\n"
            "Columns added by the current compute_features() (available to "
            "reference AFTER each is assigned):\n"
            f"  {', '.join(feat_cols)}\n\n"
            "⚠️ Any column name NOT in either list above does NOT exist and "
            "MUST be computed inside compute_features() BEFORE you reference "
            "it in an interaction or in FEATURE_COLS. Hallucinating column "
            "names (e.g. `iv_proxy`, `adx14` that aren't already here) will "
            "crash the pre-flight check and waste the experiment."
        )
    except Exception as e:
        return f"(column inventory unavailable: {type(e).__name__}: {e})"


def _build_context_snippet(use_paper: bool = False) -> str:
    """Build code snippets for Claude. use_paper=True shows paper model state."""
    ml_source = "ml_engine_paper.py" if (use_paper and _PAPER_FILE.exists()) else "ml_engine.py"
    ml = _read_file(ml_source)
    sig = _read_file("signal_engine.py")
    at = _read_file("auto_trader.py")

    paper_note = (
        f"### Note: Showing PAPER model ({ml_source})\n"
        "Propose 'file': 'ml_engine.py' — changes go to paper model.\n\n"
        if use_paper
        else ""
    )

    feat_section = _extract_section(ml, "FEATURE_COLS", lines_after=40)
    compute_section = _extract_section(ml, "def compute_features", lines_after=70)
    score_section = _extract_section(sig, "def score_row", lines_after=60)
    at_section = _extract_section(at, "LOT_SIZE", lines_after=14)

    return (
        paper_note
        + "### ml_engine.py — FEATURE_COLS\n```python\n"
        + feat_section
        + "\n```\n\n### ml_engine.py — compute_features()\n```python\n"
        + compute_section
        + "\n```\n\n### signal_engine.py — score_row()\n```python\n"
        + score_section
        + "\n```\n\n### auto_trader.py — trading constants\n```python\n"
        + at_section
        + "\n```"
    )


# ── Claude API ────────────────────────────────────────────────────────────────

def _make_client():
    try:
        import anthropic
    except ImportError:
        print("ERROR: 'anthropic' package not installed.")
        sys.exit(1)
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)
    return anthropic.Anthropic(api_key=api_key)


def _build_system_prompt() -> str:
    path = _HERE / "research_program_bn.md"
    try:
        brief = path.read_text(encoding="utf-8")
    except Exception:
        brief = "(research_program_bn.md not found)"

    return (
        "You are an expert ML feature engineer specialising in Indian equity derivatives.\n\n"
        "Your job: propose ONE targeted code change to improve the BankNifty ML model composite score.\n\n"
        "IMPORTANT: ml_engine.py changes go into a paper (test) model first. "
        "After 3 nights of outperforming the live model by ≥1.5%, they automatically go live. "
        "So be bold — paper experiments have no risk.\n\n"
        "CRITICAL RULES:\n"
        "0. CODE PLACEMENT — ALL new feature code MUST be inserted at the AUTOLOOP APPEND ZONE.\n"
        "   The APPEND ZONE is identified by this exact comment line near the end of compute_features():\n"
        "       # ── AUTOLOOP APPEND ZONE — add new features HERE, just above this line ──────\n"
        "   When editing ml_engine_paper.py for a new feature, your old_code MUST be exactly:\n"
        "       # ── AUTOLOOP APPEND ZONE — add new features HERE, just above this line ──────\n"
        "   And your new_code must be:  <your feature code>\\n    # ── AUTOLOOP APPEND ZONE ...\n"
        "   (i.e. prepend your code to that comment, keeping the comment intact as the anchor).\n"
        "   NEVER insert code anywhere else in compute_features() — not in 'Interaction features',\n"
        "   not in the 'PCR momentum' block, not anywhere before the APPEND ZONE comment.\n"
        "   The append zone is safe because ALL existing features are already computed above it.\n"
        "1. Adding a new ML feature requires TWO edits: one to compute_features() and one to FEATURE_COLS.\n"
        "   In BN ml_engine.py, compute_features() is at line ~228 and FEATURE_COLS is at line ~332 — "
        "~104 lines apart. They cannot be spanned in one replacement.\n"
        "   Use the 'changes' array to submit both edits atomically (see JSON format below).\n"
        "2. The RandomForest only uses columns listed in FEATURE_COLS. If you compute a column but don't add it\n"
        "   to FEATURE_COLS (or vice versa), the experiment crashes immediately.\n"
        "   EDITING FEATURE_COLS: when removing an entry, take the ENTIRE line including its trailing comment\n"
        "   and the comma. Never split a line in the middle of an inline comment (apostrophes like \"today's\"\n"
        "   will cause Python's implicit string concatenation to merge comment text into the list). Safe pattern:\n"
        "   old_code = '    \"bn_ret60\",        # 3-month return — bull vs bear phase\\n'  ← full line + newline\n"
        "   new_code = ''                                                                ← delete cleanly\n"
        "3. Rolling window limits: the dataset has ~1500+ rows but beware of NaN chains.\n"
        "   Use rolling windows ≤60 days for new features. Windows >100 days risk NaN-induced row drops.\n"
        "   For z-scores/normalisation use rolling(20) or rolling(60). Guard divisions with .replace(0, np.nan).\n"
        "4. If your change doesn't strictly improve the score (>) it will be reverted. Equal-score = no effect.\n"
        "5. Do NOT repeat a feature already tried (see experiment log).\n"
        "6. NO LEAKAGE — RAW SAME-DAY CLOSE/HIGH/LOW: The label = sign(close_today - open_today).\n"
        "   Any raw use of bn_close, bn_high, or bn_low for today's row leaks it. Forbidden:\n"
        "   (bn_close - bn_open), (bn_high - bn_low), body_ratio, zscore using unshifted close.\n"
        "   Features with |corr(feature, label)| > 0.85 are auto-rejected.\n\n"
        "7. SHIFT-FIRST RULE — ALL ROLLING WINDOWS: Any rolling or ewm operation on price series\n"
        "   (bn_close, bn_high, bn_low, bn_volume, vix, nifty_close, etc.) MUST call .shift(1)\n"
        "   BEFORE the rolling/ewm call. Today's close is NOT known until 3:30 PM — it must never\n"
        "   appear inside any window fed to FEATURE_COLS.\n"
        "   WRONG: d['bn_close'].rolling(20).mean()              ← includes today's close\n"
        "   WRONG: d['bn_close'].pct_change(10)                  ← (close_today - close_10)\n"
        "   WRONG: d['bn_close'].ewm(span=5).mean()              ← includes today's close\n"
        "   RIGHT: d['bn_close'].shift(1).rolling(20).mean()     ← window ends yesterday\n"
        "   RIGHT: d['bn_close'].shift(1).pct_change(9)          ← shift first, then diff\n"
        "   RIGHT: d['bn_close'].shift(1).ewm(span=5).mean()     ← EWM ends yesterday\n"
        "   EXCEPTION: bn_open is known at 9:15 AM (no shift needed). rsi14 and hv20 in the\n"
        "   existing FEATURE_COLS already use today's close and are grandfathered — do NOT\n"
        "   add new RSI/HV variants without shifting.\n\n"
        "Here is the complete research program:\n\n"
        + brief
    )


def _call_claude(
    client,
    system_prompt: str,
    code_context: str,
    experiment_log: list[dict],
    experiment_number: int,
    total_experiments: int,
) -> dict | None:
    import anthropic

    if experiment_log:
        log_lines = [
            f"  Exp {e['n']}: {e['description']}  |  {e['before']:.4f} → {e['after']:.4f}  "
            f"{'KEPT' if e['kept'] else 'DISCARDED'}"
            for e in experiment_log[-10:]
        ]
        log_str = "### Experiments so far (last 10)\n" + "\n".join(log_lines)
    else:
        log_str = "### Experiments so far\n(none yet — this is the first)"

    user_message = (
        f"### Current code snippets (experiment {experiment_number}/{total_experiments})\n\n"
        + code_context
        + "\n\n### Column inventory (what actually exists right now)\n"
        + _build_column_inventory()
        + "\n\n"
        + log_str
        + _load_experiment_history()
        + _reversal_summary()
        + "\n\n"
        "### Your task\n"
        "Propose ONE targeted change. Return ONLY a valid JSON object.\n\n"
        "FOR A SINGLE EDIT (hyperparameter, refactoring, single-section change):\n"
        '  {"file": "ml_engine.py", "description": "...", "plain_english": "...",\n'
        '   "old_code": "exact unique substring", "new_code": "replacement"}\n\n'
        "FOR ADDING A NEW FEATURE (requires TWO edits — compute_features + FEATURE_COLS):\n"
        '  {"file": "ml_engine.py", "description": "...", "plain_english": "...",\n'
        '   "changes": [\n'
        '     {"old_code": "...compute_features section...", "new_code": "...with computation added..."},\n'
        '     {"old_code": "...FEATURE_COLS section...", "new_code": "...with new col added..."}\n'
        "   ]}\n\n"
        "Rules:\n"
        "- Each old_code must be a UNIQUE substring of the target file.\n"
        "- Do NOT repeat a change already tried (see experiment log).\n"
        "- No markdown fences. No explanation outside the JSON. JSON only.\n"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
        )
        _record_api_usage(response.usage)
        raw = response.content[0].text.strip()

        if raw.startswith("```"):
            raw = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("```")).strip()

        # Robust extraction: find the first complete JSON object by tracking braces.
        # Handles cases where Claude appends explanation text after the JSON.
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            if start == -1:
                raise
            depth = 0
            in_string = False
            escape = False
            for i in range(start, len(raw)):
                ch = raw[i]
                if escape:
                    escape = False
                    continue
                if ch == "\\" and in_string:
                    escape = True
                    continue
                if ch == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(raw[start:i+1])
            raise json.JSONDecodeError("Unbalanced braces", raw, len(raw))
    except json.JSONDecodeError as e:
        print(f"  Claude returned invalid JSON: {e}")
        return None
    except Exception as e:
        print(f"  Claude API error: {e}")
        return None


# ── Pre-flight validator ──────────────────────────────────────────────────────

def _preflight_feature_check(paper_file: Path) -> list[str]:
    """Dynamically import the paper model, run compute_features() on real data,
    and verify every FEATURE_COLS entry is present in the output.

    Catches ALL crash patterns:
      - Feature in FEATURE_COLS but never computed        (new-feature omission)
      - Feature removed from compute_features but still   (existing-feature deletion)
        referenced in FEATURE_COLS
      - SyntaxError / import error in the patched file
      - Any KeyError / ZeroDivision inside compute_features

    Much more reliable than the old regex approach (~5-8s, vs 5 min for full eval).
    """
    _MOD = "_ml_paper_preflight_"
    if _MOD in sys.modules:
        del sys.modules[_MOD]

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(_MOD, paper_file)
        mod  = importlib.util.module_from_spec(spec)
        sys.modules[_MOD] = mod          # register before exec so self-refs work
        spec.loader.exec_module(mod)     # SyntaxError surfaces here

        df_raw  = mod.load_all_data()
        df_feat = mod.compute_features(df_raw)

        missing = [c for c in mod.FEATURE_COLS if c not in df_feat.columns]
        return missing

    except SyntaxError as e:
        return [f"SyntaxError: {e}"]
    except Exception as e:
        return [f"{type(e).__name__}: {e}"]
    finally:
        if _MOD in sys.modules:
            del sys.modules[_MOD]


# ── Change applicator ─────────────────────────────────────────────────────────

def _apply_change(proposal: dict) -> tuple[bool, str, str]:
    """Apply change(s). Routes ml_engine.py changes to ml_engine_paper.py.

    Supports two formats:
      • Single: {"old_code": "...", "new_code": "..."}
      • Multi:  {"changes": [{"old_code": "...", "new_code": "..."}, ...]}
    All edits are applied atomically — if any edit fails, the file is reverted.
    """
    filename = proposal.get("file", "")
    if filename not in _ALLOWED_FILES:
        return False, f"file '{filename}' not in allowed list", filename

    actual_file = "ml_engine_paper.py" if filename in _PAPER_FILES else filename
    path = _HERE / actual_file
    if not path.exists():
        return False, f"{actual_file} not found", actual_file

    # Build normalised list of (old, new) pairs
    if "changes" in proposal:
        pairs = [(c.get("old_code", ""), c.get("new_code", "")) for c in proposal["changes"]]
    else:
        pairs = [(proposal.get("old_code", ""), proposal.get("new_code", ""))]

    if not pairs:
        return False, "no changes specified", actual_file

    original = path.read_text(encoding="utf-8")
    content = original

    for idx, (old_code, new_code) in enumerate(pairs):
        label = f"change[{idx}]"
        if not old_code:
            path.write_text(original, encoding="utf-8")
            return False, f"{label}: old_code is empty", actual_file
        if old_code == new_code:
            path.write_text(original, encoding="utf-8")
            return False, f"{label}: old_code == new_code", actual_file
        if old_code not in content:
            path.write_text(original, encoding="utf-8")
            return False, f"{label}: old_code not found in {actual_file}", actual_file
        if content.count(old_code) > 1:
            path.write_text(original, encoding="utf-8")
            return False, f"{label}: old_code not unique in {actual_file}", actual_file
        content = content.replace(old_code, new_code, 1)

    path.write_text(content, encoding="utf-8")
    return True, "ok", actual_file


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(_HERE / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="BankNifty autoresearch daily midnight loop")
    parser.add_argument("--experiments", type=int, default=N_EXPERIMENTS, help=f"Number of experiments (default: {N_EXPERIMENTS})")
    parser.add_argument("--dry-run", action="store_true", help="Compute baselines only; no Claude API calls")
    parser.add_argument("--no-evolver", action="store_true", help="Skip model_evolver.py after promotion")
    args = parser.parse_args()

    n_experiments = args.experiments
    ist_now = datetime.now(_IST)
    date_str = ist_now.strftime("%Y-%m-%d")
    date_display = ist_now.strftime("%d %b %Y, %I:%M %p IST")

    print(f"\n{'='*60}")
    print(f"  BankNifty Autoresearch — {date_display}")
    print(f"  Experiments: {n_experiments} | Dry-run: {args.dry_run}")
    print(f"{'='*60}\n")

    # ── Step 1: Ensure paper file exists (always — live-eval needs it even in dry-run)
    _ensure_paper_file(commit=not args.dry_run)

    # ── Step 2: Baselines ─────────────────────────────────────────────────────
    print("[Baseline] Running live ML evaluator...")
    baseline_live = _run_ml_experiment(module="ml_engine")
    if "error" in baseline_live:
        msg = f"❌ <b>Autoresearch couldn't start</b>\n\nError: <code>{baseline_live['error']}</code>\n\nCheck the logs."
        print(msg)
        _send(msg)
        sys.exit(1)

    b_live = baseline_live["composite"]
    b_pnl_live = baseline_live.get("pnl_proxy", 0.0)
    n_train = baseline_live.get("n_train", "?")
    n_val = baseline_live.get("n_val", "?")
    print(f"  [Live]  composite: {b_live:.4f}  pnl_proxy: {b_pnl_live:.4f}")

    print("[Baseline] Running paper ML evaluator...")
    baseline_paper = _run_ml_experiment(module="ml_engine_paper") if _PAPER_FILE.exists() else {"composite": b_live, "pnl_proxy": b_pnl_live}
    if "error" in baseline_paper:
        print(f"  [Paper] error: {baseline_paper['error']} — using live score")
        b_paper = b_live
        b_pnl_paper = b_pnl_live
    else:
        b_paper = baseline_paper.get("composite", b_live)
        b_pnl_paper = baseline_paper.get("pnl_proxy", b_pnl_live)
        print(f"  [Paper] composite: {b_paper:.4f}  pnl_proxy: {b_pnl_paper:.4f}")

    # Live-trade validation: score paper model on real fill dates
    print("[Live-eval] Scoring paper model on actual trade dates...")
    live_eval = _score_paper_on_live_trades()
    n_live = live_eval["n_trades"]
    if n_live >= MIN_LIVE_FOR_MIX:
        live_adv = live_eval["paper_acc"] - live_eval["live_acc"]
        holdout_adv = b_paper - b_live
        combined_adv = LIVE_WEIGHT * live_adv + HOLDOUT_WEIGHT * holdout_adv
        print(f"  Combined advantage: {combined_adv:+.4f}  "
              f"(live {live_adv:+.4f} × {LIVE_WEIGHT}  +  holdout {holdout_adv:+.4f} × {HOLDOUT_WEIGHT})")
    else:
        print(f"  Only {n_live} labeled live trades — using holdout only (need {MIN_LIVE_FOR_MIX}+)")

    # Log today's performance (with live eval data)
    # Skip if paper == live on holdout — models are identical after a promotion,
    # and the live-eval gap (historical oracle_correct vs current model) is a
    # frozen artifact that would otherwise trigger infinite re-promotions.
    models_identical = abs(b_paper - b_live) < 0.0001
    if models_identical:
        print("[Paper] Paper = Live (models identical after promotion) — skipping streak log.")
    elif not args.dry_run:
        _log_paper_performance(date_str, b_live, b_paper, live_eval)

    # ── Step 3: Check paper promotion ─────────────────────────────────────────
    should_promote, streak, latest_adv = _check_paper_promotion()
    print(f"[Paper] Streak: {streak}/{PAPER_WIN_STREAK}  Latest combined advantage: {latest_adv:+.4f}")

    if should_promote and not args.dry_run:
        _promote_paper_to_live(streak, latest_adv)
        b_live = b_paper  # after promotion live = paper
        # Run evolver to retrain models on promoted code
        if not args.no_evolver:
            print("[Evolver] Running model_evolver.py after promotion...")
            try:
                subprocess.run([sys.executable, "model_evolver.py"], cwd=str(_HERE), timeout=3600)
                print("[Evolver] Done.")
            except subprocess.TimeoutExpired:
                print("[Evolver] Timed out after 1 hour.")
                _send("⚠️ <b>Model retraining timed out</b>\nRun <code>python3 model_evolver.py</code> manually.")
            except Exception as e:
                print(f"[Evolver] Error: {e}")
        return  # Done for tonight — promotion was the main event

    # ── Step 4: Dry-run exits here ────────────────────────────────────────────
    if args.dry_run:
        paper_vs_live = b_paper - b_live
        streak_str = f"{streak}/{PAPER_WIN_STREAK} nights ahead" if streak > 0 else "not ahead yet"
        if n_live >= MIN_LIVE_FOR_MIX:
            live_eval_line = (
                f"🧾 <b>On real trades ({n_live}):</b> paper {live_eval['paper_acc']:.0%}  "
                f"vs live {live_eval['live_acc']:.0%}  "
                f"({live_eval['paper_acc'] - live_eval['live_acc']:+.0%})\n"
            )
        else:
            live_eval_line = f"🧾 <b>Real trade data:</b> {n_live} trades logged (need {MIN_LIVE_FOR_MIX} to unlock) — holdout only\n"
        _send(
            f"🔬 <b>Autoresearch — Daily Check</b>  ·  {date_display}\n\n"
            f"📊 <b>Live model (252-day test):</b>  {b_live:.2%}\n"
            f"📄 <b>Paper model (252-day test):</b> {b_paper:.2%}  ({paper_vs_live:+.2%})\n"
            f"{live_eval_line}"
            f"🔢 <b>Promotion streak:</b> {streak_str}\n\n"
            f"📅 <b>Data:</b> {n_train} training days · {n_val} test days\n\n"
            f"✅ System healthy. Ready for tonight's experiments."
        )
        return

    # ── Step 5: Telegram start ────────────────────────────────────────────────
    paper_vs_live = b_paper - b_live
    streak_info = f"Paper ahead {streak}/{PAPER_WIN_STREAK} nights" if streak > 0 else "Paper not ahead yet"
    if n_live >= MIN_LIVE_FOR_MIX:
        live_eval_line = (
            f"🧾 <b>Real trades ({n_live}):</b> paper {live_eval['paper_acc']:.0%}  "
            f"vs live {live_eval['live_acc']:.0%}  "
            f"({live_eval['paper_acc'] - live_eval['live_acc']:+.0%})\n"
        )
        scoring_note = f"(promotion uses {int(LIVE_WEIGHT*100)}% real trades + {int(HOLDOUT_WEIGHT*100)}% historical test)"
    else:
        live_eval_line = f"🧾 <b>Real trade data:</b> {n_live} trades logged (need {MIN_LIVE_FOR_MIX} to unlock) — using holdout only for now\n"
        scoring_note = f"(will mix in real trade accuracy once {MIN_LIVE_FOR_MIX} trades logged)"
    _send(
        f"🤖 <b>BankNifty Brain Training — Starting</b>\n"
        f"{date_display}\n\n"
        f"Running {n_experiments} experiments tonight.\n"
        f"ML changes go into the <b>paper model</b> first — they need {PAPER_WIN_STREAK} good nights to go live.\n\n"
        f"📊 <b>Live model (252-day test):</b>  {b_live:.2%}\n"
        f"📄 <b>Paper model (252-day test):</b> {b_paper:.2%}  ({paper_vs_live:+.2%})\n"
        f"{live_eval_line}"
        f"🎯 <b>Promotion streak:</b> {streak_info}\n"
        f"<i>{scoring_note}</i>\n\n"
        f"🌙 Go to sleep — I'll update you as experiments complete."
    )

    # ── Step 6: Setup Claude ──────────────────────────────────────────────────
    client = _make_client()
    system_prompt = _build_system_prompt()

    best_paper = b_paper
    best_live = b_live
    best_pnl_p = b_pnl_paper
    best_pnl_l = b_pnl_live

    # Backtest baseline
    print("[Baseline BT] Running autoexperiment_backtest.py...")
    baseline_bt = _run_backtest_experiment()
    if "error" in baseline_bt:
        print(f"  [BT]  baseline failed: {baseline_bt['error']} — auto_trader.py experiments skipped")
        b_bt = 0.0
    else:
        b_bt = baseline_bt["composite"]
        print(f"  [BT]  composite: {b_bt:.4f}")
    best_bt = b_bt

    experiment_log: list[dict] = []
    kept_paper_count = 0
    kept_immediate_count = 0

    # ── Step 7: Experiment loop ───────────────────────────────────────────────
    for i in range(1, n_experiments + 1):
        print(f"\n[Exp {i}/{n_experiments}] Calling Claude...")
        t0 = time.time()

        # Show paper state for ML experiments
        code_context = _build_context_snippet(use_paper=True)

        proposal = _call_claude(
            client,
            system_prompt,
            code_context,
            experiment_log,
            i,
            n_experiments,
        )

        if proposal is None:
            print(f"  Claude returned no valid proposal — skipping exp {i}")
            experiment_log.append(
                {
                    "n": i,
                    "description": "(Claude API error)",
                    "before": best_paper,
                    "after": 0.0,
                    "kept": False,
                }
            )
            _send(f"⚠️ <b>Idea #{i} — skipped</b>\n\nCouldn't get a valid idea from the AI this round. Moving on.")
            continue

        description = proposal.get("description", "(no description)")
        plain_eng = proposal.get("plain_english", description)
        filename = proposal.get("file", "?")
        is_paper = filename in _PAPER_FILES
        is_backtest = filename in _BACKTEST_FILES

        print(f"  Proposal: [{filename}] {description}")
        print(f"  Plain: {plain_eng}")

        # Apply change (paper routing for ml_engine.py)
        ok, reason, actual_file = _apply_change(proposal)
        if not ok:
            print(f"  Apply failed: {reason} — skipping")
            experiment_log.append(
                {
                    "n": i,
                    "description": description + f" [SKIP: {reason}]",
                    "before": best_paper if is_paper else best_bt,
                    "after": 0.0,
                    "kept": False,
                }
            )
            continue

        # Pre-flight: catch FEATURE_COLS/compute_features mismatch before 30s run
        if is_paper:
            uncomputed = _preflight_feature_check(_PAPER_FILE)
            if uncomputed:
                print(f"  Pre-flight: {uncomputed} in FEATURE_COLS but not computed — reverting")
                _revert_files([actual_file])
                pf_err = f"FEATURE_COLS has {uncomputed} but no df[col]= assignment in compute_features"
                experiment_log.append({
                    "n": i,
                    "description": description + f" [PREFLIGHT FAIL: {pf_err[:60]}]",
                    "before": best_paper,
                    "after": 0.0,
                    "kept": False,
                })
                _send(
                    f"⚠️ <b>Idea #{i} of {n_experiments} — pre-flight failed</b>\n\n"
                    f"💡 {plain_eng}\n\n"
                    f"Problem: <code>{', '.join(uncomputed[:3])}</code>\n"
                    f"Reverted. Moving on."
                )
                continue

        # Route to correct evaluator
        if is_backtest:
            if b_bt == 0.0:
                print(f"  Backtest baseline unavailable — reverting {actual_file}")
                _revert_files([actual_file])
                experiment_log.append(
                    {
                        "n": i,
                        "description": description + " [SKIP: BT baseline unavailable]",
                        "before": best_bt,
                        "after": 0.0,
                        "kept": False,
                    }
                )
                continue
            print(f"  Running backtest evaluator...")
            result = _run_backtest_experiment()
            cur_best = best_bt
            cur_pnl = b_bt
        elif is_paper:
            print(f"  Running paper ML evaluator...")
            result = _run_ml_experiment(module="ml_engine_paper")
            cur_best = best_paper
            cur_pnl = best_pnl_p
        else:
            # Immediate ML file (signal_engine.py)
            print(f"  Running live ML evaluator...")
            result = _run_ml_experiment(module="ml_engine")
            cur_best = best_live
            cur_pnl = best_pnl_l

        elapsed = time.time() - t0

        if "error" in result:
            print(f"  Experiment failed: {result['error']} — reverting")
            _revert_files([actual_file])
            experiment_log.append(
                {
                    "n": i,
                    "description": description + f" [FAIL: {result['error'][:220]}]",
                    "before": cur_best,
                    "after": 0.0,
                    "kept": False,
                }
            )
            _send(f"⚠️ <b>Idea #{i} of {n_experiments} — test crashed</b>\n\n💡 {plain_eng}\n\nThrown away, moving on.")
            continue

        new_composite = result["composite"]
        new_pnl = result.get("pnl_proxy", 0.0)
        pnl_floor = cur_pnl * PNL_GUARD

        print(
            f"  Score: {cur_best:.4f} → {new_composite:.4f}  "
            f"pnl: {cur_pnl:.4f} → {new_pnl:.4f}  ({elapsed:.0f}s)"
        )

        prev_best = cur_best
        # Require strict improvement — equal scores mean the change had no effect
        if new_composite > cur_best and new_pnl >= pnl_floor:
            # Keep it — commit
            if is_paper:
                commit_msg = f"autoloop paper exp {i}: {description} ({cur_best:.4f}→{new_composite:.4f})"
                mode_label = "📄 Paper model"
                commit_files = [actual_file]
            elif is_backtest:
                commit_msg = f"autoloop live exp {i}: {description} ({cur_best:.4f}→{new_composite:.4f})"
                mode_label = "✅ Live (trade params)"
                commit_files = [actual_file]
            else:
                commit_msg = f"autoloop live exp {i}: {description} ({cur_best:.4f}→{new_composite:.4f})"
                mode_label = "✅ Live (signal rules)"
                commit_files = [actual_file]

            committed = _commit(commit_msg, commit_files)
            if committed:
                experiment_log.append(
                    {
                        "n": i,
                        "description": description,
                        "before": prev_best,
                        "after": new_composite,
                        "kept": True,
                    }
                )
                _record_experiment(description, kept=True, score_before=prev_best, score_after=new_composite)
                delta = new_composite - prev_best
                delta_str = f"+{delta:.2%}" if delta > 0 else "no change"

                if is_paper:
                    best_paper = new_composite
                    best_pnl_p = new_pnl
                    kept_paper_count += 1
                    _log_paper_change(description, plain_eng, prev_best, new_composite)
                    _send(
                        f"📄 <b>Paper model improved — experiment #{i}</b>\n\n"
                        f"💡 {plain_eng}\n\n"
                        f"Paper score: {prev_best:.2%} → {new_composite:.2%}  ({delta_str})\n\n"
                        f"<i>Needs {PAPER_WIN_STREAK} nights ahead to go live.</i>"
                    )
                elif is_backtest:
                    best_bt = new_composite
                    kept_immediate_count += 1
                    _send(
                        f"✅ <b>Trade settings improved — live now</b>\n\n"
                        f"💡 {plain_eng}\n\n"
                        f"Score: {prev_best:.2%} → {new_composite:.2%}  ({delta_str})\n\n"
                        f"<i>This affects tomorrow's trade directly.</i>"
                    )
                else:
                    best_live = new_composite
                    best_pnl_l = new_pnl
                    kept_immediate_count += 1
                    _send(
                        f"✅ <b>Signal rules improved — live now</b>\n\n"
                        f"💡 {plain_eng}\n\n"
                        f"Score: {prev_best:.2%} → {new_composite:.2%}  ({delta_str})\n\n"
                        f"<i>This affects tomorrow's trade directly.</i>"
                    )
                print(f"  ✅ KEPT ({mode_label})")
            else:
                print("  git commit failed — reverting")
                _revert_files([actual_file])
                experiment_log.append(
                    {
                        "n": i,
                        "description": description + " [git commit failed]",
                        "before": prev_best,
                        "after": new_composite,
                        "kept": False,
                    }
                )
                _send(
                    f"⚠️ <b>Idea #{i} — couldn't save</b>\n\n"
                    f"💡 {plain_eng}\n\n"
                    f"Score looked good ({prev_best:.2%} → {new_composite:.2%}) but git save failed. Thrown away."
                )
        else:
            _revert_files([actual_file])
            reason_str = (
                f"pnl {new_pnl:.4f} < floor {pnl_floor:.4f}"
                if new_composite > cur_best
                else f"composite {new_composite:.4f} not > {cur_best:.4f} (no improvement)"
            )
            experiment_log.append(
                {"n": i, "description": description, "before": cur_best, "after": new_composite, "kept": False}
            )
            _record_experiment(description, kept=False, score_before=cur_best, score_after=new_composite)
            _send(
                f"❌ <b>Idea #{i} of {n_experiments} didn't help</b>\n\n"
                f"💡 {plain_eng}\n\n"
                f"Score: {cur_best:.2%} → {new_composite:.2%}  (no improvement)\n\n"
                f"Thrown away. Back to previous version."
            )
            print(f"  ❌ DISCARDED ({reason_str})")

    # ── Step 8: End-of-run summary ────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  Autoresearch complete")
    print(f"  Paper: {kept_paper_count} kept  |  Immediate: {kept_immediate_count} kept")
    print(f"  Paper score: {b_paper:.4f} → {best_paper:.4f}")
    print(f"  Live score:  {b_live:.4f} → {best_live:.4f}")
    print(f"{'='*60}\n")

    # Push any local commits from this run (paper experiments, promotion, etc.)
    # so GitHub mirrors the VM. Non-interactive: fails fast if auth isn't set up.
    if not args.dry_run and (kept_paper_count + kept_immediate_count) > 0:
        branch = _current_branch()
        rc, out = _git("push", "origin", branch)
        if rc == 0:
            print(f"[Git] Pushed new commits to origin/{branch}.")
        else:
            print(f"[Git] Push skipped/failed (not fatal): {out.splitlines()[0] if out else 'no output'}")

    kept_total = kept_paper_count + kept_immediate_count
    discarded = n_experiments - kept_total
    claude_cost = _get_claude_cost_yesterday()

    # Check updated streak
    should_promote2, streak2, _ = _check_paper_promotion()
    streak_str = (
        f"{streak2}/{PAPER_WIN_STREAK} nights ahead — promotion {'TOMORROW' if streak2 == PAPER_WIN_STREAK - 1 else 'building'}!"
        if streak2 > 0
        else "No streak yet"
    )

    paper_adv = best_paper - best_live

    if kept_total > 0:
        kept_items = [e for e in experiment_log if e["kept"]]
        kept_lines = "\n".join(f"  {j+1}. {e['description']}" for j, e in enumerate(kept_items))
        summary_msg = (
            f"🌅 <b>Brain Training Done</b>  ·  {date_display}\n\n"
            f"✅ {kept_paper_count} paper  ·  ✅ {kept_immediate_count} live  ·  ❌ {discarded} thrown away\n\n"
            f"📄 Paper model: {b_paper:.2%} → {best_paper:.2%}\n"
            f"📊 Live model:  {b_live:.2%} → {best_live:.2%}\n"
            f"📈 Paper lead:  {paper_adv:+.2%}\n\n"
            f"🔢 Promotion: {streak_str}\n\n"
            f"<b>What changed tonight:</b>\n{kept_lines}\n\n"
            f"💰 Claude API cost yesterday: {claude_cost}"
        )
    else:
        summary_msg = (
            f"🌅 <b>Brain Training Done</b>  ·  {date_display}\n\n"
            f"Tried {n_experiments} ideas — none helped tonight.\n\n"
            f"📄 Paper model: {best_paper:.2%}\n"
            f"📊 Live model:  {best_live:.2%}\n"
            f"📈 Paper lead:  {paper_adv:+.2%}\n\n"
            f"🔢 Promotion: {streak_str}\n\n"
            f"💰 Claude API cost yesterday: {claude_cost}"
        )

    _send(summary_msg)

    # ── Step 9: Run evolver if immediate changes were made ────────────────────
    if kept_immediate_count > 0 and not args.no_evolver:
        print("[Evolver] Immediate changes made — running model_evolver.py...")
        _send("🔄 <b>Signal rules changed — retraining models now</b>\n\nTakes ~10 minutes. Next update when done.")
        try:
            subprocess.run([sys.executable, "model_evolver.py"], cwd=str(_HERE), timeout=3600)
            print("[Evolver] Done.")
        except subprocess.TimeoutExpired:
            print("[Evolver] Timed out after 1 hour.")
            _send("⚠️ <b>Model retraining timed out</b>\nRun manually: <code>python3 model_evolver.py</code>")
        except Exception as e:
            print(f"[Evolver] Error: {e}")
    else:
        print("[Evolver] No immediate changes — skipping.")

    # ── Step 10: Update dynamic VIX threshold ────────────────────────────────
    # analyze_confidence.py scans VIX buckets on the 252-day holdout and writes
    # the lowest VIX level where accuracy >= 52% to data/vix_threshold.json.
    # auto_trader.py reads this at startup — so as the model improves overnight,
    # it automatically trades on more days.
    print("[VIX Threshold] Recomputing dynamic trade threshold...")
    try:
        r = subprocess.run(
            [sys.executable, "analyze_confidence.py", "--write-threshold"],
            cwd=str(_HERE), capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            # Print just the threshold line for the log
            for line in r.stdout.splitlines():
                if "VIX_MIN_TRADE" in line or "Written" in line:
                    print(f"  {line.strip()}")
        else:
            print(f"[VIX Threshold] Failed: {r.stderr[:300]}")
    except Exception as e:
        print(f"[VIX Threshold] Error: {e}")


if __name__ == "__main__":
    main()
