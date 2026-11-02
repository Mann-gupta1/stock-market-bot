"""Intraday opening-range breakout on L1 quotes. Pure + deterministic: time comes only from quote timestamps.

Spec (v1, hypothesis - NOT a proven edge; candle replay in intraday.py lost money in all 9 markets):
  OR        = max mid over [open, open + or_minutes) local exchange time.
  bar       = 1-minute bars of mid built from quotes; volume = cum_volume delta.
  ENTER     = first completed bar after OR with close > OR high AND close > session VWAP(mid, volume)
              AND bar volume >= vol_mult * median volume for that minute slot (input profile, prior sessions).
              limit = ask at decision; NO_TRADE if ask > bar close * (1 + max_chase).
              stop = bar low; R = limit - stop; target = limit + target_r * R; qty = risk_per_trade / R (lots).
  EXIT      = bid <= stop, bid >= target, hold_minutes elapsed, or flat_before_close minutes to close.
              Exit limit = bid (marketable), reduce-only, intraday book only.
  One entry per instrument per day. Missing profile -> NO_TRADE.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from .model import Action, Book, Instrument, Quote, Side, Signal

NAME, VERSION = "orb", "1.0.0"
PROFILE_DAYS = 20
NS = 1_000_000_000


@dataclass(frozen=True)
class OrbParams:
    or_minutes: int = 15
    vol_mult: float = 1.5
    max_chase: float = 0.002
    target_r: float = 1.5
    hold_minutes: int = 15
    flat_before_close_min: int = 10
    risk_per_trade: float = 1000.0     # instrument currency


@dataclass
class _Day:
    date: str
    or_high: float = 0.0
    pv: float = 0.0
    vol: int = 0
    last_cum: int | None = None
    bar_min: int | None = None
    bar: list = field(default_factory=list)    # [open, high, low, close, volume]
    traded: bool = False
    noted: set = field(default_factory=set)       # NO_TRADE kinds already logged today
    slot_vols: dict = field(default_factory=dict)  # slot -> bar volume today (feeds tomorrow's profile)
    entry: dict | None = None                  # {stop, target, deadline_ns, qty}


class OrbStrategy:
    name = NAME
    def __init__(self, instruments: dict[str, Instrument], tz: dict[str, str], session: dict[str, tuple[str, str]],
                 vol_profile: dict[str, dict[int, list[int]]], params: OrbParams = OrbParams()) -> None:
        self.inst, self.tz, self.session, self.p = instruments, tz, session, params
        self.profile = {k: {s: list(v) for s, v in d.items()} for k, d in vol_profile.items()}  # own copy
        self.days: dict[str, _Day] = {}

    def on_fill(self, instrument: str, qty: int, stop: float, target: float, deadline_ns: int) -> None:
        d = self.days[instrument]
        d.entry = dict(stop=stop, target=target, deadline_ns=deadline_ns, qty=qty)

    def on_flat(self, instrument: str) -> None:
        if instrument in self.days:
            self.days[instrument].entry = None

    def on_quote(self, q: Quote) -> list[Signal]:
        inst = self.inst[q.instrument]
        local = datetime.fromtimestamp(q.ts_exch_ns / NS, ZoneInfo(self.tz[inst.exchange]))
        o, c, *rest = self.session[inst.exchange]
        c = (rest[0] if rest else {}).get(local.date().isoformat(), c)     # early close
        t_open = local.replace(hour=int(o[:2]), minute=int(o[3:]), second=0, microsecond=0)
        t_close = local.replace(hour=int(c[:2]), minute=int(c[3:]), second=0, microsecond=0)
        if not (t_open <= local < t_close) or q.bid is None or q.ask is None:
            return []
        d = self.days.get(q.instrument)
        if d is None or d.date != local.date().isoformat():
            if d is not None:                      # roll: today's slots join the profile (point-in-time)
                prof = self.profile.setdefault(q.instrument, {})
                for slot, v in d.slot_vols.items():
                    prof[slot] = (prof.get(slot, []) + [v])[-PROFILE_DAYS:]
            d = self.days[q.instrument] = _Day(local.date().isoformat())
        mid = (q.bid + q.ask) / 2
        dv = 0 if d.last_cum is None else max(q.cum_volume - d.last_cum, 0)
        d.last_cum = q.cum_volume
        minute = int((local - t_open).total_seconds() // 60)
        out: list[Signal] = []
        if d.entry:
            out += self._exit(q, d, local, t_close)
        if minute < self.p.or_minutes:
            d.or_high = max(d.or_high, mid)
            d.pv, d.vol = d.pv + mid * dv, d.vol + dv
            return out
        if d.bar_min is not None and minute != d.bar_min and d.bar:
            out += self._on_bar_close(q, d, inst, d.bar, d.bar_min)   # VWAP excludes this new-bar quote
            d.bar = []
        d.pv, d.vol = d.pv + mid * dv, d.vol + dv
        if not d.bar:
            d.bar, d.bar_min = [mid, mid, mid, mid, dv], minute
        else:
            d.bar = [d.bar[0], max(d.bar[1], mid), min(d.bar[2], mid), mid, d.bar[4] + dv]
        return out

    def _no(self, q: Quote, d: _Day, kind: str, reason: str, inputs: tuple) -> list[Signal]:
        """NO_TRADE is a logged outcome, once per kind per instrument-day (keeps the log readable)."""
        if kind in d.noted:
            return []
        d.noted.add(kind)
        return [self._sig(q, Action.NO_TRADE, reason, inputs=inputs)]

    def _sig(self, q: Quote, action: Action, reason: str, **kw) -> Signal:
        return Signal(strategy=NAME, version=VERSION, instrument=q.instrument, book=Book.INTRADAY, action=action,
                      decided_ns=q.ts_exch_ns, reason=reason, **kw)

    def _on_bar_close(self, q: Quote, d: _Day, inst: Instrument, bar: list, slot: int) -> list[Signal]:
        _o, _h, low, close, vol = bar
        d.slot_vols[slot] = vol
        vwap = d.pv / d.vol if d.vol else close
        hist = self.profile.get(q.instrument, {}).get(slot, [])
        med = median(hist) if len(hist) >= 10 else None
        inputs = tuple(sorted(dict(or_high=round(d.or_high, 6), bar_close=round(close, 6), bar_low=round(low, 6),
                                   bar_vol=vol, vwap=round(vwap, 6), vol_median=med, ask=q.ask, bid=q.bid,
                                   slot=slot, params=repr(self.p)).items()))
        if d.traded or d.entry or d.or_high <= 0:      # or_high 0 = joined after OR window: no setup
            return []
        if not (close > d.or_high and close > vwap):
            return []                                    # no setup this bar: nothing to record
        if med is None:
            return self._no(q, d, "profile", "no volume profile (need >=10 prior sessions)", inputs)
        if vol < self.p.vol_mult * med:
            return self._no(q, d, "volume", f"bar vol {vol} < {self.p.vol_mult}x median {med}", inputs)
        d.traded = True
        limit = q.ask
        if limit > close * (1 + self.p.max_chase):
            return [self._sig(q, Action.NO_TRADE, f"ask {limit} beyond max entry {close * (1 + self.p.max_chase):.4f}"
                                                  ": no chase", inputs=inputs)]
        r = limit - low
        if r <= 0:
            return [self._sig(q, Action.NO_TRADE, "stop >= entry", inputs=inputs)]
        lots = int(self.p.risk_per_trade / r) // inst.lot_size
        if lots < 1:
            return [self._sig(q, Action.NO_TRADE, "risk budget < 1 lot", inputs=inputs)]
        return [self._sig(q, Action.ENTER, "OR breakout", side=Side.BUY, qty=lots * inst.lot_size,
                          limit_price=limit, stop=low, target=limit + self.p.target_r * r,
                          expiry_ns=q.ts_exch_ns + 60 * NS,
                          exit_rule=f"stop {low} | target {self.p.target_r}R | {self.p.hold_minutes}min | "
                                    f"flat {self.p.flat_before_close_min}min before close",
                          inputs=inputs)]

    def _exit(self, q: Quote, d: _Day, local: datetime, t_close: datetime) -> list[Signal]:
        e = d.entry
        why = ("stop" if q.bid <= e["stop"] else "target" if q.bid >= e["target"] else
               "time" if q.ts_exch_ns >= e["deadline_ns"] else
               "session close" if local >= t_close - timedelta(minutes=self.p.flat_before_close_min) else "")
        if not why:
            return []
        d.entry = None   # one exit intent; engine re-arms via on_fill if exit order dies unfilled
        return [self._sig(q, Action.EXIT, why, side=Side.SELL, qty=e["qty"], limit_price=q.bid,
                          expiry_ns=q.ts_exch_ns + 5 * NS,
                          inputs=(("bid", q.bid), ("stop", e["stop"]), ("target", e["target"])))]
