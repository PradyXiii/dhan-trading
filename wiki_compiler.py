#!/usr/bin/env python3
"""
wiki_compiler.py — Karpathy LLM Wiki pattern for the NF trading system.

Reads raw discovery files from docs/wiki/raw/, calls Claude API to compile
them into structured wiki articles, then updates index.md and log.md.

Usage:
    python3 wiki_compiler.py              # compile all unprocessed raw files
    python3 wiki_compiler.py --dry-run    # show what would be compiled, no API calls
    python3 wiki_compiler.py --lint       # check for orphan pages, missing cross-links
    python3 wiki_compiler.py --rebuild    # force recompile from scratch (slow)
"""

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_HERE       = Path(__file__).parent.resolve()
_WIKI       = _HERE / "docs" / "wiki"
_RAW        = _WIKI / "raw"
_PROCESSED  = _RAW / "processed"
_INDEX      = _WIKI / "index.md"
_LOG        = _WIKI / "log.md"
_IST        = timezone(timedelta(hours=5, minutes=30))

MODEL       = "claude-opus-4-6"
MAX_TOKENS  = 4096

WIKI_PAGES = {
    "strategy/ic_research.md":      "NF IC + Bull Put strategy research, backtest results, discarded strategies",
    "features/feature_history.md":  "ML feature experiments — kept, discarded, reserved names, checklist",
    "bugs/known_issues.md":         "Session-discovered bugs — ML shadows, API, lot sizing, routing",
}


def _today_ist() -> str:
    return datetime.now(_IST).date().isoformat()


def _get_unprocessed_raw() -> list[Path]:
    """Return raw files that haven't been processed yet."""
    if not _RAW.exists():
        return []
    processed_names = {p.name for p in _PROCESSED.iterdir()} if _PROCESSED.exists() else set()
    files = []
    for f in sorted(_RAW.iterdir()):
        if f.is_file() and f.suffix in (".txt", ".md") and f.name not in processed_names:
            files.append(f)
    return files


def _read_wiki_page(rel_path: str) -> str:
    p = _WIKI / rel_path
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _write_wiki_page(rel_path: str, content: str):
    p = _WIKI / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _append_log(entry: str):
    today = _today_ist()
    line = f"\n## [{today}] {entry}\n"
    with open(_LOG, "a", encoding="utf-8") as f:
        f.write(line)


def _mark_processed(raw_file: Path):
    _PROCESSED.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.move(str(raw_file), str(_PROCESSED / raw_file.name))


def _build_compile_prompt(raw_content: str, raw_filename: str) -> tuple[str, str]:
    """Return (system_prompt, user_message) for wiki compilation."""
    pages_context = ""
    for rel_path, desc in WIKI_PAGES.items():
        content = _read_wiki_page(rel_path)
        if content:
            pages_context += f"\n\n### {rel_path} ({desc})\n```\n{content[:3000]}\n```"

    system = (
        "You are a knowledge base compiler for an automated Nifty50 options trading system. "
        "Your job: read raw discovery text and update the relevant wiki articles.\n\n"
        "Rules:\n"
        "1. Return ONLY a JSON object — no markdown fences, no explanation outside JSON.\n"
        "2. Each key = wiki page path relative to docs/wiki/ (e.g. 'bugs/known_issues.md').\n"
        "3. Each value = the COMPLETE new content for that page (not a diff — full replacement).\n"
        "4. Only include pages that actually need updating based on the raw content.\n"
        "5. Preserve all existing entries — never delete existing knowledge.\n"
        "6. Add new entries in the correct section.\n"
        "7. Keep [[wikilink]] cross-reference format.\n"
        "8. 'Last updated' date should be today: " + _today_ist() + "\n"
    )

    user = (
        f"### Raw discovery file: {raw_filename}\n\n"
        f"```\n{raw_content}\n```\n\n"
        f"### Current wiki pages:\n{pages_context}\n\n"
        "Which pages need updating? Return JSON: {\"path/to/page.md\": \"full updated content\", ...}"
    )

    return system, user


def _call_claude(system: str, user: str) -> dict | None:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        text = response.content[0].text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        import json
        return json.loads(text)
    except Exception as e:
        print(f"Claude API error: {e}")
        return None


def _lint(deep: bool = False):
    """Check for orphan pages, broken links, and (if deep=True) contradictions via Claude."""
    import re
    print("Linting wiki...")
    issues = []
    all_pages = list(_WIKI.rglob("*.md"))
    all_page_stems = {p.stem for p in all_pages}

    for page in all_pages:
        content = page.read_text(encoding="utf-8")
        links = re.findall(r"\[\[([^\]]+)\]\]", content)
        for link in links:
            link_stem = Path(link).stem
            if link_stem not in all_page_stems:
                issues.append(f"  {page.relative_to(_WIKI)}: broken link [[{link}]]")

    index_content = _INDEX.read_text(encoding="utf-8") if _INDEX.exists() else ""
    for page in all_pages:
        if page.name in ("index.md", "log.md"):
            continue
        rel = str(page.relative_to(_WIKI)).replace("\\", "/")
        if page.stem not in index_content and rel not in index_content:
            issues.append(f"  Orphan page (not in index): {rel}")

    if issues:
        print(f"Structural issues ({len(issues)}):")
        for i in issues:
            print(i)
    else:
        print("No structural issues found.")

    # Deep lint: Claude checks for contradictions across pages
    if deep:
        print("\nDeep lint: checking for contradictions...")
        all_content = ""
        for page in all_pages:
            if page.name in ("log.md",):
                continue
            rel = str(page.relative_to(_WIKI)).replace("\\", "/")
            all_content += f"\n\n### {rel}\n{page.read_text(encoding='utf-8')[:2000]}"

        system = (
            "You are auditing a trading system knowledge base for contradictions. "
            "Find claims that directly contradict each other across different pages. "
            "A contradiction = two statements that cannot both be true. "
            "Return a numbered list. If none found, return 'No contradictions found.'"
        )
        user = f"Review these wiki pages for contradictions:\n{all_content}"
        result = _call_claude(system, user)
        if result:
            print("Contradiction check result:")
            print(result)
        else:
            # _call_claude returns dict for compile, but here we want raw text
            print("  (Claude API unavailable for deep lint)")


def _query(question: str, save: bool = True):
    """Answer a question using wiki knowledge + optionally save as new wiki page."""
    print(f"Querying wiki: {question}")
    all_content = ""
    for page in _WIKI.rglob("*.md"):
        if page.name == "log.md":
            continue
        rel = str(page.relative_to(_WIKI)).replace("\\", "/")
        all_content += f"\n\n### {rel}\n{page.read_text(encoding='utf-8')[:3000]}"

    system = (
        "You are a knowledge base for an automated Nifty50 options trading system. "
        "Answer the question using ONLY information from the wiki pages provided. "
        "If the information is not in the wiki, say so explicitly. "
        "Be specific: cite which page contains the relevant information. "
        "Format: direct answer, then 'Source: [[page]]'."
    )
    user = f"Wiki pages:\n{all_content}\n\nQuestion: {question}"

    # Use raw text call for query (not JSON)
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        answer = response.content[0].text.strip()
        print(f"\nAnswer:\n{answer}\n")

        if save:
            # Save valuable query results as new wiki pages
            slug = question.lower()[:40].replace(" ", "_").replace("?", "")
            slug = "".join(c for c in slug if c.isalnum() or c == "_")
            page_path = f"queries/{slug}.md"
            content = (
                f"# Query: {question}\n\n"
                f"**Asked:** {_today_ist()}\n\n"
                f"## Answer\n\n{answer}\n\n"
                f"## Related pages\n*(add links manually)*\n"
            )
            _write_wiki_page(page_path, content)
            _append_log(f"query | '{question[:60]}' → saved as {page_path}")
            print(f"  Answer saved to: docs/wiki/{page_path}")
    except Exception as e:
        print(f"Query failed: {e}")


def _refresh_research_program():
    """
    Update research_program_nf.md with recently tried ideas from experiment history.
    Reads data/experiment_history.json, extracts last 60 experiments, asks Claude
    to add a 'Recently tried' section marking what's been explored.
    """
    import json
    exp_history = _HERE / "data" / "experiment_history.json"
    research_prog = _HERE / "research_program_nf.md"

    if not exp_history.exists() or not research_prog.exists():
        print("  Skipping research program refresh (files not found).")
        return

    history = json.loads(exp_history.read_text())
    if not history:
        return

    recent = history[-60:]
    kept = [e for e in recent if e["kept"]]
    discarded = [e for e in recent if not e["kept"]]

    kept_lines = "\n".join(f"  ✅ {e['date']}: {e['description']}" for e in kept[-20:])
    disc_lines = "\n".join(f"  ❌ {e['date']}: {e['description']}" for e in discarded[-20:])

    current_prog = research_prog.read_text(encoding="utf-8")

    # Check if recently-tried section already exists
    if "## Recently tried" in current_prog:
        # Replace the section
        import re
        new_section = (
            f"## Recently tried (auto-updated {_today_ist()}, last {len(recent)} experiments)\n\n"
            f"### Kept ({len(kept)} experiments improved composite):\n{kept_lines or '  (none yet)'}\n\n"
            f"### Discarded ({len(discarded)} experiments didn't help):\n{disc_lines or '  (none yet)'}\n"
        )
        updated = re.sub(
            r"## Recently tried.*?(?=\n## |\Z)", new_section, current_prog, flags=re.DOTALL
        )
    else:
        # Append new section
        updated = current_prog + (
            f"\n\n---\n\n## Recently tried (auto-updated {_today_ist()}, last {len(recent)} experiments)\n\n"
            f"### Kept ({len(kept)} experiments improved composite):\n{kept_lines or '  (none yet)'}\n\n"
            f"### Discarded ({len(discarded)} experiments didn't help):\n{disc_lines or '  (none yet)'}\n"
        )

    research_prog.write_text(updated, encoding="utf-8")
    print(f"  research_program_nf.md refreshed: {len(kept)} kept, {len(discarded)} discarded in last {len(recent)} experiments.")


def compile_raw_files(dry_run: bool = False):
    """Main compile loop — process all unprocessed raw files."""
    raw_files = _get_unprocessed_raw()
    if not raw_files:
        print("No unprocessed raw files found.")
        return

    print(f"Found {len(raw_files)} raw file(s) to compile.")

    for raw_file in raw_files:
        print(f"\nCompiling: {raw_file.name}")
        raw_content = raw_file.read_text(encoding="utf-8")

        if dry_run:
            print(f"  [DRY RUN] Would compile {len(raw_content)} chars → wiki")
            print(f"  First 200 chars: {raw_content[:200]}")
            continue

        system, user = _build_compile_prompt(raw_content, raw_file.name)
        updates = _call_claude(system, user)

        if not updates:
            print(f"  No updates returned — skipping {raw_file.name}")
            continue

        for page_path, new_content in updates.items():
            _write_wiki_page(page_path, new_content)
            print(f"  Updated: {page_path}")

        _mark_processed(raw_file)
        _append_log(f"compile | {raw_file.name} → {len(updates)} pages updated: {', '.join(updates.keys())}")
        print(f"  Processed and moved to raw/processed/")


def rebuild_all():
    """Force recompile all wiki pages from scratch using existing raw sources."""
    print("Rebuild mode: compiling from all sources in docs/ and CLAUDE.md...")
    # Read key source files
    sources = {}
    for src_path in [
        _HERE / "CLAUDE.md",
        _HERE / "STRATEGY_RESEARCH.md",
        _HERE / "research_program_nf.md",
    ]:
        if src_path.exists():
            sources[src_path.name] = src_path.read_text(encoding="utf-8")[:8000]

    if not sources:
        print("No source files found.")
        return

    combined = "\n\n---\n\n".join(
        f"### {name}\n{content}" for name, content in sources.items()
    )

    # Write to a temporary raw file and compile
    tmp = _RAW / f"{_today_ist()}_rebuild.txt"
    _RAW.mkdir(parents=True, exist_ok=True)
    tmp.write_text(f"FULL REBUILD — compiled from: {', '.join(sources.keys())}\n\n{combined}")
    print(f"Written temp raw file: {tmp.name}")
    compile_raw_files(dry_run=False)


def main():
    parser = argparse.ArgumentParser(description="Compile raw discoveries into wiki")
    parser.add_argument("--dry-run",  action="store_true", help="Show what would compile, no API calls")
    parser.add_argument("--lint",     action="store_true", help="Check for broken links and orphan pages")
    parser.add_argument("--lint-deep",action="store_true", help="--lint + Claude contradiction check")
    parser.add_argument("--rebuild",  action="store_true", help="Recompile from scratch")
    parser.add_argument("--query",    type=str, default="", help="Answer a question from wiki knowledge")
    parser.add_argument("--no-save",  action="store_true", help="With --query: don't save answer as page")
    args = parser.parse_args()

    if args.query:
        _query(args.query, save=not args.no_save)
    elif args.lint or args.lint_deep:
        _lint(deep=args.lint_deep)
    elif args.rebuild:
        rebuild_all()
        _refresh_research_program()
    else:
        compile_raw_files(dry_run=args.dry_run)
        if not args.dry_run:
            _refresh_research_program()


if __name__ == "__main__":
    main()
