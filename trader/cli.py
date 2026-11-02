"""Operator CLI. Paper mode only; there is no live broker adapter.

  python -m trader.cli synth  --db data/md.db --days 25 --seed 7   # write a deterministic synthetic NSE feed
  python -m trader.cli yahoo  --db data/yahoo.db --symbols RELIANCE.NS TCS.NS --seconds 60
  python -m trader.cli replay --db data/md.db [--state data/paper.db]   # strategy+risk+OMS on recorded quotes
  python -m trader.cli bench  --db data/md.db                            # replay twice: determinism + latency
"""
from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .broker import Faults, PaperBroker
from .engine import Engine
from .model import Instrument, Quote
from .oms import OMS
from .portfolio import FxTable, pnl
from .risk import Account, Limits, RiskEngine, Session
from .store import Store
from .strategy_orb import OrbParams, OrbStrategy

CFG = Path(__file__).resolve().parent.parent / "config"
NS = 1_000_000_000


def load_config(risk_path: Path | None = None) -> tuple:
    reg = {d["id"]: Instrument(**d) for d in json.loads((CFG / "instruments.json").read_text())["instruments"]}
    ex = {k: v for k, v in json.loads((CFG / "exchanges.json").read_text()).items() if not k.startswith("_")}
    sessions = {k: Session(v["tz"], v["open"], v["close"], frozenset(v["holidays"]), v["early_closes"],
                           tuple(tuple(b) for b in v["breaks"])) for k, v in ex.items()}
    acfg = json.loads((CFG / "accounts.example.json").read_text())
    if acfg.get("live_enabled") or acfg.get("mode") != "paper":
        raise SystemExit("live mode requested but no live adapter exists and no approval recorded: refusing")
    accounts = {a["id"]: Account(a["id"], a["currency"], float(a["settled_cash"]), frozenset(a["exchanges"]))
                for a in acfg["accounts"]}
    limits, h = Limits.from_file(risk_path or CFG / "risk.example.json")
    fx = FxTable(limits.base_currency)
    for ccy, rate in acfg.get("fx_paper", {}).items():
        fx.add(ccy, 0, rate)
    return reg, ex, sessions, accounts, limits, h, fx


def synth(db: str, days: int, seed: int) -> int:
    """Random-walk L1 quotes, ~1/s per instrument, NSE hours. SIMULATED data for plumbing tests only."""
    rng, store = random.Random(seed), Store(db)
    names = {"XNSE:RELIANCE": 1250.0, "XNSE:TCS": 3000.0, "XNSE:INFY": 1500.0}
    tz, n, d0 = ZoneInfo("Asia/Kolkata"), 0, datetime(2026, 9, 1, tzinfo=ZoneInfo("Asia/Kolkata"))
    seq = {k: 0 for k in names}
    store.db.execute("BEGIN")
    day = d0
    for _ in range(days):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        t, end = day.replace(hour=9, minute=15), day.replace(hour=15, minute=30)
        px, cum = dict(names), {k: 0 for k in names}
        drift = {k: rng.gauss(0, 0.0004) for k in names}
        burst = {k: 1.0 for k in names}
        while t < end:
            if t.second == 0:                      # ~4% of minutes are volume bursts with positive drift
                burst = {k: (4.0 if rng.random() < 0.04 else 1.0) for k in names}
            for k in names:
                px[k] = max(1.0, px[k] * (1 + drift[k] + (0.0002 if burst[k] > 1 else 0) + rng.gauss(0, 0.0004)))
                mid = round(px[k] / 0.05) * 0.05
                half = 0.05 * rng.choice((1, 1, 2))
                cum[k] += int(rng.expovariate(1 / 400) * burst[k])
                seq[k] += 1
                ts = int(t.timestamp() * NS) + rng.randrange(0, 900_000_000)
                q = Quote(k, seq[k], ts, ts + rng.randrange(1_000_000, 30_000_000), round(mid - half, 2),
                          round(mid + half, 2), rng.randrange(50, 2000), rng.randrange(50, 2000), round(mid, 2),
                          cum[k])
                store.record_quote(q)
                n += 1
            t += timedelta(seconds=1)
        day += timedelta(days=1)
    store.db.execute("COMMIT")
    return n


def yahoo(db: str, symbols: list[str], seconds: int, every: float) -> int:
    """Polls Yahoo (delayed, unlicensed for redistribution: research only). Missing bid/ask is recorded as
    None so the quality gate blocks trading on it. Not a substitute for a broker tick feed."""
    import yfinance as yf
    store, n, seq = Store(db), 0, {s: 0 for s in symbols}
    end = time.time() + seconds
    while time.time() < end:
        for s in symbols:
            try:
                info = yf.Ticker(s).fast_info
                last = float(info["last_price"])
            except Exception as e:   # network/parse: record nothing, report, keep going
                print(f"! {s}: {type(e).__name__}: {e}")
                continue
            seq[s] += 1
            now = time.time_ns()
            store.record_quote(Quote(f"YF:{s}", seq[s], now, now, None, None, 0, 0, last, 0))
            n += 1
        time.sleep(every)
    return n


def build(state: str, risk_path: Path | None = None, faults: Faults | None = None) -> Engine:
    reg, ex, sessions, accounts, limits, h, fx = load_config(risk_path)
    store = Store(state)
    broker = PaperBroker("paper-in", faults)
    strat = OrbStrategy(reg, {k: v["tz"] for k, v in ex.items()}, {k: (v["open"], v["close"], v["early_closes"]) for k, v in ex.items()},
                        {}, OrbParams(risk_per_trade=limits.risk_per_trade))
    oms = OMS(store, broker, "paper-in")
    issues = oms.reconcile()                       # startup: resolve INTENT/SUBMITTING/UNKNOWN first
    if issues:
        print("! reconcile issues on startup:", *issues, sep="\n  ")
    return Engine(store, strat, RiskEngine(limits, h, sessions, accounts), oms,
                  broker, reg, fx, initial_equity=sum(a.settled_cash for a in accounts.values()))


def replay(db: str, state: str = ":memory:") -> Engine:
    eng = build(state)
    for q in Store(db).quotes():
        eng.ingest(q)
        eng.run_queue()
    return eng


def summary(eng: Engine) -> dict:
    cur = {i.id: i.currency for i in eng.reg.values()}
    marks = {k: (q.bid + q.ask) / 2 for k, q in eng.last.items() if q.bid and q.ask}
    sig = [json.loads(p) for (p,) in eng.store.db.execute("SELECT payload FROM events WHERE kind='signal'")]
    orders = eng.store.orders()
    return dict(
        signals={a: sum(1 for s in sig if s["action"] == a) for a in ("ENTER", "EXIT", "NO_TRADE")},
        orders={st: sum(1 for o in orders if o.state.value == st) for st in sorted({o.state.value for o in orders})},
        open_positions=eng.store.positions(),
        pnl_base=({k: round(v, 2) for k, v in pnl(eng.store.fills(), cur, marks, eng.fx, eng.now_ns).items()}),
        quality=eng.quality.counts, dropped=eng.dropped, decision_hash=eng.decision_hash()[:16])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["synth", "yahoo", "replay", "bench"])
    ap.add_argument("--db", default="data/md.db")
    ap.add_argument("--state", default=":memory:")
    ap.add_argument("--days", type=int, default=25)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--symbols", nargs="*", default=["RELIANCE.NS"])
    ap.add_argument("--seconds", type=int, default=60)
    a = ap.parse_args()
    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    if a.cmd == "synth":
        print(f"recorded {synth(a.db, a.days, a.seed)} SIMULATED quotes -> {a.db}")
    elif a.cmd == "yahoo":
        print(f"recorded {yahoo(a.db, a.symbols, a.seconds, 5.0)} polls -> {a.db}")
    elif a.cmd == "replay":
        print(json.dumps(summary(replay(a.db, a.state)), indent=1, default=str))
    else:
        t = time.perf_counter()
        e1 = replay(a.db)
        secs = time.perf_counter() - t
        e2 = replay(a.db)
        n = len(e1.lat.feed_to_decision) + sum(e1.quality.counts.values())
        print(json.dumps(dict(deterministic=e1.decision_hash() == e2.decision_hash(), quotes=n,
                              throughput_qps=round(n / secs), latency=e1.lat.report(), dropped=e1.dropped,
                              conditions="single process, in-memory SQLite state, PaperBroker in-process: "
                                         "submit_to_ack is SIMULATED, excludes network/broker"), indent=1))


if __name__ == "__main__":
    main()
