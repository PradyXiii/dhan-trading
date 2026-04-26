#!/bin/bash
# scripts/smoke_test.sh — full system check before live trading.
# Run after any package cleanup, dependency change, or VM migration.
# Run from repo root:  bash scripts/smoke_test.sh

set +e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE" || exit 1

PASS=0
FAIL=0
mark_pass() { PASS=$((PASS+1)); printf "  \xe2\x9c\x85  %s\n" "$1"; }
mark_fail() { FAIL=$((FAIL+1)); printf "  \xe2\x9d\x8c  %s\n" "$1"; }

echo "================ 1. IMPORT TEST (every .py in repo) ================"
for f in *.py; do
  mod="${f%.py}"
  err=$(python3 -c "import $mod" 2>&1)
  if [ -z "$err" ]; then mark_pass "$mod"
  else mark_fail "$mod  -->  $(echo "$err" | tail -1)"; fi
done

echo ""
echo "================ 2. CRITICAL LIBRARY TEST ================"
python3 - <<'PY' && mark_pass "all libs importable" || mark_fail "library import failed"
import numpy, scipy, pandas, sklearn, xgboost, lightgbm, catboost
import requests, yfinance, optuna, joblib, dotenv
PY

echo ""
echo "================ 3. STALE-PACKAGE AUDIT (CUDA/PyTorch leak) ================"
hits=$(pip list 2>/dev/null | grep -iE "torch|nvidia|cuda|triton|tabpfn|hf-xet")
if [ -z "$hits" ]; then mark_pass "no PyTorch/CUDA leak"
else mark_fail "stale packages found - uninstall:"; echo "$hits"; fi

echo ""
echo "================ 4. DHAN API LIVE TEST ================"
python3 dhan_journal.py --positions >/dev/null 2>&1 \
  && mark_pass "dhan_journal /v2/positions" \
  || mark_fail "dhan_journal /v2/positions"

python3 - <<'PY' && mark_pass "auto_trader Dhan helpers" || mark_fail "auto_trader Dhan helpers"
from auto_trader import get_capital, get_expiry
cap = get_capital()
exp = get_expiry()
assert cap > 0, "capital <= 0"
assert exp, "expiry empty"
PY

echo ""
echo "================ 5. ML 4-MODEL ENSEMBLE TEST ================"
out=$(python3 ml_engine.py --predict-today 2>&1)
echo "$out" | grep -q "Ensemble (4 models" \
  && mark_pass "ML ensemble has 4 models" \
  || mark_fail "ML ensemble missing model(s) -- $(echo "$out" | grep -i 'could not load')"

echo ""
echo "================ 6. AUTO-TRADER DRY RUN ================"
out=$(python3 auto_trader.py --dry-run 2>&1)
echo "$out" | grep -qE "(Bull Put placed|Iron Condor placed|Straddle placed|No Trade)" \
  && mark_pass "auto_trader dry-run completes" \
  || mark_fail "auto_trader dry-run -- last lines: $(echo "$out" | tail -3)"

echo ""
echo "================ 7. DATA FILE FRESHNESS ================"
for f in data/nifty50.csv data/india_vix.csv data/signals_ml.csv; do
  if [ -f "$f" ]; then
    age_h=$(( ($(date +%s) - $(stat -c %Y "$f")) / 3600 ))
    if [ "$age_h" -lt 72 ]; then mark_pass "$f (${age_h}h old)"
    else mark_fail "$f stale -- ${age_h}h old"; fi
  else mark_fail "$f MISSING"; fi
done

echo ""
echo "================ 8. CRON SCHEDULE INTACT ================"
crontab -l 2>/dev/null | grep -q "auto_trader.py" && mark_pass "auto_trader cron exists" \
  || mark_fail "auto_trader cron missing"
crontab -l 2>/dev/null | grep -q "exit_positions.py" && mark_pass "exit_positions cron exists" \
  || mark_fail "exit_positions cron missing"
crontab -l 2>/dev/null | grep -q "trade_journal.py" && mark_pass "trade_journal cron exists" \
  || mark_fail "trade_journal cron missing"

echo ""
echo "================ 9. DISK ================"
df -h / | tail -1

echo ""
echo "================================================================"
echo "  PASSED: $PASS    FAILED: $FAIL"
echo "================================================================"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
