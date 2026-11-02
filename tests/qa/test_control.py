"""Items 11, 12: kill switch levels (control.py docstring), auth, lockout, limit changes."""
import hashlib
import json

import pytest

from trader import control
from trader.control import Controller
from trader.model import Book, OrderState, Side

from .helpers import INFY, NS, TCS, Feed, act, make_engine, sig, ts

TOKEN = "s3cret-token"
SHA = hashlib.sha256(TOKEN.encode()).hexdigest()
T0 = ts(10, 0)


class Rig:
    def __init__(self, state=":memory:", broker=None):
        self.e, self.f, self.t = make_engine(state, broker=broker), Feed(), T0
        self.c = Controller(self.e, SHA)
        self.tick()

    def tick(self, aq=1000, bq=1000, insts=(TCS, INFY)):
        self.t += NS
        for i in insts:
            b, a = (3000.0, 3000.05) if i == TCS else (1500.0, 1500.05)
            self.e.on_quote(self.f.q(i, self.t, b, a, aq=aq, bq=bq))

    def buy(self, inst, qty, px, book=Book.INTRADAY):
        s = sig(inst, Side.BUY, qty, px, self.t, book=book)
        assert "|True|" in act(self.e, s)
        return self.e.oms.client_id(s.id)


def test_level1_halts_entries_only():
    r = Rig()
    r.buy(TCS, 5, 3000.05); r.tick()                         # long 5 intraday
    resting = r.buy(INFY, 5, 1495.0)
    rep = r.c.kill(TOKEN, 1, "ops")
    assert rep["level"] == "HALT_ENTRIES"
    assert r.e.store.order(resting).state == OrderState.ACKED          # working order untouched
    assert "kill switch level 1" in act(r.e, sig(INFY, Side.BUY, 1, 1500.05, r.t))
    assert "|True|" in act(r.e, sig(TCS, Side.SELL, 5, 3000.0, r.t))  # exits still allowed
    r.tick()
    assert ("paper-in", "intraday", TCS) not in r.e.store.positions()


def test_level2_cancels_and_keeps_racing_fill():
    r = Rig()
    a = r.buy(TCS, 10, 3000.05)                               # will race the cancel on next quote
    b = r.buy(INFY, 5, 1495.0)                                # rests, gets cancelled clean
    rep = r.c.kill(TOKEN, 2, "ops")
    assert rep["cancel_requested"] == 2
    assert {r.e.store.order(x).state for x in (a, b)} == {OrderState.CANCEL_REQUESTED}
    r.tick(aq=4)                                              # fill 4 matched before the cancel lands
    oa, ob = r.e.store.order(a), r.e.store.order(b)
    assert (oa.state, oa.filled_qty) == (OrderState.CANCELLED, 4)
    assert (ob.state, ob.filled_qty) == (OrderState.CANCELLED, 0)
    assert r.e.store.positions() == {("paper-in", "intraday", TCS): 4}
    assert r.e.oms.reconcile() == []


def test_level2_full_fill_race_ends_filled():
    r = Rig()
    a = r.buy(TCS, 10, 3000.05)
    r.c.kill(TOKEN, 2, "ops")
    r.tick()
    assert r.e.store.order(a).state == OrderState.FILLED and r.e.oms.reconcile() == []


def test_level3_flattens_every_book_and_reports_unquoted():
    r = Rig()
    r.buy(TCS, 5, 3000.05); r.buy(TCS, 4, 3000.05, book=Book.SWING); r.buy(INFY, 3, 1500.05); r.tick()
    entry = r.buy(INFY, 2, 1495.0)                            # working entry -> cancelled
    rep = r.c.kill(TOKEN, 3, "ops")
    assert rep["level"] == "FLATTEN" and len(rep["flatten"]) == 3
    assert all("ACKED" in x for x in rep["flatten"]), rep
    r.tick()
    assert r.e.store.positions() == {}
    assert r.e.store.order(entry).state == OrderState.CANCELLED
    assert r.e.oms.reconcile() == []
    assert r.c.kill(TOKEN, 1, "ops") and r.e.kill_level == 3   # lower level never downgrades


def test_level3_flattens_position_that_had_resting_exit():
    r = Rig()
    r.buy(TCS, 5, 3000.05); r.tick()
    assert "|True|" in act(r.e, sig(TCS, Side.SELL, 5, 3001.0, r.t))   # resting exit
    rep = r.c.kill(TOKEN, 3, "ops")
    # level 2 cancels the resting exit first; flatten must then re-sell, not double-sell
    r.tick(); r.tick()
    pos = r.e.store.positions().get(("paper-in", "intraday", TCS), 0)
    assert pos == 0, rep


def test_level3_without_fresh_bid_reports_manual_action():
    r = Rig()
    r.buy(TCS, 5, 3000.05); r.tick()
    r.e.last.pop(TCS)
    rep = r.c.kill(TOKEN, 3, "ops")
    assert any("NOT submitted" in x and "manual action" in x for x in rep["flatten"])


def test_kill_survives_restart(tmp_path):
    db = tmp_path / "s.db"
    r = Rig(db)
    r.c.kill(TOKEN, 1, "ops")
    e2 = make_engine(db, broker=r.e.broker)
    assert e2.kill_level == 1


# ---- auth ----
def test_wrong_token_changes_nothing_and_is_audited(tmp_path):
    r = Rig()
    lim = tmp_path / "l.json"
    lim.write_text(json.dumps({**r.e.risk.limits.__dict__, "max_order_qty": 999999}))
    for call in (lambda: r.c.kill("nope", 3, "eve"), lambda: r.c.set_limits("nope", str(lim), "eve"),
                 lambda: r.c.resume("nope", "eve")):
        with pytest.raises(PermissionError):
            call()
    assert r.e.kill_level == 0 and r.e.risk.limits.max_order_qty == 500
    rows = r.e.store.audit_rows()
    assert sum(1 for a, act_, _ in rows if a == "eve" and act_.startswith("DENIED")) == 3
    assert all(TOKEN not in str(x) and "nope" not in str(x) for x in rows)


def test_no_configured_token_denies_everyone(monkeypatch):
    monkeypatch.delenv("TRADER_ADMIN_TOKEN_SHA256", raising=False)
    c = Controller(make_engine())
    for tok in ("", TOKEN):
        with pytest.raises(PermissionError):
            c.kill(tok, 1, "x")


def test_lockout_after_five_failures(monkeypatch):
    r = Rig()
    now = [1000.0]
    monkeypatch.setattr(control.time, "monotonic", lambda: now[0])
    for _ in range(5):
        with pytest.raises(PermissionError):
            r.c.kill("bad", 1, "eve")
    with pytest.raises(PermissionError):
        r.c.resume(TOKEN, "ops")                              # non-safety action refused while locked
    assert r.c.kill(TOKEN, 1, "ops")["level"] == "HALT_ENTRIES"   # design: valid kill never blocked by lockout
    now[0] += 61
    r.c.resume(TOKEN, "ops")


def test_set_limits_authorized_validates_before_change(tmp_path):
    r = Rig()
    base = dict(r.e.risk.limits.__dict__)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**base, "max_drawdown": 1.5}))
    with pytest.raises(ValueError):
        r.c.set_limits(TOKEN, str(bad), "ops")
    good = tmp_path / "good.json"
    good.write_text(json.dumps({**base, "max_order_qty": 7}))
    h = r.c.set_limits(TOKEN, str(good), "ops")
    assert r.e.risk.limits.max_order_qty == 7 and r.e.risk.hash == h
    assert "max_order_qty" in act(r.e, sig(TCS, Side.BUY, 8, 3000.05, r.t))


@pytest.mark.parametrize("val", ["NaN", "Infinity"])
def test_set_limits_rejects_non_finite(tmp_path, val):
    r = Rig()
    p = tmp_path / "l.json"
    p.write_text(json.dumps(dict(r.e.risk.limits.__dict__)).replace('"max_daily_loss": 3000', f'"max_daily_loss": {val}'))
    assert val in p.read_text()
    with pytest.raises(ValueError):
        r.c.set_limits(TOKEN, str(p), "ops")


def test_valid_operator_retries_do_not_extend_lockout(monkeypatch):
    r = Rig()
    now = [1000.0]
    monkeypatch.setattr(control.time, "monotonic", lambda: now[0])
    for _ in range(5):
        with pytest.raises(PermissionError):
            r.c.kill("bad", 1, "eve")
    for _ in range(5):                                        # operator keeps trying the right token
        now[0] += 10
        with pytest.raises(PermissionError):
            r.c.resume(TOKEN, "ops")
    now[0] = 1000.0 + 61                                      # original lockout expired, not extended
    r.c.resume(TOKEN, "ops")
