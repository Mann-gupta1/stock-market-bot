"""Broker boundary + a deterministic paper broker with fault injection.

Live adapters (Kite/Upstox/Dhan/IBKR) implement the same Protocol; none is wired yet - live is disabled.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol

from .model import Execution, Order, Quote, Side


class BrokerTimeout(Exception):
    """Outcome unknown. The order may or may not exist at the broker. Never retry blindly."""


class BrokerRejected(Exception):
    """Broker definitively did not accept the order."""


class RateLimited(BrokerRejected):
    """Throttled: definitively not accepted (HTTP 429 semantics)."""


@dataclass(frozen=True)
class BrokerOrder:
    client_id: str
    broker_id: str
    state: str          # WORKING | FILLED | CANCELLED | REJECTED | EXPIRED
    filled_qty: int


@dataclass(frozen=True)
class BrokerEvent:
    kind: str           # fill | cancelled | cancel_rejected | expired
    client_id: str
    execution: Execution | None = None


class Broker(Protocol):
    def submit(self, order: Order) -> str: ...
    def cancel(self, client_id: str) -> None: ...
    def order_status(self, client_id: str) -> BrokerOrder | None: ...
    def executions(self) -> list[Execution]: ...
    def positions(self) -> dict[tuple[str, str], int]: ...
    def poll(self) -> list[BrokerEvent]: ...


@dataclass
class Faults:
    """Deterministic fault injection for QA. Counters are 1-based submit numbers."""
    timeout_after_accept: set = field(default_factory=set)
    timeout_before_accept: set = field(default_factory=set)
    rate_limit: set = field(default_factory=set)
    reject: set = field(default_factory=set)
    duplicate_fills: bool = False
    fee_bps: float = 0.0


class PaperBroker:
    """Fills limit orders against later quotes at the touch, capped by displayed size (partial fills).
    SIMULATED: no queue position, no hidden liquidity. Cancels take effect on the next quote, after matching,
    so fills can race a cancel exactly like a real venue."""

    def __init__(self, account: str, faults: Faults | None = None) -> None:
        self.account, self.f = account, faults or Faults()
        self._orders: dict[str, dict] = {}
        self._execs: list[Execution] = []
        self._events: list[BrokerEvent] = []
        self._pending_cancel: set[str] = set()
        self.n_submit = 0

    def submit(self, order: Order) -> str:
        self.n_submit += 1
        n = self.n_submit
        if n in self.f.rate_limit:
            raise RateLimited("429 throttled")
        if n in self.f.reject:
            raise BrokerRejected("RMS reject")
        if n in self.f.timeout_before_accept:
            raise BrokerTimeout("timeout (not accepted)")
        if order.client_id in self._orders:          # broker-side idempotency on client id
            return self._orders[order.client_id]["broker_id"]
        self._orders[order.client_id] = dict(order=order, broker_id=f"P{n}", state="WORKING", filled=0)
        if n in self.f.timeout_after_accept:
            raise BrokerTimeout("timeout (accepted)")
        return f"P{n}"

    def cancel(self, client_id: str) -> None:
        if client_id in self._orders:
            self._pending_cancel.add(client_id)

    def on_quote(self, q: Quote) -> None:
        for cid, o in self._orders.items():
            od: Order = o["order"]
            if o["state"] != "WORKING" or od.instrument != q.instrument:
                continue
            px, size = (q.ask, q.ask_qty) if od.side == Side.BUY else (q.bid, q.bid_qty)
            crosses = px is not None and (px <= od.limit_price if od.side == Side.BUY else px >= od.limit_price)
            if crosses and size > 0 and not q.halted:
                fq = min(od.qty - o["filled"], size)
                e = Execution(exec_id=f"{o['broker_id']}-{q.seq}", client_id=cid, qty=fq, price=px,
                              ts_ns=q.ts_exch_ns, fee=fq * px * self.f.fee_bps / 1e4)
                o["filled"] += fq
                self._execs.append(e)
                self._events.append(BrokerEvent("fill", cid, e))
                if self.f.duplicate_fills:
                    self._events.append(BrokerEvent("fill", cid, e))
                if o["filled"] == od.qty:
                    o["state"] = "FILLED"
        for cid in sorted(self._pending_cancel):
            o = self._orders[cid]
            if o["state"] == "WORKING":
                o["state"] = "CANCELLED"
                self._events.append(BrokerEvent("cancelled", cid))
            else:
                self._events.append(BrokerEvent("cancel_rejected", cid))
        self._pending_cancel.clear()

    def order_status(self, client_id: str) -> BrokerOrder | None:
        o = self._orders.get(client_id)
        return BrokerOrder(client_id, o["broker_id"], o["state"], o["filled"]) if o else None

    def executions(self) -> list[Execution]:
        return list(self._execs)

    def positions(self) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = {}
        for e in self._execs:
            od: Order = self._orders[e.client_id]["order"]
            k = (self.account, od.instrument)
            out[k] = out.get(k, 0) + (e.qty if od.side == Side.BUY else -e.qty)
        return {k: v for k, v in out.items() if v}

    def poll(self) -> list[BrokerEvent]:
        ev, self._events = self._events, []
        return ev

    def drop_events(self) -> None:
        """Fault: simulate callbacks lost during a disconnect."""
        self._events = []

    def with_faults(self, **kw) -> "PaperBroker":
        self.f = replace(self.f, **kw)
        return self
