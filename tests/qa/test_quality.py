"""Item 2 + 7: data-quality gate, clock skew, out-of-order, duplicates."""
import math

import pytest

from trader.model import OrderState, Quote, Side
from trader.quality import QualityGate

from .helpers import NS, TCS, Feed, act, make_engine, sig, ts


def Q(seq, bid=100.0, ask=100.05, t=1_000 * NS, lag_ms=5, halted=False, inst="X"):
    return Quote(inst, seq, t, t + int(lag_ms * 1e6), bid, ask, 10, 10, None, 0, halted)


@pytest.mark.parametrize("bad,kind", [
    (dict(bid=100.1, ask=100.0), "invalid"),        # crossed
    (dict(bid=None), "invalid"),
    (dict(ask=None), "invalid"),
    (dict(bid=0.0), "invalid"),
    (dict(bid=-1.0), "invalid"),
    (dict(halted=True), "halted"),
    (dict(lag_ms=2_500), "lag"),
    (dict(lag_ms=-800), "skew"),                    # exchange ts 800ms ahead of local receive
])
def test_bad_quote_dropped_and_blocks(bad, kind):
    g = QualityGate()
    assert g.check(Q(1)) == (True, "") and not g.blocked("X")
    use, why = g.check(Q(2, **bad))
    assert (use, why) == (False, kind) and g.blocked("X") and g.counts[kind] == 1
    for s in range(3, 3 + g.clean_after - 1):
        assert g.check(Q(s))[0] and g.blocked("X")
    g.check(Q(3 + g.clean_after - 1))
    assert not g.blocked("X")


def test_gap_fed_but_blocks_until_clean_run():
    g = QualityGate()
    g.check(Q(1))
    assert g.check(Q(5)) == (True, "gap") and g.blocked("X")
    for s in range(6, 6 + g.clean_after):
        assert g.blocked("X")
        g.check(Q(s))
    assert not g.blocked("X")


def test_gap_inside_clean_run_restarts_count():
    g = QualityGate()
    g.check(Q(1)); g.check(Q(3))
    for s in (4, 5, 6):
        g.check(Q(s))
    g.check(Q(9))                                   # second gap
    for s in (10, 11, 12, 13):
        g.check(Q(s))
    assert g.blocked("X")                           # only 4 clean since last gap


def test_duplicates_and_out_of_order_dropped_and_counted():
    g = QualityGate()
    for s in (1, 2, 3):
        g.check(Q(s))
    assert g.check(Q(3)) == (False, "duplicate")
    assert g.check(Q(2)) == (False, "out_of_order")
    assert g.counts["duplicate"] == 1 and g.counts["out_of_order"] == 1
    assert g.last_seq["X"] == 3 and not g.blocked("X")   # dropping doesn't block


def test_seq_is_per_instrument():
    g = QualityGate()
    g.check(Q(1, inst="A")); g.check(Q(1, inst="B")); g.check(Q(2, inst="A"))
    assert g.counts["duplicate"] == 0 and g.counts["gap"] == 0


def test_nan_quote_rejected():
    g = QualityGate()
    g.check(Q(1))
    assert g.check(Q(2, bid=math.nan))[0] is False


# ---- engine level ----
def _engine_with_quote():
    e, f = make_engine(), Feed()
    t = ts(10, 0)
    e.on_quote(f.q(TCS, t, 3000.0, 3000.05))
    return e, f, t


@pytest.mark.parametrize("bad", [dict(bid=3000.1, ask=3000.0), dict(halted=True), dict(bid=None),
                                 dict(lag_ms=3000), dict(lag_ms=-1000)])
def test_engine_bad_quote_blocks_new_exposure(bad):
    e, f, t = _engine_with_quote()
    kw = dict(bid=3000.0, ask=3000.05) | bad
    e.on_quote(f.q(TCS, t + NS, kw.pop("bid"), kw.pop("ask"), **kw))
    d = act(e, sig(TCS, Side.BUY, 1, 3000.05, t + NS))
    assert "|False|" in d and "data quality block" in d
    assert e.store.orders() == []


def test_engine_gap_blocks_then_clears():
    e, f, t = _engine_with_quote()
    e.on_quote(f.q(TCS, t + NS, 3000.0, 3000.05, seq=5))        # gap
    assert "data quality block" in act(e, sig(TCS, Side.BUY, 1, 3000.05, t + NS))
    for i in range(2, 2 + e.quality.clean_after):
        e.on_quote(f.q(TCS, t + i * NS, 3000.0, 3000.05))
    d = act(e, sig(TCS, Side.BUY, 1, 3000.05, t + 7 * NS))
    assert "|True|" in d


def test_engine_drops_duplicate_quote_from_strategy():
    seen = []
    e, f, t = _engine_with_quote()
    e.strategy.on_quote = lambda q: seen.append(q.seq) or []
    good = f.q(TCS, t + NS, 3000.0, 3000.05)
    e.on_quote(good); e.on_quote(good); e.on_quote(f.q(TCS, t, 2999.0, 2999.05, seq=1))
    assert seen == [good.seq]
    assert e.last[TCS] == good


def test_skewed_quote_does_not_advance_engine_clock():
    e, f, t = _engine_with_quote()
    act(e, sig(TCS, Side.BUY, 1, 2990.0, t, expiry=t + 60 * NS))      # resting, won't fill
    skew = f.q(TCS, t + 3600 * NS, 3000.0, 3000.05, lag_ms=-3600_000)  # exch ts 1h ahead of recv
    e.on_quote(skew)
    assert e.quality.counts["skew"] == 1
    assert e.now_ns < t + 3600 * NS
    e.on_quote(f.q(TCS, t + 2 * NS, 3000.0, 3000.05))
    assert e.store.orders()[0].state == OrderState.ACKED          # not cancelled as "expired"


def test_quote_older_than_max_quote_age_blocks_entry():
    e, f, t = _engine_with_quote()
    e.on_quote(f.q(TCS, t + NS, 3000.0, 3000.05, lag_ms=1800))
    assert "|False|" in act(e, sig(TCS, Side.BUY, 1, 3000.05, t + NS))


def test_duplicate_quote_does_not_double_fill_at_paper_broker():
    e, f, t = _engine_with_quote()
    act(e, sig(TCS, Side.BUY, 15, 3000.05, t))
    q = f.q(TCS, t + NS, 3000.0, 3000.05, aq=10)
    e.on_quote(q); e.on_quote(q)
    assert e.broker.order_status(e.store.orders()[0].client_id).filled_qty == 10
    assert e.oms.reconcile() == []


def test_nan_quote_never_crashes_engine():
    e, f, t = _engine_with_quote()
    e.on_quote(f.q(TCS, t + NS, 3000.0, math.nan))
    d = act(e, sig(TCS, Side.BUY, 1, e.last[TCS].ask, t + NS))
    assert "|False|" in d
