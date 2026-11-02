"""Item 5: partial fills, cancel/fill races, duplicate callbacks; hypothesis state-machine style property test."""
from hypothesis import HealthCheck, given, settings, strategies as st

from trader.broker import Faults, PaperBroker
from trader.model import OPEN, Book, Execution, OrderState, Quote, Side
from trader.oms import OMS
from trader.store import Store

I = "XNSE:TCS"


def rig(**faults):
    s, b = Store(), PaperBroker("A", Faults(**faults))
    return s, b, OMS(s, b, "A")


class Tape:
    def __init__(self):
        self.seq = 0

    def __call__(self, b, ask, aq, bid=None, bq=1000):
        self.seq += 1
        b.on_quote(Quote(I, self.seq, self.seq * 10**9, self.seq * 10**9 + 1, bid if bid else ask - 1, ask, bq, aq,
                         None, 0))


def test_partial_fills_accumulate_with_vwap():
    s, b, o = rig()
    t = Tape()
    od = o.place("sig1", Book.INTRADAY, I, Side.BUY, 10, 100.0, False)
    t(b, 99.0, 3); t(b, 100.0, 4); o.pump()
    od = s.order(od.client_id)
    assert (od.state, od.filled_qty) == (OrderState.PARTIAL, 7)
    assert abs(od.avg_price - (3 * 99 + 4 * 100) / 7) < 1e-9
    t(b, 98.0, 50); o.pump()
    assert s.order(od.client_id).state == OrderState.FILLED and s.positions() == {("A", "intraday", I): 10}


def test_duplicate_fill_callbacks_idempotent():
    s, b, o = rig(duplicate_fills=True)
    t = Tape()
    od = o.place("sig1", Book.INTRADAY, I, Side.BUY, 10, 100.0, False)
    t(b, 100.0, 4)
    evs = b.poll()
    assert len(evs) == 2
    for ev in evs + evs:
        o.handle(ev)
    assert s.order(od.client_id).filled_qty == 4
    assert s.add_execution(evs[0].execution) is False


def test_cancel_racing_partial_fill_ends_cancelled_with_fill():
    s, b, o = rig()
    t = Tape()
    od = o.place("sig1", Book.INTRADAY, I, Side.BUY, 10, 100.0, False)
    o.cancel(od.client_id)
    t(b, 100.0, 4); o.pump()                       # fill matched before cancel takes effect
    od = s.order(od.client_id)
    assert (od.state, od.filled_qty) == (OrderState.CANCELLED, 4)
    assert s.positions() == {("A", "intraday", I): 4} and o.reconcile() == []


def test_cancel_racing_full_fill_ends_filled():
    s, b, o = rig()
    t = Tape()
    od = o.place("sig1", Book.INTRADAY, I, Side.BUY, 10, 100.0, False)
    o.cancel(od.client_id)
    t(b, 100.0, 50); o.pump()
    assert s.order(od.client_id).state == OrderState.FILLED and o.reconcile() == []


def test_late_fill_after_cancelled_is_recorded():
    s, b, o = rig()
    od = o.place("sig1", Book.INTRADAY, I, Side.BUY, 10, 100.0, False)
    s.set_state(od.client_id, OrderState.CANCELLED)
    assert s.add_execution(Execution("x1", od.client_id, 3, 100.0, 1))
    od = s.order(od.client_id)
    assert (od.state, od.filled_qty) == (OrderState.CANCELLED, 3)


def test_overfill_rejected_atomically():
    s, b, o = rig()
    od = o.place("sig1", Book.INTRADAY, I, Side.BUY, 5, 100.0, False)
    s.add_execution(Execution("x1", od.client_id, 4, 100.0, 1))
    try:
        s.add_execution(Execution("x2", od.client_id, 2, 100.0, 2))
        raise AssertionError("overfill accepted")
    except ValueError:
        pass
    assert s.order(od.client_id).filled_qty == 4 and len(s.fills()) == 1


ACTIONS = st.lists(st.one_of(
    st.tuples(st.just("buy"), st.integers(1, 40), st.sampled_from([99.0, 100.0, 101.0]), st.sampled_from(list(Book))),
    st.tuples(st.just("sell"), st.integers(1, 40), st.sampled_from([98.0, 99.0, 100.0]), st.sampled_from(list(Book))),
    st.tuples(st.just("quote"), st.sampled_from([98.0, 99.0, 100.0, 101.0, 102.0]), st.integers(0, 25),
              st.integers(0, 25)),
    st.tuples(st.just("cancel"), st.integers(0, 30)),
    st.tuples(st.just("pump")), st.tuples(st.just("drop")), st.tuples(st.just("dup")),
    st.tuples(st.just("reconcile")),
), min_size=5, max_size=80)


@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ACTIONS)
def test_random_order_flow_invariants(actions):
    s, b, o = rig()
    t, n = Tape(), 0
    for a in actions:
        kind = a[0]
        if kind == "buy":
            n += 1
            o.place(f"s{n}", a[3], I, Side.BUY, a[1], a[2], False)
        elif kind == "sell":                         # only what risk would allow: held - working sells, per book
            held = s.positions().get(("A", a[3].value, I), 0)
            working = sum(x.qty - x.filled_qty for x in s.orders(OPEN) if x.side == Side.SELL and x.book == a[3])
            q = min(a[1], held - working)
            if q > 0:
                n += 1
                o.place(f"s{n}", a[3], I, Side.SELL, q, a[2], True)
        elif kind == "quote":
            t(b, a[1], a[2], bid=a[1] - 1, bq=a[3])
        elif kind == "cancel":
            ords = s.orders()
            if ords:
                o.cancel(ords[a[1] % len(ords)].client_id)
        elif kind == "pump":
            o.pump()
        elif kind == "drop":
            b.drop_events()
        elif kind == "dup":
            b.with_faults(duplicate_fills=not b.f.duplicate_fills)
        else:
            o.reconcile()
        for x in s.orders():
            assert 0 <= x.filled_qty <= x.qty
            assert (x.state == OrderState.FILLED) == (x.filled_qty == x.qty) or x.state in (
                OrderState.CANCEL_REQUESTED,)                       # FILLED iff complete
        assert all(v > 0 for v in s.positions().values()), s.positions()
    o.pump()
    assert o.reconcile() == []
    for x in s.orders():
        bo = b.order_status(x.client_id)
        assert bo is not None and bo.filled_qty == x.filled_qty
        if bo.state in ("FILLED", "CANCELLED"):
            assert x.state.value == bo.state, (x, bo)
    mine = {}
    for (acct, _bk, inst), q in s.positions().items():
        mine[(acct, inst)] = mine.get((acct, inst), 0) + q
    assert mine == b.positions()
