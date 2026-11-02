"""Order management. Persist intent BEFORE any broker call; never retry after a timeout.

client_id = deterministic hash of the signal -> the same signal can never become two economic orders,
including across restarts and replays.
"""
from __future__ import annotations

import time

from .broker import Broker, BrokerEvent, BrokerRejected, BrokerTimeout, RateLimited
from .model import OPEN, Book, Order, OrderState, Side
from .store import Store

BROKER_STATE = {"FILLED": OrderState.FILLED, "CANCELLED": OrderState.CANCELLED,
                "REJECTED": OrderState.REJECTED, "EXPIRED": OrderState.EXPIRED}


class OMS:
    def __init__(self, store: Store, broker: Broker, account: str) -> None:
        self.store, self.broker, self.account = store, broker, account
        self.throttled = 0           # count of 429s; engine backs off entries while > 0 recently
        self.last_send: tuple | None = None   # (start_ns, end_ns, state) around broker.submit only

    @staticmethod
    def client_id(signal_id: str) -> str:
        return "c" + signal_id[:23]

    def place(self, signal_id: str, book: Book, instrument: str, side: Side, qty: int, limit_price: float,
              reduce_only: bool) -> Order:
        cid = self.client_id(signal_id)
        o = Order(client_id=cid, signal_id=signal_id, account=self.account, book=book, instrument=instrument,
                  side=side, qty=qty, limit_price=limit_price, state=OrderState.INTENT, reduce_only=reduce_only)
        if not self.store.insert_order(o):
            return self.store.order(cid)            # already exists: no second order, whatever its state
        return self._send(o)

    def _send(self, o: Order) -> Order:
        self.store.set_state(o.client_id, OrderState.SUBMITTING)
        t0 = time.perf_counter_ns()
        try:
            bid = self.broker.submit(o)
            self.last_send = (t0, time.perf_counter_ns(), "ACKED")    # before the DB write: broker time only
            self.store.set_state(o.client_id, OrderState.ACKED, broker_id=bid)
        except RateLimited as e:
            self.last_send = (t0, time.perf_counter_ns(), 'RateLimited')
            self.throttled += 1
            self.store.set_state(o.client_id, OrderState.REJECTED, f"throttled, not accepted: {e}")
        except BrokerRejected as e:
            self.last_send = (t0, time.perf_counter_ns(), 'BrokerRejected')
            self.store.set_state(o.client_id, OrderState.REJECTED, str(e))
        except BrokerTimeout as e:
            self.last_send = (t0, time.perf_counter_ns(), 'BrokerTimeout')
            self.store.set_state(o.client_id, OrderState.UNKNOWN, f"timeout, NOT retried: {e}")
        return self.store.order(o.client_id)

    CANCELLABLE = (OrderState.ACKED, OrderState.PARTIAL, OrderState.CANCEL_REQUESTED, OrderState.UNKNOWN,
                   OrderState.SUBMITTING)

    def cancel(self, client_id: str) -> None:
        """Idempotent; re-sendable from CANCEL_REQUESTED/UNKNOWN. A cancel timeout leaves CANCEL_REQUESTED."""
        o = self.store.order(client_id)
        if not o or o.state not in self.CANCELLABLE:
            return
        if o.state != OrderState.UNKNOWN:            # UNKNOWN stays UNKNOWN until reconcile proves state
            self.store.set_state(client_id, OrderState.CANCEL_REQUESTED)
        try:
            self.broker.cancel(client_id)
        except (BrokerTimeout, BrokerRejected) as e:
            self.store.audit("oms", "cancel_failed", {"client_id": client_id, "error": str(e)})

    def cancel_all(self) -> int:
        live = [o for o in self.store.orders(OPEN) if o.state in self.CANCELLABLE]
        for o in live:
            self.cancel(o.client_id)
        return len(live)

    def handle(self, ev: BrokerEvent) -> None:
        o = self.store.order(ev.client_id)
        if o is None:
            self.store.audit("oms", "orphan_event", {"kind": ev.kind, "client_id": ev.client_id})
            return
        if ev.kind == "fill" and ev.execution:
            self.store.add_execution(ev.execution)       # duplicate exec_id -> no-op
        elif ev.kind == "cancelled" and o.state != OrderState.FILLED:
            self.store.set_state(o.client_id, OrderState.CANCELLED, f"filled {o.filled_qty}/{o.qty}")
        elif ev.kind == "cancel_rejected" and o.state == OrderState.CANCEL_REQUESTED:
            # already filled at the venue; fills arrive (or arrived) separately
            self.store.set_state(o.client_id, OrderState.FILLED if o.filled_qty == o.qty else OrderState.PARTIAL,
                                 "cancel rejected")
        elif ev.kind == "expired" and o.state not in (OrderState.FILLED, OrderState.CANCELLED):
            self.store.set_state(o.client_id, OrderState.EXPIRED)

    def pump(self) -> int:
        evs = self.broker.poll()
        for ev in evs:
            self.handle(ev)
        return len(evs)

    def reconcile(self) -> list[str]:
        """Run on startup and every reconnect. Broker is the source of truth for what exists."""
        issues: list[str] = []
        for e in self.broker.executions():
            if self.store.order(e.client_id) is None:
                issues.append(f"broker execution for unknown order {e.client_id}")
                continue
            self.store.add_execution(e)
        for o in self.store.orders(OPEN):
            bo = self.broker.order_status(o.client_id)
            o = self.store.order(o.client_id)
            if bo is None:
                if o.state in (OrderState.INTENT, OrderState.SUBMITTING, OrderState.UNKNOWN):
                    self.store.set_state(o.client_id, OrderState.REJECTED, "not at broker on reconcile; not resent")
                else:
                    issues.append(f"{o.client_id} {o.state.value} locally but missing at broker")
                    self.store.set_state(o.client_id, OrderState.UNKNOWN, "missing at broker")
                continue
            if bo.filled_qty != o.filled_qty:
                issues.append(f"{o.client_id} filled {o.filled_qty} local vs {bo.filled_qty} broker")
            target = BROKER_STATE.get(bo.state) or (OrderState.PARTIAL if bo.filled_qty else OrderState.ACKED)
            if o.state == OrderState.CANCEL_REQUESTED and bo.state == "WORKING":
                continue
            if target != o.state:
                self.store.set_state(o.client_id, target, "reconciled", broker_id=bo.broker_id)
        mine: dict[tuple[str, str], int] = {}
        for (acct, _book, inst), q in self.store.positions().items():
            mine[(acct, inst)] = mine.get((acct, inst), 0) + q
        theirs = self.broker.positions()
        for k in sorted(set(mine) | set(theirs)):
            if mine.get(k, 0) != theirs.get(k, 0):
                issues.append(f"position mismatch {k}: local {mine.get(k, 0)} broker {theirs.get(k, 0)}")
        self.store.audit("oms", "reconcile", {"issues": issues})
        return issues
