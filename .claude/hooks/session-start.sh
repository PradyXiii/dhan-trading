#!/bin/bash
# Auto-pull latest CLAUDE.md, rules, and gotchas from repo on every new session.
# Also injects wiki index into session context for full knowledge map on startup.

PROJ="${CLAUDE_PROJECT_DIR:-.}"
BRANCH="nifty-strategies"

# Refuse to checkout/pull when the working tree has uncommitted changes —
# previously could overwrite half-edited files when a session opened mid-edit.
DIRTY=$(git -C "$PROJ" status --porcelain 2>/dev/null)
if [ -n "$DIRTY" ]; then
  STATUS_NOTE=" | dirty tree detected — pull SKIPPED ($(echo "$DIRTY" | wc -l) modified files)"
else
  git -C "$PROJ" checkout "$BRANCH" --quiet 2>/dev/null
  git -C "$PROJ" pull origin "$BRANCH" --quiet 2>/dev/null
  STATUS_NOTE=""
fi

LOG=$(git -C "$PROJ" log --oneline -3 2>/dev/null || echo "no git log available")
SCORE=""
if [ -f "$PROJ/data/paper_performance.csv" ]; then
  SCORE=" | last composite: $(tail -1 "$PROJ/data/paper_performance.csv" | cut -d',' -f3 2>/dev/null)"
fi

# Wiki index — inject top-level knowledge map into every session
WIKI_CTX=""
if [ -f "$PROJ/docs/wiki/index.md" ]; then
  WIKI_CTX=" | WIKI KNOWLEDGE MAP: $(cat "$PROJ/docs/wiki/index.md" | head -40 | tr '\n' ' ' | sed 's/  */ /g')"
  if [ -f "$PROJ/docs/wiki/log.md" ]; then
    LAST_LOG=$(grep "^## \[" "$PROJ/docs/wiki/log.md" | tail -1)
    WIKI_CTX="$WIKI_CTX | Last wiki compile: $LAST_LOG"
  fi
fi

MSG="NF IC repo auto-pulled | branch: $BRANCH$STATUS_NOTE$SCORE$WIKI_CTX | recent commits: $LOG"
printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}' \
  "$(echo "$MSG" | sed 's/"/\\"/g' | tr '\n' ' ')"
