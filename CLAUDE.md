# Bull/Bear/Judge Paper Trader

## What this does

Pulls **real live market data** from Polymarket's public API (no account needed),
runs a three-agent Claude debate on each market, and tracks **fake trades** in a
local JSON ledger. Over time you build a performance record showing whether the
AI strategy would have made money.

No private keys. No real money. No external SDKs — only the Python standard
library + `anthropic`.

---

## Setup

```bash
pip install anthropic
export ANTHROPIC_API_KEY="sk-ant-..."
```

That's it.

---

## Commands

```bash
# Debate markets interactively (no trades recorded)
python paper_trader.py --demo

# Scan live Polymarket markets, debate top ones, record paper trades
python paper_trader.py --scan

# Print your performance report
python paper_trader.py --report

# Check which markets resolved and score your calls
python paper_trader.py --resolve

# Run a full cycle
python paper_trader.py --scan && python paper_trader.py --resolve && python paper_trader.py --report
```

---

## How it works

```
Polymarket public API  →  active markets + YES prices
         │
         ▼
   Bull agent (Claude)  ←→  Bear agent (Claude)
         │                         │
         └──────── debate ─────────┘
                      │
                      ▼
              Judge agent (Claude)
              {verdict, our_probability, confidence, edge_detected}
                      │
          [confidence ≥ 0.62 AND edge ≥ 5%]
                      │
                      ▼
            paper_ledger.json
            (fake trade recorded)
                      │
         [later, when market resolves]
                      │
                      ▼
              PnL calculated
              Report generated
```

---

## Config (edit at top of paper_trader.py)

| Variable | Default | Meaning |
|---|---|---|
| `STARTING_BANKROLL` | `1000.0` | Fake starting dollars |
| `MAX_TRADE_PCT` | `0.03` | Max 3% of bankroll per trade |
| `MIN_CONFIDENCE` | `0.62` | Judge must be ≥ this to trade |
| `MIN_EDGE_PCT` | `0.05` | Our prob must differ from market by ≥ 5% |
| `DEBATE_ROUNDS` | `2` | Back-and-forth rounds |
| `MAX_MARKETS` | `5` | Markets to scan per run |

---

## Output files

- `paper_ledger.json` — all trades, bankroll, running PnL
- Console report shows win rate, total return, per-trade breakdown

---

## Typical workflow

1. Run `--scan` once a day (or a few times a week)
2. Run `--resolve` to score any markets that have since resolved
3. Run `--report` to see how the strategy is doing
4. After 20-30 trades you'll have a meaningful sample

If win rate > 55% and total PnL is positive after 30+ trades, the strategy has real signal.
If not, tune `MIN_CONFIDENCE`, `MIN_EDGE_PCT`, or the judge system prompt.
