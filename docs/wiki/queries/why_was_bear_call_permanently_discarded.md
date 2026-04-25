# Query: Why was Bear Call permanently discarded?

**Asked:** 2026-04-24
**Reconciled:** 2026-04-25

## Answer

Bear Call was permanently discarded because the **CALL signal means the market is going UP**, which creates a fundamental **direction conflict** with a Bear Call spread (which is short the call side and profits when the market goes *down* or stays flat). As a result, the short CE consistently gets hit. Over the 7-year backtest it showed only a **13.5% win rate** and lost **-₹24.03L**.

## Reconciliation note (Apr 25 2026)

The newer `backtest_spreads.py --strategy nf_bear_call_credit --ml` shows Bear Call at **61.9% WR, ₹35.9L over 5yr** — which contradicts the discard verdict at first glance. The discrepancy is **routing**, not the strategy itself:

- **Old backtest (which produced the -₹24.03L verdict):** Bear Call ran on **CALL signal days** — direction conflict; short CE got hit when model said "market going up".
- **New backtest:** Bear Call runs on **PUT signal days** — model says "market going down", short CE = aligned direction.

Even with corrected routing, **Bull Put on PUT days still wins** (65.7% WR, ₹46.8L over 5yr vs Bear Call's 61.9% WR, ₹35.9L). Same direction, same days, Bull Put has higher WR and more P&L. So:

- Verdict holds: Bear Call stays out of the live system.
- The reasoning is now "Bull Put dominates Bear Call on aligned days" rather than "direction conflict" — but the conclusion is identical.

Source: [[strategy/ic_research]], NF `backtest_spreads.py` output 2026-04-25.

## Related pages
- [[strategy/ic_research]] — full strategy verdict table
- [[bugs/known_issues]] — `Bear Call appears profitable in NF backtest_spreads.py output` gotcha
