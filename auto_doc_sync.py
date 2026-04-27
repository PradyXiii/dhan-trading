#!/usr/bin/env python3
"""
auto_doc_sync.py — Autonomously keeps README.md + CLAUDE.md fresh.

Runs nightly via cron. Reads:
  - Last 10 commits (git log)
  - Current file index (all .py files in repo)
  - Recent feature changes from data/experiment_history.json
  - Existing README.md + CLAUDE.md

Calls Claude API with the full state, asks for SURGICAL updates:
  - Remove stale references (deprecated files, old strategies)
  - Add new features added since last sync
  - Update Known Gotchas table with new bugs from raw discoveries
  - Keep architectural map current

Writes updated docs back, commits + pushes if changes detected.
NO credentials ever pass through (only public file content + diffs).

Cron: 23:30 IST (after model_evolver at 23:00).
Manual: python3 auto_doc_sync.py [--dry-run]
"""

import os
import sys
import json
import subprocess
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

_HERE = Path(__file__).parent
_IST  = timezone(timedelta(hours=5, minutes=30))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DRY_RUN = "--dry-run" in sys.argv

# Files to keep fresh
README_PATH    = _HERE / "README.md"
CLAUDE_MD_PATH = _HERE / "CLAUDE.md"

# Hard credential patterns — never commit these
CRED_PATTERNS = [
    r"DHAN_ACCESS_TOKEN\s*=\s*['\"]?[A-Za-z0-9._-]{20,}",
    r"TELEGRAM_BOT_TOKEN\s*=\s*['\"]?[0-9]+:[A-Za-z0-9_-]{30,}",
    r"ANTHROPIC_API_KEY\s*=\s*['\"]?sk-ant-[A-Za-z0-9_-]{30,}",
    r"sk-ant-api[0-9]+-[A-Za-z0-9_-]{50,}",   # Claude API key format
    r"sk-[A-Za-z0-9]{40,}",                    # Generic API key
    r"-----BEGIN.+PRIVATE KEY-----",
]


def _run(cmd, cwd=None):
    """Run shell command, return (rc, stdout)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd or _HERE, timeout=30)
        return r.returncode, r.stdout + r.stderr
    except Exception as e:
        return -1, str(e)


def scan_for_creds(text: str) -> list[str]:
    """Return list of matched credential patterns. Empty list = clean."""
    hits = []
    for pat in CRED_PATTERNS:
        m = re.search(pat, text)
        if m:
            hits.append(m.group(0)[:80])
    return hits


def get_recent_commits(n=10) -> str:
    rc, out = _run(f"git log --oneline -{n} --no-decorate")
    return out if rc == 0 else ""


def get_file_index() -> str:
    """One-line summary of every .py file (file: first docstring or comment)."""
    lines = []
    for f in sorted(_HERE.glob("*.py")):
        try:
            content = f.read_text()[:500]
            # Find first non-import comment or docstring
            doc = ""
            for line in content.split("\n"):
                line = line.strip()
                if not line or line.startswith("#!"):
                    continue
                if line.startswith("#") or line.startswith('"""'):
                    doc = line.lstrip("# ").lstrip('"').strip()
                    if doc:
                        break
            lines.append(f"{f.name}: {doc[:90]}")
        except Exception:
            pass
    return "\n".join(lines)


def get_recent_experiments() -> str:
    p = _HERE / "data" / "experiment_history.json"
    if not p.exists():
        return ""
    try:
        with open(p) as f:
            data = json.load(f)
        recent = data[-20:]
        lines = []
        for e in recent:
            kept = "KEPT" if e.get("kept") else "DROPPED"
            desc = e.get("description", "")[:80]
            lines.append(f"  [{e.get('date', '?')}] {kept}: {desc}")
        return "\n".join(lines)
    except Exception:
        return ""


def call_claude(prompt: str, max_tokens=8000) -> str:
    if not ANTHROPIC_API_KEY:
        print("  ANTHROPIC_API_KEY not set — skipping doc sync")
        return ""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model":      "claude-sonnet-4-5",
                "max_tokens": max_tokens,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )
        if r.status_code == 200:
            return r.json()["content"][0]["text"]
        print(f"  Claude API HTTP {r.status_code}: {r.text[:200]}")
        return ""
    except Exception as e:
        print(f"  Claude API error: {e}")
        return ""


def sync_doc(doc_path: Path, doc_role: str, context: dict) -> bool:
    """Update one doc file. Returns True if modified, False if no change."""
    if not doc_path.exists():
        print(f"  {doc_path.name}: file missing, skipping")
        return False

    current = doc_path.read_text()

    prompt = f"""You are maintaining the {doc_role} for an automated Nifty50 options trading system.

Your job: surgically update the document below to reflect the CURRENT state of the codebase.

RULES:
1. Remove any references to files/strategies/features no longer in the code.
2. Add documentation for any new features mentioned in recent commits or experiments.
3. Update tables (Known Gotchas, file index, ML feature counts) if numbers/entries changed.
4. Keep all factual content (backtest numbers, strategy verdicts, lot sizes).
5. Do NOT add new sections — only update existing ones unless commits clearly add a new system.
6. Output the FULL updated document, ready to write to disk. No commentary, no markdown wrapper.
7. If nothing meaningfully changed, output the EXACT current document unchanged.

=== CURRENT FILE INDEX (all .py files in repo) ===
{context['file_index']}

=== LAST 10 COMMITS ===
{context['commits']}

=== RECENT EXPERIMENT LOG (kept/dropped features) ===
{context['experiments']}

=== CURRENT {doc_path.name} (truncated to 60K chars) ===
{current[:60000]}

Output the full updated {doc_path.name} now:"""

    updated = call_claude(prompt, max_tokens=12000)
    if not updated:
        return False

    # Strip code-fence wrappers if Claude added them
    if updated.startswith("```"):
        updated = re.sub(r"^```\w*\n", "", updated)
        updated = re.sub(r"\n```\s*$", "", updated)

    # Sanity: must be at least 500 chars and not contain creds
    if len(updated) < 500:
        print(f"  {doc_path.name}: response too short ({len(updated)} chars) — aborting")
        return False

    creds = scan_for_creds(updated)
    if creds:
        print(f"  {doc_path.name}: BLOCKED — credentials detected in proposed update: {creds}")
        return False

    if updated.strip() == current.strip():
        print(f"  {doc_path.name}: no changes needed")
        return False

    from atomic_io import write_atomic_text
    if DRY_RUN:
        print(f"  {doc_path.name}: would update ({len(current)} → {len(updated)} chars)")
        diff_path = doc_path.with_suffix(doc_path.suffix + ".proposed")
        write_atomic_text(str(diff_path), updated)
        print(f"  Diff at: {diff_path}")
        return False

    write_atomic_text(str(doc_path), updated)
    print(f"  {doc_path.name}: updated ({len(current)} → {len(updated)} chars)")
    return True


def main():
    print(f"=== auto_doc_sync — {datetime.now(_IST).strftime('%Y-%m-%d %H:%M IST')} ===")

    # Pre-flight: scan repo for stray creds (excluding .env which is .gitignored).
    # ABORT on any hit — previously only WARNED and proceeded to commit + push.
    # If real credentials had leaked into a tracked file, the run would have
    # pushed them to the remote.
    rc, out = _run("git ls-files | xargs grep -lE 'DHAN_ACCESS_TOKEN|TELEGRAM_BOT_TOKEN|sk-ant-' 2>/dev/null")
    if out.strip():
        print(f"  ❌ ABORT: credentials present in tracked files: {out[:300]}")
        print(f"  Remove them from the listed files (use git filter-branch / BFG) "
              f"and re-run. auto_doc_sync will NOT push while creds are tracked.")
        sys.exit(1)

    context = {
        "commits":     get_recent_commits(10),
        "file_index":  get_file_index(),
        "experiments": get_recent_experiments(),
    }

    changed = False
    changed |= sync_doc(README_PATH,    "user-facing README.md",                              context)
    changed |= sync_doc(CLAUDE_MD_PATH, "developer-facing CLAUDE.md (architectural map)",     context)

    if changed and not DRY_RUN:
        print("\n  Committing doc updates...")
        _run("git add README.md CLAUDE.md")
        rc, out = _run('git commit -m "docs: auto-sync README + CLAUDE.md to current state\n\nAutomated by auto_doc_sync.py — reflects last 10 commits + current file index.\n"')
        if rc == 0:
            print("  Committed. Pushing...")
            rc, out = _run("git push origin HEAD")
            if rc == 0:
                print("  Pushed to remote.")
            else:
                print(f"  Push failed: {out[:200]}")
        else:
            print(f"  Commit failed (or no changes): {out[:200]}")
    elif not changed:
        print("\n  No doc changes — repo already in sync.")


if __name__ == "__main__":
    main()
