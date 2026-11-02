"""Items 3, 9, 10: limits across books/strategies, currency/permission/settlement, book isolation."""
from dataclasses import replace

import pytest

from trader.model import Book, Side

from .helpers import INFY, NS, TCS, Feed, act, make_engine, sig, ts

T0 = ts(10, 0)
PX = {TCS: (3000.0, 3000.05), INFY: (1500.0, 1500.05)}


class Desk:
    """Engine + feed with a running clock; quotes both instruments every tick so the paper venue matches."""
    def __init__(self, state=":memory:", broker=None):
        self.e, self.f, self.t = make_engine(state, broker=broker), Feed(), T0
        self.tick()

    def tick(self, **px):
        self.t += NS
        for inst, (b, a) in (PX | px).items():
            self.e.on_quote(self.f.q(inst, self.t, b, a))

    def do(self, inst, side, qty, px=None, book=Book.INTRADAY):
        px = px or (PX[inst][1] if side == Side.BUY else PX[inst][0])
        return act(self.e, sig(inst, side, qty, px, self.t, book=book))

    def hold(self, inst, qty, book):
        assert "|True|" in self.do(inst, Side.BUY, qty, book=book)
        self.tick()

    def pos(self):
        return self.e.store.positions()


def test_position_limit_is_per_book_per_instrument():
    d = Desk()
    d.hold(TCS, 15, Book.INTRADAY)                                  # 45k
    assert "max_position_value" in d.do(TCS, Side.BUY, 2)            # 51k > 50k in same book
    assert "|True|" in d.do(TCS, Side.BUY, 2, book=Book.SWING)       # other book: own limit


def test_sector_limit_spans_books_and_instruments():
    d = Desk()
    d.hold(TCS, 15, Book.SWING)                                     # it: 45k
    assert "|True|" in d.do(INFY, Side.BUY, 19)                      # 45k + 28.5k < 75k
    d.tick()
    assert "max_sector_exposure (it)" in d.do(INFY, Side.BUY, 1, book=Book.SWING)


def test_gross_and_strategy_limits_across_books():
    d = Desk()
    L = d.e.risk.limits
    d.e.risk.limits = replace(L, max_gross_exposure=60_000, max_sector_exposure=1e9, max_strategy_exposure=40_000)
    d.hold(TCS, 13, Book.SWING)                                     # ~39k swing
    assert "max_strategy_exposure" in d.do(TCS, Side.BUY, 1, book=Book.SWING)
    assert "|True|" in d.do(INFY, Side.BUY, 13)                      # intraday 19.5k, gross ~58.5k
    d.tick()
    r = d.do(INFY, Side.BUY, 2)
    assert "max_gross_exposure" in r and "max_strategy_exposure" not in r


def test_working_orders_count_toward_sector_limit():
    d = Desk()
    assert "|True|" in d.do(TCS, Side.BUY, 15, px=2995.0)                       # resting intraday, 45k
    r = d.do(INFY, Side.BUY, 25, px=1500.0, book=Book.SWING)                     # swing 37.5k -> it 82.5k
    assert "max_sector_exposure" in r


def test_unmarked_position_still_counts_after_restart(tmp_path):
    db = tmp_path / "s.db"
    d = Desk(state=db)
    d.hold(TCS, 15, Book.SWING)
    e2 = make_engine(db, broker=d.e.broker)                          # restart; TCS not quoted yet
    f = Feed()
    f.seq = dict(d.f.seq)
    e2.on_quote(f.q(INFY, d.t + NS, *PX[INFY]))
    r = act(e2, sig(INFY, Side.BUY, 1, 1500.05, d.t + NS, book=Book.SWING))
    assert "|False|" in r and "no mark for held XNSE:TCS" in r


# ---- item 10: book isolation ----
def test_intraday_exit_cannot_sell_swing_holding():
    d = Desk()
    d.hold(TCS, 10, Book.SWING)
    r = d.do(TCS, Side.SELL, 10, book=Book.INTRADAY)
    assert "|False|" in r and "cross-book" in r
    assert "|True|" in d.do(TCS, Side.SELL, 10, book=Book.SWING)


def test_intraday_exit_larger_than_intraday_book_rejected():
    d = Desk()
    d.hold(TCS, 10, Book.SWING)
    d.hold(TCS, 5, Book.INTRADAY)
    assert "cross-book" in d.do(TCS, Side.SELL, 6)
    assert "|True|" in d.do(TCS, Side.SELL, 5)


def test_second_intraday_exit_cannot_eat_swing_holding():
    d = Desk()
    d.hold(TCS, 10, Book.SWING)
    d.hold(TCS, 10, Book.INTRADAY)
    assert "|True|" in d.do(TCS, Side.SELL, 10)
    second = d.do(TCS, Side.SELL, 10, px=2999.95)                   # different signal (price/time differ)
    d.tick()
    pos = d.pos()
    assert "|False|" in second
    assert pos.get(("paper-in", "swing", TCS)) == 10 and pos.get(("paper-in", "intraday", TCS), 0) >= 0


# ---- item 9: currency, permission, settlement ----
def test_foreign_currency_and_unpermitted_exchange_rejected():
    e, f = make_engine(), Feed()
    t = ts(10, 0, day=(2026, 9, 1), tz=__import__("zoneinfo").ZoneInfo("America/New_York"))
    e.on_quote(f.q("XNYS:AAPL", t, 230.0, 230.01))
    r = act(e, sig("XNYS:AAPL", Side.BUY, 1, 230.01, t))
    assert "|False|" in r and "USD instrument in INR account" in r and "no verified permission for XNYS" in r
    assert e.store.orders() == []


def test_sell_proceeds_not_reused_same_day():
    d = Desk()
    for _ in range(2):                                              # round-trip 45k twice: 90k bought
        d.hold(TCS, 15, Book.INTRADAY)
        assert "|True|" in d.do(TCS, Side.SELL, 15)
        d.tick()
    assert d.pos() == {}
    r = d.do(INFY, Side.BUY, 10)                                     # 15k: fits only if proceeds reused
    assert "|False|" in r and "settled cash" in r
    assert "|True|" in d.do(INFY, Side.BUY, 6)                       # 90k + 9k <= 100k


def test_open_buy_orders_reserve_cash():
    d = Desk()
    d.e.risk.limits = replace(d.e.risk.limits, max_position_value=1e9, max_sector_exposure=1e9)
    assert "|True|" in d.do(TCS, Side.BUY, 16, px=2995.0)            # resting 47.9k
    assert "|True|" in d.do(INFY, Side.BUY, 31, px=1499.0, book=Book.SWING)
    assert "settled cash" in d.do(INFY, Side.BUY, 5, px=1499.5, book=Book.SWING)
