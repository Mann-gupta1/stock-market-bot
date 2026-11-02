# Operator runbook (paper mode)

## Setup
```
cd stockbot
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
export TRADER_ADMIN_TOKEN_SHA256=$(printf '%s' "$YOUR_TOKEN" | shasum -a 256 | cut -d' ' -f1)
```
Never commit the token or put it in config. If the variable is unset, every control action is denied.

## Daily commands
| Task | Command |
|---|---|
| Swing scan, all markets (after close) | `.venv/bin/python bot.py scan` |
| Record a manual buy or sell | `.venv/bin/python bot.py buy IN RELIANCE 1250 10` / `sell IN RELIANCE 1300` |
| Swing backtest per market | `.venv/bin/python bot.py backtest IN 8` |
| Intraday candle replay (research) | `.venv/bin/python intraday.py all` |
| Synthetic L1 feed (plumbing tests) | `.venv/bin/python -m trader.cli synth --days 25` |
| Replay through strategy, risk, OMS and paper broker | `.venv/bin/python -m trader.cli replay [--state data/paper.db]` |
| Determinism and latency benchmark | `.venv/bin/python -m trader.cli bench` |
| Tests | `.venv/bin/python -m pytest -q tests test_bot.py` |

## Risk limits
1. Edit a copy of `config/risk.example.json` and **bump `version`**.
2. Apply it with `Controller.set_limits(token, path, actor)`. The file is validated before anything changes.

The hash of the active limits is stored with every risk decision. Invalid files are rejected and audited.

## Kill switch: `Controller.kill(token, level, actor)`
| Level | Effect on new entries | Working orders | Positions |
|---|---|---|---|
| 1 HALT_ENTRIES | blocked | untouched | untouched |
| 2 CANCEL_ORDERS | blocked | cancel requested. A fill that races the cancel is kept; the final state comes from the broker. | untouched |
| 3 FLATTEN | blocked | cancelled | Reduce-only sells at the bid, re-submitted on every quote until flat. No duplicates while an exit is working. Without a fresh bid it reports "manual action needed". |

- The kill level persists across restarts.
- `resume` clears it only when `reconcile()` reports no issues.
- 5 bad tokens lock non-kill actions for 60 s. A valid kill is never locked out.

## Restart and recovery
1. Startup runs `oms.reconcile()`. Orders left in INTENT or SUBMITTING that are not at the broker become REJECTED and are **not re-sent**. UNKNOWN orders resolve from broker state.
2. While any order is UNKNOWN, new entries are blocked. Reduce-only exits still work.
3. Position mismatches are printed and audited. Investigate them before `resume`.

## What must be supplied before live trading (live trading stays disabled)
- **Broker:** which broker, the API plan, a static IP registered with the broker, and the algo-ID process (`docs/REGULATORY.md` §1 and §5).
- **Capital and limits:** starting capital per account, max daily loss, max drawdown, and per-market permissions.
- **Instrument master:** verified tick sizes, lot sizes and holiday calendars per exchange.
- **Explicit written authorisation** to enable `live_enabled`, which also needs a live adapter that does not exist yet.
