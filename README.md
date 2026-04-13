# BankNifty Options Auto-Trader

Fully automated BankNifty options trading on a GCP VM. Every weekday at 9:15 AM IST, the system wakes up, reads the market, picks a direction, sizes the position, places a Dhan Super Order with stop-loss and target baked in, and sends a Telegram alert. Nothing to do during market hours.

---

> **Disclaimer**
> This project is for educational and research purposes only. Nothing here is financial advice or a recommendation to trade. Options trading involves substantial risk of loss. Past results do not guarantee future performance. Trade at your own risk. Consult a SEBI-registered advisor before making any real trading decisions.

---

## What runs and when

```
Every 5 minutes (all 7 days)
  └── renew_token.py         — checks if token is 23h50m old, renews if so
                               (never lets it expire, no manual action needed)

9:15 AM IST, Mon–Fri
  └── auto_trader.py
        1. Pull latest market data
        2. Run rule-based signal engine  →  CALL / PUT / NONE
        3. Run ML direction model        →  overrides rules when confident
        4. Size the position (lots)      →  5% of capital at risk, min 1 lot
        5. Place Dhan Super Order        →  entry + SL + target in one call
        6. Send Telegram alert

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
        — Retrains the ML brain: Optuna HPO across RF + XGBoost + LightGBM
        — Injects live trade outcomes as real-label training signal (10× weight)
        — Boosts historical rows matching miss-day patterns to reduce repeat errors
        — Picks the best model, saves it as models/champion.pkl
        — Sends a plain-English brain-training report to Telegram

1st of every month, 10:00 AM IST
  └── lot_expiry_scanner.py
        — Checks if NSE has changed BankNifty lot size or expiry structure
        — Alerts via Telegram if anything has changed
```

---

## Trade parameters

| | |
|---|---|
| Instrument | BankNifty options (monthly last-Tuesday expiry, Phase 4 Sep 2025+) |
| Trading days | All 5 weekdays — Wednesday is a normal day (not expiry day) |
| Direction | CALL or PUT — decided by ML oracle, informed by rule-based score |
| Strike | ATM first, walks OTM if unaffordable for your capital |
| Stop-loss | 15% of premium |
| Target | 37.5% of premium (RR = 2.5×) |
| Trailing stop | ₹5 option price movement |
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

A walk-forward model (champion selected nightly from RF / XGBoost / LightGBM) overrides the rule direction. It is a direction oracle — always outputs CALL or PUT, never skips a trade day.

**Step 3 — Live feedback loop:**

After each live trade, `trade_journal.py` records the actual outcome. Every night the evolver injects those real outcomes back into training and boosts historical rows that match "miss-day" market conditions, so the model incrementally corrects its own mistakes.

---

## Architecture

```
auto_trader.py         Morning runner — orchestrates everything, places the order
signal_engine.py       Rule-based indicators → data/signals.csv
ml_engine.py           Walk-forward ML → data/signals_ml.csv; loads champion.pkl for fast predict
model_evolver.py       Nightly 11 PM — Optuna HPO (RF/XGB/LGB) → models/champion.pkl
backtest_engine.py     Historical P&L simulation with full cost model
data_fetcher.py        Downloads OHLCV + global data → data/  (incremental, parallel)
exit_positions.py      EOD 3:15 PM — closes any open NRML positions before market close
trade_journal.py       EOD 3:30 PM — captures actual fills vs oracle, builds live_trades.csv
lot_expiry_scanner.py  Monthly check: BankNifty lot size / expiry day changes
renew_token.py         Dynamic token renewal — 23h50m interval, 7 days a week
notify.py              Telegram send helper
dhan_mcp.py            MCP server — query live Dhan positions/P&L from Claude Code
setup_automation.sh    One-shot VM setup: install deps, set up all cron jobs, dry-run test
```

---

## Setup

### What you need

- GCP VM (or any Linux server) with Python 3.10+
- Dhan trading account with API access enabled
- Telegram bot (create one at t.me/BotFather)

### 1. Clone and install

```bash
git clone <your-repo-url>
cd dhan-trading
pip3 install pandas numpy yfinance requests python-dotenv scikit-learn \
             optuna xgboost lightgbm joblib mcp --break-system-packages
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
```

### 3. First-time data fetch and ML training

```bash
python3 data_fetcher.py       # downloads all market data → data/
python3 signal_engine.py      # generates data/signals.csv
python3 ml_engine.py          # walk-forward training (~2 min) → data/signals_ml.csv
python3 model_evolver.py      # first HPO run — takes 5–10 min, saves models/champion.pkl
```

### 4. Install cron and test

```bash
bash setup_automation.sh      # sets up all cron jobs, tests API, runs dry-run
```

This installs six cron jobs: token renewer (every 5 min), auto trader (9:15 AM), EOD squareoff (3:15 PM), trade journal (3:30 PM), model evolver (11 PM), lot scanner (1st of month).

### 5. Verify

```bash
python3 auto_trader.py --dry-run    # full morning flow without placing an order
crontab -l                          # confirm all cron jobs are listed
cat token_meta.json                 # shows when token was last renewed
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

# Live test
python3 auto_trader.py --dry-run             # see exactly what would trade today

# Model evolver
python3 model_evolver.py                     # run nightly HPO manually
python3 model_evolver.py --no-data           # HPO only, skip data fetch

# EOD scripts (run manually if needed)
python3 exit_positions.py --dry-run          # check open positions without closing
python3 trade_journal.py --dry-run           # journal check without API calls

# Token management
python3 renew_token.py                       # check / force token renewal
cat token_meta.json                          # see when it was last renewed

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

**Live feedback loop** — After each trade, the actual outcome (SL hit vs TP hit) gets injected back into the model's training data with high weight. Patterns that caused past misses get upweighted in historical training so the model gradually reduces repeat errors.

**Monthly expiry (Phase 4, Sep 2025+)** — BankNifty switched from weekly Thursday expiry to monthly last-Tuesday expiry. All 5 weekdays are now valid trading days.

**Dynamic token renewal** — `renew_token.py` checks every 5 minutes and renews at exactly T+23h50m. The token never reaches its 24h expiry regardless of day or time.

**1-lot minimum** — The 5% risk rule sizes positions (more capital → more lots), not a gate. If you can physically afford 1 lot, the system trades it.

**Nightly model competition** — RF, XGBoost, and LightGBM compete every night via Optuna HPO (120 trials total). The winner is saved as champion.pkl and loaded in under 5 seconds the next morning.

---

> **Risk warning**: Options trading carries a high level of risk. This system is provided as-is with no warranty of fitness for live trading. Use at your own risk.
