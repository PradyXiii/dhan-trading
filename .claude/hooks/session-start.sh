#!/bin/bash
# Auto-pull latest CLAUDE.md, rules, and gotchas from repo on every new session.

PROJ="${CLAUDE_PROJECT_DIR:-.}"
BRANCH="nifty-strategies"

# Ensure we're on the right branch, then pull
git -C "$PROJ" checkout "$BRANCH" --quiet 2>/dev/null
git -C "$PROJ" pull origin "$BRANCH" --quiet 2>/dev/null

LOG=$(git -C "$PROJ" log --oneline -3 2>/dev/null || echo "no git log available")
SCORE=""
if [ -f "$PROJ/data/paper_performance.csv" ]; then
  SCORE=" | last composite: $(tail -1 "$PROJ/data/paper_performance.csv" | cut -d',' -f3 2>/dev/null)"
fi

MSG="NF IC repo auto-pulled | branch: $BRANCH$SCORE | recent commits: $LOG"

printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}' \
  "$(echo "$MSG" | sed 's/"/\\"/g' | tr '\n' ' ')"
