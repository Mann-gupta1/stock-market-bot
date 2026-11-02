import numpy as np
import pandas as pd

import bot
import intraday


def frame(n=80, base=100.0):
    idx = pd.bdate_range("2024-01-01", periods=n)
    c = np.full(n, base)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 1e7}, index=idx)


def with_breakout(df, day=60):
    df = df.copy()
    df.loc[df.index[day], ["Open", "High", "Close", "Volume"]] = [100, 106, 105, 3e7]  # breakout + 3x vol
    return df


def test_breakout_signal_fires_only_on_breakout_day():
    d = bot.indicators(with_breakout(frame()), 0)
    assert list(d.index[d.setup]) == [d.index[60]]


def test_gap_down_exits_below_stop():
    df = with_breakout(frame())
    df.loc[df.index[61], ["Open", "High", "Low", "Close"]] = [105, 106, 104, 105]  # entry fill 105
    df.loc[df.index[62], ["Open", "High", "Low", "Close"]] = [90, 91, 89, 90]      # gap far below stop
    d = bot.indicators(df, 0)
    t = bot.simulate(d, pd.Series(True, index=d.index), 0.0015)
    assert len(t) == 1 and t[0]["reason"] == "stop" and t[0]["px"] == 90


def test_skips_entry_when_open_gaps_too_high():
    df = with_breakout(frame())
    df.loc[df.index[61], ["Open", "High", "Low", "Close"]] = [110, 111, 109, 110]  # > close*1.02
    d = bot.indicators(df, 0)
    assert bot.simulate(d, pd.Series(True, index=d.index), 0.0015) == []


def test_orb_target_and_no_chase():
    idx = pd.date_range("2024-01-02 09:15", periods=8, freq="5min")
    day = pd.DataFrame({"Open": [100, 100, 100, 100, 101.1, 102, 103, 103],
                        "High": [101, 101, 101, 101.2, 101.5, 104, 104, 104],
                        "Low": [99, 99, 99, 100.5, 101, 101.5, 102, 102],
                        "Close": [100, 100, 100, 101.1, 101.4, 103, 103, 103],
                        "Volume": [1e5] * 3 + [3e5] + [1e5] * 4}, index=idx)
    med = np.full(8, 1e5)
    t = intraday.day_trades(day, med, 0.0, 0.0)
    assert t[0]["status"] == "FILLED" and t[0]["why"] == "target"   # entry 101.1, stop 100.5, target 102
    gap = day.copy()
    gap.iloc[4, 0] = 102                                              # next open > close+0.2%
    assert intraday.day_trades(gap, med, 0.0, 0.0)[0]["status"] == "SKIP"
