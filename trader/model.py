"""Core value types. All frozen: state changes produce new objects or durable rows, never in-place edits."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Book(str, Enum):
    """Separate strategy books. An intraday exit can only reduce the intraday book."""
    INTRADAY = "intraday"
    SWING = "swing"


class Action(str, Enum):
    ENTER = "ENTER"
    EXIT = "EXIT"
    NO_TRADE = "NO_TRADE"


class OrderState(str, Enum):
    INTENT = "INTENT"                    # persisted, not yet sent
    SUBMITTING = "SUBMITTING"            # send in flight
    ACKED = "ACKED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"                  # timeout / lost ack: must reconcile before more exposure


TERMINAL = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED})
OPEN = frozenset(OrderState) - TERMINAL


@dataclass(frozen=True)
class Instrument:
    id: str            # stable id "XNSE:RELIANCE"
    exchange: str      # MIC-like code, key into sessions config
    symbol: str
    currency: str
    tick_size: float
    lot_size: int
    sector: str
    verified: bool = False   # tick/lot confirmed from exchange source


@dataclass(frozen=True)
class Quote:
    instrument: str
    seq: int                  # per-stream sequence number
    ts_exch_ns: int           # exchange timestamp
    ts_recv_ns: int           # local receive timestamp
    bid: float | None
    ask: float | None
    bid_qty: int
    ask_qty: int
    last: float | None
    cum_volume: int           # day cumulative volume
    halted: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @staticmethod
    def from_json(s: str) -> "Quote":
        def no_const(c: str) -> float:
            raise ValueError(f"non-finite number {c} in quote")
        return Quote(**json.loads(s, parse_constant=no_const))


@dataclass(frozen=True)
class Signal:
    """A strategy decision, NO_TRADE included. Everything needed to audit or replay it."""
    strategy: str
    version: str
    instrument: str
    book: Book
    action: Action
    decided_ns: int           # decision time = event time of triggering quote (deterministic)
    reason: str
    side: Side | None = None
    qty: int = 0
    limit_price: float | None = None
    stop: float | None = None
    target: float | None = None
    expiry_ns: int | None = None
    exit_rule: str = ""
    inputs: tuple = field(default_factory=tuple)   # ((name, value), ...) sorted

    @property
    def id(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:24]

    def to_json(self) -> str:
        d = asdict(self)
        d.update(book=self.book.value, action=self.action.value, side=self.side.value if self.side else None)
        return json.dumps(d, sort_keys=True, default=str)


@dataclass(frozen=True)
class Order:
    client_id: str
    signal_id: str
    account: str
    book: Book
    instrument: str
    side: Side
    qty: int
    limit_price: float
    state: OrderState
    filled_qty: int = 0
    avg_price: float = 0.0
    broker_id: str | None = None
    reduce_only: bool = False
    note: str = ""


@dataclass(frozen=True)
class Execution:
    exec_id: str          # broker execution id, idempotency key
    client_id: str
    qty: int
    price: float
    ts_ns: int
    fee: float = 0.0
