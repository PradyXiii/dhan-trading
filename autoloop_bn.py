#!/usr/bin/env python3
"""
autoloop_bn.py — Overnight autoresearch loop for BankNifty ML system.

Karpathy-style: Claude API proposes ONE code change per experiment →
autoexperiment_bn.py evaluates it → improvement is kept (git commit) or
reverted → Telegram notification per experiment → summary at end.

Usage:
    python3 autoloop_bn.py                   # 20 experiments, full run
    python3 autoloop_bn.py --experiments 5   # quick 5-experiment run
    python3 autoloop_bn.py --dry-run         # baseline only, no Claude API calls
    python3 autoloop_bn.py --no-evolver      # skip model_evolver.py at end

Cron (Saturday 11:30 PM IST = 18:00 UTC):
    0 18 * * 6  cd /path/to/dhan-trading && python3 autoloop_bn.py >> logs/autoloop_bn.log 2>&1
"""

import argparse
import json
import os
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

# ── Constants ─────────────────────────────────────────────────────────────────
N_EXPERIMENTS = 20
MODEL         = "claude-sonnet-4-6"
MAX_TOKENS    = 2048
PNL_GUARD     = 0.90       # pnl_proxy must not fall below 90% of baseline
_IST          = timezone(timedelta(hours=5, minutes=30))
_HERE         = Path(__file__).parent.resolve()

# Files the agent is allowed to modify
_ALLOWED_FILES = {"ml_engine.py", "signal_engine.py"}


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _send(message: str) -> bool:
    """Send to Telegram using notify.py's send()."""
    try:
        sys.path.insert(0, str(_HERE))
        import notify
        return notify.send(message)
    except Exception as e:
        print(f"[Telegram error] {e}")
        return False


# ── Git helpers ───────────────────────────────────────────────────────────────

def _git(*args: str) -> tuple[int, str]:
    """Run a git command; return (returncode, stdout+stderr)."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(_HERE),
        capture_output=True,
        text=True,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _revert_files() -> None:
    """Discard any uncommitted changes to the two editable files."""
    _git("checkout", "--", "ml_engine.py", "signal_engine.py")


def _commit(message: str) -> bool:
    """Stage editable files and create a commit. Returns True on success."""
    rc, out = _git("add", "ml_engine.py", "signal_engine.py")
    if rc != 0:
        print(f"  git add failed: {out}")
        return False
    rc, out = _git("commit", "-m", message)
    if rc != 0:
        print(f"  git commit failed: {out}")
        return False
    return True


# ── Experiment runner ─────────────────────────────────────────────────────────

def _run_experiment() -> dict:
    """
    Run autoexperiment_bn.py in a subprocess and return parsed JSON.
    Returns {"composite": 0.0, "error": "..."} on failure.
    """
    try:
        result = subprocess.run(
            [sys.executable, "autoexperiment_bn.py"],
            cwd=str(_HERE),
            capture_output=True,
            text=True,
            timeout=300,    # 5-minute hard limit
        )
        stdout = result.stdout.strip()
        if not stdout:
            return {"composite": 0.0, "error": f"empty stdout (stderr: {result.stderr[:200]})"}
        # Find the last JSON line in output
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        return {"composite": 0.0, "error": f"no JSON in stdout: {stdout[:200]}"}
    except subprocess.TimeoutExpired:
        return {"composite": 0.0, "error": "timeout after 300s"}
    except json.JSONDecodeError as e:
        return {"composite": 0.0, "error": f"JSON parse error: {e}"}
    except Exception as e:
        return {"composite": 0.0, "error": str(e)}


# ── File content helpers ──────────────────────────────────────────────────────

def _read_file(filename: str) -> str:
    path = _HERE / filename
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _extract_section(content: str, section: str, lines_after: int = 80) -> str:
    """
    Extract a function/section from a file by finding 'section' and
    returning that line + lines_after lines.
    """
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if section in line:
            end = min(i + lines_after, len(lines))
            return "\n".join(lines[i:end])
    return content[:3000]   # fallback: first 3000 chars


def _build_context_snippet() -> str:
    """
    Build the relevant code snippets to send to Claude per experiment.
    Keeps the user message small (dynamic content) — the research brief
    is in the (cached) system prompt.
    """
    ml  = _read_file("ml_engine.py")
    sig = _read_file("signal_engine.py")

    # Extract FEATURE_COLS list
    feat_section   = _extract_section(ml, "FEATURE_COLS", lines_after=40)
    # Extract compute_features() function header + first ~60 lines
    compute_section = _extract_section(ml, "def compute_features", lines_after=70)
    # Extract score_row() from signal_engine
    score_section  = _extract_section(sig, "def score_row", lines_after=60)

    return (
        "### ml_engine.py — FEATURE_COLS\n```python\n"
        + feat_section
        + "\n```\n\n### ml_engine.py — compute_features() (first 70 lines)\n```python\n"
        + compute_section
        + "\n```\n\n### signal_engine.py — score_row() (first 60 lines)\n```python\n"
        + score_section
        + "\n```"
    )


# ── Claude API ────────────────────────────────────────────────────────────────

def _make_client():
    """Create Anthropic client; raises if API key missing."""
    try:
        import anthropic
    except ImportError:
        print("ERROR: 'anthropic' package not installed. Run: pip3 install anthropic")
        sys.exit(1)

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    return anthropic.Anthropic(api_key=api_key)


def _build_system_prompt() -> str:
    """Load research_program_bn.md as the (cacheable) system prompt."""
    path = _HERE / "research_program_bn.md"
    try:
        brief = path.read_text(encoding="utf-8")
    except Exception:
        brief = "(research_program_bn.md not found)"

    return (
        "You are an expert ML feature engineer specialising in Indian equity derivatives.\n\n"
        "Your job: propose ONE targeted code change to improve the BankNifty ML model composite score.\n\n"
        "Here is the complete research program that defines your scope:\n\n"
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
    """
    Call Claude API. Returns parsed JSON dict or None on error.
    Uses prompt caching on the system prompt (static content).
    """
    import anthropic

    # Build experiment log summary (compact)
    if experiment_log:
        log_lines = [
            f"  Exp {e['n']}: {e['description']}  |  {e['before']:.4f} → {e['after']:.4f}  "
            f"{'KEPT' if e['kept'] else 'DISCARDED'}"
            for e in experiment_log[-10:]   # last 10 to fit context
        ]
        log_str = "### Experiments so far (last 10)\n" + "\n".join(log_lines)
    else:
        log_str = "### Experiments so far\n(none yet — this is the first experiment)"

    user_message = (
        f"### Current code snippets (experiment {experiment_number}/{total_experiments})\n\n"
        + code_context
        + "\n\n"
        + log_str
        + "\n\n"
        "### Your task\n"
        "Propose ONE small, targeted change. Return ONLY a valid JSON object with these keys:\n"
        '  "file": "ml_engine.py" or "signal_engine.py"\n'
        '  "description": "short one-line description"\n'
        '  "old_code": "exact unique substring to find and replace"\n'
        '  "new_code": "replacement string"\n\n'
        "Rules:\n"
        "- old_code must be a UNIQUE substring of the file (include 2-3 lines of context).\n"
        "- Do NOT repeat a change already tried (see experiment log).\n"
        "- ONE change only. No markdown fences. No explanation. JSON only.\n"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},  # cache the static brief
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown fences if Claude disobeyed the instruction
        if raw.startswith("```"):
            raw = "\n".join(
                line for line in raw.splitlines()
                if not line.strip().startswith("```")
            ).strip()

        return json.loads(raw)

    except json.JSONDecodeError as e:
        print(f"  Claude returned invalid JSON: {e}\n  Raw: {raw[:300]}")
        return None
    except Exception as e:
        print(f"  Claude API error: {e}")
        return None


# ── Change applicator ─────────────────────────────────────────────────────────

def _apply_change(proposal: dict) -> tuple[bool, str]:
    """
    Apply old_code → new_code replacement to the specified file.
    Returns (success, reason).
    """
    filename = proposal.get("file", "")
    if filename not in _ALLOWED_FILES:
        return False, f"file '{filename}' not in allowed list {_ALLOWED_FILES}"

    old_code = proposal.get("old_code", "")
    new_code = proposal.get("new_code", "")

    if not old_code:
        return False, "old_code is empty"
    if old_code == new_code:
        return False, "old_code == new_code (no change)"

    path = _HERE / filename
    content = path.read_text(encoding="utf-8")

    if old_code not in content:
        return False, f"old_code not found in {filename}"

    if content.count(old_code) > 1:
        return False, f"old_code is not unique in {filename} ({content.count(old_code)} matches)"

    new_content = content.replace(old_code, new_code, 1)
    path.write_text(new_content, encoding="utf-8")
    return True, "ok"


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    # Load .env
    try:
        from dotenv import load_dotenv
        load_dotenv(_HERE / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="BankNifty autoresearch overnight loop")
    parser.add_argument("--experiments", type=int, default=N_EXPERIMENTS,
                        help=f"Number of experiments to run (default: {N_EXPERIMENTS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute baseline only; do not call Claude or modify files")
    parser.add_argument("--no-evolver", action="store_true",
                        help="Skip model_evolver.py at the end")
    args = parser.parse_args()

    n_experiments = args.experiments
    ist_now = datetime.now(_IST)
    date_str = ist_now.strftime("%d %b %Y, %I:%M %p IST")

    print(f"\n{'='*60}")
    print(f"  BankNifty Autoresearch — {date_str}")
    print(f"  Experiments: {n_experiments} | Dry-run: {args.dry_run}")
    print(f"{'='*60}\n")

    # ── Step 1: Baseline ──────────────────────────────────────────────────────
    print("[Baseline] Running autoexperiment_bn.py...")
    baseline = _run_experiment()
    if "error" in baseline:
        msg = (
            f"❌ <b>Autoresearch couldn't start</b>\n"
            f"Something went wrong before any experiments ran.\n\n"
            f"Error: <code>{baseline['error']}</code>\n\n"
            f"Check the logs on the VM."
        )
        print(msg)
        _send(msg)
        sys.exit(1)

    b_composite = baseline["composite"]
    b_pnl       = baseline.get("pnl_proxy", 0.0)
    n_train     = baseline.get("n_train", "?")
    n_val       = baseline.get("n_val", "?")
    print(f"  Baseline composite: {b_composite:.4f}  pnl_proxy: {b_pnl:.4f}")
    print(f"  Train rows: {n_train}  Val rows: {n_val}")

    # ── Dry-run exits here ────────────────────────────────────────────────────
    if args.dry_run:
        print("\n[Dry-run] Baseline complete. Exiting (no experiments).")
        _send(
            f"🔬 <b>Autoresearch — Test Check</b>  ·  {date_str}\n\n"
            f"Just measuring where the model stands today.\n\n"
            f"📊 <b>Current model score:</b> {b_composite:.2%}  (higher = better)\n"
            f"🎯 <b>Direction accuracy:</b>   {b_pnl:.2%}\n"
            f"📅 <b>Data:</b> {n_train} days training · {n_val} days testing\n\n"
            f"✅ System looks healthy. Ready for a real overnight run."
        )
        return

    # ── Step 2: Telegram start ────────────────────────────────────────────────
    _send(
        f"🤖 <b>BankNifty Brain Training — Starting Now</b>\n"
        f"{date_str}\n\n"
        f"Tonight I'll try <b>{n_experiments} small ideas</b> to improve the model.\n"
        f"Each idea gets tested — if it helps, I save it. If not, I throw it away.\n\n"
        f"📊 <b>Starting score:</b>      {b_composite:.2%}\n"
        f"🎯 <b>Direction accuracy:</b>  {b_pnl:.2%}\n"
        f"📅 <b>Data:</b> {n_train} days training · {n_val} days for testing\n\n"
        f"🌙 Go to sleep. I'll update you after each experiment!"
    )

    # ── Step 3: Setup Claude ──────────────────────────────────────────────────
    client        = _make_client()
    system_prompt = _build_system_prompt()

    best_composite = b_composite
    best_pnl       = b_pnl
    experiment_log: list[dict] = []
    kept_count     = 0

    # ── Step 4: Experiment loop ───────────────────────────────────────────────
    for i in range(1, n_experiments + 1):
        print(f"\n[Exp {i}/{n_experiments}] Calling Claude...")
        t0 = time.time()

        # Build fresh code context (files may have changed)
        code_context = _build_context_snippet()

        proposal = _call_claude(
            client, system_prompt, code_context,
            experiment_log, i, n_experiments,
        )

        if proposal is None:
            print(f"  Claude returned no valid proposal — skipping experiment {i}")
            experiment_log.append({
                "n": i, "description": "(Claude API error)",
                "before": best_composite, "after": 0.0, "kept": False,
            })
            _send(
                f"⚠️ <b>Idea #{i} — skipped</b>\n"
                f"Couldn't get a valid idea from Claude this round. Moving on."
            )
            continue

        description = proposal.get("description", "(no description)")
        filename    = proposal.get("file", "?")
        print(f"  Proposal: [{filename}] {description}")

        # Apply change
        ok, reason = _apply_change(proposal)
        if not ok:
            print(f"  Apply failed: {reason} — skipping")
            experiment_log.append({
                "n": i, "description": description + f" [SKIP: {reason}]",
                "before": best_composite, "after": 0.0, "kept": False,
            })
            continue

        # Run evaluation
        print(f"  Running autoexperiment_bn.py...")
        result = _run_experiment()
        elapsed = time.time() - t0

        if "error" in result:
            print(f"  Experiment failed: {result['error']} — reverting")
            _revert_files()
            experiment_log.append({
                "n": i, "description": description + f" [FAIL: {result['error'][:60]}]",
                "before": best_composite, "after": 0.0, "kept": False,
            })
            _send(
                f"⚠️ <b>Idea #{i} of {n_experiments} — crashed during test</b>\n"
                f"💡 Idea: {description}\n\n"
                f"The test run hit an error. Thrown away, moving on."
            )
            continue

        new_composite = result["composite"]
        new_pnl       = result.get("pnl_proxy", 0.0)
        pnl_floor     = best_pnl * PNL_GUARD

        print(f"  Score: {best_composite:.4f} → {new_composite:.4f}  "
              f"pnl: {best_pnl:.4f} → {new_pnl:.4f}  ({elapsed:.0f}s)")

        # Accept / reject
        prev_composite = best_composite
        if new_composite >= best_composite and new_pnl >= pnl_floor:
            # Attempt to commit — if git fails, treat as discard
            commit_msg = (
                f"autoloop exp {i}: {description} "
                f"({best_composite:.4f}→{new_composite:.4f})"
            )
            committed = _commit(commit_msg)
            if committed:
                experiment_log.append({
                    "n": i, "description": description,
                    "before": prev_composite, "after": new_composite, "kept": True,
                })
                best_composite = new_composite
                best_pnl       = new_pnl
                kept_count    += 1
                delta = new_composite - prev_composite
                delta_str = f"+{delta:.2%}" if delta > 0 else "no change"
                _send(
                    f"✅ <b>Idea #{i} of {n_experiments} worked!</b>\n"
                    f"💡 {description}\n\n"
                    f"Score before: {prev_composite:.2%}\n"
                    f"Score after:  {new_composite:.2%}  ({delta_str})\n\n"
                    f"Saved ✓  Moving to idea #{i+1}..."
                )
                print(f"  ✅ KEPT")
            else:
                print("  git commit failed — reverting, counting as DISCARDED")
                _revert_files()
                experiment_log.append({
                    "n": i, "description": description + " [git commit failed]",
                    "before": prev_composite, "after": new_composite, "kept": False,
                })
                _send(
                    f"⚠️ <b>Idea #{i} of {n_experiments} — couldn't save</b>\n"
                    f"💡 {description}\n\n"
                    f"Score looked good ({prev_composite:.2%} → {new_composite:.2%}) "
                    f"but git save failed. Thrown away for safety."
                )
        else:
            # DISCARD
            _revert_files()
            reason_str = ""
            if new_composite < best_composite:
                reason_str = f"composite {new_composite:.4f} < {best_composite:.4f}"
            else:
                reason_str = f"pnl {new_pnl:.4f} < floor {pnl_floor:.4f}"

            experiment_log.append({
                "n": i, "description": description,
                "before": best_composite, "after": new_composite, "kept": False,
            })

            _send(
                f"❌ <b>Idea #{i} of {n_experiments} didn't help</b>\n"
                f"💡 {description}\n\n"
                f"Score before: {best_composite:.2%}\n"
                f"Score after:  {new_composite:.2%}  (worse)\n\n"
                f"Thrown away. Back to previous version."
            )
            print(f"  ❌ DISCARDED ({reason_str})")

    # ── Step 5: Summary ───────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  Autoresearch complete")
    print(f"  {kept_count}/{n_experiments} experiments kept")
    print(f"  Best composite: {best_composite:.4f}  (was {b_composite:.4f})")
    print(f"{'='*60}\n")

    kept_items = [e for e in experiment_log if e["kept"]]
    discarded  = n_experiments - kept_count

    improvement_pct = ((best_composite - b_composite) / max(b_composite, 0.001)) * 100

    if kept_items:
        kept_lines = "\n".join(
            f"  {j+1}. {e['description']}\n"
            f"      {e['before']:.2%} → {e['after']:.2%}"
            for j, e in enumerate(kept_items)
        )
        score_line = (
            f"📈 Score: {b_composite:.2%} → {best_composite:.2%}  ({improvement_pct:+.1f}%) 🚀"
            if improvement_pct > 0.1
            else f"📊 Score held steady at {best_composite:.2%}"
        )
        summary_msg = (
            f"🌅 <b>BankNifty Brain Training — Done!</b>\n"
            f"{date_str}\n\n"
            f"✅ {kept_count} ideas worked  ·  ❌ {discarded} didn't\n\n"
            f"{score_line}\n\n"
            f"<b>What changed:</b>\n{kept_lines}\n"
        )
        if not args.no_evolver:
            summary_msg += (
                f"\n🔄 Retraining the 4 models with the improved code...\n"
                f"You'll get another message when that's done."
            )
    else:
        summary_msg = (
            f"🌅 <b>BankNifty Brain Training — Done</b>\n"
            f"{date_str}\n\n"
            f"Tried {n_experiments} ideas — none helped.\n\n"
            f"📊 Score stayed at {best_composite:.2%}. The model is already well-tuned.\n"
            f"Nothing changed in the code. Models were NOT retrained.\n\n"
            f"Try again next week — the market will give us new patterns to learn from."
        )

    _send(summary_msg)

    # ── Step 6: Run model_evolver if improvements were made ───────────────────
    if kept_count > 0 and not args.no_evolver:
        print("[Evolver] Running model_evolver.py to retrain 4 models on improved code...")
        try:
            subprocess.run(
                [sys.executable, "model_evolver.py"],
                cwd=str(_HERE),
                timeout=3600,   # 1-hour hard limit
            )
            print("[Evolver] Done.")
        except subprocess.TimeoutExpired:
            print("[Evolver] Timed out after 1 hour.")
            _send(
                f"⚠️ <b>Model retraining timed out</b>\n"
                f"Ran for over 1 hour and was stopped.\n\n"
                f"The code improvements are saved — run <code>python3 model_evolver.py</code> manually tomorrow."
            )
        except Exception as e:
            print(f"[Evolver] Error: {e}")
            _send(
                f"⚠️ <b>Model retraining failed</b>\n"
                f"Error: {e}\n\n"
                f"The code improvements are saved — run <code>python3 model_evolver.py</code> manually."
            )
    elif kept_count == 0:
        print("[Evolver] No improvements kept — skipping model_evolver.")


if __name__ == "__main__":
    main()
