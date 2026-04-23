# Trading Wiki — Index

Karpathy LLM Wiki pattern. Raw discoveries → compiled articles.  
Maintained by `wiki_compiler.py`. Read this at session start for full context.

```bash
# Compile new raw discoveries into wiki:
python3 wiki_compiler.py

# Add a raw discovery manually:
echo "date: 2026-04-23 | discovery: ..." >> docs/wiki/raw/YYYY-MM_session.txt
```

---

## Strategy

| Page | Summary |
|---|---|
| [[strategy/ic_research]] | NF IC + Bull Put hybrid — 7yr backtest, final verdict, DOW breakdown, discarded strategies |

---

## Features

| Page | Summary |
|---|---|
| [[features/feature_history]] | All 60 features, kept/discarded experiment log, reserved variable names, addition checklist |

---

## Bugs

| Page | Summary |
|---|---|
| [[bugs/known_issues]] | 15+ session-discovered bugs — ML shadows, API format, lot sizing, regime/routing bugs |

---

## Raw sources (gitignored — VM only)

`docs/wiki/raw/` — drop discoveries here. Files compiled by `wiki_compiler.py`.

Naming convention:
- `YYYY-MM_experiments.txt` — autoloop experiment results (auto-populated)
- `YYYY-MM-DD_session.txt` — manual session discoveries
- `YYYY-MM_trades.txt` — monthly live trade outcomes

Processed files move to `docs/wiki/raw/processed/` after compilation.

---

## Change log

See [[log]] for full history. Quick view:
```bash
grep "^## \[" docs/wiki/log.md | tail -10
```
