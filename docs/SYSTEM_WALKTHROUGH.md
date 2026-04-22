# Nifty50 Iron Condor — Full System Walkthrough

A plain-English guide to what happens every trading day, from pre-market to midnight. Written for reference when starting a new session or debugging a live issue.

---

## The One-Line Version

Every weekday the system wakes up, reads the market, builds a 4-leg Iron Condor, places 4 real orders via Dhan, monitors every minute, closes automatically on SL/TP, and retrains the ML brain at night.

---

## Why Iron Condor?

An Iron Condor sells premium on both sides simultaneously:

```
SELL ATM CE  + BUY ATM+150 CE    ← upper wing (Bear Call spread)
SELL ATM PE  + BUY ATM-150 PE    ← lower wing (Bull Put spread)
```

You collect cash upfront (net credit). You keep that cash if Nifty stays between the two short strikes until expiry. You lose if Nifty blows past either wing. With weekly Tuesday expiry (DTE ≤ 7), theta decay works in your favour — option premium bleeds out fast when time is short.

**Why NF and not BN?** BankNifty lost weekly expiry in Nov 2024 (SEBI). Monthly contracts = longer DTE = gamma risk dominates theta = IC win rate collapsed to 27% in 2025. Nifty50 kept weekly Tuesday expiry. Every NF IC is naturally DTE ≤ 7.

**Backtest result:** 84.6% win rate across 5 years (Aug 2021 – Apr 2026), 1,114 real-money-simulated trades, max drawdown −0.8%. These numbers are from real 1-min Dhan option data, not a formula.

---

## Daily Timeline

### 8:50 AM IST — `health_ping.py` (Pre-market check)

Before anyone touches the market, health_ping checks:
- Dhan token is valid (not expired)
- Capital is above minimum threshold
- `data/signals_ml.csv` was updated today (ML ran last night)
- No stale lock file from a crashed previous run

Sends a Telegram: "✅ All clear" or "🚨 ALERT: [specific problem]".

If you don't get this message, don't trust auto_trader.

---

### 9:15 AM IST — `morning_brief.py` (News sentiment)

Fetches Nifty50-relevant RSS headlines (ET Economy, Google News: RBI/CPI/FII/SEBI) from the last 20 hours. Calls Claude API with the headlines + live Nifty spot + India VIX. Claude returns:

```json
{"direction": "BULLISH|BEARISH|NEUTRAL", "confidence": "HIGH|MEDIUM|LOW", "reason": "one sentence"}
```

Writes to `data/news_sentiment.json`. auto_trader reads this as one extra vote at 9:30 AM.

---

### 9:30 AM IST — `auto_trader.py` (The main event)

This is the core runner. Here's the exact sequence:

**Step 0: Acquire lock**
`fcntl` advisory lock prevents two cron runs overlapping (e.g. if previous run hung).

**Step 1: Check credentials**
Calls Dhan API to verify token is live. If dead → Telegram alert, exit.

**Step 2: Check lot size**
Reads `data/lot_size_overrides.json` (written by lot_expiry_scanner). Warns if `LOT_SIZE` constant in code doesn't match the expected lot size for today's date.

**Step 3: Check exit marker**
Verifies yesterday's `exit_positions.py` ran clean (no open carry positions). Skipped in paper mode.

**Step 4: Refresh data + signal**
Runs three subprocesses in sequence:
1. `data_fetcher.py` — pulls latest OHLCV + macro + options data
2. `signal_engine.py` — computes rule-based signal → `data/signals.csv`
3. `ml_engine.py --predict-today` — fast prediction (< 10 sec) using saved ensemble → `data/signals_ml.csv`

**Step 5: Is it a trading day?**
Checks `NSE_HOLIDAYS_2026` hardcoded set. If holiday → Telegram "No trade today (holiday)", exit.

**Step 6: Get today's signal**
Reads `data/signals_ml.csv`. If NONE → exit. If CALL or PUT → continue.

Signal vote breakdown (what auto_trader reads):
- 4 rule-based indicators (EMA20 cross, trend5, VIX level, NF gap)
- ML ensemble vote (RF + XGB + LGB + CAT, weighted by holdout accuracy)
- News sentiment from morning_brief (one extra vote)

**Step 7: VIX regime filter**
- VIX < 13 → skip (too calm, premium too cheap to bother)
- VIX > 20 → skip (panic regime, wings may not hold)
- If skipped → Telegram "VIX out of range", exit

**Step 8: ML confidence filter**
- ML ensemble confidence < 0.55 → skip ("model not sure enough")
- Sends Telegram, exits

**Step 9: Get capital**
Calls Dhan `/fundlimit` API → available margin for position sizing.

**Step 10: Get expiry**
Calls Dhan `/optionchain/expirylist` for Nifty50 (scrip=13). Takes first date >= today (IST). This is always the nearest Tuesday.

**Step 11: Get IC legs**
Calls Dhan option chain for this expiry. Finds ATM strike (nearest to current spot). Builds 4 legs:

```
Call signal:  SELL ATM CE, BUY (ATM+150) CE, SELL ATM PE, BUY (ATM-150) PE
Put signal:   Same structure — IC is symmetric regardless of signal direction
```

Computes `net_credit = short_CE_ltp + short_PE_ltp − long_CE_ltp − long_PE_ltp`.

Sizes position: queries Dhan `/margincalculator/multi` with all 4 legs × 1 lot → returns actual SPAN+Exposure margin. Then `lots = min(MAX_LOTS=10, floor(capital / api_margin))`. All 4 legs placed at `lots × 65` shares each (equal qty across legs). Fallback to ₹1L constant only if API errors.

**Step 12: Chain signals (informational)**
Computes max pain, GEX, straddle cost — sent to Telegram for context, doesn't affect trade decision.

**Step 13: Send pre-trade Telegram**
Shows: signal direction, VIX, confidence, all 4 strike/security IDs, net credit, SL/TP triggers, lot count.

**Step 14: Check no existing position**
Calls Dhan positions API. If any open Nifty FNO position with `netQty != 0` → abort. Prevents doubling up.

**Step 15: Place 4 orders (MANDATORY ORDER)**
```
1. BUY ATM+150 CE  ← long wing first (Dhan margin rule)
   wait 2 seconds
2. SELL ATM CE     ← short leg (margin benefit now applies)
   wait 2 seconds  
3. BUY ATM-150 PE  ← long wing first
   wait 2 seconds
4. SELL ATM PE     ← short leg
```

**Why this order matters:** Dhan requires the long leg recorded in your portfolio before the short leg qualifies for hedged margin. Reverse the order and Dhan charges full unhedged margin on the short — could be 3–5× more.

Failure handling:
- BUY fails → log FAILED, Telegram alert, stop
- SELL fails after BUY placed → 🚨 CRITICAL Telegram "PARTIAL SPREAD — naked long exposure"

**Step 16: Write today_trade.json**
Writes `data/today_trade.json` with full IC schema: both security IDs, entry LTPs, net credit, SL/TP triggers, lot count, expiry. This file is the shared state — spread_monitor, exit_positions, and trade_journal all read it.

---

### 9:30 AM–3:14 PM (every 1 min) — `spread_monitor.py` (Intraday watch)

Cron runs every minute during market hours. Each run:

1. Reads `today_trade.json` — gets all 4 security IDs + net_credit + SL/TP thresholds
2. Fetches live LTP for all 4 legs via Dhan `/marketfeed/ltp`
3. Computes `current_spread_cost = call_spread_cost + put_spread_cost`
   - `call_spread_cost = short_CE_ltp − long_CE_ltp`
   - `put_spread_cost = short_PE_ltp − long_PE_ltp`
4. SL check: `current_spread_cost >= net_credit × 1.5` → spread cost grew 50% above credit collected → close all 4 legs
5. TP check: `current_spread_cost <= net_credit × 0.35` → spread cost collapsed to 35% → retain 65% of credit → close all 4 legs
6. On trigger: places 4 market orders to close (reverse the IC), marks `data/today_trade.json` with exit info

No Telegram on every run — only on SL/TP hit or error.

---

### 11:00 AM IST — `midday_conviction.py` (Health check)

Fetches live data: current BN/NF spread cost, S&P futures, DXY, India VIX, crude — all via Dhan + yfinance.

Computes a 4-factor conviction score:
- NF spot vs entry (moving for us or against?)
- S&P 500 futures overnight trend
- India VIX direction since open
- Current option premium (how close to SL?)

Sends plain-English Telegram: "Trade looking good — 3 of 4 signs positive" or "Getting shaky — VIX jumped, S&P weak".

Also writes `data/midday_checkpoints.csv` (one row per day): spot, macro, conviction score, reversal detected. The model_evolver reads this at 11 PM — reversal days get 5× weight in ML training.

---

### 3:15 PM IST — `exit_positions.py` (End-of-day squareoff)

Fetches all open Nifty NRML positions from Dhan. If any remain (SL/TP didn't fire intraday), closes them with market orders.

This is the safety net. If spread_monitor missed a fill, or cron glitched, exit_positions catches it. No position should carry overnight.

Writes an exit marker file so tomorrow's auto_trader knows today closed clean.

---

### 3:30 PM IST — `trade_journal.py` (P&L log)

Reads `data/today_trade.json` (entry data) + Dhan positions API (exit fill prices).

Computes:
- Actual P&L: `(net_credit_at_entry − net_cost_at_exit) × lot_size × lots`
- Exit reason: SL / TP / EOD
- Duration: entry time → exit time

Appends one row to `data/live_ic_trades.csv` (the live oracle file). Sends EOD Telegram summary.

This CSV is the ground truth for the ML model. Every row = a real trade with a real outcome.

---

### 11:00 PM IST — `model_evolver.py` (Brain retraining)

The nightly ML competition:

**Data loading:**
- All historical OHLCV + macro + options data
- `compute_features()` from ml_engine.py → 60 features (all shifted by 1 day — no lookahead)
- `data/live_ic_trades.csv` → real outcomes at 10× weight
- `data/midday_checkpoints.csv` → reversal days at 5× weight

**Feature selection:**
Random Forest feature importance — keep features with > 1% importance. Discards noise.

**Optuna HPO — 120 trials:**
4 models × 30 trials each:
- Random Forest
- XGBoost
- LightGBM
- CatBoost

Each trial picks random hyperparameters and evaluates on a 252-day (1 year) temporal holdout. No data leakage — holdout always comes after training.

**Champion selection:**
Best composite score = blend of accuracy + recall. Saved as `models/champion.pkl`.

**Ensemble:**
All 4 best-of-class models saved to `models/ensemble/`. Live predictions use weighted vote from all 4.

**Telegram report:**
Plain-English summary — which model won, accuracy on holdout, whether live trades are influencing the model, any feature changes.

---

### Midnight IST — `autoloop_nf.py` (Autoresearch)

The AI experiment loop:

1. Reads `data/paper_performance.csv` — compares paper model vs live model over last N days
2. Reads `data/paper_changes.json` — accumulated log of what the paper model tried
3. Reads `data/midday_checkpoints.csv` — recent reversal patterns
4. Calls Claude API with all this context + current FEATURE_COLS + current ml_engine code
5. Claude proposes ONE small, specific change: add a feature, remove a feature, tweak a threshold
6. Writes the change to `ml_engine_paper.py`
7. Runs `autoexperiment_nf.py --module ml_engine_paper` — evaluates composite score on 252-day holdout
8. If score >= 0.5358 (NF baseline): records improvement, accumulates in paper model
9. If score < 0.5358: reverts change
10. After 3 nights of consecutive outperformance: auto-promotes paper model → live ml_engine.py, commits to git, pushes via SSH

The autoresearcher can only touch `ml_engine_paper.py` and `data/paper_changes.json`. It cannot touch live code, cron, or Dhan calls. Promotion requires 3 consecutive nights beating the baseline — single-night improvements get discarded.

---

## The Feedback Loop

```
Live trade → live_ic_trades.csv
                ↓ (10× weight)
           model_evolver (11 PM)
                ↓
           champion.pkl + ensemble
                ↓
           auto_trader (9:30 AM next day)
                ↓
           new IC trade
```

Plus:
```
Midday data → midday_checkpoints.csv
                   ↓ (5× weight, reversal days)
              model_evolver
```

Plus:
```
autoloop proposes change → paper model tested → 3-night streak → live model
```

The system gets smarter every night by learning from its own trades.

---

## What Each File Owns

| File | Owns |
|---|---|
| `data/today_trade.json` | Shared IC state (written by auto_trader, read by spread_monitor/exit/journal) |
| `data/live_ic_trades.csv` | Real trade oracle (written by trade_journal, read by model_evolver) |
| `data/midday_checkpoints.csv` | Midday reversal data (written by midday_conviction, read by model_evolver + autoloop) |
| `data/signals_ml.csv` | Today's ML prediction (written by ml_engine, read by auto_trader) |
| `models/champion.pkl` | Best single model (written by model_evolver, read by ml_engine fast path) |
| `models/ensemble/*.pkl` | 4-model ensemble (written by model_evolver, read by ml_engine fast path) |
| `data/paper_performance.csv` | Paper vs live comparison (written by autoloop, drives promotion decision) |

---

## Common Failure Modes

| Symptom | Likely cause | Check |
|---|---|---|
| No 8:50 AM Telegram | health_ping cron not running | `crontab -l`, `logs/health_ping.log` |
| VIX filter skipped trade | VIX outside 13–20 range | Expected — not a bug |
| "No positions found" from exit_positions | spread_monitor already closed | Check `data/today_trade.json` for exit_time |
| model_evolver Telegram missing | Ran after midnight (took > 1h) | `logs/model_evolver.log` |
| Autoresearch not pushing | SSH key not configured | `ssh -T git@github.com` on VM |
| PARTIAL SPREAD critical alert | Short leg placed before long filled | Check order logs, manual close needed |

---

## Key Constants (all in `auto_trader.py`)

```python
LOT_SIZE   = 65           # NF lot size since Jan 6 2026
MAX_LOTS   = 10           # max 10 IC lots (both sides tie up margin)
PAPER_MODE = False        # LIVE — real orders from Apr 22 2026
SPREAD_WIDTH     = 150    # pts between short and long leg (ATM ± 3 strikes)
CREDIT_SL_FRAC   = 0.5    # SL when spread cost = net_credit × 1.5
CREDIT_TP_FRAC   = 0.65   # TP when spread cost falls to net_credit × 0.35 (backtest-validated)
ML_CONF_THRESHOLD = 0.55  # skip if ensemble confidence below this
VIX_MIN_TRADE    = 13.0   # dynamic (analyze_confidence updates nightly)
VIX_MAX_TRADE    = 20.0   # panic ceiling
```
