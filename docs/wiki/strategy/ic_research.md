# Strategy Research — NF IC + Bull Put Hybrid

**Source:** STRATEGY_RESEARCH.md (April 2026)  
**Data:** 7-year backtest, Sep 2025–Apr 2026 = Tue expiry regime  
**Last updated:** 2026-04-25  

---

## Final verdict (locked)

| Capital | Strategy | WR | 7yr P&L |
|---|---|---|---|
| < ₹2.3L (current) | IC (CALL days) + Bull Put (PUT days) | 97.5% | ₹18.36L |
| ≥ ₹2.3L (upgrade) | Short Straddle | 94.9% | ₹3.15Cr |

**Live capital at go-live (22 Apr 2026): ₹1,12,370 → IC+BullPut.**

---

## NF IC backtest (real 1-min data, 2021–2026)

| Year | Trades | WR | P&L |
|---|---|---|---|
| 2021 (Aug–Dec) | 100 | 88% | ₹9.6L |
| 2022 | 238 | 80% | ₹25.2L |
| 2023 | 235 | 91% | ₹24.1L |
| 2024 | 235 | 86% | ₹25.8L |
| 2025 | 237 | 86% | ₹25.4L |
| 2026 (Jan–Apr) | 69 | 67% | ₹7.2L |
| **5yr total** | **1114** | **84.6%** | **₹1.17Cr** |

Max drawdown: **-0.8%**. No year below 67%.

---

## IC structure (CALL days)

```
SELL ATM CE  + BUY ATM+150 CE   (upper wing — bear call side)
SELL ATM PE  + BUY ATM-150 PE   (lower wing — bull put side)
spread_width = 150pts
SL: spread cost > net_credit × 1.5   (NO TP — EOD exit = +₹21L over 5yr)
exit: 3:15 PM via exit_positions.py
```

## Bull Put structure (PUT days)

```
SELL ATM PE  + BUY ATM-150 PE
SL: spread cost > net_credit × 1.5
TP: spread cost < net_credit × 0.35   (retain 65%)
Backtest Sep 2025–Apr 2026: 100% WR, ₹3,794 avg (51 trades)
```

---

## Why each discarded strategy was discarded

| Strategy | WR | P&L (7yr) | Reason discarded |
|---|---|---|---|
| Bear Call | 13.5% | -₹24.03L | CALL signal = market going UP → short CE always gets hit. Direction conflict. |
| Naked Buy (CALL/PUT) | <50% | -₹40L+ | Theta decay kills premium buyers. Options expire worthless ~70% of time. |
| Long Straddle | 5–31% | -₹248L | Buying both sides = paying theta every minute. Net negative EV. |

---

## DOW breakdown (Tue expiry regime, Sep 2025+)

```
Day     IC WR    Bull Put WR
Mon     88.5%    100%
Tue     89.5%    100%    ← expiry day (DTE=0)
Wed     95.8%    100%
Thu     100%     100%
Fri     100%     100%
```

**IC works ALL 5 days.** "Wed is bad" was stale Thu-expiry data.

---

## CAT (Classifier Accuracy Tracker) log

| Date | Acc | Score | Signal | Conf | Top 3 Features |
|---|---|---|---|---|---|
| 2026-04-24 | 70.6% | 0.7071 | PUT | 69% | nf_ret5, trend5, us10y_chg |
| 2026-04-25 | 70.6% | 0.7071 | PUT | 69% | nf_ret5, trend5, us10y_chg |

---

## Key regime facts

- **NSE NF expiry changed:** Thursday → Tuesday, effective Sep 1 2025 (NSE circular)
- **NF lot size:** 75 before Jan 6 2026, 65 from Jan 6 2026
- **Weekly Tuesday expiry** = every NF IC trade is naturally DTE ≤ 7
- **Pre-Sep-2025 DOW stats biased** toward old Thursday-expiry patterns — ignore for current strategy

---

## Related pages
- [[bugs/known_issues]] — IC-specific bugs and gotchas
- [[features/feature_history]] — ML features that improve IC signal quality
