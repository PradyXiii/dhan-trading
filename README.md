# BankNifty Options Auto-Trader

Fully automated BankNifty options trading on a GCP VM. Every weekday at 9:30 AM IST the system wakes up, reads the market, picks a direction, sizes the position, places a Dhan Super Order with stop-loss and target baked in, and sends a Telegram alert. Every Saturday night it runs an AI-driven experiment loop that tries to improve its own model — keeping what works, reverting what doesn't. Nothing to do during market hours.

---

> **Disclaimer**
> This project is for educational and research purposes only. Nothing here is financial advice or a recommendation to trade. Options trading involves substantial risk of loss. Past results do not guarantee future performance. Trade at your own risk. Consult a SEBI-registered advisor before making any real trading decisions.

---

## What runs and when

```
Every 5 minutes (all 7 days)
  └── renew_token.py         — checks if token is 23h50m old, renews if so
                               (never lets it expire, no manual action needed)

8:50 AM IST, Mon–Fri
  └── health_ping.py
        — Pre-market heartbeat: Dhan token, signal freshness, capital,
          stale lock-file, critical alert log
        — Sends a single "all clear" (or alarm) to Telegram

9:30 AM IST, Mon–Fri
  └── auto_trader.py
        1. Pull latest market data
        2. Run rule-based signal engine  →  CALL / PUT / NONE
        3. Run ML direction model        →  overrides rules when confident
        4. Size the position (lots)      →  5% of capital at risk, min 1 lot
        5. Place Dhan Super Order        →  entry + SL + target in one call
        6. Send Telegram alert

11:00 AM IST, Mon–Fri
  └── midday_conviction.py
        — Reassesses the morning thesis using intraday BN spot, option LTP,
          and macro tape. Sends a Telegram conviction summary.

3:15 PM IST, Mon–Fri
  └── exit_positions.py
        — Closes any open BankNifty NRML positions not already hit by SL/TP
        — Prevents unintended overnight carry

3:30 PM IST, Mon–Fri
  └── trade_journal.py
        — Reads Dhan tradebook for actual fill prices
        — Compares oracle intent vs actual entry/exit
        — Computes slippage, P&L, exit reason (SL/TP/TRAIL/EOD)
        — Appends row to data/live_trades.csv
        — Sends EOD journal to Telegram

11:00 PM IST, Mon–Fri
  └── model_evolver.py
        — Retrains the ML brain: Optuna HPO across RF + XGBoost + LightGBM + CatBoost
        — Injects live trade outcomes as real-label training signal (10× weight)
        — Boosts historical rows matching miss-day patterns to reduce repeat errors
        — Picks the best model, saves it as models/champion.pkl
        — Also saves 4-model ensemble at models/ensemble/*.pkl
        — Sends a plain-English brain-training report to Telegram

11:30 PM IST, every Saturday
  └── autoloop_bn.py  (Autoresearch)
        — Claude AI proposes one small code change per experiment (feature or signal tweak)
        — autoexperiment_bn.py evaluates it: trains RF on all data, tests on last 252 days
        — If score improves: change is committed to git
        — If score drops: change is reverted, next idea tried
        — Runs 20 experiments overnight; sends Telegram after each + a morning summary
        — On completion, triggers model_evolver.py to retrain the 4 models on improved code

1st of every month, 10:00 AM IST
  └── lot_expiry_scanner.py
        — Checks if NSE has changed BankNifty lot size or expiry structure
        — Alerts via Telegram if anything has changed

Every Sunday, 2:00 AM IST
  └── Log rotation — trims any logs over 10 MB to the last 1000 lines
```

---

## Trade parameters

| | |
|---|---|
| Instrument | BankNifty options (monthly last-Tuesday expiry, Phase 4 Sep 2025+) |
| Trading days | All 5 weekdays — Wednesday is a normal day (not expiry day) |
| Direction | CALL or PUT — decided by ML oracle, informed by rule-based score |
| Strike | ATM first; walks OTM if unaffordable, ITM if capital is flush |
| Stop-loss | 15% of premium |
| Target | 37.5% of premium (RR = 2.5×) |
| Risk per trade | 5% of available capital |
| Minimum trade | 1 lot (always trades if you can physically afford it) |
| Max lots | 20 |
| Lot size | 30 (Jan 2026+) |
| Product | MARGIN (NRML) — EOD squareoff by exit_positions.py at 3:15 PM |

---

## How direction is decided

**Step 1 — Rule score (4 signals):**

| Signal | Bullish +1 | Bearish −1 |
|---|---|---|
| EMA20 | BN above 20-day average | BN below 20-day average |
| 5-day trend | BN up >+1% over 5 days | BN down >−1% over 5 days |
| VIX direction | VIX falling | VIX rising |
| BN vs Nifty | BN outperforming Nifty >+0.5% | Underperforming >−0.5% |

Score ≥ +1 → tentative CALL · Score ≤ −1 → tentative PUT

**Step 2 — ML override:**

An ensemble of walk-forward models (RF / XGBoost / LightGBM / CatBoost) votes on direction. The champion is the single best model; the full ensemble is used for live prediction agreement. It always outputs CALL or PUT — never skips a trade day.

31 features across five layers: rule signals, global markets (S&P 500, Nikkei, S&P futures), macro drivers (crude oil, DXY, US 10Y yield, USD/INR), options sentiment (PCR, VIX), and institutional flow (FII net cash, z-scored).

**Step 3 — Autoresearch (weekly, Saturday night):**

Claude AI iterates over the feature engineering and signal logic. Each proposed change is evaluated on a 252-day out-of-sample holdout. Improvements are committed to git; regressions are reverted. The 4-model ensemble is retrained on Sunday morning using the improved code. The model gets smarter every week without any human input.

**Step 4 — Live feedback loop:**

After each live trade, `trade_journal.py` records the actual outcome. Every night the evolver injects those real outcomes back into training and boosts historical rows that match "miss-day" market conditions, so the model incrementally corrects its own mistakes.

---

## Architecture

```
auto_trader.py           Morning runner — orchestrates everything, places the order
signal_engine.py         Rule-based indicators → data/signals.csv
ml_engine.py             Walk-forward ML → data/signals_ml.csv; loads champion.pkl for fast predict
model_evolver.py         Nightly 11 PM — Optuna HPO (RF/XGB/LGB/CAT) → models/champion.pkl + ensemble/
backtest_engine.py       Historical P&L simulation with full cost model
backtest_live_context.py Research backtest — tests intraday live-context overrides
data_fetcher.py          Downloads OHLCV + global data → data/ (incremental, parallel)
health_ping.py           Pre-market (8:50 AM) heartbeat: token + capital + freshness checks
midday_conviction.py     Midday (11 AM) thesis reassessment + Telegram update
exit_positions.py        EOD 3:15 PM — closes any open NRML positions before market close
trade_journal.py         EOD 3:30 PM — captures actual fills vs oracle, builds live_trades.csv
lot_expiry_scanner.py    Monthly check: BankNifty lot size / expiry day changes
replay_today.py          Post-mortem tool — runs after evolver to replay today with new ensemble
renew_token.py           Dynamic token renewal — 23h50m interval, 7 days a week
notify.py                Telegram send helper
dhan_mcp.py              MCP server — query live Dhan positions/P&L from Claude Code
autoloop_bn.py           Saturday autoresearch loop — Claude API experiments → git keep/revert
autoexperiment_bn.py     Fast 252-day holdout evaluator used by autoloop
research_program_bn.md   Research brief — defines what autoresearch may and may not change
setup_automation.sh      One-shot VM setup: install deps, set up all cron jobs, dry-run test
```

---

## Setup

### What you need

- GCP VM (or any Linux server) with Python 3.10+
- Dhan trading account with API access enabled
- Telegram bot (create one at t.me/BotFather)
- Anthropic API key (for the Saturday autoresearch loop — get one at console.anthropic.com)

### 1. Clone and install

```bash
git clone <your-repo-url>
cd dhan-trading
pip3 install pandas numpy yfinance requests python-dotenv scikit-learn \
             optuna xgboost lightgbm catboost joblib anthropic \
             --break-system-packages
```

### 2. Set up credentials

```bash
cp .env.example .env
nano .env
```

Fill in:
```
DHAN_ACCESS_TOKEN=your_token       # from dhan.co → API settings
DHAN_CLIENT_ID=your_client_id
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
ANTHROPIC_API_KEY=your_key         # from console.anthropic.com → API keys
```

### 3. First-time data fetch and ML training

```bash
python3 data_fetcher.py       # downloads all market data → data/
python3 signal_engine.py      # generates data/signals.csv
python3 ml_engine.py          # walk-forward training (~2 min) → data/signals_ml.csv
python3 model_evolver.py      # first HPO run — takes 8–12 min, saves models/champion.pkl
```

### 4. Install cron and test

```bash
bash setup_automation.sh      # sets up all cron jobs, tests API, runs dry-run
```

This installs every scheduled job automatically: token renewer (every 5 min), health ping (9:05 AM), auto trader (9:30 AM), midday conviction (11 AM), EOD squareoff (3:15 PM), trade journal (3:30 PM), model evolver (11 PM), autoresearch loop (Saturday 11:30 PM), lot scanner (1st of month), log rotation (Sunday 2 AM).

### 5. Verify

```bash
python3 auto_trader.py --dry-run       # full morning flow without placing an order
python3 autoloop_bn.py --dry-run       # autoresearch baseline check + Telegram test
crontab -l                             # confirm all cron jobs are listed
```

---

## Useful commands

```bash
# Data and signals
python3 data_fetcher.py                      # fetch latest market data
python3 signal_engine.py                     # recompute rule signals
python3 ml_engine.py --predict-today         # fast single ML prediction (<10 sec)
python3 ml_engine.py --analyze               # feature importance report

# Backtesting
python3 backtest_engine.py                   # rule-based backtest
python3 backtest_engine.py --real-premium    # use real historical premiums
python3 backtest_engine.py --real-premium-ml # ML backtest with real premiums
python3 backtest_live_context.py             # explore intraday-context overrides

# Live test
python3 auto_trader.py --dry-run             # see exactly what would trade today

# Model evolver
python3 model_evolver.py                     # run nightly HPO manually
python3 model_evolver.py --no-data           # HPO only, skip data fetch
python3 model_evolver.py --trials 30         # override trial count (default 30)

# Autoresearch
python3 autoloop_bn.py --dry-run             # measure baseline score + Telegram test
python3 autoloop_bn.py --experiments 3       # quick 3-experiment live test
python3 autoloop_bn.py --no-evolver          # run experiments without retraining at end

# Post-mortem replay
python3 replay_today.py                      # replay today with current ensemble, vs actual

# Pre-market checks
python3 health_ping.py                       # manual pre-market heartbeat
python3 midday_conviction.py --dry-run       # midday thesis check without Telegram

# EOD scripts (run manually if needed)
python3 exit_positions.py --dry-run          # check open positions without closing
python3 trade_journal.py --dry-run           # journal check without API calls

# Token management
python3 renew_token.py                       # check / force token renewal

# Lot/expiry scanner
python3 lot_expiry_scanner.py --show         # print current lot size and expiry override
python3 lot_expiry_scanner.py                # run scan + alert if anything changed
```

---

## Live P&L from Claude Code

`dhan_mcp.py` is an MCP server that lets you ask Claude Code about your live positions:

> "Show me my current positions"
> "What's today's P&L?"
> "Did my stop-loss trigger?"

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "dhan": {
      "command": "python3",
      "args": ["/path/to/dhan-trading/dhan_mcp.py"]
    }
  }
}
```

---

## Key design choices

**ML as direction oracle, not filter** — Rules provide a starting direction. The ML model overrides it when market conditions disagree. It always produces a direction — CALL or PUT — and never skips a trade.

**Autoresearch (Karpathy pattern)** — Every Saturday night Claude AI proposes small changes to feature engineering (`ml_engine.py`) and signal logic (`signal_engine.py`). Each change is evaluated on a 252-day out-of-sample holdout using a fixed RF. Improvements are committed; regressions are reverted. The 4 models retrain on the improved code. The system improves itself week over week without human involvement.

**Live feedback loop** — After each trade, the actual outcome (SL hit vs TP hit) gets injected back into the model's training data with high weight. Patterns that caused past misses get upweighted in historical training so the model gradually reduces repeat errors.

**Monthly expiry (Phase 4, Sep 2025+)** — BankNifty switched from weekly Thursday expiry to monthly last-Tuesday expiry. All 5 weekdays are now valid trading days.

**Dynamic token renewal** — `renew_token.py` checks every 5 minutes and renews at exactly T+23h50m. The token never reaches its 24h expiry regardless of day or time.

**1-lot minimum** — The 5% risk rule sizes positions (more capital → more lots), not a gate. If you can physically afford 1 lot, the system trades it.

**Nightly model competition** — RF, XGBoost, LightGBM, and CatBoost compete every night via Optuna HPO (30 trials per model = 120 trials total). The winner is saved as champion.pkl and the full 4-model ensemble is saved to models/ensemble/ for live agreement voting.

**Macro feature layer** — The model explicitly captures FII-driving forces: crude oil return, DXY return, US 10-year yield change, and USD/INR return. When rupee weakens, dollar strengthens, or crude spikes, FII outflows accelerate and banking stocks lead the selloff — these signals are now baked in alongside the technical and options-sentiment features.

---

## What is NOT in this repo

Gitignored for safety — these live only on the VM, never in version control:

- `.env` — Dhan token, client ID, Telegram bot token, chat ID, Anthropic API key
- `data/` — all fetched OHLCV, signal CSVs, live trade journal, today_trade.json
- `models/` — trained champion.pkl, champion_meta.json, ensemble/*.pkl
- `logs/` — cron log output for every scheduled job
- `__pycache__/`, `*.pyc` — Python bytecode
- `token_meta.json` — token renewal state

---

> **Risk warning**: Options trading carries a high level of risk. This system is provided as-is with no warranty of fitness for live trading. Use at your own risk.
