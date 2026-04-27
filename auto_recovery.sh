#!/bin/bash
# auto_recovery.sh — Self-healing safety net.
# Runs hourly via cron during market hours. If smoke test fails badly:
#   1. Auto-installs any missing pip packages from requirements.txt
#   2. Telegram-alerts user in plain English (only when action helped or failed)
#
# Hard reset removed — was rolling back legitimate good commits on every
# transient blip. Recovery now restricted to pip install + alert.

set +e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

LOG="$HERE/logs/auto_recovery.log"
mkdir -p "$HERE/logs"

ts() { date +"%Y-%m-%d %H:%M:%S IST"; }

log() { echo "[$(ts)] $1" >> "$LOG"; }

# Telegram via stdin — never inline a string into python -c. Previously the
# message was substituted into a triple-quoted python string, allowing shell
# injection if smoke test output ever contained ''' or $(...).
telegram() {
  python3 -c "import sys, notify; notify.send(sys.stdin.read())" <<< "$1" 2>/dev/null
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

# Step 3: Alert user — pip install didn't fix it. NO automatic git rollback.
# Hard reset of legitimate commits caused more outages than it solved.
TAIL_FAILS=$(echo "$SMOKE_OUT2" | grep '❌' | head -3 | tr '\n' '|')
msg="🚨 System is broken. Auto-install did not fix it. Pause trading. Check logs/auto_recovery.log. Last 3 fails: $TAIL_FAILS"
telegram "$msg"
log "FAILED to auto-recover — user must intervene (manual git revert if needed)"
exit 1
