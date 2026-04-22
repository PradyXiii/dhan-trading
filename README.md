# Nifty50 Multi-Strategy Auto-Trader

Fully automated Nifty50 options trading on a GCP VM. Every trading day at 9:30 AM IST (except Wednesday) the system reads the market, routes to the right strategy, places orders via Dhan, and sends a Telegram alert. `spread_monitor.py` watches every minute and exits on SL. Every weeknight at 11 PM the ML model retrains. Every night at midnight an AI experiment loop tries to improve the model.

> **Disclaimer:** Educational and research purposes only. Not financial advice. Options trading involves substantial risk of loss. Past results do not guarantee future performance. Trade at your own risk. Consult a SEBI-registered advisor before making any real trading decisions.

---

## Strategy Routing (9:30 AM IST)

| Day | Signal | Strategy | Legs |
|---|---|---|---|
| Mon | CALL or PUT | Iron Condor | SELL ATM CE + BUY ATM+150 CE + SELL ATM PE + BUY ATM-150 PE |
| Tue | CALL or PUT | Iron Condor | same 4-leg IC |
| Wed | — | **No trade** | DTE 6 — all strategies net-negative after costs |
| Thu | CALL | Bear Call Credit Spread | SELL ATM CE + BUY ATM+150 CE |
| Thu | PUT | Bull Put Credit Spread | SELL ATM PE + BUY ATM-150 PE |
| Fri | CALL | Bear Call Credit Spread | SELL ATM CE + BUY ATM+150 CE |
| Fri | PUT | Bull Put Credit Spread | SELL ATM PE + BUY ATM-150 PE |
| Any (capital ≥ ₹2.17L) | CALL or PUT | Short Straddle | SELL ATM CE + SELL ATM PE (overrides all above) |

---

## What runs and when

```
Every 5 minutes (all 7 days)
  └── renew_token.py         — checks if Dhan token is 23h50m old, renews if so

8:50 AM IST, Mon–Fri
  └── health_ping.py
        — Pre-market heartbeat: token, capital, signal freshness, lock file
        — Sends "all clear" or alarm to Telegram

9:15 AM IST, Mon–Fri
  └── morning_brief.py
        — Fetches Nifty50 headlines, asks Claude for sentiment
        — Writes data/news_sentiment.json (one vote in auto_trader.py)

9:30 AM IST, Mon–Fri (except Wed)
  └── auto_trader.py
        1. Pull latest market data + ML signal
        2. Wed skip — no trade (DTE 6, all strategies lose after costs)
        3. Straddle auto-upgrade — if capital ≥ ₹2.17L → straddle all days
        4. Thu/Fri routing — CALL signal → Bear Call, PUT signal → Bull Put
        5. Mon/Tue → Iron Condor (4 legs)
        6. Size position — floor(capital / Dhan live margin API)
        7. Place orders via Dhan — BUY long leg first (margin rule), then SELL
        8. Write today_trade.json, send Telegram alert

Every 1 min, 9:30 AM–3:14 PM IST
  └── spread_monitor.py
        — Fetches live LTPs for all active legs
        — IC/spread SL: total spread cost ≥ net_credit × 1.5  (50% loss)
        — Straddle SL:  CE_ltp + PE_ltp ≥ net_credit × 1.5
        — IC: SL-only, exits at EOD 3:15 PM (no TP — EOD maximises theta capture)
        — Spread/Straddle: SL-only
        — On SL hit: closes all legs, marks exit in today_trade.json

11:00 AM IST, Mon–Fri
  └── midday_conviction.py
        — Fetches live option LTPs + macro (S&P futures, DXY, VIX, crude)
        — Sends plain-English trade health summary to Telegram
        — Writes data/midday_checkpoints.csv (evolver uses reversal days)

3:15 PM IST, Mon–Fri
  └── exit_positions.py
        — Closes any open Nifty NRML positions not already hit by SL
        — Prevents unintended overnight carry

3:30 PM IST, Mon–Fri
  └── trade_journal.py
        — Reads exit data from today_trade.json
        — Computes P&L, exit reason (SL/EOD)
        — Appends row to data/live_ic_trades.csv
        — Sends EOD journal to Telegram

11:00 PM IST, Mon–Fri
  └── model_evolver.py
        — Retrains ML brain: Optuna HPO across RF + XGBoost + LightGBM + CatBoost
        — Injects live trade outcomes as real-label signal (10× weight)
        — Injects midday reversal days at 5× weight
        — Saves models/champion.pkl + models/ensemble/*.pkl
        — Sends plain-English brain-training report to Telegram

Midnight IST, Mon–Fri
  └── autoloop_nf.py  (Autoresearch)
        — Claude AI proposes one small code change per experiment
        — autoexperiment_nf.py evaluates on last 252 trading days
        — Improvements committed; regressions reverted
        — Paper model accumulates 3-night winning streak before auto-promoting to live

1st of every month, 10:00 AM IST
  └── lot_expiry_scanner.py
        — Checks if NSE has changed Nifty50 lot size or expiry structure
        — Alerts via Telegram if anything changed

Every Sunday, 2:00 AM IST
  └── Log rotation — trims logs over 10 MB
```

---

## Strategy Details

### Iron Condor (Mon/Tue)

| | |
|---|---|
| Structure | SELL ATM CE + BUY ATM+150 CE + SELL ATM PE + BUY ATM-150 PE |
| Spread width | 150 pts per side (NF strike spacing = 50 pts, ATM ± 3 strikes) |
| Net credit | ~₹108/lot average |
| Stop-loss | Total spread cost ≥ net_credit × 1.5 (50% loss) |
| Take-profit | None — holds to EOD 3:15 PM (EOD captures last 35% of theta; TP cost ₹21L/5yr) |
| Lots | floor(capital / live Dhan margin), max 10 |
| Margin/lot | ~₹93,202 (Dhan SPAN+Exposure) |
| WR (backtest) | 84.6% over 1114 trades, 2021–2026 |

**Order sequence (mandatory):**
1. BUY ATM+150 CE (long call wing)
2. SELL ATM CE (short call)
3. BUY ATM-150 PE (long put wing)
4. SELL ATM PE (short put)

Long leg must be on books before short leg — Dhan margin rule.

### Bear Call Credit Spread (Thu/Fri, CALL signal)

| | |
|---|---|
| Structure | SELL ATM CE + BUY ATM+150 CE |
| Net credit | ~₹65–81/lot |
| SL | Spread cost ≥ net_credit × 1.5 |
| Lots | floor(capital / live Dhan margin), max 10 |
| Margin/lot | ~₹51,244 |

### Bull Put Credit Spread (Thu/Fri, PUT signal)

| | |
|---|---|
| Structure | SELL ATM PE + BUY ATM-150 PE |
| Net credit | small positive |
| SL | Spread cost ≥ net_credit × 1.5 |
| Lots | floor(capital / live Dhan margin), max 10 |
| Margin/lot | ~₹51,699 |

### Short Straddle (auto-upgrade when capital ≥ ₹2.17L)

| | |
|---|---|
| Structure | SELL ATM CE + SELL ATM PE (unhedged — no wings) |
| SL | CE_ltp + PE_ltp ≥ net_credit × 1.5 |
| Lots | floor(capital / live Dhan margin), max 5 |
| Margin/lot | ~₹2,16,492 (Dhan SPAN+Exposure) |
| Trigger | capital ≥ STRADDLE_MARGIN_PER_LOT (₹2,17,000) |

---

## How direction is decided

Signal engine and ML model vote CALL vs PUT. IC trades both sides regardless — signal governs Thu/Fri routing (CALL → Bear Call, PUT → Bull Put) and Mon/Tue IC entry. No VIX or ML confidence filter — maximum trade frequency = maximum P&L per backtest.

**63 features** across nine layers: rule signals, technicals (RSI, ADX, HV20), global markets (S&P 500, Nikkei, S&P futures), macro (crude oil, DXY, US 10Y yield, USD/INR), volatility regime (VIX level, percentile, HV ratio), options sentiment (PCR, IV skew, OI surface at ATM±3, max pain), flow (FII net cash, bank ETF, top-5 constituent breadth), momentum (NF momentum, 52-week high distance, ORB range), calendar (DOW, DTE).

**Nightly model competition:** RF, XGBoost, LightGBM, CatBoost compete via Optuna HPO (30 trials each = 120 total). Winner saved as champion.pkl. Full 4-model ensemble saved for live voting.

---

## Architecture

```
auto_trader.py              9:30 AM runner — strategy router, places orders, Telegram
spread_monitor.py           1-min intraday SL watch — handles IC/spread/straddle schemas
exit_positions.py           3:15 PM EOD squareoff — closes any open Nifty NRML positions
trade_journal.py            3:30 PM — logs trade outcome to live_ic_trades.csv + Telegram
signal_engine.py            Rule-based indicators → data/signals.csv
ml_engine.py                Walk-forward ML → data/signals_ml.csv; champion.pkl fast predict
ml_engine_paper.py          Paper copy — autoresearcher tests here before promoting to live
model_evolver.py            11 PM — Optuna HPO + ensemble retrain → models/
data_fetcher.py             Downloads OHLCV + global + options data → data/
morning_brief.py            9:15 AM — news sentiment via Claude API → data/news_sentiment.json
health_ping.py              8:50 AM — pre-market heartbeat: token + capital + freshness
midday_conviction.py        11 AM — leg LTP check + macro + reversal detection + Telegram
lot_expiry_scanner.py       Monthly — Nifty50 lot size / expiry change detection
replay_today.py             Post-mortem — replay today's prediction with current ensemble
analyze_confidence.py       Confidence bucket + VIX regime diagnostics
renew_token.py              Token renewal — 23h50m interval, 7 days a week
notify.py                   Telegram send helper
autoloop_nf.py              Midnight autoresearch — Claude API experiments → paper model → promote
autoexperiment_nf.py        252-day holdout evaluator (composite score)
autoexperiment_backtest.py  Backtest evaluator for strategy constant changes
validate_all.py             Pre-deployment end-to-end health check
backtest_spreads.py         Multi-leg spread backtest — NF IC + 7 other NF variants
fetch_intraday_options.py   Fetch 1-min NF option cache → data/nifty_options_cache/
check_margins.py            Live margin checker — all 5 strategies vs account balance
optimize_params.py          VIX + confidence grid search
CLAUDE.md                   Architecture map + standing rules — auto-loaded every session
setup_automation.sh         One-shot VM setup: deps + all cron jobs
```

---

## Setup

### Requirements

- GCP VM (or any Linux server), Python 3.10+
- Dhan trading account with API access
- Telegram bot (create at t.me/BotFather)
- Anthropic API key (for midnight autoresearch — console.anthropic.com)

### 1. Clone and install

```bash
git clone <your-repo-url>
cd dhan-trading
pip3 install pandas numpy yfinance requests python-dotenv scikit-learn \
             optuna xgboost lightgbm catboost joblib anthropic \
             --break-system-packages
```

### 2. Credentials

```bash
cp .env.example .env
nano .env
```

```
DHAN_ACCESS_TOKEN=your_token
DHAN_CLIENT_ID=your_client_id
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
ANTHROPIC_API_KEY=your_key
```

### 3. First-time data + model

```bash
python3 data_fetcher.py
python3 data_fetcher.py --backfill
python3 data_fetcher.py --fetch-options
python3 data_fetcher.py --fetch-intraday
python3 signal_engine.py
python3 ml_engine.py
python3 model_evolver.py
```

Fetch real 1-min Nifty option cache (required for backtests):
```bash
python3 fetch_intraday_options.py --instrument NF --spreads --start 2021-08-01
```

### 4. Install cron + verify

```bash
bash setup_automation.sh
```

---

## Useful commands

```bash
# Data
python3 data_fetcher.py
python3 data_fetcher.py --fetch-options
python3 signal_engine.py
python3 ml_engine.py --predict-today
python3 ml_engine.py --analyze

# Backtest (real 1-min option data)
python3 backtest_spreads.py --instrument NF --strategy nf_iron_condor --ml
python3 backtest_spreads.py --instrument NF --strategy all --ml

# Live margin check (all 5 strategies vs current balance)
python3 check_margins.py

# Dry runs (safe — no orders placed)
python3 auto_trader.py --dry-run
python3 spread_monitor.py --dry-run
python3 midday_conviction.py --dry-run
python3 exit_positions.py --dry-run
python3 trade_journal.py --dry-run
python3 autoloop_nf.py --dry-run
python3 fetch_intraday_options.py --dry-run
python3 lot_expiry_scanner.py --dry-run

# Standalone diagnostics (read-only)
python3 health_ping.py
python3 validate_all.py
python3 lot_expiry_scanner.py --show
python3 analyze_confidence.py
python3 replay_today.py
python3 autoexperiment_nf.py

# Model
python3 model_evolver.py
python3 model_evolver.py --no-data
```

---

## Key design choices

**Multi-strategy routing over single-strategy** — IC optimal on Mon/Tue (DTE 0-1, 84.6% WR). On Thu/Fri (DTE 4-5) IC net-negative after costs; Bear Call and Bull Put directional credits win instead (+₹65–81/lot). Wed skipped entirely (DTE 6, all strategies lose). Routing maximises capital efficiency across the week.

**EOD-only IC exit** — IC holds to 3:15 PM with no TP. A TP at 65% capture cost ₹21L over 5 years vs EOD hold — last 35% of theta is free money.

**Weekly Tuesday expiry** — Nifty50 has weekly Tuesday expiry (confirmed via Dhan expirylist API). Every trade is naturally DTE ≤ 7, maximising theta decay. BankNifty lost weekly expiry in Nov 2024 — this is why the system moved to Nifty50.

**Real 1-min option data** — All backtests use actual Dhan 1-min option bars from `data/nifty_options_cache/`. OHLCV-formula estimates are never used for strategy decisions — formula can't see theta decay, IV crush, or slippage.

**No VIX/confidence filter** — Grid search confirmed: filtering by VIX or ML confidence reduces trade frequency without improving P&L. Maximum frequency = maximum P&L for this strategy set.

**Straddle auto-upgrade** — When capital reaches ₹2.17L, Short Straddle (no wings, higher credit) automatically replaces all other strategies. Monitored by `check_margins.py`.

**Nightly model competition** — RF, XGBoost, LightGBM, CatBoost compete every night. Live trade outcomes injected at 10× weight. Midday reversal checkpoints injected at 5×. Model corrects its own mistakes over time.

**Buy-first order sequence** — Dhan margin rules require long leg on books before short leg. Violation triggers full unhedged margin on short legs. Enforced in `auto_trader.py`.

---

## What is NOT in this repo

Gitignored — lives only on the VM:

- `.env` — credentials
- `data/` — all CSVs, signals, trade history, today_trade.json
- `data/live_ic_trades.csv` — trade journal
- `data/nifty_options_cache/` — real 1-min NF option bars
- `models/` — trained champion.pkl, ensemble/*.pkl
- `logs/` — cron output

**No backtest P&L numbers, trade-level results, or credentials are ever committed.**

---

> **Risk warning:** Options trading carries a high level of risk. This system is provided as-is with no warranty. Use at your own risk.
