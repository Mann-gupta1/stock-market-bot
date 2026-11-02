# Implementation checklist

Legend: [x] done and tested · [~] partial · [ ] not started

## Stage 1: recorder, deterministic replay, one strategy per mode
- [x] Instrument registry (`config/instruments.json`). Tick and lot sizes are **unverified placeholders**: NSE sets ticks by price band and reviews them monthly.
- [x] Immutable quote recording; replay in arrival order
- [x] Data quality: stale or lagged, skewed, gap, duplicate, out-of-order, crossed, NaN, halted
- [x] Intraday ORB on L1 quotes, deterministic, with a NO_TRADE outcome. Same decision hash across replays.
- [~] Swing breakout strategy: exists in `bot.py` (research). Not yet ported to the engine.
- [~] Recorder: synthetic feed plus a Yahoo poller, which gives no bid/ask, so trading on it is blocked. A broker WebSocket recorder is still needed.

## Stage 2: realistic backtest and validity review
- [x] Candle replay with worse-fill sensitivity (`intraday.py`). The ORB edge is gone after costs in all 9 markets, so ORB is **rejected**.
- [ ] Point-in-time universe (the current one is today's large caps, which means survivorship bias)
- [ ] Train, validation and untouched-test split with walk-forward; parameter-search log; multiple-testing correction
- [ ] Corporate-action-adjusted tick replay with recorded bid/ask, plus a latency model

## Stage 3: broker sandbox or paper trading with reconciliation
- [x] OMS lifecycle: intent, submit, ack, partial, fill, cancel, reject, expire, unknown
- [x] Reconcile on startup; timeouts are never retried; idempotent fills; handling of cancel-fill races
- [ ] Live market-data adapter. **Needs a broker choice** (Kite, Upstox or Dhan) **and an API subscription.**
- [ ] Measured feed-to-ack latency against a real broker

## Stage 4: dashboard, alerts, fault recovery, hardening
- [x] Authenticated kill switch, audit log, limit validation, persisted kill level, persisted daily-loss baseline
- [ ] Dashboard and alerts
- [ ] Audit hash chain for operator actions; OS-level file permissions; secret manager integration

## Stage 5: more markets
- [x] Session model: timezone and DST, lunch breaks, holidays, early closes (config placeholders, except NSE 2026)
- [x] FX in timestamped base currency, with P&L split into trading and FX
- [ ] Verified per-exchange calendars, fees and permissions for each account

## Intelligence layer (news, ownership, policy)
- [x] Time-aware evidence graph: merge only on registry IDs; impact flows from owned company to owner, never to siblings; cycles cut; materiality threshold
- [x] Event model: published and first-received timestamps; factuality; policy-stage lifecycle; corrections; clustering of wire copies
- [x] Assessment keeps business impact, direction probabilities and eligibility as separate fields. Status is one of WATCH, REVIEW, ELIGIBLE, NO_ACTION or INSUFFICIENT_EVIDENCE.
- [x] Classifier (Jev) output passes a schema gate; unfamiliar event types go to REVIEW
- [x] Hash-chained forecast ledger: one score per independent event; abstentions count against coverage
- [ ] Source registry and coverage matrix per country (NSE/BSE filings, SEC EDGAR 13F/8-K, UK Find a Tender ...)
- [ ] Ingestion adapters; Jev integration evaluated on labelled examples
- [ ] Three frozen paper arms (PV, PV+NEWS, FULL) running on live data

## Stage 6: limited-capital pilot
- [ ] Blocked until: positive net expectancy on unseen data **and** in live paper trading, drawdown within budget, the edge surviving worse fills, and explicit user authorisation
