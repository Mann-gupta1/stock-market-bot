"""Durable state in one SQLite file (stdlib, WAL, fsync on commit).

- events / audit: append-only, enforced by triggers (UPDATE/DELETE abort).
- orders: current state per client order id; every transition also appended to order_log.
- executions: PRIMARY KEY exec_id -> duplicate fill callbacks are no-ops.
Positions are derived from executions, never stored separately.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from .model import Book, Execution, Order, OrderState, Quote, Side

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, stream TEXT NOT NULL, seq INTEGER NOT NULL,
  ts_recv_ns INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_stream ON events(stream, id);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts_ns INTEGER NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(client_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL, account TEXT NOT NULL,
  book TEXT NOT NULL, instrument TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty > 0),
  limit_price REAL NOT NULL, state TEXT NOT NULL, filled_qty INTEGER NOT NULL DEFAULT 0, avg_price REAL NOT NULL
  DEFAULT 0, broker_id TEXT, reduce_only INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
  CHECK(filled_qty <= qty));
CREATE TABLE IF NOT EXISTS order_log(id INTEGER PRIMARY KEY, ts_ns INTEGER NOT NULL, client_id TEXT NOT NULL,
  state TEXT NOT NULL, note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS executions(exec_id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES orders,
  qty INTEGER NOT NULL CHECK(qty > 0), price REAL NOT NULL, ts_ns INTEGER NOT NULL, fee REAL NOT NULL DEFAULT 0);
"""
APPEND_ONLY = ("events", "audit", "order_log", "executions")


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.db = sqlite3.connect(str(path), isolation_level=None)  # explicit transactions
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        for t in APPEND_ONLY:
            for op in ("UPDATE", "DELETE"):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                                f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END")

    # ---- market data / signals (immutable) ----
    def append_event(self, stream: str, seq: int, ts_recv_ns: int, kind: str, payload: str) -> None:
        self.db.execute("INSERT INTO events(stream, seq, ts_recv_ns, kind, payload) VALUES (?,?,?,?,?)",
                        (stream, seq, ts_recv_ns, kind, payload))

    def record_quote(self, q: Quote, stream: str = "md") -> None:
        self.append_event(stream, q.seq, q.ts_recv_ns, "quote", q.to_json())

    def quotes(self, stream: str = "md") -> Iterator[Quote]:
        """Replay in recorded (arrival) order - the order the live engine saw them."""
        for (p,) in self.db.execute("SELECT payload FROM events WHERE stream=? AND kind='quote' ORDER BY id",
                                    (stream,)):
            yield Quote.from_json(p)

    def audit(self, actor: str, action: str, detail: dict) -> None:
        self.db.execute("INSERT INTO audit(ts_ns, actor, action, detail) VALUES (?,?,?,?)",
                        (time.time_ns(), actor, action, json.dumps(detail, sort_keys=True, default=str)))

    def audit_rows(self) -> list[tuple]:
        return self.db.execute("SELECT actor, action, detail FROM audit ORDER BY id").fetchall()

    # ---- orders ----
    def insert_order(self, o: Order) -> bool:
        """False if client_id already exists (idempotent intent)."""
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute(
                    "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (o.client_id, o.signal_id, o.account, o.book.value, o.instrument, o.side.value, o.qty,
                     o.limit_price, o.state.value, o.filled_qty, o.avg_price, o.broker_id, int(o.reduce_only),
                     o.note))
                self._log(o.client_id, o.state, "intent")
            return True
        except sqlite3.IntegrityError:
            return False

    def set_state(self, client_id: str, state: OrderState, note: str = "", broker_id: str | None = None) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("UPDATE orders SET state=?, note=?, broker_id=COALESCE(?, broker_id) WHERE client_id=?",
                            (state.value, note, broker_id, client_id))
            self._log(client_id, state, note)

    def add_execution(self, e: Execution) -> bool:
        """Apply a fill exactly once. Returns False for a duplicate exec_id."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM executions WHERE exec_id=?", (e.exec_id,)).fetchone():
                return False
            row = self.db.execute("SELECT qty, filled_qty, avg_price FROM orders WHERE client_id=?",
                                  (e.client_id,)).fetchone()
            if row is None:
                raise KeyError(f"execution for unknown order {e.client_id}")
            qty, filled, avg = row
            new_filled = filled + e.qty
            if new_filled > qty:
                raise ValueError(f"overfill {e.client_id}: {new_filled} > {qty}")
            self.db.execute("INSERT INTO executions VALUES (?,?,?,?,?,?)",
                            (e.exec_id, e.client_id, e.qty, e.price, e.ts_ns, e.fee))
            state = OrderState.FILLED if new_filled == qty else OrderState.PARTIAL
            cur = OrderState(self.db.execute("SELECT state FROM orders WHERE client_id=?",
                                             (e.client_id,)).fetchone()[0])
            if state == OrderState.PARTIAL and cur in (OrderState.CANCEL_REQUESTED, OrderState.CANCELLED,
                                                       OrderState.REJECTED, OrderState.EXPIRED):
                state = cur        # late fill after cancel/terminal: record fill, keep (terminal) state
            self.db.execute("UPDATE orders SET filled_qty=?, avg_price=?, state=? WHERE client_id=?",
                            (new_filled, (avg * filled + e.price * e.qty) / new_filled, state.value, e.client_id))
            self._log(e.client_id, state, f"fill {e.qty}@{e.price} {e.exec_id}")
            return True

    def order(self, client_id: str) -> Order | None:
        r = self.db.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone()
        return _order(r) if r else None

    def orders(self, states: frozenset[OrderState] | None = None) -> list[Order]:
        rows = [_order(r) for r in self.db.execute("SELECT * FROM orders ORDER BY rowid")]
        return [o for o in rows if states is None or o.state in states]

    def positions(self) -> dict[tuple[str, str, str], int]:
        """(account, book, instrument) -> net qty, from executions only."""
        out: dict[tuple[str, str, str], int] = {}
        for acct, book, inst, side, q in self.db.execute(
                "SELECT o.account, o.book, o.instrument, o.side, SUM(e.qty) FROM executions e "
                "JOIN orders o USING(client_id) GROUP BY 1,2,3,4"):
            k = (acct, book, inst)
            out[k] = out.get(k, 0) + (q if side == "BUY" else -q)
        return {k: v for k, v in out.items() if v}

    def fills(self) -> list[tuple]:
        return self.db.execute(
            "SELECT o.account, o.book, o.instrument, o.side, e.qty, e.price, e.fee, e.ts_ns FROM executions e "
            "JOIN orders o USING(client_id) ORDER BY e.ts_ns, e.rowid").fetchall()

    def _log(self, client_id: str, state: OrderState, note: str) -> None:
        self.db.execute("INSERT INTO order_log(ts_ns, client_id, state, note) VALUES (?,?,?,?)",
                        (time.time_ns(), client_id, state.value, note))


def _order(r: tuple) -> Order:
    return Order(client_id=r[0], signal_id=r[1], account=r[2], book=Book(r[3]), instrument=r[4], side=Side(r[5]),
                 qty=r[6], limit_price=r[7], state=OrderState(r[8]), filled_qty=r[9], avg_price=r[10],
                 broker_id=r[11], reduce_only=bool(r[12]), note=r[13])
