"""
One-time backfill of data/experiment_history.json from git log + autoloop log.

Kept experiments:   parsed from git commit messages ("autoloop paper/live exp N: ...")
Discarded experiments: parsed from logs/autoloop_bn.log

Run once:  python3 backfill_experiment_history.py
Then:      python3 backfill_experiment_history.py --mcx  (for MCX history)
"""
import argparse
import json
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).parent


def backfill_bn():
    out_path = HERE / "data" / "experiment_history.json"
    log_path = HERE / "logs" / "autoloop_bn.log"
    history = []

    # ── 1. Kept experiments from git log ──────────────────────────────────────
    result = subprocess.run(
        ["git", "log", "--pretty=format:%ad %s", "--date=short"],
        capture_output=True, text=True, cwd=HERE
    )
    kept_pat = re.compile(
        r"(\d{4}-\d{2}-\d{2}) autoloop (?:paper|live) exp \d+: (.+?) \((\d+\.\d+)→(\d+\.\d+)\)"
    )
    kept = []
    for line in result.stdout.splitlines():
        m = kept_pat.match(line)
        if m:
            kept.append({
                "date": m.group(1),
                "description": m.group(2).strip(),
                "kept": True,
                "before": float(m.group(3)),
                "after": float(m.group(4)),
            })
    kept.reverse()  # oldest first
    print(f"  Kept from git log:       {len(kept)}")

    # ── 2. Discarded experiments from log file ─────────────────────────────────
    discarded = []
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        # Pattern: date line followed by proposal line followed by discard line
        # [HH:MM:SS IST] ❌ Idea #N...
        # Lines just above: "  Proposal: [file] description"
        # And score line:   "  Score: X → Y"
        blocks = re.split(r"={60,}", text)
        for block in blocks:
            proposals = re.findall(
                r"Proposal:\s*\[[\w_.]+\]\s*(.+?)\n",
                block
            )
            discards = re.findall(
                r"❌ DISCARDED \(composite (\d+\.\d+) not > (\d+\.\d+)",
                block
            )
            # Find the date of this block
            date_m = re.search(r"(\d{2} \w{3} \d{4})", block)
            date_str = ""
            if date_m:
                from datetime import datetime
                try:
                    date_str = datetime.strptime(date_m.group(1), "%d %b %Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass

            for desc, (after, before) in zip(proposals, discards):
                discarded.append({
                    "date": date_str,
                    "description": desc.strip()[:200],
                    "kept": False,
                    "before": float(before),
                    "after": float(after),
                })
    print(f"  Discarded from log file: {len(discarded)}")

    # ── 3. Merge: sort by date, deduplicate by description ─────────────────────
    all_entries = kept + discarded
    seen = set()
    merged = []
    for e in sorted(all_entries, key=lambda x: x["date"]):
        key = e["description"][:80]
        if key not in seen:
            seen.add(key)
            merged.append(e)

    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(merged, indent=2))
    print(f"  Written {len(merged)} entries → {out_path}")
    return merged


def backfill_mcx():
    out_path = HERE / "data" / "experiment_history_mcx.json"
    log_path = HERE / "logs" / "autoloop.log"
    history = []

    result = subprocess.run(
        ["git", "log", "--all", "--pretty=format:%ad %s", "--date=short"],
        capture_output=True, text=True, cwd=HERE
    )
    kept_pat = re.compile(
        r"(\d{4}-\d{2}-\d{2}) autoloop (?:paper|live) exp \d+: (.+?) \((\d+\.\d+)→(\d+\.\d+)\)"
    )
    kept = []
    for line in result.stdout.splitlines():
        m = kept_pat.match(line)
        if m:
            kept.append({
                "date": m.group(1),
                "description": m.group(2).strip(),
                "kept": True,
                "before": float(m.group(3)),
                "after": float(m.group(4)),
            })
    kept.reverse()
    print(f"  Kept from git log (all branches): {len(kept)}")

    discarded = []
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        blocks = re.split(r"={60,}", text)
        for block in blocks:
            proposals = re.findall(r"Proposal:\s*\[[\w_.]+\]\s*(.+?)\n", block)
            discards = re.findall(
                r"❌ DISCARDED \(composite (\d+\.\d+) not > (\d+\.\d+)", block
            )
            date_m = re.search(r"(\d{2} \w{3} \d{4})", block)
            date_str = ""
            if date_m:
                from datetime import datetime
                try:
                    date_str = datetime.strptime(date_m.group(1), "%d %b %Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass
            for desc, (after, before) in zip(proposals, discards):
                discarded.append({
                    "date": date_str,
                    "description": desc.strip()[:200],
                    "kept": False,
                    "before": float(before),
                    "after": float(after),
                })
    print(f"  Discarded from log file: {len(discarded)}")

    all_entries = kept + discarded
    seen = set()
    merged = []
    for e in sorted(all_entries, key=lambda x: x["date"]):
        key = e["description"][:80]
        if key not in seen:
            seen.add(key)
            merged.append(e)

    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(merged, indent=2))
    print(f"  Written {len(merged)} entries → {out_path}")
    return merged


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcx", action="store_true", help="Backfill MCX history")
    args = parser.parse_args()

    if args.mcx:
        print("Backfilling MCX experiment history...")
        entries = backfill_mcx()
    else:
        print("Backfilling BankNifty experiment history...")
        entries = backfill_bn()

    print(f"\nSample (last 5):")
    for e in entries[-5:]:
        status = "✅ KEPT   " if e["kept"] else "❌ DISCARD"
        print(f"  {e['date']}  {status}  {e['before']:.4f}→{e['after']:.4f}  {e['description'][:80]}")
