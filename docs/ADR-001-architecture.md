# ADR-001: Stage-1 architecture for the paper trading platform

Status: accepted, 2026-11-02. Mode: **PAPER ONLY**. No live broker adapter exists.

## Context
- The repo already had research scripts: `bot.py` (swing scan and backtest), `intraday.py` (5-minute ORB replay) and `markets.py` (configuration for 9 markets).
- Those research scripts showed the candle-based ORB losing money in all 9 markets, even before costs. Swing results pass a profit-factor gate in IN, JP, US and CA, but the backtest has survivorship bias.
- Constraints: an India-resident individual and a cash account. Overseas money comes through LRS, which forbids margin.
- From 2026-04-01, Indian algo orders need a static IP. Below 10 orders/sec per exchange, a self-built algo needs no exchange registration. Sources are in `docs/REGULATORY.md`.

## Decision
| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.14, single process, single thread | Measured strategy compute is p50 2.2 µs and p99 7.2 µs. Broker round trips are in milliseconds, so a faster language would not help yet. |
| Durable state | One SQLite file: WAL mode, synchronous=FULL | Stdlib only. Append-only tables are enforced by triggers. Positions are derived from executions only. |
| Market data | `Quote` (L1) events recorded before any action; replay in arrival order | Deterministic replay. Decision time comes only from event time, so replays reproduce exactly. |
| Data quality | `quality.QualityGate` | Drops duplicate, out-of-order, crossed, NaN, halted, clock-skewed and lagged quotes. Blocks an instrument after a gap until 5 clean quotes arrive. |
| Orders | `oms.OMS`: intent persisted before the broker call. `client_id = H(signal)` | A timeout becomes UNKNOWN and is never retried. Reconcile runs on startup and on reconnect. Fills are deduplicated by `exec_id`. |
| Risk | `risk.RiskEngine` with versioned, validated JSON limits. Each decision records the limits hash | Reduce-only orders always pass. The daily loss limit is keyed to the base-timezone (IST) day, so opening another market does not reset it. |
| Books | `Book.INTRADAY` and `Book.SWING` are separate | An exit only reduces its own book, and working sells are already counted. A cash account cannot go short. |
| Kill switch | `control.Controller`, authenticated with a sha256 token | Levels: 1 halts new entries, 2 also cancels orders, 3 also flattens positions. The level persists across restarts. A valid kill is never blocked by the lockout. |
| News and filings AI | `intel.py`, outside the order path | Model output is checked against a schema. It can only produce an Assessment; it cannot change limits or place orders. |
| Profit claims | Every strategy is a hypothesis until paper evidence passes the gate | — |

## Interfaces (Python Protocol and dataclasses)
- `broker.Broker`: submit, cancel, order_status, executions, positions, poll. Implemented by `PaperBroker`, which supports fault injection. Live adapters for Kite, Upstox, Dhan and IBKR come in stage 3.
- `strategy.on_quote(Quote) -> list[Signal]`, plus `on_fill`, and a `name` attribute. NO_TRADE is a valid Signal.
- `RiskEngine.check(OrderRequest, RiskView, Quote) -> Decision`

## Consequences and limits
- **Latency figures:** they are local and in-process, and submit-to-ack is SIMULATED. Real broker latency is unmeasured until a sandbox or paper account is connected.
- **Paper fills:** they ignore queue position, hidden liquidity and auctions.
- **Settled cash:** sell proceeds are never credited back, which is deliberately conservative.
- **Book relationships:** a check is O(orders) per quote. That is acceptable at stage-1 volumes; index it when profiling shows the need.
- **Audit log:** append-only within the database, but anyone with file access can rewrite it. Only the forecast ledger is hash-chained.
