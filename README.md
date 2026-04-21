# Nifty50 Iron Condor Auto-Trader

Fully automated Nifty50 options trading on a GCP VM. Every weekday at 9:30 AM IST the system wakes up, reads the market, picks a direction, builds a 4-leg Iron Condor (SELL ATM CE + BUY ATM+150 CE + SELL ATM PE + BUY ATM-150 PE), places all 4 legs via Dhan, and sends a Telegram alert. `spread_monitor.py` watches every 1 minute during market hours and exits all 4 legs automatically on SL or TP hit. Every weeknight at 11 PM an ML model retrains. Every night at midnight an AI experiment loop tries to improve the model.

> **Disclaimer:** This project is for educational and research purposes only. Nothing here is financial advice. Options trading involves substantial risk of loss. Past results do not guarantee future performance. Trade at your own risk. Consult a SEBI-registered advisor before making any real trading decisions.

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
        — Writes data/news_sentiment.json (consumed by auto_trader.py as one vote)

9:30 AM IST, Mon–Fri
  └── auto_trader.py
        1. Pull latest market data
        2. Rule-based signal engine        →  CALL / PUT
        3. ML direction model              →  overrides rules when confident
        4. Build Iron Condor legs          →  SELL ATM CE + BUY ATM+150 CE
                                               SELL ATM PE + BUY ATM-150 PE
        5. Size position (lots)            →  5% of capital at risk, max 10 lots
        6. Place 4 legs via Dhan           →  BUY long wings first (margin rule),
                                               then SELL short legs
        7. Send Telegram alert

Every 1 min, 9:30 AM–3:14 PM IST
  └── spread_monitor.py
        — Fetches live LTPs for all 4 IC legs
        — SL fires if total spread cost ≥ net_credit × 1.5  (50% loss)
        — TP fires if total spread cost ≤ net_credit × 0.10 (90% of credit kept)
        — On hit: closes all 4 legs, marks exit in today_trade.json

11:00 AM IST, Mon–Fri
  └── midday_conviction.py
        — Checks all 4 IC leg LTPs + macro (S&P futures, DXY, VIX, crude)
        — Sends plain-English IC health summary to Telegram
        — Writes data/midday_checkpoints.csv (evolver uses reversal days)

3:15 PM IST, Mon–Fri
  └── exit_positions.py
        — Closes any open Nifty NRML positions not already hit by SL/TP
        — Prevents unintended overnight carry

3:30 PM IST, Mon–Fri
  └── trade_journal.py
        — Reads exit data from today_trade.json
        — Computes P&L, exit reason (SL/TP/EOD)
        — Appends row to data/live_ic_trades.csv
        — Sends EOD journal to Telegram

11:00 PM IST, Mon–Fri
  └── model_evolver.py
        — Retrains ML brain: Optuna HPO across RF + XGBoost + LightGBM + CatBoost
        — Injects live trade outcomes as real-label signal (10× weight)
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

## Strategy: Nifty50 Iron Condor

| | |
|---|---|
| Instrument | Nifty50 options (weekly Thursday expiry) |
| Trading days | All 5 weekdays — every day is a valid IC day |
| Structure | SELL ATM CE + BUY ATM+150 CE + SELL ATM PE + BUY ATM-150 PE |
| Spread width | 150 pts per side (NF strike spacing = 50 pts, ATM ± 3 strikes) |
| Net credit | ~₹108/lot average at entry |
| Stop-loss | Total spread cost grows to net_credit × 1.5 (50% loss of credit) |
| Take-profit | Total spread cost falls to net_credit × 0.10 (retain 90% of credit) |
| Risk per trade | 5% of available capital |
| Max lots | 10 (IC uses margin on both sides) |
| Lot size | 65 (Nifty50, Jan 2026+) |
| Order type | MARKET — BUY long wings first, SELL short legs second (Dhan margin rule) |
| Product | NRML — `spread_monitor.py` watches 1 min; `exit_positions.py` squares off at 3:15 PM |

### Why Iron Condor works on Nifty50

Weekly Thursday expiry means every IC is naturally DTE ≤ 7. Theta (time decay) destroys option premium from both sides simultaneously. The IC wins when Nifty stays within the wings — which happens ~85% of the time. The max-loss scenario (one wing blown through) is capped by the long legs.

### Order placement sequence (mandatory)

Dhan requires long leg on books before short leg gets margin benefit:
1. BUY ATM+150 CE (long call wing)
2. SELL ATM CE (short call leg)
3. BUY ATM-150 PE (long put wing)
4. SELL ATM PE (short put leg)

Never reverse this — full unhedged margin triggered on short legs if long not placed first.

---

## How direction is decided

Signal engine and ML model still vote on CALL vs PUT (which side of the market is favored). The IC trades BOTH sides regardless — the signal just determines which days the system enters (CALL signal → market expected to hold or rise moderately, PUT signal → hold or fall moderately; both suit IC). No VIX or ML confidence filter applied — maximum trade frequency gives maximum P&L.

**63 features** across nine layers: rule signals, technicals (RSI, ADX, HV20), global markets (S&P 500, Nikkei, S&P futures), macro (crude oil, DXY, US 10Y yield, USD/INR), volatility regime (VIX level, percentile, HV ratio), options sentiment (PCR, IV skew, OI surface at ATM±3, max pain), flow (FII net cash, bank ETF, top-5 constituent breadth), momentum (NF momentum, 52-week high distance, ORB range), calendar (DOW, DTE).

**Nightly model competition:** RF, XGBoost, LightGBM, CatBoost compete via Optuna HPO (30 trials each = 120 total). Winner saved as champion.pkl. Full 4-model ensemble saved for live voting.

---

## Architecture

```
auto_trader.py              9:30 AM runner — builds IC legs, places 4 orders, Telegram
spread_monitor.py           1-min intraday SL/TP watch — closes all 4 IC legs on trigger
exit_positions.py           3:15 PM EOD squareoff — closes any open Nifty NRML positions
trade_journal.py            3:30 PM — logs IC trade outcome to live_ic_trades.csv + Telegram
signal_engine.py            Rule-based indicators → data/signals.csv
ml_engine.py                Walk-forward ML → data/signals_ml.csv; champion.pkl fast predict
ml_engine_paper.py          Paper copy — autoresearcher tests here before promoting to live
model_evolver.py            11 PM — Optuna HPO + ensemble retrain → models/
data_fetcher.py             Downloads OHLCV + global + options data → data/
morning_brief.py            9:15 AM — news sentiment via Claude API → data/news_sentiment.json
health_ping.py              8:50 AM — pre-market heartbeat: token + capital + freshness
midday_conviction.py        11 AM — IC leg LTP check + macro + Telegram summary
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
scan_ic_rr.py               IC SL/TP parameter scanner (RR optimisation)
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
python3 scan_ic_rr.py               # scan SL/TP combinations

# Dry runs (safe — no orders placed, no Telegram sent)
python3 auto_trader.py --dry-run
python3 spread_monitor.py --dry-run
python3 midday_conviction.py --dry-run
python3 exit_positions.py --dry-run
python3 trade_journal.py --dry-run

# Model
python3 model_evolver.py
python3 model_evolver.py --no-data
python3 replay_today.py

# Autoresearch
python3 autoloop_nf.py --dry-run
python3 autoexperiment_nf.py

# Health
python3 health_ping.py
python3 validate_all.py
python3 lot_expiry_scanner.py --show
```

---

## Key design choices

**Iron Condor over naked options** — Real 1-min backtest (2021–2026, 1114 trades) confirmed IC wins ~85% of the time on NF weekly expiry. Naked long-option buying on the same data was a net loser due to theta decay and IV crush. IC collects premium from both sides simultaneously.

**Weekly Thursday expiry** — Nifty50 kept weekly Thursday expiry (SEBI mandate). Every IC is naturally DTE ≤ 7, maximising theta decay benefit. BankNifty lost weekly expiry in Nov 2024 — this is why the system moved to Nifty50.

**Real 1-min option data** — All backtests use actual Dhan 1-min option bars from `data/nifty_options_cache/`. OHLCV-formula estimates are never used for strategy decisions — formula can't see theta decay, IV crush, or slippage.

**No VIX/confidence filter** — Grid search confirmed: filtering by VIX or ML confidence reduces trade frequency without improving P&L. Maximum frequency = maximum P&L for this strategy.

**SL=50%, TP=90%** — Confirmed optimal via `scan_ic_rr.py` on 5-year real data. TP=90% fires ~16 times/year (expiry-day theta collapse). Holding to EOD on other days captures natural decay. Both parameters produce identical max drawdown.

**Nightly model competition** — RF, XGBoost, LightGBM, CatBoost compete every night. Live trade outcomes injected at 10× weight. Midday reversal checkpoints injected at 5×. Model corrects its own mistakes over time.

**Buy-first order sequence** — Dhan margin rules require long leg on books before short leg. Violation triggers full unhedged margin on short legs. Enforced in `auto_trader.py`.

---

## What is NOT in this repo

Gitignored — lives only on the VM:

- `.env` — credentials
- `data/` — all CSVs, signals, trade history, today_trade.json
- `data/live_ic_trades.csv` — IC trade journal
- `data/nifty_options_cache/` — real 1-min NF option bars
- `models/` — trained champion.pkl, ensemble/*.pkl
- `logs/` — cron output

**No backtest P&L numbers, trade-level results, or credentials are ever committed.**

---

> **Risk warning:** Options trading carries a high level of risk. This system is provided as-is with no warranty. Use at your own risk.
