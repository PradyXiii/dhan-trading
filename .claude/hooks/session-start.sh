#!/bin/bash
# Auto-pull latest CLAUDE.md, rules, and gotchas from repo on every new session.
# This keeps the "Known Gotchas", "ML Feature Rule", and "Bug Fix Rule" sections
# in CLAUDE.md up-to-date without any manual action.

PROJ="${CLAUDE_PROJECT_DIR:-.}"

# Pull latest from current branch (silent — don't spam the session with git output)
BRANCH=$(git -C "$PROJ" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
git -C "$PROJ" pull origin "$BRANCH" --quiet 2>/dev/null

# Build a brief context note for Claude — branch + last 3 commits
LOG=$(git -C "$PROJ" log --oneline -3 2>/dev/null || echo "no git log available")
SCORE=""
if [ -f "$PROJ/data/paper_performance.csv" ]; then
  SCORE=" | last composite: $(tail -1 "$PROJ/data/paper_performance.csv" | cut -d',' -f3 2>/dev/null)"
fi

MSG="BN repo auto-pulled | branch: $BRANCH$SCORE | recent commits: $LOG"

# Output JSON so Claude receives this as additional context at session start
printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}' \
  "$(echo "$MSG" | sed 's/"/\\"/g' | tr '\n' ' ')"
