"""Data quality gate. Decides per quote: feed it to the strategy or not, and block new exposure or not.

seq is per instrument. Policy:
- duplicate / out-of-order (seq <= last seen): dropped, not fed to strategy, counted.
- gap (seq jumps): fed, but instrument blocked until CLEAN_AFTER consecutive in-sequence quotes.
- invalid (crossed/non-positive/missing side), halted, clock skew, feed lag: fed? no - dropped and blocked.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .model import Quote


@dataclass
class QualityGate:
    max_feed_lag_ms: float = 2_000          # ts_recv - ts_exch
    max_future_skew_ms: float = 500         # exchange ts ahead of local clock
    clean_after: int = 5
    last_seq: dict = field(default_factory=dict)
    clean_run: dict = field(default_factory=dict)
    counts: dict = field(default_factory=lambda: dict(duplicate=0, out_of_order=0, gap=0, invalid=0, skew=0,
                                                      lag=0, halted=0))

    def check(self, q: Quote) -> tuple[bool, str]:
        """Returns (use_for_strategy, problem). Blocking state is read via blocked()."""
        last = self.last_seq.get(q.instrument)
        if last is not None and q.seq <= last:
            self.counts["duplicate" if q.seq == last else "out_of_order"] += 1
            return False, "duplicate" if q.seq == last else "out_of_order"
        problem = ""
        if last is not None and q.seq > last + 1:
            self.counts["gap"] += 1
            problem = "gap"
        self.last_seq[q.instrument] = q.seq
        skew_ms = (q.ts_exch_ns - q.ts_recv_ns) / 1e6
        lag_ms = -skew_ms
        bad = ("halted" if q.halted else
               "invalid" if (q.bid is None or q.ask is None or not math.isfinite(q.bid) or not math.isfinite(q.ask)
                             or q.bid <= 0 or q.ask < q.bid) else
               "skew" if skew_ms > self.max_future_skew_ms else
               "lag" if lag_ms > self.max_feed_lag_ms else "")
        if bad:
            self.counts[bad] += 1
            self.clean_run[q.instrument] = 0
            return False, bad
        self.clean_run[q.instrument] = 0 if problem else self.clean_run.get(q.instrument, self.clean_after) + 1
        return True, problem

    def blocked(self, instrument: str) -> bool:
        return self.clean_run.get(instrument, 0) < self.clean_after
