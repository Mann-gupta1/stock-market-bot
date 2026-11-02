"""Sessions (DST, lunch break, early close, holidays), FX point-in-time, bounded queue, ORB vs early close."""
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from trader import cli
from trader.model import Quote
from trader.portfolio import FxTable, pnl
from trader.strategy_orb import OrbParams, OrbStrategy

from .helpers import NS, TCS, Feed, make_engine, ts

_, _, SESS, *_ = cli.load_config()


def utc(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * NS)


@pytest.mark.parametrize("ex,when,is_open", [
    ("XLON", utc(2026, 3, 27, 7, 30), False),     # GMT: 07:30 local
    ("XLON", utc(2026, 3, 30, 7, 30), True),      # BST: 08:30 local
    ("XLON", utc(2026, 3, 30, 15, 29), True),     # 16:29 BST
    ("XLON", utc(2026, 3, 30, 15, 30), False),    # 16:30 BST = close
    ("XLON", utc(2026, 10, 26, 7, 30), False),    # back to GMT
    ("XLON", utc(2026, 10, 26, 16, 29), True),
    ("XNYS", utc(2026, 3, 6, 13, 45), False),     # EST: 08:45
    ("XNYS", utc(2026, 3, 9, 13, 30), True),      # EDT: 09:30 open (inclusive)
    ("XNYS", utc(2026, 3, 16, 13, 30), True),     # US/UK DST mismatch week
    ("XLON", utc(2026, 3, 16, 7, 59), False),
    ("XNYS", utc(2026, 11, 2, 13, 45), False),    # EST again: 08:45
    ("XNYS", utc(2026, 11, 2, 20, 59), True),     # 15:59 EST
    ("XNYS", utc(2026, 11, 2, 21, 0), False),     # 16:00 = close (exclusive)
    ("XNYS", utc(2026, 11, 7, 15, 0), False),     # Saturday
    ("XJPX", utc(2026, 9, 1, 2, 29), True),       # 11:29 JST
    ("XJPX", utc(2026, 9, 1, 2, 30), False),      # 11:30 lunch
    ("XJPX", utc(2026, 9, 1, 3, 29, 59), False),  # 12:29:59 lunch
    ("XJPX", utc(2026, 9, 1, 3, 30), True),       # 12:30 afternoon
    ("XJPX", utc(2026, 9, 1, 6, 25), False),      # 15:25 close
    ("XNSE", ts(9, 15, day=(2026, 11, 10)), False),   # configured holiday
    ("XNSE", ts(9, 14, 59, day=(2026, 11, 9)), False),
    ("XNSE", ts(15, 14, 59, day=(2026, 11, 9)), True),
])
def test_session_is_open(ex, when, is_open):
    assert SESS[ex].is_open(when) is is_open


def test_early_close():
    s = replace(SESS["XNYS"], early_closes={"2026-11-27": "13:00"})
    ny = ZoneInfo("America/New_York")
    at = lambda d, h, m: int(datetime(2026, 11, d, h, m, tzinfo=ny).timestamp() * NS)
    assert s.is_open(at(27, 12, 59)) and not s.is_open(at(27, 13, 0))
    assert s.is_open(at(30, 15, 59))                              # next session normal


def test_fx_point_in_time_no_lookahead():
    fx = FxTable("INR")
    fx.add("USD", 100, 84.0)
    fx.add("USD", 50, 83.0)                                       # inserted out of order
    fx.add("USD", 200, 85.0)
    with pytest.raises(KeyError):
        fx.at("USD", 49)
    assert [fx.at("USD", t) for t in (50, 99, 100, 199, 10**18)] == [83.0, 83.0, 84.0, 84.0, 85.0]
    assert fx.at("INR", 0) == 1.0
    with pytest.raises(ValueError):
        fx.add("USD", 300, 0.0)


def test_pnl_split_uses_rate_at_fill_and_mark_time():
    fx = FxTable("INR")
    fx.add("USD", 1, 80.0)
    fx.add("USD", 5, 90.0)                                        # known only after the fill
    fills = [("A", "swing", "X", "BUY", 10, 100.0, 1.0, 2)]
    early = pnl(fills, {"X": "USD"}, {"X": 110.0}, fx, 4)
    assert early["fx"] == 0 and early["trading"] == pytest.approx(8000) and early["fees"] == pytest.approx(-80)
    late = pnl(fills, {"X": "USD"}, {"X": 110.0}, fx, 10)
    assert late["trading"] == pytest.approx(8000) and late["fx"] == pytest.approx(11000)
    assert late["total"] == pytest.approx(10 * (110 * 90 - 100 * 80) - 80)


def test_bounded_queue_drops_oldest_and_counts():
    e, f = make_engine(queue_max=3), Feed()
    qs = [f.q(TCS, ts(10, 0, i), 3000.0, 3000.05) for i in range(5)]
    for q in qs:
        e.ingest(q)
    assert e.dropped == 2 and [q.seq for q in e.queue] == [3, 4, 5]
    e.run_queue()
    assert e.last[TCS].seq == 5 and not e.queue


def test_orb_flattens_before_early_close():
    reg, ex, *_ = cli.load_config()
    early = "2026-09-01"                                          # pretend NSE closes 13:00 today
    s = OrbStrategy(reg, {k: v["tz"] for k, v in ex.items()}, {k: (v["open"], v["close"], {early: "13:00"}) for k, v in ex.items()},
                    {}, OrbParams())
    f = Feed()
    s.on_quote(f.q(TCS, ts(12, 0, day=(2026, 9, 1)), 3000.0, 3000.05))
    s.on_fill(TCS, 5, 1.0, 1e9, ts(15, 0))
    out = s.on_quote(f.q(TCS, ts(12, 55, day=(2026, 9, 1)), 3000.0, 3000.05))
    assert early and out and out[0].reason == "session close"
