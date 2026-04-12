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
python3 -c "import joblib"    2>/dev/null && echo "    joblib        ✓" || { echo "    joblib        ✗  (run: pip3 install joblib --break-system-packages)"; }

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
echo "[5] Installing cron job (9:15 AM IST = 3:45 AM UTC, Mon–Fri)..."

CRON_CMD="45 3 * * 1-5 cd $SCRIPT_DIR && python3 auto_trader.py >> $LOG_DIR/auto_trader.log 2>&1"
CRON_COMMENT="# BankNifty Auto Trader — runs at 9:15 AM IST"

# Dynamic token renewer — runs every 5 minutes, renews when 23h50m have elapsed
# since the last renewal (10-min buffer before 24h expiry). Script exits immediately
# if not due yet, so the 5-min polling is lightweight (just a file read + exit).
RENEWER_CMD="*/5 * * * * cd $SCRIPT_DIR && python3 renew_token.py >> $LOG_DIR/renew_token.log 2>&1"
RENEWER_COMMENT="# Token renewer — every 5 min, renews at 23h50m elapsed (10-min buffer, dynamic)"

# Monthly lot/expiry scanner — 1st of month at 10 AM IST = 4:30 AM UTC
SCANNER_CMD="30 4 1 * * cd $SCRIPT_DIR && python3 lot_expiry_scanner.py >> $LOG_DIR/scanner.log 2>&1"
SCANNER_COMMENT="# BankNifty lot/expiry scanner — runs 1st of month 10 AM IST"

# Nightly model evolver — 11 PM IST = 17:30 UTC, Mon–Fri
EVOLVER_CMD="30 17 * * 1-5 cd $SCRIPT_DIR && python3 model_evolver.py >> $LOG_DIR/evolver.log 2>&1"
EVOLVER_COMMENT="# ML Model Evolver — nightly brain training at 11 PM IST"

# Remove old entries (auto_trader + scanner + evolver + renewer) if any
EXISTING=$(crontab -l 2>/dev/null | grep -v "auto_trader" | grep -v "lot_expiry_scanner" | grep -v "model_evolver" | grep -v "renew_token" | grep -v "refresh_token" | grep -v "token_refresh" | grep -v "BankNifty Auto Trader" | grep -v "BankNifty lot/expiry" | grep -v "ML Model Evolver" | grep -v "Token renewer")

# Add fresh entries
NEW_CRON="$(echo "$EXISTING")
$RENEWER_COMMENT
$RENEWER_CMD
$CRON_COMMENT
$CRON_CMD
$SCANNER_COMMENT
$SCANNER_CMD
$EVOLVER_COMMENT
$EVOLVER_CMD"

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
echo "  Token renewer  : every 5 min (renews at 23h50m — 10-min buffer, dynamic)"
echo "  Renewer log    : $LOG_DIR/renew_token.log"
echo ""
echo "  Auto trader    : 9:15 AM IST every weekday (Mon–Fri)"
echo "  Trader log     : $LOG_DIR/auto_trader.log"
echo ""
echo "  ML Evolver     : 11:00 PM IST every weekday (Mon–Fri)"
echo "  Evolver log    : $LOG_DIR/evolver.log"
echo ""
echo "  To watch the log live:     tail -f $LOG_DIR/auto_trader.log"
echo "  To test manually now:      python3 auto_trader.py --dry-run"
echo "  To force a live trade:     python3 auto_trader.py"
echo "  To run evolver now:        python3 model_evolver.py --no-data"
echo "  To test token renewal:     python3 renew_token.py"
echo "════════════════════════════════════════════════"
