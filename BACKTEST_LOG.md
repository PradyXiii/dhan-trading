# BankNifty Options Backtest — Project Log

## What This Project Does
Backtests a weekly BankNifty ATM options trading strategy from Sep 2021 to Apr 2026.
Trades are taken only on **Tuesdays and Fridays** based on a signal scoring system.
All code runs on a **Google Cloud VM** at IP `34.45.55.132` (whitelisted on Dhan's platform).

---

## Architecture — Files and What They Do

| File | What it does |
|---|---|
| `.env` | Stores Dhan API credentials (never committed to GitHub) |
| `.gitignore` | Keeps `.env` and data CSVs out of GitHub |
| `test_connection.py` | Quick test to verify Dhan API token is valid |
| `data_fetcher.py` | Downloads all 6 market data sources and saves to `data/` folder |
| `signal_engine.py` | Computes 8 indicators, scores each Tue/Fri, generates CALL/PUT/NONE |
| `backtest_engine.py` | Simulates trades with capital management, costs, and P&L calculation |
| `data/` | Folder containing all CSV files (not in GitHub — lives only on GCP VM) |

---

## How to Run Everything (on GCP VM)

**Step 1 — Open terminal:** Go to console.cloud.google.com → Compute Engine → VM instances → SSH button

**Step 2 — Activate environment:**
```bash
cd ~/dhan-env && source ~/dhan-env/bin/activate
```

**Step 3 — Refresh token (if needed):**
Edit `~/.dhan-env/.env` and update `DHAN_ACCESS_TOKEN` (token expires every ~24 hours)
```bash
python3 test_connection.py
```

**Step 4 — Re-fetch data (only needed when new data required):**
```bash
python3 data_fetcher.py
```

**Step 5 — Run signal engine with a threshold:**
```bash
python3 signal_engine.py 3      # threshold ±3 (current default)
python3 signal_engine.py 2      # test with threshold ±2
python3 signal_engine.py 4      # test with threshold ±4
```

**Step 6 — Run backtest:**
```bash
python3 backtest_engine.py
```

---

## Data Sources

| Data | Source | Frequency | File |
|---|---|---|---|
| BankNifty OHLCV | Dhan API (securityId=25, IDX_I) | Daily | banknifty.csv |
| Nifty50 OHLCV | Dhan API (securityId=13, IDX_I) | Daily | nifty50.csv |
| India VIX | Yahoo Finance (^INDIAVIX) | Daily | india_vix.csv |
| S&P 500 | Yahoo Finance (^GSPC) | Daily | sp500.csv |
| Nikkei 225 | Yahoo Finance (^N225) | Daily | nikkei.csv |
| S&P 500 Futures | Yahoo Finance (ES=F) | Daily | sp500_futures.csv |

**Period:** 1 Sep 2021 → 9 Apr 2026 | **~1,125 trading days**

---

## Signal Engine Logic

### 8 Indicators and Scoring

Each indicator scores +1 (bullish), -1 (bearish), or 0 (neutral).
Total score range: -8 to +8.

| # | Indicator | Bullish (+1) | Bearish (-1) | Neutral (0) |
|---|---|---|---|---|
| 1 | EMA20 | BN close > EMA20 | BN close < EMA20 | — |
| 2 | RSI14 | RSI > 55 | RSI < 45 | 45–55 |
| 3 | 5-day trend | BN change > +1% | BN change < -1% | within ±1% |
| 4 | VIX direction | VIX falling | VIX rising | unchanged |
| 5 | S&P500 change | prev-day S&P > 0% | prev-day S&P < 0% | — |
| 6 | Nikkei change | prev-day Nikkei > 0% | prev-day Nikkei < 0% | — |
| 7 | S&P futures gap | gap > +0.2% | gap < -0.2% | within ±0.2% |
| 8 | BN-NF divergence | BN outperforms NF >+0.5% | BN underperforms >-0.5% | within ±0.5% |

### Signal Decision
- Score ≥ +THRESHOLD → **BUY CALL**
- Score ≤ -THRESHOLD → **BUY PUT**
- Otherwise → **NO TRADE**

---

## Trade Rules

| Parameter | Value |
|---|---|
| Trade days | Tuesdays and Fridays only |
| Option type | ATM weekly options (BankNifty) |
| Lot size | 30 |
| Risk per trade | 5% of current capital |
| Stop-loss | 30% of premium |
| Target (RR) | **1.4:1 on Tuesdays**, **2.0:1 on Fridays** |
| Entry | Day's open price |
| Exit | Intraday (same day) — no carryforward |
| Starting capital | ₹30,000 |
| Monthly top-up | ₹10,000 added at start of each new month |

### Premium Estimation (no live options data)
- Tuesday (1 day to expiry): **0.4% of spot price**
- Friday (5 days to expiry): **0.9% of spot price**

### Intraday Exit Simulation (using daily OHLCV)
- Uses delta ≈ 0.5 (ATM option) to convert underlying move → option price change
- CALL: TP hit if day's HIGH ≥ TP level; SL hit if day's LOW ≤ SL level
- PUT: TP hit if day's LOW ≤ TP level; SL hit if day's HIGH ≥ SL level
- Both triggered same day: use open→close direction to decide which hit first
- Neither triggered: exit at close (partial P&L)

---

## Transaction Costs (Dhan Platform)

Based on Dhan's pricing + NSE statutory charges:

| Charge | Rate | Applies to |
|---|---|---|
| Brokerage | ₹20 per order (₹40 round-trip) | Dhan flat fee |
| STT | 0.0625% of premium | Sell side |
| NSE Exchange charges | 0.053% of premium | Both sides |
| NSCCL Clearing | 0.0005% of premium | Both sides |
| GST | 18% on brokerage + exchange | — |
| Stamp duty | 0.003% of premium | Buy side |
| SEBI turnover | 0.0001% | Both sides |

---

## Backtest Results Log

### Run 1 — Threshold ±4 (8 signals, no costs)
- Trades: 85 | Trade rate: 37.7%
- Wins: 43 | Losses: 39 | Win rate: 52.4%
- Net P&L: ₹1,83,858 | Ending capital: ₹6,63,858
- Max drawdown: -15.4%
- **Decision:** Trade rate too low. Tested ±3.

### Run 2 — Threshold ±3 (8 signals, no costs)
- Trades: 118 | Trade rate: 52.4% ✅ (above 50% target)
- Wins: 60 | Losses: 55 | Win rate: 52.2% ✅ (barely changed)
- Net P&L: ₹4,39,238 | Ending capital: ₹9,69,238
- Max drawdown: -24.3%
- **Decision:** Win rate held. Threshold ±3 adopted as default. Testing ±2 next.

### Run 3 — Threshold ±2 (8 signals, with Dhan costs)
*(pending)*

### Run 4 — Threshold ±3 (8 signals, with Dhan costs)
*(pending)*

---

## Decisions Made

| Decision | Reason |
|---|---|
| Trade only Tue/Fri | Weekly options strategy requires specific entry days |
| Threshold ±3 > ±4 | Win rate held at 52% while trade rate jumped from 38% to 52% |
| No carryforward | Avoid theta decay overnight; user preference |
| Delta ≈ 0.5 for exits | ATM option approximation — no live options data available |
| Dhan API for BN/NF data | Most accurate Indian index data available |
| yfinance for global data | VIX, S&P, Nikkei not available via Dhan |

---

## Known Limitations

1. **Premium is estimated** — real premiums vary with IV. High-IV days = expensive premiums not captured.
2. **No options chain data** — PCR, Max Pain, OI not yet included (planned in Round 1 additions).
3. **Daily OHLCV only** — intraday SL/TP simulation is approximate.
4. **No news/events filter** — RBI policy days, Budget, F&O expiry weeks not excluded.
5. **Fixed lot size** — assumes standard lot size of 30 throughout (SEBI may change this).

---

## Planned Improvements (Round 1)

- [ ] PCR (Put-Call Ratio) — NSE historical data
- [ ] FII/DII net buy-sell data — NSE daily reports
- [ ] GIFT Nifty / SGX pre-market gap signal
- [ ] BankNifty 20-day historical volatility signal

## Planned Improvements (Round 2)

- [ ] Max Pain level signal
- [ ] Open Interest buildup direction
- [ ] IV Rank / IVP (needs historical options chain)
- [ ] News/event calendar filter (avoid RBI, Budget days)

---

## GCP VM Setup Notes

- **VM IP:** 34.45.55.132 (static, whitelisted on Dhan)
- **Python env:** `~/dhan-env/` (virtualenv)
- **Activate:** `source ~/dhan-env/bin/activate`
- **Data files:** `~/dhan-env/data/`
- **Dhan token expires:** ~24 hours — regenerate at dhan.co → API settings

---
*Last updated: Apr 2026*
