# BankNifty Options Backtest — Project Log

## What This Project Does
Backtests a weekly BankNifty ATM options trading strategy from Sep 2021 to Apr 2026.
Trades are taken **every trading day (Mon–Fri)** based on a signal scoring system.
Wednesday (expiry day) is included as a 0 DTE gamma trade.
All code runs on a **Google Cloud VM** at IP `34.45.55.132` (whitelisted on Dhan's platform).

---

## Architecture — Files and What They Do

| File | What it does |
|---|---|
| `.env` | Stores Dhan API credentials (never committed to GitHub) |
| `.gitignore` | Keeps `.env` and data CSVs out of GitHub |
| `test_connection.py` | Quick test to verify Dhan API token is valid |
| `data_fetcher.py` | Downloads all 6 market data sources and saves to `data/` folder |
| `signal_engine.py` | Computes 10 indicators, scores each day, generates CALL/PUT/NONE |
| `backtest_engine.py` | Simulates trades with capital management, costs, and P&L calculation |
| `fetch_round2_data.py` | Downloads NSE F&O bhavcopy + participant OI (Round 2 data, retained for research) |
| `data/` | Folder containing all CSV files (not in GitHub — lives only on GCP VM) |

---

## How to Run Everything (on GCP VM)

**Step 1 — Open terminal:** Go to console.cloud.google.com → Compute Engine → VM instances → SSH button

**Step 2 — Navigate to project:**
```bash
cd ~/dhan-trading
```

**Step 3 — Refresh token (if needed):**
Edit `~/dhan-trading/.env` and update `DHAN_ACCESS_TOKEN` (token expires every ~24 hours)
```bash
python3 test_connection.py
```

**Step 4 — Re-fetch data (only needed when new data required):**
```bash
python3 data_fetcher.py
```

**Step 5 — Run signal engine + backtest (default: all 5 days, threshold ±1):**
```bash
python3 signal_engine.py && python3 backtest_engine.py
```

**Step 5b — Test specific day combinations or thresholds:**
```bash
python3 signal_engine.py 1 mon,tue,wed,thu,fri   # all 5 days (default)
python3 signal_engine.py 1 thu,tue                # Thursday + Tuesday only
python3 signal_engine.py 2 thu                    # Thursday alone, threshold ±2
python3 signal_engine.py 1 wed                    # Wednesday (0 DTE) alone
```

**Step 5c — Run all thresholds at once (comparison mode):**
```bash
python3 backtest_engine.py --compare              # ±1 through ±4, current day config
python3 backtest_engine.py --compare thu,tue       # comparison for specific days
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

**Period:** 1 Sep 2021 → 8 Apr 2026 | **~1,123 trading days**

### Dhan API Timezone Fix
Dhan timestamps are midnight IST (UTC+5:30). When parsed as UTC, every date shifts 1 day
earlier (Mon→Sun, Tue→Mon, etc.). Fixed in `data_fetcher.py` by adding IST offset.
One-time patch for existing CSVs: `python3 data_fetcher.py --fix-dates`

---

## Signal Engine Logic

### 4 Active Indicators (as of Run 8)

Each indicator scores +1 (bullish), -1 (bearish), or 0 (neutral).
Active score range: -4 to +4.

| # | Indicator | Bullish (+1) | Bearish (-1) | Neutral (0) | Status |
|---|---|---|---|---|---|
| 1 | EMA20 | BN close > EMA20 | BN close < EMA20 | — | **ACTIVE** |
| 2 | 5-day trend | BN change > +1% | BN change < -1% | within ±1% | **ACTIVE** |
| 3 | VIX direction | VIX falling | VIX rising | unchanged | **ACTIVE** |
| 4 | BN-NF divergence | BN outperforms NF >+0.5% | BN underperforms >-0.5% | within ±0.5% | **ACTIVE** |
| 5 | RSI14 | RSI > 55 | RSI < 45 | 45–55 | inactive (audit trail only) |
| 6 | S&P500 change | prev-day S&P > 0% | prev-day S&P < 0% | — | inactive (audit trail only) |
| 7 | Nikkei change | prev-day Nikkei > 0% | prev-day Nikkei < 0% | — | inactive (audit trail only) |
| 8 | S&P futures gap | gap > +0.2% | gap < -0.2% | within ±0.2% | inactive (audit trail only) |
| 9 | HV20 (historical vol) | HV < 12% (calm) | HV > 20% (chaotic) | 12–20% | inactive (audit trail only) |
| 10 | BN overnight gap | gap > +0.3% | gap < -0.3% | within ±0.3% | inactive (audit trail only) |

Inactive indicators are still computed and written to signals.csv (for audit/research) but do not contribute to the score used for trade decisions.

### Signal Decision
- Score ≥ +THRESHOLD → **BUY CALL**
- Score ≤ -THRESHOLD → **BUY PUT**
- Otherwise → **NO TRADE**
- Event days (RBI MPC + Budget) → **forced NO TRADE** regardless of score

### Indicators Tested and Deactivated
**Round 2 (PCR, OI, MaxPain, FII F&O, IV Rank):** all 5 degraded performance; wrong time horizon and redundant with HV20. Raw data retained in `data/` for research.

**Run 8 attribution study:** Macro signals (S&P500, Nikkei, S&P futures, BN overnight gap) identified as negative drag via indicator_attribution.py. RSI14 and HV20 marginally positive but below noise threshold. Deactivated 6 indicators, kept 4 India-specific technical signals.

---

## Trade Rules

### Per-Day Configuration

| Day | DTE | Premium | RR | Breakeven WR |
|---|---|---|---|---|
| Monday | 2 | spot × 0.57% | 1.6:1 | 38.5% |
| Tuesday | 1 | spot × 0.40% | 1.4:1 | 41.7% |
| Wednesday | 0.25 | spot × 0.20% | 1.0:1 | 50.0% |
| Thursday | 6 | spot × 0.98% | 2.0:1 | 33.3% |
| Friday | 5 | spot × 0.89% | 2.0:1 | 33.3% |

### Premium Estimation Formula
`premium = spot × 0.004 × sqrt(DTE)`

Calibrated from: Tuesday (1 DTE) = 0.4%, Friday (5 DTE) = 0.9%.
Wednesday uses DTE=0.25 (≈6 hours of trading at open on expiry day).

### Capital Management

| Parameter | Value |
|---|---|
| Starting capital | ₹30,000 |
| Monthly top-up | ₹10,000 added at start of each new month |
| Lot size | 30 |
| Max lots | 20 (cap for liquidity/margin realism) |
| Risk per trade | 5% of current capital |
| Stop-loss | 30% of premium |

### Intraday Exit Simulation (using daily OHLCV)
- Entry at day's OPEN. Exit attempted intraday — no overnight holding.
- Uses delta ≈ 0.5 (ATM option) to convert underlying move → option price change
- CALL: TP hit if day's HIGH ≥ TP level; SL hit if day's LOW ≤ SL level
- PUT: TP hit if day's LOW ≤ TP level; SL hit if day's HIGH ≥ SL level
- Both triggered same day: use open→close direction to decide which hit first
- Neither triggered: exit at close (partial P&L)

---

## Transaction Costs (Dhan Platform)

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

### Runs 1–4 — Early exploration (Tue/Fri only, 8 indicators, pre-timezone fix)

These used **incorrect dates** (Dhan timezone bug — all dates were 1 day early).
Results are retained for historical reference but should not be compared to corrected runs.

| Run | Config | Trades | WR | Net P&L | Decision |
|---|---|---|---|---|---|
| 1 | ±4, 8 ind, no costs | 85 | 52.4% | ₹1.84L | Trade rate too low |
| 2 | ±3, 8 ind, no costs | 118 | 52.2% | ₹4.39L | Better, adopted ±3 |
| 3 | ±2, 8 ind, with costs | 153 | 54.7% | ₹10.16L | Best so far |
| 4 | ±3, 8 ind, with costs | 118 | 52.2% | ₹4.03L | Worse than ±2 |

### Run 5 — Threshold sweep (10 indicators, pre-timezone fix)

| Threshold | Trades | WR | Net P&L | Decision |
|---|---|---|---|---|
| **±1** | 209 | 50.7% | **₹6.35L** | Best P&L — adopted |
| ±2 | 166 | 47.8% | ₹2.03L | Noise |
| ±3 | 119 | 53.0% | ₹3.82L | Good WR but less P&L |
| ±4 | 95 | 51.1% | ₹2.11L | Too few trades |

### Run 6 — Round 2 indicators tested and removed

All 5 Round 2 indicators (PCR, OI, MaxPain, FII F&O, IV Rank) degraded performance.
Win rate dropped from 50.7% → 47.3-47.7%, P&L from ₹6.35L → <₹2L.
Reverted to 10 indicators. Event filter (RBI MPC + Budget) kept.

### Run 8 — Indicator attribution + 4-indicator optimisation (CURRENT BEST) ✅

**Methodology:** `indicator_attribution.py` tested each of 10 indicators individually and in combinations. Found that macro signals (S&P500, Nikkei, S&P futures gap, BN overnight gap) are net negative drag — they add noise that overrides genuine India-specific signals.

**Attribution results (threshold ±1, all 5 days, indicator_attribution.py):**

| Config | Trades | WR | Net P&L |
|---|---|---|---|
| All 10 combined (baseline) | ~990 | 50.5% | ₹1.00Cr |
| No macro (5 India-only) | ~895 | 51.8% | ₹1.30Cr |
| Top 2: trend5 + BN-NF div | 585 | 55.6% | ₹1.65Cr |
| **Top 4: +EMA20 +VIX** | **756** | **54.6%** | **₹1.50Cr** |
| Top 5: +RSI14 (no macro) | 834 | 53.3% | ₹1.40Cr |

**Decision:** "Top 4" selected over "Top 2" because:
- 756 trades (vs 585) = more statistical confidence, less overfitting risk
- ₹1.50Cr vs ₹1.65Cr — only 10% difference in P&L
- MaxDD -25.4% vs estimated -28% for Top 2 (fewer trades = deeper drawdowns)
- +50% improvement over 10-indicator baseline

**Changes to signal_engine.py:**
- `score_row()` now uses only 4 active indicators: s_ema20, s_trend5, s_vix, s_bn_nf_div
- 6 inactive indicators still computed and written to signals.csv for audit trail
- Score range now -4 to +4 (threshold ±1 still applies = trade on any net directional signal)

**Additional studies completed this session:**
- **Straddle vs Directional Long** (`strangle_backtest.py`): Directional ₹98.7L vs Straddle -₹5.4L LOSS. BN avg daily range (~586 pts) is below straddle breakeven for Mon/Tue/Thu/Fri. Directional long wins decisively.
- **Entry timing** (`timing_backtest.py`): 9:15 vs 9:30 AM difference = ₹248/trade noise. Keep 9:15 AM.
- **Slippage sensitivity**: Survives 1% slippage (₹81L vs ₹98.7L). Realistic ATM spread 0.5-1.5%.
- **Signal source analysis**: GIFT Nifty ≈ BN overnight gap (same construct). US close / Nikkei are negative drag. India-only signals dominate the edge.

### Run 7 — Timezone fix + all 5 days ✅

**Critical fix discovered:** Dhan API timestamps at midnight IST were parsed as UTC,
shifting all dates 1 day earlier. What we thought was "Tuesday" data was actually Wednesday
(expiry day). Patched with `python3 data_fetcher.py --fix-dates`.

**Threshold sweep (all 5 days, corrected data, 20-lot cap):**

| Threshold | Trades | WR | Net P&L | Max DD |
|---|---|---|---|---|
| **±1** | **990** | **50.5%** | **₹98.5L** | **-23.2%** |
| ±2 | 646 | 46.8% | ₹74.0L | -31.9% |
| ±3 | 507 | 47.7% | ₹60.1L | -34.4% |
| ±4 | 368 | 49.0% | ₹53.8L | -31.8% |

**Per-day breakdown (±1 threshold, corrected data):**

| Day | Trades | WR | Net P&L | DTE | Breakeven WR | Margin |
|---|---|---|---|---|---|---|
| Monday | 209 | 51% | ₹25.9L | 2 | 38.5% | +12.5% |
| Tuesday | 199 | 53% | ₹15.2L | 1 | 41.7% | +11.3% |
| Wednesday | 195 | 62% | ₹7.3L | 0.25 | 50.0% | +12.1% |
| Thursday | 199 | 35% | ₹12.5L | 6 | 33.3% | +1.7% |
| Friday | 188 | 41% | ₹37.6L | 5 | 33.3% | +7.7% |
| **Total** | **990** | **50.5%** | **₹98.5L** | | | |

**Day-combination tests:**

| Config | Trades | WR | Net P&L | Max DD |
|---|---|---|---|---|
| **Mon–Fri (all 5)** | **990** | **50.5%** | **₹98.5L** | **-23.2%** |
| Mon+Tue+Thu+Fri | 795 | 46.8% | ₹86.9L | -27.4% |
| Mon+Tue+Fri | 596 | 49.4% | ₹70.4L | -44.1% |
| Thu+Tue | 393 | 45.7% | ₹40.8L | -33.3% |
| Thu alone (±2) | 160 | 35.5% | ₹0.52L | -34.1% |
| Wed alone | 195 | 62.1% | ₹5.85L | -20.7% |

**Key findings:**
- All 5 days gives best P&L AND lowest drawdown (diversification effect)
- Wednesday has the highest WR (62%) — 0 DTE gamma works with our signals
- Thursday has the thinnest edge (35% vs 33.3% breakeven) but acts as a diversifier
- Dropping any day worsens max drawdown — more trading days = smoother equity curve
- Friday dominates P&L (₹37.6L) because capital has compounded all week by then

**Final result:**
- ₹5.8L injected (₹30K start + ₹10K/month × 55 months)
- ₹1.04 Cr ending capital
- **17x return on capital over 4.5 years**
- Charges: ₹3.5L (3.4% of gross P&L)

---

## Decisions Made

| Decision | Reason |
|---|---|
| Trade all 5 days (Mon–Fri) | Best P&L AND lowest drawdown (-23.2%). Each day profitable with different DTE profile. |
| Wednesday 0 DTE included | 62% WR — highest of any day. Premium tiny but signal accuracy high. |
| Threshold ±1 (no threshold) | Signal direction matters; score magnitude adds no edge. ±1 gives best P&L at every config tested. |
| 20-lot cap | Uncapped lots reached 100+ in later months — unrealistic for liquidity/margin. Cap reduces P&L but makes results credible. |
| No carryforward | Avoid theta decay overnight; user preference |
| Delta ≈ 0.5 for exits | ATM option approximation — no live options data available |
| Dhan API for BN/NF data | Most accurate Indian index data available (with IST timezone fix) |
| yfinance for global data | VIX, S&P, Nikkei not available via Dhan |

---

## Event Filter — Calendar Used

RBI MPC decision days (6 per year) + Union Budget day = ~7 no-trade days per year.
These are hard NONE overrides — score is irrelevant on these days.
30 event days blocked across the 4.5 year backtest period.

---

## Known Limitations

1. **Premium is estimated** — real premiums vary with IV. High-IV days = expensive premiums not captured.
2. **Daily OHLCV only** — intraday SL/TP simulation is approximate.
3. **20-lot cap** — artificial constraint; real max depends on account margin and liquidity.
4. **Thursday edge is thin** — 35% WR vs 33.3% breakeven = only 1.7% margin. Could flip in live trading.
5. **Compounding effect dominates** — Friday's ₹37.6L is partly because capital has grown from Mon–Thu trades. In isolation, Friday contributes ₹3.6L.
6. **No slippage modeled** — real fills may differ from OHLCV open price, especially on high-vol days.

---

## GCP VM Setup Notes

- **VM IP:** 34.45.55.132 (static, whitelisted on Dhan)
- **Working directory:** `~/dhan-trading/` (git clone of this repo)
- **Data files:** `~/dhan-trading/data/`
- **Dhan token expires:** ~24 hours — regenerate at dhan.co → API settings

---
*Last updated: Apr 2026 — Run 8 (4-indicator attribution optimisation, ₹1.50Cr net P&L)*
