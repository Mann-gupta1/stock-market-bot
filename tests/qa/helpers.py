"""Shared builders for QA tests. Real config, stub or real strategy, PaperBroker."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from trader import cli
from trader.broker import Faults, PaperBroker
from trader.engine import Engine
from trader.model import Action, Book, Side, Signal, Quote
from trader.oms import OMS
from trader.risk import RiskEngine
from trader.store import Store
from trader.strategy_orb import OrbParams

NS = 1_000_000_000
IST = ZoneInfo("Asia/Kolkata")
TCS, INFY, REL = "XNSE:TCS", "XNSE:INFY", "XNSE:RELIANCE"


def ts(h: int, m: int, s: float = 0, day: tuple = (2026, 9, 1), tz=IST) -> int:
    """2026-09-01 is a Tuesday, NSE open 09:15-15:15 IST."""
    return int(datetime(*day, h, m, tzinfo=tz).timestamp() * NS) + int(s * NS)


class Stub:
    """Strategy stub: emits nothing by itself; tests call engine._act directly."""
    name = "stub"
    p = OrbParams()

    def __init__(self):
        self.fills = []

    def on_quote(self, q):
        return []

    def on_fill(self, *a):
        self.fills.append(a)


def make_engine(state=":memory:", broker=None, faults=None, strategy=None, queue_max=10_000):
    reg, ex, sessions, accounts, limits, h, fx = cli.load_config()
    store = Store(state)
    broker = broker or PaperBroker("paper-in", faults)
    return Engine(store, strategy or Stub(), RiskEngine(limits, h, sessions, accounts),
                  OMS(store, broker, "paper-in"), broker, reg, fx,
                  initial_equity=sum(a.settled_cash for a in accounts.values()), queue_max=queue_max)


class Feed:
    """Monotonic per-instrument seq; quotes at the touch with configurable size."""
    def __init__(self):
        self.seq = {}

    def q(self, inst, t, bid, ask, bq=1000, aq=1000, lag_ms=5, halted=False, seq=None, cum=0):
        if seq is None:
            seq = self.seq.get(inst, 0) + 1
        self.seq[inst] = max(self.seq.get(inst, 0), seq)
        return Quote(inst, seq, t, t + int(lag_ms * 1e6), bid, ask, bq, aq, None, cum, halted)


def sig(inst, side, qty, px, t, book=Book.INTRADAY, action=None, strategy="stub", reason="qa", expiry=None):
    action = action or (Action.ENTER if side == Side.BUY else Action.EXIT)
    return Signal(strategy=strategy, version="1", instrument=inst, book=book, action=action, decided_ns=t,
                  reason=reason, side=side, qty=qty, limit_price=px, expiry_ns=expiry)


def act(eng, s):
    """Run a signal through risk + OMS exactly like the engine does; returns the decision string."""
    eng._act(s, eng.last.get(s.instrument), 0)
    return eng.decisions[-1]
