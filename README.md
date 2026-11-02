# stock-market-bot

A research and paper-trading platform for stock markets. It studies daily price data, tests trading rules against history, and runs an event-driven intraday engine in **paper mode only**. Live trading is disabled and cannot be turned on from this repo yet.

> Not financial advice. No profit is guaranteed. Past backtest results are not proof of future returns.

## What it does

| Part | File | Purpose |
|---|---|---|
| Swing research | `bot.py` | Daily 20-day breakout rules for 9 markets (IN, JP, UK, US, HK, AU, DE, FR, CA). Scans for setups, backtests, tracks your manual trades. |
| Intraday research | `intraday.py` | 5-minute opening-range breakout (ORB) replay with cost and worse-fill checks. |
| Market config | `markets.py` | Per-market index, timezone, hours, currency, cost model, ticker list. |
| Trading platform | `trader/` | Quote recording, data-quality gate, deterministic replay, risk checks, order management (OMS), paper broker, kill switch. |
| News and ownership intelligence | `trader/intel.py` | Ownership graph, event model, assessment (WATCH / REVIEW / ELIGIBLE / NO_ACTION / INSUFFICIENT_EVIDENCE), hash-chained forecast ledger. No live data sources yet. |
| Config | `config/` | Risk limits, exchange sessions, instrument master, account example. |
| Docs | `docs/` | Architecture decision, runbook, checklist, regulatory notes, security notes. |

## Current status

- **Paper mode only.** `live_enabled` is `false`. `trader/cli.py` refuses to start if live is enabled or mode is not `paper`.
- **Intraday ORB: rejected.** It lost money in all 9 markets, even before costs.
- **Swing rules:** passed the profit-factor gate in US, CA, IN and JP. Other markets are PAPER ONLY. The backtest universe is today's large caps, so it has **survivorship bias**.
- **Latency numbers** (`bench`) are measured in-process on synthetic data. Real broker latency is not measured yet.
- **No broker connection.** There is no live market-data or order adapter yet.

## Requirements

- Python 3.14 (tested). Other 3.x versions may work but are untested.
- Internet access for `bot.py` and `intraday.py` (they download prices from Yahoo Finance via `yfinance`).

## Setup

```bash
git clone https://github.com/Mann-gupta1/stock-market-bot.git
cd stock-market-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
```

## Keys and secrets

| Key | Needed? | Where to set it | Notes |
|---|---|---|---|
| Yahoo Finance | **No key needed** | — | Used through `yfinance`. Free, no sign-up. Data can be delayed or incomplete. |
| `TRADER_ADMIN_TOKEN_SHA256` | Only for kill switch and operator controls | Shell environment variable, or a secret manager | This is the SHA-256 hash of your admin token. Not the token itself. |
| Broker API keys (Kite, Upstox, Dhan, IBKR) | **Not yet**, needed only for live adapters, which are not built | Future: `.env` file or secret manager | `.env` is in `.gitignore`. Never commit keys. Never put keys in `config/` or frontend code. |

Create the admin token hash (keep the token itself private):

```bash
export TRADER_ADMIN_TOKEN_SHA256=$(printf '%s' "YOUR_LONG_RANDOM_TOKEN" | shasum -a 256 | cut -d' ' -f1)
```

If this variable is not set, every operator control action is denied.

## How to run

### Swing research (daily)

```bash
.venv/bin/python bot.py scan                 # all markets, after market close
.venv/bin/python bot.py scan IN US           # some markets only
.venv/bin/python bot.py backtest IN 8        # 8-year backtest for one market
.venv/bin/python bot.py buy IN RELIANCE 1250 10     # record a manual buy (price, qty, optional stop)
.venv/bin/python bot.py sell IN RELIANCE 1300       # record a manual sell
```

### Intraday research

```bash
.venv/bin/python intraday.py all             # 5-minute ORB replay, all markets
.venv/bin/python intraday.py IN              # one market
```

### Trading platform (synthetic data)

```bash
.venv/bin/python -m trader.cli synth --days 25            # write a synthetic NSE quote feed to data/md.db
.venv/bin/python -m trader.cli replay --db data/md.db     # strategy + risk + OMS + paper broker
.venv/bin/python -m trader.cli bench --db data/md.db      # replay twice: determinism + latency
```

`yahoo` mode also exists (`trader.cli yahoo`). It records polled Yahoo quotes, but those have no bid or ask, so trading on them is blocked.

### Tests

```bash
.venv/bin/python -m pytest -q tests test_bot.py
```

## Where results appear

| Output | Created by | Contents |
|---|---|---|
| Terminal | all commands | Trades per year, win rate, average return, profit factor, portfolio return vs index buy-and-hold, max drawdown, and the gate result: `LIVE-ELIGIBLE` or `PAPER ONLY`. |
| `results.json` | `bot.py backtest`, `intraday.py` | Stored result per market (trades, win rate, average, profit factor, date). |
| `holdings.json` | `bot.py buy` / `sell` | Your open positions (local, not committed). |
| `trades.csv` | `bot.py sell` | Closed trades with P&L in local currency and INR (local, not committed). |
| `journal.csv` | `bot.py scan` | Daily scan log (local, not committed). |
| `data/md.db`, `data/paper.db` | `trader.cli` | Recorded quotes, orders, executions, audit log (SQLite, local, not committed). |

The gate for swing results: at least 100 trades, profit factor ≥ 1.2, and positive average return after costs. Passing it gives `LIVE-ELIGIBLE`. It does not allow live trading. Live trading still needs explicit approval, a live adapter, and risk limits.

## Risk limits

`config/risk.example.json` holds the limits: order size, position and sector caps, daily loss, drawdown, spread and slippage limits. To change them:

1. Copy the file and bump `version`.
2. Apply it with `Controller.set_limits(token, path, actor)`. Invalid files are rejected and audited.

## Kill switch

Run through `trader.control.Controller` with the admin token. It has three levels:

| Level | New entries | Working orders | Positions |
|---|---|---|---|
| 1 HALT | blocked | untouched | untouched |
| 2 CANCEL | blocked | cancel requested | untouched |
| 3 FLATTEN | blocked | cancelled | reduce-only sells until flat |

The level persists across restarts. Five bad tokens lock non-kill actions for 60 seconds. A valid kill is never blocked.

## Documentation

- `docs/RUNBOOK.md`: operator commands, recovery steps, what is needed before live trading.
- `docs/ADR-001-architecture.md`: architecture decisions and their limits.
- `docs/CHECKLIST.md`: implementation status by stage.
- `docs/REGULATORY.md`: SEBI algo rules, broker API facts, FX and tax notes, with sources. Checked 2026-11-02.
- `docs/SECURITY.md`: threat model, fixed issues, known residual risks.

## Known limits

- Paper fills ignore queue position, hidden liquidity and auctions.
- The audit log can be rewritten by anyone with file access. Only the forecast ledger is hash-chained.
- Tick and lot sizes in `config/instruments.json` are unverified placeholders.
- Holiday calendars are verified for NSE 2026 only.
- Backtests use today's large caps (survivorship bias).
- No live news, filings or policy data source is connected yet.

## Next steps

1. Choose a broker for NSE (Kite, Upstox or Dhan).
2. Set capital and loss limits per account.
3. Connect a broker feed in paper mode.
4. Live trading only after explicit written approval.
