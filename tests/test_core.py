"""Author tests for order/risk invariants. Independent QA tests live in tests/qa/."""
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trader.broker import Faults, PaperBroker
from trader.cli import build
from trader.control import Controller
from trader.model import Book, Execution, OrderState, Quote, Side
from trader.oms import OMS
from trader.portfolio import FxTable, pnl
from trader.risk import Limits
from trader.store import Store

IST = ZoneInfo("Asia/Kolkata")
NS = 1_000_000_000


def ts(h, m, s=0, day=2):
    return int(datetime(2026, 11, day, h, m, s, tzinfo=IST).timestamp() * NS)


def quote(inst="XNSE:TCS", seq=1, t=None, bid=3000.0, ask=3000.1, qty=1000, **kw):
    t = t or ts(10, 0)
    return Quote(inst, seq, t, t + 1_000_000, bid, ask, qty, qty, (bid + ask) / 2, 0, **kw)


def order_args(sig="s1", side=Side.BUY, qty=10, px=3000.1, book=Book.INTRADAY, ro=False):
    return (sig, book, "XNSE:TCS", side, qty, px, ro)


# ---------- order safety ----------
def test_timeout_never_creates_duplicate_economic_order():
    store, br = Store(), PaperBroker("a", Faults(timeout_after_accept={1}))
    oms = OMS(store, br, "a")
    o = oms.place(*order_args())
    assert o.state == OrderState.UNKNOWN
    again = oms.place(*order_args())                      # same signal re-decided after restart
    assert again.client_id == o.client_id and br.n_submit == 1
    assert oms.reconcile() == [] and store.order(o.client_id).state == OrderState.ACKED


def test_timeout_before_accept_resolves_to_rejected_not_resent():
    store, br = Store(), PaperBroker("a", Faults(timeout_before_accept={1}))
    oms = OMS(store, br, "a")
    o = oms.place(*order_args())
    oms.reconcile()
    assert store.order(o.client_id).state == OrderState.REJECTED and br.n_submit == 1


def test_duplicate_fill_callbacks_counted_once_and_partial_then_cancel_race():
    store, br = Store(), PaperBroker("a", Faults(duplicate_fills=True))
    oms = OMS(store, br, "a")
    o = oms.place(*order_args(qty=10))
    br.on_quote(quote(qty=4))                             # partial 4
    oms.cancel(o.client_id)                               # cancel races next fill
    br.on_quote(quote(seq=2, qty=3))                      # fill 3 arrives before cancel takes effect
    oms.pump()
    got = store.order(o.client_id)
    assert got.filled_qty == 7 and got.state == OrderState.CANCELLED
    assert store.positions() == {("a", "intraday", "XNSE:TCS"): 7}
    assert oms.reconcile() == []


def test_restart_recovers_orders_and_positions(tmp_path):
    db = tmp_path / "s.db"
    br = PaperBroker("a")
    oms = OMS(Store(db), br, "a")
    o = oms.place(*order_args(qty=10))
    br.on_quote(quote(qty=6))                             # fill happens while we "crash" (events lost)
    br.drop_events()
    store2 = Store(db)                                    # restart from disk
    oms2 = OMS(store2, br, "a")
    assert oms2.reconcile() == []
    assert store2.order(o.client_id).filled_qty == 6 and store2.positions()[("a", "intraday", "XNSE:TCS")] == 6


def test_append_only_tables_reject_update():
    s = Store()
    s.audit("x", "y", {})
    with pytest.raises(Exception, match="append-only"):
        s.db.execute("UPDATE audit SET actor='z'")


# ---------- risk ----------
def eng(**faults):
    return build(":memory:", faults=Faults(**faults) if faults else None)


def place_via_engine(e, side=Side.BUY, qty=10, book=Book.INTRADAY, px=3000.1, q=None):
    from trader.model import Action, Signal
    q = q or quote()
    e.quality.check(q)                                    # real path: every quote passes the gate first
    e.now_ns = q.ts_exch_ns
    e.last[q.instrument] = q
    s = Signal("t", "1", q.instrument, book, Action.ENTER if side == Side.BUY else Action.EXIT, q.ts_exch_ns,
               "test", side=side, qty=qty, limit_price=px)
    e._act(s, q, 0)
    return e.store.order(e.oms.client_id(s.id))


def test_intraday_exit_cannot_close_swing_holding():
    e = eng()
    o = place_via_engine(e, book=Book.SWING, qty=10)
    e.broker.on_quote(quote(seq=2))
    e._pump()
    assert e.store.positions() == {("paper-in", "swing", "XNSE:TCS"): 10}
    assert place_via_engine(e, side=Side.SELL, qty=10, book=Book.INTRADAY, px=3000.0) is None
    assert o.state == OrderState.ACKED


def test_stale_quote_blocks_entry():
    e = eng()
    e.now_ns = ts(10, 0)
    old = quote(t=ts(10, 0) - 10 * NS)                    # 10 s old > max_quote_age_ms
    from trader.model import Action, Signal
    s = Signal("t", "1", old.instrument, Book.INTRADAY, Action.ENTER, ts(10, 0), "t", side=Side.BUY, qty=1,
               limit_price=3000.1)
    e._act(s, old, 0)
    assert e.store.order(e.oms.client_id(s.id)) is None


def test_risk_lock_still_allows_reduction():
    e = eng()
    place_via_engine(e, qty=10)
    e.broker.on_quote(quote(seq=2))
    e._pump()
    e.kill_level = 1
    assert place_via_engine(e, qty=1, q=quote(seq=3)) is None                        # entry blocked
    sold = place_via_engine(e, side=Side.SELL, qty=10, px=3000.0, q=quote(seq=4, t=ts(10, 1)))
    assert sold is not None and sold.reduce_only


def test_outside_hours_and_holiday_blocked():
    e = eng()
    assert place_via_engine(e, q=quote(t=ts(8, 0))) is None
    assert place_via_engine(e, q=quote(t=ts(10, 0, day=10))) is None                # 2026-11-10 NSE holiday


def test_global_daily_loss_not_reset_by_other_market():
    e = eng()
    e.day_start, e.day = 100_000.0, datetime.fromtimestamp(ts(10, 0) / NS, IST).date().isoformat()
    e.initial = 100_000.0 - 3_000                         # already lost the daily budget (in IST day)
    assert place_via_engine(e) is None


def test_settled_cash_limit():
    e = eng()
    assert place_via_engine(e, qty=40) is None            # 40 * 3000 > 100k settled cash (and position limit)


def test_limits_validation_rejects_bad_config(tmp_path):
    p = tmp_path / "r.json"
    p.write_text('{"version": "x"}')
    with pytest.raises(ValueError):
        Limits.from_file(p)


# ---------- control ----------
def test_unauthorized_cannot_change_limits_or_kill(tmp_path):
    e = eng()
    c = Controller(e, hashlib.sha256(b"right").hexdigest())
    with pytest.raises(PermissionError):
        c.set_limits("wrong", "config/risk.example.json", "mallory")
    with pytest.raises(PermissionError):
        c.kill("wrong", 3, "mallory")
    assert e.kill_level == 0 and any("DENIED" in r[1] for r in e.store.audit_rows())
    with pytest.raises(PermissionError):                  # no token configured -> everything denied
        Controller(e, "").kill("", 1, "x")


def test_kill_flatten_cancels_and_sells():
    e = eng()
    place_via_engine(e, qty=10)
    e.broker.on_quote(quote(seq=2))
    e.quality.check(quote(seq=2))
    e._pump()
    place_via_engine(e, qty=5, px=2999.0, q=quote(seq=3))          # resting buy, will be cancelled
    c = Controller(e, hashlib.sha256(b"t").hexdigest())
    r = c.kill("t", 3, "ops")
    assert r["cancel_requested"] == 1 and "SUBMITTING" not in str(r)
    assert any("ACKED" in x for x in r["flatten"])


# ---------- P&L ----------
def test_fx_split_trading_vs_currency():
    fx = FxTable("INR")
    fx.add("USD", 0, 80.0)
    fx.add("USD", 10, 84.0)
    fills = [("a", "swing", "X", "BUY", 10, 100.0, 0.0, 1), ("a", "swing", "X", "SELL", 10, 110.0, 0.0, 11)]
    r = pnl(fills, {"X": "USD"}, {}, fx, 12)
    assert r["trading"] == pytest.approx(10 * 10 * 80) and r["fx"] == pytest.approx(10 * 110 * 4)
    assert r["total"] == pytest.approx(10 * (110 * 84 - 100 * 80))


def test_fx_no_lookahead():
    fx = FxTable("INR")
    fx.add("USD", 100, 84.0)
    with pytest.raises(KeyError):
        fx.at("USD", 50)


def test_overfill_rejected():
    s = Store()
    oms = OMS(s, PaperBroker("a"), "a")
    o = oms.place(*order_args(qty=5))
    with pytest.raises(ValueError):
        s.add_execution(Execution("x1", o.client_id, 6, 1.0, 0))


# ---------- regressions from independent review ----------
def test_second_sell_with_exit_working_is_not_reduce_only():
    e = eng()
    place_via_engine(e, qty=10)
    e.broker.on_quote(quote(seq=2))
    e.quality.check(quote(seq=2))
    e._pump()
    first = place_via_engine(e, side=Side.SELL, qty=10, px=3005.0, q=quote(seq=3))     # resting exit
    assert first.reduce_only and first.state == OrderState.ACKED
    e.kill_level = 3
    assert place_via_engine(e, side=Side.SELL, qty=10, px=3000.0, q=quote(seq=4)) is None   # would go short
    assert any("already working" in x for x in e.flatten("ops"))


def test_future_timestamp_quote_does_not_advance_clock():
    e = eng()
    e.on_quote(quote(seq=1))
    t = e.now_ns
    e.on_quote(quote(seq=2, t=ts(10, 0, day=3)).__class__(**{**quote(seq=2).__dict__,
                                                              "ts_exch_ns": ts(10, 0, day=3)}))
    assert e.now_ns == t and e.quality.counts["skew"] == 1


def test_restart_restores_daily_loss_baseline(tmp_path):
    db = str(tmp_path / "s.db")
    e1 = build(db)
    e1.on_quote(quote(seq=1))
    start = e1.day_start
    e1.store.audit("risk", "day_start", {"day": e1.day, "equity": start + 5000})   # baseline set earlier today
    e2 = build(db)
    assert e2.day == e1.day and e2.day_start == start + 5000
    e2.on_quote(quote(seq=2))
    assert e2.day_start == start + 5000                    # not reset to current equity


def test_nan_quote_blocked_and_rejected_on_parse():
    import math
    e = eng()
    use, why = e.quality.check(quote(bid=math.nan, ask=math.nan))
    assert not use and why == "invalid"
    with pytest.raises(ValueError):
        Quote.from_json('{"instrument":"X","seq":1,"ts_exch_ns":1,"ts_recv_ns":1,"bid":NaN,"ask":1,'
                        '"bid_qty":1,"ask_qty":1,"last":1,"cum_volume":0,"halted":false}')


def test_limits_reject_nonfinite(tmp_path):
    import json as j
    d = j.load(open("config/risk.example.json"))
    p = tmp_path / "r.json"
    p.write_text(j.dumps({**d, "max_daily_loss": float("inf")}))
    with pytest.raises(ValueError):
        Limits.from_file(p)


def test_kill_survives_restart_and_lockout(tmp_path):
    db = str(tmp_path / "s.db")
    e = build(db)
    c = Controller(e, hashlib.sha256(b"t").hexdigest())
    for _ in range(5):
        with pytest.raises(PermissionError):
            c.kill("bad", 1, "x")
    c.kill("t", 1, "ops")                                 # valid token still halts during lockout
    assert build(db).kill_level == 1
