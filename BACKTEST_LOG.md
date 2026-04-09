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
python3 backtest_engine.py                # uses whatever signals.csv is active
```

**Step 6b — Run all thresholds at once (comparison mode):**
```bash
python3 backtest_engine.py --compare      # runs ±1 through ±4 and prints comparison table
```

This is the fastest way to evaluate thresholds — runs everything in one shot, prints side-by-side table with costs, win rate, net P&L for each threshold.

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

### 10–12 Indicators and Scoring (Round 1 added indicators 9–12)

Each indicator scores +1 (bullish), -1 (bearish), or 0 (neutral).
Total score range: -10 to +10 (or -12 to +12 with PCR + FII/DII).

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
| 9 | HV20 (historical vol) | HV < 12% (calm) | HV > 20% (chaotic) | 12–20% |
| 10 | BN overnight gap | gap > +0.3% | gap < -0.3% | within ±0.3% |
| 11 | PCR (Put-Call Ratio) | PCR > 1.2 (contrarian bullish) | PCR < 0.8 (contrarian bearish) | 0.8–1.2 |
| 12 | FII net flows | FII net > +500Cr | FII net < -500Cr | within ±500Cr |

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
- Trades: 153 | Trade rate: 68.0% ✅
- Wins: 82 | Losses: 68 | Partials: 3 | Win rate: 54.7% ✅
- Gross P&L: ₹10,55,671 | Charges: ₹39,318 | Net P&L: ₹10,16,353
- Ending capital: ~₹15,46,353 | Max drawdown: -28.8%
- **Decision:** Best threshold so far. Pending ±1 test.

### Run 4 — Threshold ±3 (8 signals, with Dhan costs)
- Trades: 118 | Trade rate: 52.4%
- Wins: 60 | Losses: 55 | Win rate: 52.2%
- Gross P&L: ₹4,42,391 | Charges: ₹39,318 | Net P&L: ₹4,03,073
- Ending capital: ~₹9,33,073 | Max drawdown: -24.8%
- **Decision:** Lower P&L and lower win rate than ±2. Not preferred.

### Run 5 — Threshold ±1 vs ±2 vs ±3 vs ±4 (10 signals, with Dhan costs) ✅

| Threshold | Trades | Win Rate | Gross P&L | Charges | Net P&L | End Capital | Max DD |
|---|---|---|---|---|---|---|---|
| **±1** | **209** | 50.7% | ₹6,80,508 | ₹45,577 | **₹6,34,931** | **₹12,04,931** | -31.0% |
| ±2 | 166 | 47.8% ⚠️ | ₹2,31,483 | ₹28,935 | ₹2,02,548 | ₹7,62,548 | -26.3% |
| ±3 | 119 | **53.0%** | ₹4,04,905 | ₹22,978 | ₹3,81,927 | ₹9,11,927 | -30.5% |
| ±4 | 95 | 51.1% | ₹2,25,116 | ₹14,270 | ₹2,10,846 | ₹7,10,846 | **-19.6%** |

**Key findings:**
- ±1 wins on total P&L (₹6.35L) — more volume at 50.7% win rate beats fewer trades at 53%
- ±2 is the worst: 47.8% win rate (below 50%) — the 2 new indicators reshuffled which days
  land in this bucket, making it net noise. Avoid ±2 with 10-indicator setup.
- ±3 has the best win rate but only 90 fewer trades than ±1 give 40% less P&L for same drawdown
- **Decision: ±1 adopted as default.** Signal score direction matters; magnitude does not add edge.

**Why ±1 = "no threshold":** With integer-scored indicators, score ≥ 1 = score > 0 = any
directional lean. Score = 0 (perfect tie) still skips — no edge to exploit.

---

## Decisions Made

| Decision | Reason |
|---|---|
| Trade only Tue/Fri | Weekly options strategy requires specific entry days |
| Threshold ±1 (no threshold) | Signal direction matters; score magnitude adds no edge. ±1 gives best P&L (₹6.35L) with 10 indicators. ±2 became noise after adding HV20+BN gap. |
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

**Status: ✅ Signal logic built** — 10 indicators active now (8 original + HV20 + BN overnight gap). PCR and FII/DII need manual data download (instructions below).

### Round 1 — New Indicators Added

| # | Indicator | How it scores | Status |
|---|---|---|---|
| 9 | BN 20-day historical vol (HV20) | Low vol (<12%) = +1, High vol (>20%) = -1, mid = 0 | ✅ Auto-computed from banknifty.csv |
| 10 | BN overnight gap (today open vs yesterday close) | Gap > +0.3% = +1, gap < -0.3% = -1, else 0 | ✅ Auto-computed from banknifty.csv |
| 11 | PCR — BankNifty Put-Call Ratio | PCR > 1.2 = +1 (fear/contrarian bullish), PCR < 0.8 = -1, else 0 | ⚠️ Needs manual data download |
| 12 | FII net flows (₹ crore, cash market) | FII net > +500Cr = +1, net < -500Cr = -1, else 0 | ⚠️ Needs manual data download |

### How to Get PCR Data (NSE)

1. Go to: https://www.nseindia.com/report-detail/fo_eq_security
2. Select: Symbol = BANKNIFTY | From: 01-Sep-2021 | To: 09-Apr-2026
3. Download CSV
4. Run: `python3 data_fetcher.py --process-pcr <downloaded_filename.csv>`
5. This creates `data/pcr.csv` automatically

### How to Get FII/DII Data

1. Go to: https://www.nseindia.com/research/content/US_FiiDiiData.xlsx
2. Download the Excel file (covers several years of daily FII/DII flows)
3. Save relevant columns as `data/fii_dii.csv` with columns: `date, fii_net`
   - `fii_net` = FII net buy/sell in ₹ crore (positive = buying)
4. Re-run `python3 signal_engine.py 2` — it will auto-detect the file

## Planned Improvements (Round 2)

**Status: ✅ Built** — Run `python3 fetch_round2_data.py` to download NSE data (~6 min), then re-run signal engine.

### Run 6 — Round 2 indicators tested, all removed (10 indicators, with costs) ✅

| Threshold | Win Rate | Net P&L | vs 10-indicator best |
|---|---|---|---|
| ±1 (15 ind) | 47.7% ⚠️ | ₹2,04,493 | -68% |
| ±2 (15 ind) | 47.3% ⚠️ | ₹1,43,710 | -77% |
| ±3 (15 ind) | 47.3% ⚠️ | ₹91,223 | -86% |
| ±4 (15 ind) | 50.5% | ₹1,63,117 | -74% |

**All 5 Round 2 indicators added noise and reduced performance.** Reverted to 10 indicators.
Event filter (RBI + Budget hard NONE) **kept** — good risk management regardless of P&L.

Root causes for why each indicator failed as a scoring signal:
- PCR, OI direction, Max Pain — weekly convergence signals, not intraday-relevant
- FII F&O net futures — lagged + hedged, noisy day-to-day
- IV Rank — redundant with HV20 (double-counted volatility information)

**Current best config: 10 indicators, threshold ±1, event filter ON → ₹6,34,931 net P&L**

### Round 2 — New Indicators

| # | Indicator | Signal logic | Data source |
|---|---|---|---|
| 11 | PCR (Put-Call Ratio) | PCR > 1.2 = +1 (fear=contrarian bullish), PCR < 0.8 = -1 | NSE F&O bhavcopy |
| 12 | OI direction | CALL OI building faster = +1 (bulls entering), PUT faster = -1 | NSE F&O bhavcopy |
| 13 | Max Pain distance | Price below max pain = +1 (drift up), above = -1 | NSE F&O bhavcopy |
| 14 | IV Rank (HV Rank) | IV Rank < 30% (calm) = +1, > 70% (stressed) = -1 | Computed from HV20 |
| 15 | FII net F&O position | FII net long futures = +1, net short = -1 | NSE participant OI |
| — | Event filter (hard) | RBI MPC + Budget days → forced NONE (hard override, not scored) | Hardcoded calendar |

### How to Get Round 2 Data

```bash
cd ~/dhan-trading
python3 fetch_round2_data.py        # ~6 min, downloads 450 files, resumable
python3 signal_engine.py            # auto-detects new files, shows 15/16 active
python3 backtest_engine.py          # or --compare for threshold sweep
```

Files produced: `data/pcr.csv`, `data/max_pain.csv`, `data/oi_buildup.csv`, `data/fii_fo.csv`

### Max Pain — How It Works
For each possible strike price S, calculate total option WRITER losses:
- Call writers lose: (S − K) × OI for all calls with K < S (in-the-money)
- Put writers lose: (K − S) × OI for all puts with K > S (in-the-money)
- Max Pain = the S where total writer loss is **minimum**

Theory: option sellers (who have the most money and hedging power) push the market toward max pain by expiry. Signal: if BN price is >1% above max pain → drift down likely (bearish). If >1% below → drift up likely (bullish).

### Event Filter — Calendar Used
RBI MPC decision days (6 per year) + Union Budget day = ~7 no-trade days per year.
These are hard NONE overrides — score is irrelevant on these days.

---

## GCP VM Setup Notes

- **VM IP:** 34.45.55.132 (static, whitelisted on Dhan)
- **Python env:** `~/dhan-env/` (virtualenv)
- **Activate:** `source ~/dhan-env/bin/activate`
- **Data files:** `~/dhan-env/data/`
- **Dhan token expires:** ~24 hours — regenerate at dhan.co → API settings

---
*Last updated: Apr 2026*
