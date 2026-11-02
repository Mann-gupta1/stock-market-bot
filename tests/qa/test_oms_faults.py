"""Items 4, 6, 8: submission timeouts, restart recovery with lost callbacks, rate-limit back-off."""
import pytest

from trader import cli
from trader.broker import Faults, PaperBroker
from trader.model import Book, OrderState, Side
from trader.strategy_orb import OrbParams, OrbStrategy

from .helpers import NS, TCS, Feed, act, make_engine, sig, ts

T0 = ts(10, 0)


def _setup(state=":memory:", **faults):
    broker = PaperBroker("paper-in", Faults(**faults))
    e, f = make_engine(state, broker=broker), Feed()
    e.on_quote(f.q(TCS, T0, 3000.0, 3000.05))
    return e, f, broker


@pytest.mark.parametrize("fault,broker_has", [("timeout_after_accept", 1), ("timeout_before_accept", 0)])
def test_timeout_never_duplicates_economic_order(tmp_path, fault, broker_has):
    db = tmp_path / "s.db"
    e, f, broker = _setup(db, **{fault: {1}})
    s = sig(TCS, Side.BUY, 5, 3000.05, T0)
    act(e, s)
    cid = e.oms.client_id(s.id)
    assert e.store.order(cid).state == OrderState.UNKNOWN
    assert "UNKNOWN state: reconcile first" in act(e, sig(TCS, Side.BUY, 5, 3000.05, T0 + 1))   # other signal blocked
    act(e, s)                                                    # same signal re-decided before restart
    # restart: new process, same durable store, same (external) broker
    e2 = make_engine(db, broker=broker)
    assert e2.oms.reconcile() == []
    assert e2.store.order(cid).state == (OrderState.ACKED if broker_has else OrderState.REJECTED)
    e2.on_quote(Feed().q(TCS, T0 + NS, 3000.0, 3000.05, seq=2, aq=0))
    act(e2, s)                                                   # restart + re-decide same signal
    assert len(broker._orders) == broker_has
    assert broker.n_submit == 1                                  # never re-sent, before or after restart
    assert len(e2.store.orders()) == 1


def test_restart_recovers_lost_fills_and_cancels(tmp_path):
    db = tmp_path / "s.db"
    e, f, broker = _setup(db)
    a = sig(TCS, Side.BUY, 12, 3000.05, T0)
    b = sig(TCS, Side.BUY, 5, 2995.0, T0, book=Book.SWING)
    c = sig(TCS, Side.BUY, 4, 2994.0, T0)
    for s in (a, b, c):
        assert "|True|" in act(e, s)
    ca, cb, cc = (e.oms.client_id(s.id) for s in (a, b, c))
    e.oms.cancel(cc)
    broker.on_quote(f.q(TCS, T0 + NS, 3000.0, 3000.05, aq=6))   # partial fill A, cancel C
    broker.drop_events()                                         # callbacks lost in disconnect; then crash
    del e
    e2 = make_engine(db, broker=broker)
    pre = {o.client_id: o.state for o in e2.store.orders()}
    assert pre == {ca: OrderState.ACKED, cb: OrderState.ACKED, cc: OrderState.CANCEL_REQUESTED}
    assert e2.oms.reconcile() == []
    st = {o.client_id: (o.state, o.filled_qty) for o in e2.store.orders()}
    assert st == {ca: (OrderState.PARTIAL, 6), cb: (OrderState.ACKED, 0), cc: (OrderState.CANCELLED, 0)}
    assert e2.store.positions() == {("paper-in", "intraday", TCS): 6}
    assert broker.positions() == {("paper-in", TCS): 6}
    # remaining live order keeps working after restart and fills exactly to qty
    e2.on_quote(Feed().q(TCS, T0 + 2 * NS, 3000.0, 3000.05, seq=3, aq=1000))
    assert e2.store.order(ca).filled_qty == 12 and e2.store.order(ca).state == OrderState.FILLED
    assert e2.oms.reconcile() == []


def test_rate_limit_backs_off_entries():
    e, f, broker = _setup(rate_limit={1})
    s1 = sig(TCS, Side.BUY, 1, 3000.05, T0)
    act(e, s1)
    assert e.store.order(e.oms.client_id(s1.id)).state == OrderState.REJECTED and broker.n_submit == 1
    for k in range(1, 5):
        e.on_quote(f.q(TCS, T0 + k * NS, 3000.0, 3000.05, aq=0))
        assert "entries paused" in act(e, sig(TCS, Side.BUY, 1, 3000.05, T0 + k * NS))
    assert broker.n_submit == 1                                  # no retry storm
    e.on_quote(f.q(TCS, T0 + 6 * NS, 3000.0, 3000.05, aq=0))
    assert "|True|" in act(e, sig(TCS, Side.BUY, 1, 3000.05, T0 + 6 * NS))
    assert broker.n_submit == 2


def test_rate_limited_exit_rearm_is_bounded():
    reg, ex, *_ = cli.load_config()
    strat = OrbStrategy(reg, {k: v["tz"] for k, v in ex.items()}, {k: (v["open"], v["close"]) for k, v in ex.items()},
                        {}, OrbParams(risk_per_trade=500))
    broker = PaperBroker("paper-in")
    e, f = make_engine(broker=broker, strategy=strat), Feed()
    e.on_quote(f.q(TCS, T0, 3000.0, 3000.05))
    act(e, sig(TCS, Side.BUY, 5, 3000.05, T0))                   # stub-owned entry: strategy not told
    broker.with_faults(rate_limit=set(range(2, 100_000)))        # every further submit is 429
    for k in range(1, 121):                                      # 12 s of quotes at 10/s
        e.on_quote(f.q(TCS, T0 + k * NS // 10, 3000.0, 3000.05))
    assert e.store.positions() == {("paper-in", "intraday", TCS): 5}
    assert 2 <= broker.n_submit <= 4                             # exits retried ~every 5 s, not every quote
