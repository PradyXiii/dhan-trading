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
MAX_TOKENS  = 32768   # Claude Opus 4.6 max output. Full wiki rewrites can be big

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
        "1. Return ONLY a JSON object — no markdown fences, no prose before or after.\n"
        "2. Each key = wiki page path relative to docs/wiki/ (e.g. 'bugs/known_issues.md').\n"
        "3. Each value = COMPLETE new content for that page (full replacement, not a diff).\n"
        "4. Only include pages that actually need updating based on raw content.\n"
        "5. If no updates needed, return exactly {} (empty object).\n"
        "6. Preserve all existing entries — never delete existing knowledge.\n"
        "6a. Do NOT rewrite pages purely to reformat — only update when raw content\n"
        "    adds new facts, bugs, or experiments not already in the page.\n"
        "6b. Aim for minimum viable update — append new rows/entries to existing\n"
        "    tables/sections rather than restructuring. Prefer SMALL delta.\n"
        "7. Keep [[wikilink]] cross-reference format.\n"
        "8. 'Last updated' date should be today: " + _today_ist() + "\n"
        "9. CRITICAL JSON escaping: inside string values, every newline must be \\n,\n"
        "   every double-quote must be \\\", every backslash must be \\\\. Markdown\n"
        "   tables, code blocks, bullet lists all need proper \\n escaping. The output\n"
        "   must parse via json.loads() without modification.\n"
    )

    user = (
        f"### Raw discovery file: {raw_filename}\n\n"
        f"```\n{raw_content}\n```\n\n"
        f"### Current wiki pages:\n{pages_context}\n\n"
        "Return JSON: {\"path/to/page.md\": \"full updated content with \\\\n escaped\"} "
        "or {} if no updates needed."
    )

    return system, user


def _call_claude(system: str, user: str, raw: bool = False) -> dict | str | None:
    """Call Claude API. raw=True returns plain text; raw=False parses as JSON dict."""
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
        stop_reason = getattr(response, "stop_reason", "?")
        usage = getattr(response, "usage", None)
        out_tokens = getattr(usage, "output_tokens", "?") if usage else "?"

        if raw:
            return text
        # Strip markdown fences then parse JSON
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        if not text:
            print("  Empty Claude response — nothing to update.")
            return {}

        # Always save raw response for debugging (overwrite per run)
        dump_path = _HERE / "data" / f"wiki_compile_raw_{_today_ist()}.txt"
        dump_path.parent.mkdir(exist_ok=True)
        dump_path.write_text(
            f"=== stop_reason: {stop_reason} | output_tokens: {out_tokens} | chars: {len(text)} ===\n\n{text}",
            encoding="utf-8",
        )

        # If Claude hit max_tokens, the response is truncated — no point parsing
        if stop_reason == "max_tokens":
            print(f"  Claude hit max_tokens ({out_tokens}). Response truncated. Saved → {dump_path}")
            print(f"  Fix: raise MAX_TOKENS in wiki_compiler.py or simplify raw discovery file.")
            return None

        # Extract biggest {...} block (tolerates prose before/after JSON)
        first = text.find("{")
        last  = text.rfind("}")
        if first == -1 or last == -1 or last <= first:
            print(f"  No JSON object in response (stop={stop_reason}, out_tokens={out_tokens}). Saved → {dump_path}")
            return None
        text = text[first:last + 1]
        import json
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            print(f"  JSON parse failed ({e}). Raw response saved → {dump_path}")
            return None
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
        result = _call_claude(system, user, raw=True)
        if result:
            print("Contradiction check result:")
            print(result)
        else:
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

    if not all_content.strip():
        print("  Wiki is empty — run wiki_compiler.py first to compile raw discoveries.")
        return

    system = (
        "You are a knowledge base for an automated Nifty50 options trading system. "
        "Answer the question using ONLY information from the wiki pages provided. "
        "If the information is not in the wiki, say so explicitly. "
        "Be specific: cite which page contains the relevant information. "
        "Format: direct answer, then 'Source: [[page]]'."
    )
    user = f"Wiki pages:\n{all_content}\n\nQuestion: {question}"

    answer = _call_claude(system, user, raw=True)
    if not answer:
        return
    print(f"\nAnswer:\n{answer}\n")

    if save:
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

    try:
        history = json.loads(exp_history.read_text())
    except (json.JSONDecodeError, ValueError):
        print("  Skipping research program refresh (experiment_history.json malformed).")
        return
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
