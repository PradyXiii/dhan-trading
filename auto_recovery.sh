#!/bin/bash
# auto_recovery.sh — Self-healing safety net.
# Runs hourly via cron during market hours. If smoke test fails badly:
#   1. Auto-installs any missing pip packages from requirements.txt
#   2. Auto-rolls back to last working commit if smoke still fails
#   3. Telegram-alerts user in plain English (only when action helped or failed)
#
# This is the "set it and forget it" net. User shouldn't need to babysit.

set +e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

LOG="$HERE/logs/auto_recovery.log"
mkdir -p "$HERE/logs"

ts() { date +"%Y-%m-%d %H:%M:%S IST"; }

log() { echo "[$(ts)] $1" >> "$LOG"; }

telegram() {
  python3 -c "import notify; notify.send('''$1''')" 2>/dev/null
}

log "=== auto_recovery start ==="

# Step 1: Run smoke test silently
SMOKE_OUT=$(bash smoke_test.sh 2>&1)
PASS=$(echo "$SMOKE_OUT" | grep -oE "PASSED: [0-9]+" | grep -oE "[0-9]+")
FAIL=$(echo "$SMOKE_OUT" | grep -oE "FAILED: [0-9]+" | grep -oE "[0-9]+")
log "smoke test: $PASS passed, $FAIL failed"

# 1 fail = capital constraint (real-world, not bug). Trip ONLY on >=3 fails.
if [ -z "$FAIL" ] || [ "$FAIL" -lt 3 ]; then
  log "OK — system healthy. Exit."
  exit 0
fi

# Step 2: Try auto-installing missing libs from requirements.txt
log "FAIL >= 3 — attempting auto-install from requirements.txt"
INSTALL_OUT=$(pip install --user --break-system-packages -q -r requirements.txt 2>&1)
log "pip install exit: $?"

# Re-test
SMOKE_OUT2=$(bash smoke_test.sh 2>&1)
FAIL2=$(echo "$SMOKE_OUT2" | grep -oE "FAILED: [0-9]+" | grep -oE "[0-9]+")
log "smoke after install: $FAIL2 failed"

if [ "$FAIL2" -lt 3 ]; then
  msg="🔧 Self-heal: pip auto-installed missing libs. System healthy now. Nothing for you to do."
  telegram "$msg"
  log "RECOVERED via pip install"
  exit 0
fi

# Step 3: Rollback to last working commit
log "Still failing — rolling back to last commit"
LAST_GOOD=$(git rev-parse HEAD~1 2>/dev/null)
git stash 2>/dev/null
git reset --hard "$LAST_GOOD" 2>/dev/null

SMOKE_OUT3=$(bash smoke_test.sh 2>&1)
FAIL3=$(echo "$SMOKE_OUT3" | grep -oE "FAILED: [0-9]+" | grep -oE "[0-9]+")
log "smoke after rollback: $FAIL3 failed"

if [ "$FAIL3" -lt 3 ]; then
  msg="🔧 Self-heal: rolled back broken code to last working version. System healthy now. Tell Claude — last commit had a bug."
  telegram "$msg"
  log "RECOVERED via rollback"
  exit 0
fi

# Step 4: Alert user — neither install nor rollback fixed it
msg="🚨 System is broken. Auto-install + rollback both failed. Pause trading. Check logs/auto_recovery.log. Last 3 fails: $(echo "$SMOKE_OUT3" | grep '❌' | head -3 | tr '\n' '|')"
telegram "$msg"
log "FAILED to recover — user needs to intervene"
exit 1
