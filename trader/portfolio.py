"""Base-currency P&L with timestamped FX, split into trading effect vs FX effect.

Per position (average cost): base P&L = q*(P1*F1 - P0*F0) = q*(P1-P0)*F0  [trading]  +  q*P1*(F1-F0)  [FX]
where F0 is the cost-weighted FX of the position, F1 the FX at sale/mark time. Fees converted at fill-time FX.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field


@dataclass
class FxTable:
    base: str
    _ts: dict = field(default_factory=dict)      # ccy -> [ts...]
    _rate: dict = field(default_factory=dict)    # ccy -> [rate...]

    def add(self, ccy: str, ts_ns: int, rate: float) -> None:
        if rate <= 0:
            raise ValueError(f"bad fx {ccy} {rate}")
        i = bisect.bisect_right(self._ts.setdefault(ccy, []), ts_ns)
        self._ts[ccy].insert(i, ts_ns)
        self._rate.setdefault(ccy, []).insert(i, rate)

    def at(self, ccy: str, ts_ns: int) -> float:
        """Latest rate known at ts (point-in-time, no look-ahead). KeyError if none yet."""
        if ccy == self.base:
            return 1.0
        i = bisect.bisect_right(self._ts.get(ccy, []), ts_ns)
        if i == 0:
            raise KeyError(f"no {ccy}->{self.base} rate at or before {ts_ns}")
        return self._rate[ccy][i - 1]


def pnl(fills: list[tuple], currency: dict[str, str], marks: dict[str, float], fx: FxTable, now_ns: int) -> dict:
    """fills: (account, book, instrument, side, qty, price, fee, ts_ns) ordered by time."""
    pos: dict[tuple, list[float]] = {}         # key -> [qty, avg_local, avg_fx]
    out = dict(trading=0.0, fx=0.0, fees=0.0)
    for acct, book, inst, side, q, p, fee, ts in fills:
        f = fx.at(currency[inst], ts)
        out["fees"] -= fee * f
        qty, avg, afx = pos.get((acct, book, inst), [0, 0.0, 0.0])
        if side == "BUY":
            new = qty + q
            pos[(acct, book, inst)] = [new, (avg * qty + p * q) / new, (avg * qty * afx + p * q * f) / (avg * qty + p * q)]
        else:
            out["trading"] += q * (p - avg) * afx
            out["fx"] += q * p * (f - afx)
            pos[(acct, book, inst)] = [qty - q, avg, afx]
    for (acct, book, inst), (qty, avg, afx) in pos.items():
        if qty and inst in marks:
            f = fx.at(currency[inst], now_ns)
            out["trading"] += qty * (marks[inst] - avg) * afx
            out["fx"] += qty * marks[inst] * (f - afx)
    out["total"] = out["trading"] + out["fx"] + out["fees"]
    return out
