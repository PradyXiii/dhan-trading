#!/bin/bash
# setup_automation.sh — One-time setup for BankNifty Auto Trader cron job
# Run this once on your GCP VM:  bash setup_automation.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"

echo "════════════════════════════════════════════════"
echo "  BankNifty Auto Trader — Setup"
echo "════════════════════════════════════════════════"
echo ""

# ── 1. Create logs directory ──────────────────────────────────────────────────
echo "[1] Creating logs directory..."
mkdir -p "$LOG_DIR"
echo "    Created: $LOG_DIR"

# ── 2. Check Python dependencies ─────────────────────────────────────────────
echo ""
echo "[2] Checking Python dependencies..."
python3 -c "import dhanhq"    2>/dev/null && echo "    dhanhq        ✓" || { echo "    dhanhq        ✗  (run: pip3 install dhanhq --break-system-packages)"; }
python3 -c "import pandas"    2>/dev/null && echo "    pandas        ✓" || { echo "    pandas        ✗  (run: pip3 install pandas --break-system-packages)"; }
python3 -c "import numpy"     2>/dev/null && echo "    numpy         ✓" || { echo "    numpy         ✗  (run: pip3 install numpy --break-system-packages)"; }
python3 -c "import requests"  2>/dev/null && echo "    requests      ✓" || { echo "    requests      ✗  (run: pip3 install requests --break-system-packages)"; }
python3 -c "import dotenv"    2>/dev/null && echo "    python-dotenv ✓" || { echo "    python-dotenv ✗  (run: pip3 install python-dotenv --break-system-packages)"; }
python3 -c "import yfinance"  2>/dev/null && echo "    yfinance      ✓" || { echo "    yfinance      ✗  (run: pip3 install yfinance --break-system-packages)"; }
python3 -c "import sklearn"   2>/dev/null && echo "    scikit-learn  ✓" || { echo "    scikit-learn  ✗  (run: pip3 install scikit-learn --break-system-packages)"; }
python3 -c "import optuna"    2>/dev/null && echo "    optuna        ✓" || { echo "    optuna        ✗  (run: pip3 install optuna --break-system-packages)"; }
python3 -c "import xgboost"   2>/dev/null && echo "    xgboost       ✓" || { echo "    xgboost       ✗  (run: pip3 install xgboost --break-system-packages)"; }
python3 -c "import lightgbm"  2>/dev/null && echo "    lightgbm      ✓" || { echo "    lightgbm      ✗  (run: pip3 install lightgbm --break-system-packages)"; }
python3 -c "import joblib"      2>/dev/null && echo "    joblib        ✓" || { echo "    joblib        ✗  (run: pip3 install joblib --break-system-packages)"; }
python3 -c "import anthropic"  2>/dev/null && echo "    anthropic     ✓" || { echo "    anthropic     ✗  (run: pip3 install anthropic --break-system-packages)"; }

# ── 3. Check .env has required keys ──────────────────────────────────────────
echo ""
echo "[3] Checking .env credentials..."
ENV_FILE="$SCRIPT_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "    .env not found! Create it:"
    echo ""
    echo "    cat > $ENV_FILE << 'EOF'"
    echo "    DHAN_ACCESS_TOKEN=your_token_here"
    echo "    DHAN_CLIENT_ID=your_client_id_here"
    echo "    TELEGRAM_BOT_TOKEN=your_bot_token_here"
    echo "    TELEGRAM_CHAT_ID=your_chat_id_here"
    echo "    EOF"
    echo ""
else
    source "$ENV_FILE" 2>/dev/null || true
    [ -n "$DHAN_ACCESS_TOKEN" ]  && echo "    DHAN_ACCESS_TOKEN   ✓" || echo "    DHAN_ACCESS_TOKEN   ✗  MISSING"
    [ -n "$DHAN_CLIENT_ID" ]     && echo "    DHAN_CLIENT_ID      ✓" || echo "    DHAN_CLIENT_ID      ✗  MISSING"
    [ -n "$TELEGRAM_BOT_TOKEN" ] && echo "    TELEGRAM_BOT_TOKEN  ✓" || echo "    TELEGRAM_BOT_TOKEN  ✗  (optional — add for notifications)"
    [ -n "$TELEGRAM_CHAT_ID" ]   && echo "    TELEGRAM_CHAT_ID    ✓" || echo "    TELEGRAM_CHAT_ID    ✗  (optional — add for notifications)"
fi

# ── 4. Test Dhan API connection ───────────────────────────────────────────────
echo ""
echo "[4] Testing Dhan API connection..."
cd "$SCRIPT_DIR"
python3 -c "
import os, requests
from dotenv import load_dotenv
load_dotenv()
t = os.getenv('DHAN_ACCESS_TOKEN','')
c = os.getenv('DHAN_CLIENT_ID','')
if not t or not c:
    print('    Cannot test — credentials missing')
else:
    r = requests.get('https://api.dhan.co/v2/fundlimit',
        headers={'access-token': t, 'client-id': c, 'Content-Type': 'application/json'}, timeout=10)
    if r.status_code == 200:
        d = r.json()
        bal = d.get('availabelBalance') or d.get('availableBalance') or d.get('net') or 0
        print(f'    Dhan API  ✓   Available balance: ₹{float(bal):,.0f}')
    elif r.status_code == 401:
        print('    Dhan API  ✗   Token expired (401) — regenerate at dhan.co → API Settings')
    else:
        print(f'    Dhan API  ?   HTTP {r.status_code}: {r.text[:100]}')
"

# ── 5. Install cron job ───────────────────────────────────────────────────────
echo ""
echo "[5] Installing cron job (9:30 AM IST = 4:00 AM UTC, Mon–Fri)..."

CRON_CMD="0 4 * * 1-5 cd $SCRIPT_DIR && python3 auto_trader.py >> $LOG_DIR/auto_trader.log 2>&1"
CRON_COMMENT="# BankNifty Auto Trader — runs at 9:30 AM IST"

# Token renewer — twice daily + @reboot safety net.
# 7:55 AM IST (2:25 UTC): renews before 9:30 AM trade. 11:00 PM IST (17:30 UTC): overnight renewal.
# @reboot: covers VM restarts between daily runs.
RENEWER_CMD_MORNING="25 2  * * *  cd $SCRIPT_DIR && python3 renew_token.py >> $LOG_DIR/renew_token.log 2>&1"
RENEWER_CMD_EVENING="30 17 * * *  cd $SCRIPT_DIR && python3 renew_token.py >> $LOG_DIR/renew_token.log 2>&1"
RENEWER_CMD_REBOOT="@reboot      sleep 30 && cd $SCRIPT_DIR && python3 renew_token.py >> $LOG_DIR/renew_token.log 2>&1"
RENEWER_COMMENT="# Token renewer — twice daily 7:55 AM IST (2:25 UTC) + 11:00 PM IST (17:30 UTC) + @reboot"

# Monthly lot/expiry scanner — 1st of month at 10 AM IST = 4:30 AM UTC
SCANNER_CMD="30 4 1 * * cd $SCRIPT_DIR && python3 lot_expiry_scanner.py >> $LOG_DIR/scanner.log 2>&1"
SCANNER_COMMENT="# BankNifty lot/expiry scanner — runs 1st of month 10 AM IST"

# Nightly model evolver — 11 PM IST = 17:30 UTC, Mon–Fri
EVOLVER_CMD="30 17 * * 1-5 cd $SCRIPT_DIR && python3 model_evolver.py >> $LOG_DIR/evolver.log 2>&1"
EVOLVER_COMMENT="# ML Model Evolver — nightly brain training at 11 PM IST"

# EOD position squareoff — 3:15 PM IST = 9:45 AM UTC, Mon–Fri
# Closes any open BankNifty NRML positions that SL/TP didn't catch by end of day
EXIT_CMD="45 9 * * 1-5 cd $SCRIPT_DIR && python3 exit_positions.py >> $LOG_DIR/exit.log 2>&1"
EXIT_COMMENT="# EOD squareoff — 3:15 PM IST, closes open NRML positions before market close"

# Intraday spread monitor — every 5 min during market hours, Mon–Fri
# Checks credit-spread SL/TP triggers; closes both legs if hit. No-op for naked options.
# Runs 9:30 AM–3:10 PM IST = 4:00–9:40 UTC
SPREAD_MON_CMD="*/5 4-9 * * 1-5 cd $SCRIPT_DIR && python3 spread_monitor.py >> $LOG_DIR/spread_monitor.log 2>&1"
SPREAD_MON_COMMENT="# Spread monitor — every 5 min 9:30 AM–3:10 PM IST, checks SL/TP on credit spreads"

# Trade journal — 3:30 PM IST = 10:00 AM UTC, Mon–Fri
# Captures actual fills vs oracle intent, appends to data/live_trades.csv
JOURNAL_CMD="0 10 * * 1-5 cd $SCRIPT_DIR && python3 trade_journal.py >> $LOG_DIR/journal.log 2>&1"
JOURNAL_COMMENT="# Trade journal — 3:30 PM IST, captures live fills + oracle scorecard"

# Midday conviction check — 11:00 AM IST = 5:30 AM UTC, Mon–Fri
# Re-evaluates open trade against live BN spot + macro, sends Telegram verdict
CONVICTION_CMD="30 5 * * 1-5 cd $SCRIPT_DIR && python3 midday_conviction.py >> $LOG_DIR/conviction.log 2>&1"
CONVICTION_COMMENT="# Midday conviction — 11:00 AM IST, live thesis reassessment"

# Pre-market health ping — 9:05 AM IST = 3:35 AM UTC, Mon–Fri
# Fires 25 min before trade: checks token, signal freshness, capital, lock file → Telegram all-clear or alert
HEALTH_CMD="35 3 * * 1-5 cd $SCRIPT_DIR && python3 health_ping.py >> $LOG_DIR/health_ping.log 2>&1"
HEALTH_COMMENT="# Pre-market health ping — 9:05 AM IST, system checks before trade"

# Morning news brief — 9:15 AM IST = 3:45 AM UTC, Mon–Fri
# Fetches BankNifty headlines, calls Claude for sentiment, writes data/news_sentiment.json
BRIEF_CMD="45 3 * * 1-5 cd $SCRIPT_DIR && python3 morning_brief.py >> $LOG_DIR/morning_brief.log 2>&1"
BRIEF_COMMENT="# Morning news brief — 9:15 AM IST, news sentiment for auto_trader"

# Weekly log rotation — Sunday 2 AM IST (8:30 PM Sat UTC)
# Truncates each log to its last 1000 lines if > 10 MB to prevent disk fill
LOG_ROTATE_CMD="30 20 * * 0 for f in $LOG_DIR/*.log; do [ -f \"\$f\" ] && [ \$(stat -c%s \"\$f\" 2>/dev/null || echo 0) -gt 10485760 ] && tail -n 1000 \"\$f\" > \"\$f.tmp\" && mv \"\$f.tmp\" \"\$f\" && echo \"Rotated \$f\"; done"
LOG_ROTATE_COMMENT="# Weekly log rotation — Sun 2 AM IST, truncate logs > 10 MB to last 1000 lines"

# Daily autoresearch — Mon–Fri midnight IST (00:00 IST = 18:30 UTC)
# Claude AI proposes feature/signal improvements with paper trading mode for ML changes.
# Paper model must beat live by ≥1.5% for 3 consecutive nights to auto-promote.
# Sends Telegram updates throughout. Retains evolver if immediate changes are made.
AUTOLOOP_CMD="30 18 * * 1-5 cd $SCRIPT_DIR && python3 autoloop_bn.py >> $LOG_DIR/autoloop_bn.log 2>&1"
AUTOLOOP_COMMENT="# Autoresearch — Mon–Fri midnight IST, paper-trading AI improvement loop"

# Remove old entries (all scripts) if any
EXISTING=$(crontab -l 2>/dev/null \
  | grep -v "auto_trader" \
  | grep -v "lot_expiry_scanner" \
  | grep -v "model_evolver" \
  | grep -v "renew_token" \
  | grep -v "refresh_token" \
  | grep -v "token_refresh" \
  | grep -v "exit_positions" \
  | grep -v "spread_monitor" \
  | grep -v "trade_journal" \
  | grep -v "midday_conviction" \
  | grep -v "health_ping" \
  | grep -v "autoloop_bn" \
  | grep -v "log rotation" \
  | grep -v "BankNifty Auto Trader" \
  | grep -v "BankNifty lot/expiry" \
  | grep -v "ML Model Evolver" \
  | grep -v "Token renewer" \
  | grep -v "EOD squareoff" \
  | grep -v "Spread monitor" \
  | grep -v "Trade journal" \
  | grep -v "Midday conviction" \
  | grep -v "Pre-market health" \
  | grep -v "Autoresearch" \
  | grep -v "Morning news brief" \
  | grep -v "Weekly log rotation")

# Add fresh entries
NEW_CRON="$(echo "$EXISTING")
$RENEWER_COMMENT
$RENEWER_CMD_MORNING
$RENEWER_CMD_EVENING
$RENEWER_CMD_REBOOT
$HEALTH_COMMENT
$HEALTH_CMD
$BRIEF_COMMENT
$BRIEF_CMD
$CRON_COMMENT
$CRON_CMD
$SPREAD_MON_COMMENT
$SPREAD_MON_CMD
$EXIT_COMMENT
$EXIT_CMD
$JOURNAL_COMMENT
$JOURNAL_CMD
$CONVICTION_COMMENT
$CONVICTION_CMD
$SCANNER_COMMENT
$SCANNER_CMD
$EVOLVER_COMMENT
$EVOLVER_CMD
$AUTOLOOP_COMMENT
$AUTOLOOP_CMD
$LOG_ROTATE_COMMENT
$LOG_ROTATE_CMD"

echo "$NEW_CRON" | crontab -

echo "    Cron installed. Verify with:  crontab -l"
echo ""
echo "    Scheduled line:"
echo "    $CRON_CMD"

# ── 6. Dry-run test ───────────────────────────────────────────────────────────
echo ""
echo "[6] Running dry-run test of auto_trader.py..."
echo "────────────────────────────────────────────────"
cd "$SCRIPT_DIR"
python3 auto_trader.py --dry-run
echo "────────────────────────────────────────────────"

echo ""
echo "════════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Token renewer  : twice daily 7:55 AM IST + 11:00 PM IST + @reboot"
echo "  Renewer log    : $LOG_DIR/renew_token.log"
echo ""
echo "  Health ping    : 9:05 AM IST every weekday (Mon–Fri)"
echo "  Ping log       : $LOG_DIR/health_ping.log"
echo ""
echo "  Auto trader    : 9:30 AM IST every weekday (Mon–Fri)"
echo "  Trader log     : $LOG_DIR/auto_trader.log"
echo ""
echo "  Spread monitor : every 5 min, 9:30 AM–3:10 PM IST (Mon–Fri)"
echo "  Monitor log    : $LOG_DIR/spread_monitor.log"
echo ""
echo "  EOD squareoff  : 3:15 PM IST every weekday (Mon–Fri)"
echo "  Exit log       : $LOG_DIR/exit.log"
echo ""
echo "  Trade journal  : 3:30 PM IST every weekday (Mon–Fri)"
echo "  Journal log    : $LOG_DIR/journal.log"
echo ""
echo "  ML Evolver     : 11:00 PM IST every weekday (Mon–Fri)"
echo "  Evolver log    : $LOG_DIR/evolver.log"
echo ""
echo "  Autoresearch   : Mon–Fri midnight IST (AI model improvement, paper trading mode)"
echo "  Autoloop log   : $LOG_DIR/autoloop_bn.log"
echo ""
echo "  Log rotation   : Sunday 2:00 AM IST weekly (truncates logs > 10 MB)"
echo ""
echo "  To watch the log live:     tail -f $LOG_DIR/auto_trader.log"
echo "  To test manually now:      python3 auto_trader.py --dry-run"
echo "  To force a live trade:     python3 auto_trader.py"
echo "  To run evolver now:        python3 model_evolver.py --no-data"
echo "  To test token renewal:     python3 renew_token.py"
echo "════════════════════════════════════════════════"
