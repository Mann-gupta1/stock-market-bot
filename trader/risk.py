"""Pre-trade risk. Limits are versioned config, validated on load, hash recorded with every decision.

Rules:
- Exposure-increasing orders pass every check.
- Reduce-only orders (sell <= long held in the SAME book) bypass loss/drawdown/kill/exposure locks, so a
  risk lock never traps a position. They still need a sane price when a fresh quote exists.
- Daily loss is a GLOBAL budget keyed by the base-timezone date: opening another market does not reset it.
"""
from __future__ import annotations

import hashlib
import math
import json
from dataclasses import dataclass, field, fields
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from .model import Book, Instrument, Quote, Side

NS = 1_000_000_000


@dataclass(frozen=True)
class Limits:
    version: str
    base_currency: str
    base_tz: str
    max_position_value: float          # base ccy, per instrument per book
    max_order_qty: int
    max_strategy_exposure: float       # base ccy, per book
    max_sector_exposure: float         # base ccy, all books/markets
    max_gross_exposure: float          # base ccy, everything
    max_daily_loss: float              # base ccy, positive number
    max_drawdown: float                # fraction of peak equity, e.g. 0.10
    max_spread_bps: float
    max_slippage_bps: float            # limit price vs touch
    max_quote_age_ms: int
    risk_per_trade: float              # base ccy risked to stop

    @staticmethod
    def from_file(path: str | Path) -> tuple["Limits", str]:
        p = Path(path)
        if p.stat().st_size > 64_000:
            raise ValueError("limits file too large")
        raw = p.read_bytes()
        d = json.loads(raw)
        names = {f.name for f in fields(Limits)}
        if set(d) != names:
            raise ValueError(f"limits keys mismatch: missing {names - set(d)}, unknown {set(d) - names}")
        lim = Limits(**d)
        for f in fields(Limits):
            v = getattr(lim, f.name)
            if f.type in ("float", "int") and (not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0
                                               or not math.isfinite(v)):
                raise ValueError(f"{f.name} must be a positive finite number, got {v!r}")
            if f.type == "int" and not isinstance(v, int):
                raise ValueError(f"{f.name} must be an integer")
            if f.type == "str" and not (isinstance(v, str) and v):
                raise ValueError(f"{f.name} must be a non-empty string")
        if lim.max_order_qty > 1_000_000:
            raise ValueError("max_order_qty implausibly large")
        if lim.max_drawdown >= 1:
            raise ValueError("max_drawdown is a fraction < 1")
        ZoneInfo(lim.base_tz)
        return lim, hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class Session:
    tz: str
    open: str
    close: str
    holidays: frozenset[str]
    early_closes: dict
    breaks: tuple = ()        # ((start, end), ...) e.g. Tokyo/HK lunch

    def is_open(self, ts_ns: int) -> bool:
        local = datetime.fromtimestamp(ts_ns / NS, ZoneInfo(self.tz))   # zoneinfo handles DST
        day = local.date().isoformat()
        if local.weekday() >= 5 or day in self.holidays:
            return False
        close = self.early_closes.get(day, self.close)
        t = local.time()
        if any(dtime.fromisoformat(a) <= t < dtime.fromisoformat(b) for a, b in self.breaks):
            return False
        return dtime.fromisoformat(self.open) <= t < dtime.fromisoformat(close)


@dataclass(frozen=True)
class Account:
    id: str
    currency: str
    settled_cash: float               # account currency; cash accounts only, no margin
    exchanges: frozenset[str]         # verified trading permissions


@dataclass(frozen=True)
class OrderRequest:
    account: str
    book: Book
    instrument: Instrument
    side: Side
    qty: int
    limit_price: float
    ts_ns: int


@dataclass(frozen=True)
class Decision:
    ok: bool
    reasons: tuple[str, ...]
    limits_version: str
    reduce_only: bool


@dataclass(frozen=True)
class RiskView:
    """Snapshot the engine builds from durable state. Values in base currency."""
    positions: dict          # (account, book, instrument_id) -> qty
    marks: dict              # instrument_id -> last price converted to BASE ccy
    fx: dict                 # currency -> base per 1 unit
    sectors: dict            # instrument_id -> sector
    equity: float
    day_start_equity: float
    peak_equity: float
    open_buy_value: dict     # account -> local-ccy value of open buy orders (reserved cash)
    unknown_orders: int
    kill_level: int          # 0 none, 1 halt entries, 2 cancel orders, 3 flatten
    blocked: frozenset[str]  # instrument ids blocked by data quality
    market_entries_blocked: bool = False
    open_sells: dict = field(default_factory=dict)   # (account, book, inst) -> working sell qty (incl. UNKNOWN)
    open_buys: dict = field(default_factory=dict)    # (account, book, inst) -> working buy value, base ccy


class RiskEngine:
    def __init__(self, limits: Limits, limits_hash: str, sessions: dict[str, Session],
                 accounts: dict[str, Account]) -> None:
        self.limits, self.hash = limits, limits_hash
        self.sessions, self.accounts = sessions, accounts

    def check(self, r: OrderRequest, view: RiskView, quote: Quote | None) -> Decision:
        L, inst = self.limits, r.instrument
        key = (r.account, r.book.value, inst.id)
        held = view.positions.get(key, 0) - view.open_sells.get(key, 0)   # working sells already claim stock
        reduce_only = r.side == Side.SELL and 0 < r.qty <= held
        bad: list[str] = []
        fx = view.fx.get(inst.currency)
        if fx is None:
            bad.append(f"no FX rate for {inst.currency}")
            fx = 0.0
        if r.qty <= 0 or r.qty % inst.lot_size:
            bad.append(f"qty {r.qty} not a positive multiple of lot {inst.lot_size}")
        if r.qty > L.max_order_qty:
            bad.append(f"qty {r.qty} > max_order_qty {L.max_order_qty}")
        if abs(round(r.limit_price / inst.tick_size) * inst.tick_size - r.limit_price) > 1e-9 * r.limit_price:
            bad.append(f"price {r.limit_price} not on tick {inst.tick_size}")
        sess = self.sessions.get(inst.exchange)
        if sess is None or not sess.is_open(r.ts_ns):
            bad.append(f"{inst.exchange} not in continuous trading session")
        if quote is not None:
            bad += self._quote_checks(r, quote)
        elif not reduce_only:
            bad.append("no quote")
        if r.side == Side.SELL and not reduce_only:
            bad.append(f"sell {r.qty} > {r.book.value} position {held}: shorting/cross-book exit not allowed")
        if reduce_only:
            return Decision(not bad, tuple(bad), self.hash, True)

        # ---- exposure-increasing checks ----
        if view.kill_level >= 1:
            bad.append(f"kill switch level {view.kill_level}: entries halted")
        if view.unknown_orders:
            bad.append(f"{view.unknown_orders} order(s) in UNKNOWN state: reconcile first")
        if inst.id in view.blocked or view.market_entries_blocked:
            bad.append("data quality block")
        if view.day_start_equity - view.equity >= L.max_daily_loss:
            bad.append(f"daily loss {view.day_start_equity - view.equity:.0f} >= {L.max_daily_loss}")
        if view.peak_equity and 1 - view.equity / view.peak_equity >= L.max_drawdown:
            bad.append(f"drawdown {1 - view.equity / view.peak_equity:.1%} >= {L.max_drawdown:.1%}")
        acct = self.accounts.get(r.account)
        if acct is None:
            bad.append(f"unknown account {r.account}")
        else:
            if inst.exchange not in acct.exchanges:
                bad.append(f"account {acct.id} has no verified permission for {inst.exchange}")
            if inst.currency != acct.currency:
                bad.append(f"{inst.currency} instrument in {acct.currency} account (no auto FX)")
            need = r.qty * r.limit_price + view.open_buy_value.get(acct.id, 0.0)
            if need > acct.settled_cash:
                bad.append(f"needs {need:.2f} {acct.currency} > settled cash {acct.settled_cash:.2f}")
        add = r.qty * r.limit_price * fx
        for k, q in view.positions.items():
            if q and k[2] not in view.marks:
                bad.append(f"no mark for held {k[2]}: exposure unknown, fail closed")
        val = lambda pred: (sum(abs(q) * view.marks.get(k[2], 0.0) for k, q in view.positions.items() if pred(k))
                            + sum(v for k, v in view.open_buys.items() if pred(k)))
        if val(lambda k: k[0] == r.account and k[1] == r.book.value and k[2] == inst.id) + add > L.max_position_value:
            bad.append("max_position_value")
        if val(lambda k: k[1] == r.book.value) + add > L.max_strategy_exposure:
            bad.append("max_strategy_exposure")
        sector = view.sectors.get(inst.id, inst.sector)
        if val(lambda k: view.sectors.get(k[2]) == sector) + add > L.max_sector_exposure:
            bad.append(f"max_sector_exposure ({sector})")
        if val(lambda k: True) + add > L.max_gross_exposure:
            bad.append("max_gross_exposure")
        return Decision(not bad, tuple(bad), self.hash, False)

    def _quote_checks(self, r: OrderRequest, q: Quote) -> list[str]:
        L, bad = self.limits, []
        if q.halted:
            bad.append("instrument halted")
        age_ms = (max(r.ts_ns, q.ts_recv_ns) - q.ts_exch_ns) / 1e6
        if age_ms > L.max_quote_age_ms:
            bad.append(f"quote age {age_ms:.0f}ms > {L.max_quote_age_ms}")
        if not q.bid or not q.ask or q.ask < q.bid:
            bad.append("no valid two-sided quote")
            return bad
        mid = (q.bid + q.ask) / 2
        if (q.ask - q.bid) / mid * 1e4 > L.max_spread_bps:
            bad.append(f"spread {(q.ask - q.bid) / mid * 1e4:.1f}bps > {L.max_spread_bps}")
        touch = q.ask if r.side == Side.BUY else q.bid
        slip = (r.limit_price - touch) / touch * 1e4 * (1 if r.side == Side.BUY else -1)
        if slip > L.max_slippage_bps:
            bad.append(f"limit {slip:.1f}bps through touch > {L.max_slippage_bps}")
        return bad
