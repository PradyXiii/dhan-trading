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

    # ── 2. Discarded/failed experiments from log file ─────────────────────────
    # Parse each experiment block individually: find Proposal line then outcome.
    discarded = []
    if log_path.exists():
        from datetime import datetime
        text = log_path.read_text(encoding="utf-8", errors="replace")

        # Split into per-session chunks on the header separator
        sessions = re.split(r"={58,}", text)
        for session in sessions:
            # Get session date from header e.g. "18 Apr 2026"
            date_m = re.search(r"(\d{1,2} \w{3} \d{4})", session)
            date_str = ""
            if date_m:
                try:
                    date_str = datetime.strptime(date_m.group(1), "%d %b %Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass

            # Split session into per-experiment chunks on "[Exp N/..."
            exp_blocks = re.split(r"\[Exp \d+/\d+\]", session)
            for exp in exp_blocks[1:]:  # skip preamble before first [Exp
                desc_m = re.search(r"Proposal:\s*\[[\w_.]+\]\s*(.+?)\n", exp)
                if not desc_m:
                    continue
                desc = desc_m.group(1).strip()[:200]

                # Outcome: DISCARDED
                d_m = re.search(
                    r"DISCARDED \(composite ([\d.]+) not > ([\d.]+)",
                    exp
                )
                if d_m:
                    discarded.append({
                        "date": date_str,
                        "description": desc,
                        "kept": False,
                        "before": float(d_m.group(2)),
                        "after": float(d_m.group(1)),
                    })
                    continue

                # Outcome: crashed / pre-flight / apply failed (also discard)
                if re.search(r"Pre-flight:|Apply failed:|Experiment failed:|incomplete change", exp):
                    score_m = re.search(r"Score: ([\d.]+) .*?([\d.]+)", exp)
                    before = float(score_m.group(1)) if score_m else 0.0
                    discarded.append({
                        "date": date_str,
                        "description": desc + " [CRASHED/FAILED]",
                        "kept": False,
                        "before": before,
                        "after": before,
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
    kept.reverse()
    print(f"  Kept from git log (all branches): {len(kept)}")

    discarded = []
    if log_path.exists():
        from datetime import datetime
        text = log_path.read_text(encoding="utf-8", errors="replace")
        sessions = re.split(r"={58,}", text)
        for session in sessions:
            date_m = re.search(r"(\d{1,2} \w{3} \d{4})", session)
            date_str = ""
            if date_m:
                try:
                    date_str = datetime.strptime(date_m.group(1), "%d %b %Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass
            exp_blocks = re.split(r"\[Exp \d+/\d+\]", session)
            for exp in exp_blocks[1:]:
                desc_m = re.search(r"Proposal:\s*\[[\w_.]+\]\s*(.+?)\n", exp)
                if not desc_m:
                    continue
                desc = desc_m.group(1).strip()[:200]
                d_m = re.search(r"DISCARDED \(composite ([\d.]+) not > ([\d.]+)", exp)
                if d_m:
                    discarded.append({
                        "date": date_str, "description": desc, "kept": False,
                        "before": float(d_m.group(2)), "after": float(d_m.group(1)),
                    })
                elif re.search(r"Pre-flight:|Apply failed:|Experiment failed:|incomplete change", exp):
                    score_m = re.search(r"Score: ([\d.]+)", exp)
                    before = float(score_m.group(1)) if score_m else 0.0
                    discarded.append({
                        "date": date_str, "description": desc + " [CRASHED/FAILED]",
                        "kept": False, "before": before, "after": before,
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
