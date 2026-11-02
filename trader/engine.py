"""Wires feed -> quality -> strategy -> risk -> OMS -> broker. Single-threaded, event-time driven.

Latency stamps (perf_counter_ns, local process only):
  feed_to_decision   : quote dequeued -> strategy returned
  decision_to_submit : strategy returned -> broker.submit called
  submit_to_ack      : broker.submit called -> returned (PaperBroker = in-process, SIMULATED)
Bounded ingress queue: when full the OLDEST quote is dropped and counted (quotes are conflatable;
broker callbacks never go through this queue).
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .model import OPEN, Action, Book, Instrument, OrderState, Quote, Side, Signal
from .oms import OMS
from .portfolio import FxTable, pnl
from .quality import QualityGate
from .risk import NS, OrderRequest, RiskEngine, RiskView
from .store import Store


@dataclass
class Latency:
    feed_to_decision: list = field(default_factory=list)
    decision_to_submit: list = field(default_factory=list)
    submit_to_ack: list = field(default_factory=list)

    def report(self) -> dict:
        def pct(xs: list, p: float) -> float:
            s = sorted(xs)
            return round(s[min(len(s) - 1, int(p * len(s)))] / 1000, 1) if s else float("nan")
        return {k: dict(n=len(v), p50_us=pct(v, .5), p95_us=pct(v, .95), p99_us=pct(v, .99))
                for k, v in vars(self).items()}


class Engine:
    def __init__(self, store: Store, strategy, risk: RiskEngine, oms: OMS, broker, registry: dict[str, Instrument],
                 fx: FxTable, initial_equity: float, queue_max: int = 10_000, record: bool = False) -> None:
        self.store, self.strategy, self.risk, self.oms, self.broker = store, strategy, risk, oms, broker
        self.reg, self.fx, self.initial = registry, fx, initial_equity
        self.quality = QualityGate()
        self.queue: deque[Quote] = deque()
        self.queue_max, self.dropped, self.record = queue_max, 0, record
        self.last: dict[str, Quote] = {}
        self.kill_level = 0
        self.entries_paused_until_ns = 0
        self.lat = Latency()
        self.decisions: list[str] = []          # signal + risk outcome, for determinism hashing
        self.meta: dict[str, Signal] = {}       # client_id -> signal (stop/target/expiry)
        self.notified: dict[str, int] = {}      # client_id -> filled qty already reported to strategy
        self.rearm_after: dict[str, int] = {}
        self.peak = self.day_start = initial_equity
        self.day = ""
        self.now_ns = 0
        for _actor, action, detail in store.audit_rows():   # survive restarts: loss budget + peak persist
            d = json.loads(detail)
            if action == "day_start":
                self.day, self.day_start = d["day"], d["equity"]
            elif action == "kill":
                self.kill_level = max(self.kill_level, {"HALT_ENTRIES": 1, "CANCEL_ORDERS": 2,
                                                        "FLATTEN": 3}[d["level"]])
            elif action == "resume" and d.get("kill_level") == 0:
                self.kill_level = 0
            elif action == "peak":
                self.peak = max(self.peak, d["equity"])

    # ---- ingress ----
    def ingest(self, q: Quote) -> None:
        if len(self.queue) >= self.queue_max:
            self.queue.popleft()
            self.dropped += 1
        self.queue.append(q)

    def run_queue(self) -> None:
        while self.queue:
            self.on_quote(self.queue.popleft())

    def on_quote(self, q: Quote) -> None:
        if self.record:
            self.store.record_quote(q)              # persist raw input before acting on it
        use, _problem = self.quality.check(q)
        if use:                                     # rejected quotes (skew/dup/stale) never move the clock
            self.now_ns = max(self.now_ns, q.ts_exch_ns)
            if hasattr(self.broker, "on_quote"):
                self.broker.on_quote(q)             # SIMULATED venue matches only on gated quotes
        self._pump()
        if not use:
            return
        self.last[q.instrument] = q
        self._roll_day()
        t0 = time.perf_counter_ns()                 # feed_to_decision = strategy compute only
        signals = self.strategy.on_quote(q)
        t1 = time.perf_counter_ns()
        self.lat.feed_to_decision.append(t1 - t0)
        for s in signals:
            self._act(s, q, t1)

    # ---- decisions ----
    def _act(self, s: Signal, q: Quote | None, t_decided: int) -> None:
        self.store.append_event("signals", 0, s.decided_ns, "signal", s.to_json())
        if s.action == Action.NO_TRADE:
            self.decisions.append(f"{s.id}|NO_TRADE")
            return
        inst = self.reg[s.instrument]
        req = OrderRequest(account=self.oms.account, book=s.book, instrument=inst, side=s.side, qty=s.qty,
                           limit_price=s.limit_price, ts_ns=s.decided_ns)
        d = self.risk.check(req, self.view(), q)
        if d.ok and not d.reduce_only and s.decided_ns < self.entries_paused_until_ns:
            d = type(d)(False, ("entries paused after throttling",), d.limits_version, False)
        self.store.append_event("risk", 0, s.decided_ns, "decision",
                                json.dumps(dict(signal=s.id, ok=d.ok, reasons=d.reasons, limits=d.limits_version)))
        self.decisions.append(f"{s.id}|{d.ok}|{';'.join(d.reasons)}")
        if not d.ok:
            if s.action == Action.EXIT:             # re-armed by _rearm_stuck_intraday after back-off
                self.rearm_after[s.instrument] = s.decided_ns + 5 * NS
            return
        throttled = self.oms.throttled
        o = self.oms.place(s.id, s.book, s.instrument, s.side, s.qty, s.limit_price, d.reduce_only)
        if self.oms.throttled > throttled:
            self.entries_paused_until_ns = s.decided_ns + 5 * NS     # bounded back-off
            self.rearm_after[s.instrument] = s.decided_ns + 5 * NS
        if self.oms.last_send:                      # stamped by OMS around broker.submit only
            start, end, state = self.oms.last_send
            self.lat.decision_to_submit.append(start - t_decided)
            self.lat.submit_to_ack.append(end - start)   # all outcomes, not just ACKED
            self.oms.last_send = None
        self.meta[o.client_id] = s

    def _pump(self) -> None:
        self.oms.pump()
        for cid, sig in self.meta.items():
            o = self.store.order(cid)
            mine = sig.action == Action.ENTER and sig.strategy == getattr(self.strategy, "name", None)
            if o and mine and o.filled_qty > self.notified.get(cid, 0):
                self.notified[cid] = o.filled_qty
                self.strategy.on_fill(sig.instrument, o.filled_qty, sig.stop, sig.target,
                                      sig.decided_ns + self.strategy.p.hold_minutes * 60 * NS)
            if o and sig.expiry_ns and self.now_ns > sig.expiry_ns and o.state in (OrderState.ACKED,
                                                                                   OrderState.PARTIAL):
                self.oms.cancel(cid)                # signal expired (entry or exit): cancel the remainder
        self._rearm_stuck_intraday()
        if self.kill_level >= 3 and self.now_ns:
            self.flatten("kill-auto")               # replaces exits whose cancel landed; no duplicates

    def _rearm_stuck_intraday(self) -> None:
        """An intraday long with no live exit order (exit rejected/expired/partial) gets an immediate time-exit."""
        live = self.store.orders(OPEN)
        selling = {o.instrument for o in live if o.side == Side.SELL and o.book == Book.INTRADAY}
        for (acct, book, inst), qty in self.store.positions().items():
            day = getattr(self.strategy, "days", {}).get(inst)
            if self.now_ns < self.rearm_after.get(inst, 0):
                continue
            if (book == Book.INTRADAY.value and qty > 0 and inst not in selling and day is not None
                    and day.entry is None and not any(o.instrument == inst and o.side == Side.BUY
                                                      and o.book == Book.INTRADAY for o in live)):
                self.strategy.on_fill(inst, qty, 0.0, float("inf"), self.now_ns)

    # ---- risk view ----
    def equity(self) -> float:
        cur = {i.id: i.currency for i in self.reg.values()}
        marks = {k: (q.bid + q.ask) / 2 for k, q in self.last.items() if q.bid and q.ask}
        return self.initial + pnl(self.store.fills(), cur, marks, self.fx, self.now_ns)["total"]

    def _roll_day(self) -> None:
        day = datetime.fromtimestamp(self.now_ns / NS, ZoneInfo(self.risk.limits.base_tz)).date().isoformat()
        if day != self.day:                         # global risk day in BASE tz: markets don't reset it
            self.day, self.day_start = day, self.equity()
            self.store.audit("risk", "day_start", {"day": day, "equity": self.day_start})

    def view(self) -> RiskView:
        eq = self.equity()
        if eq > self.peak * 1.001:                  # ponytail: persisted at 0.1% steps, not every tick
            self.store.audit("risk", "peak", {"equity": eq})
        self.peak = max(self.peak, eq)
        fxs = {}
        for i in self.reg.values():
            try:
                fxs[i.currency] = self.fx.at(i.currency, self.now_ns)
            except KeyError:
                pass
        marks = {k: (q.bid + q.ask) / 2 * fxs[self.reg[k].currency] for k, q in self.last.items()
                 if q.bid and q.ask and self.reg[k].currency in fxs}
        live = self.store.orders(OPEN)
        open_sells: dict[tuple, int] = {}
        open_buys: dict[tuple, float] = {}           # base-ccy value of working buy remainder
        for o in live:
            k = (o.account, o.book.value, o.instrument)
            rem = o.qty - o.filled_qty
            if o.side == Side.SELL:
                open_sells[k] = open_sells.get(k, 0) + rem
            else:
                open_buys[k] = open_buys.get(k, 0.0) + rem * o.limit_price * fxs.get(self.reg[o.instrument].currency, 0.0)
        spent: dict[str, float] = {}   # open buy orders + executed buys; sell proceeds NOT credited
        for o in live:                            # ponytail: proceeds count only after settlement (not modeled)
            if o.side == Side.BUY:
                spent[o.account] = spent.get(o.account, 0.0) + (o.qty - o.filled_qty) * o.limit_price
        for acct, _b, _i, side, q, p, fee, _t in self.store.fills():
            spent[acct] = spent.get(acct, 0.0) + (q * p if side == "BUY" else 0.0) + fee
        return RiskView(
            positions={k: v for k, v in self.store.positions().items()}, marks=marks, fx=fxs,
            sectors={i.id: i.sector for i in self.reg.values()}, equity=eq, day_start_equity=self.day_start,
            peak_equity=self.peak,
            open_buy_value=spent,
            unknown_orders=len(self.store.orders(frozenset({OrderState.UNKNOWN}))),
            kill_level=self.kill_level, open_sells=open_sells, open_buys=open_buys,
            blocked=frozenset(i for i in self.reg if self.quality.blocked(i)))

    def decision_hash(self) -> str:
        return hashlib.sha256("\n".join(self.decisions).encode()).hexdigest()

    def flatten(self, actor: str) -> list[str]:
        """Submit reduce-only marketable sells for every long, per book. Returns outcomes."""
        out = []
        working = {}
        for o in self.store.orders(OPEN):
            if o.side == Side.SELL:
                k = (o.account, o.book.value, o.instrument)
                working[k] = working.get(k, 0) + o.qty - o.filled_qty
        for (acct, book, inst), held in sorted(self.store.positions().items()):
            qty = held - working.get((acct, book, inst), 0)
            if held > 0 and qty <= 0:
                out.append(f"{inst} {book} {held}: exit already working - not duplicated")
                continue
            q = self.last.get(inst)
            if qty <= 0 or q is None or not q.bid:
                out.append(f"{inst} {book} {qty}: NOT submitted (no fresh bid) - manual action needed")
                continue
            s = Signal(strategy="kill", version="1", instrument=inst, book=Book(book), action=Action.EXIT,
                       decided_ns=self.now_ns, reason=f"kill flatten by {actor}", side=Side.SELL, qty=qty,
                       limit_price=q.bid)
            self._act(s, q, time.perf_counter_ns())
            o = self.store.order(self.oms.client_id(s.id))
            out.append(f"{inst} {book} {qty}: {o.state.value if o else 'BLOCKED by risk'}")
        return out
