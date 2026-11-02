"""Intraday opening-range breakout (ORB) replay on 5-minute bars, any market.

  python intraday.py IN        # replay last ~60 days (yfinance 5m limit)
  python intraday.py all

Rules (test settings, NOT proven): first 15 min = observe only. Signal = 5m bar closes above opening-range
high AND above VWAP AND bar volume >= 1.5x median of same time-slot over prior 20 sessions. One trade per
stock per day. Entry = next bar open + slippage; SKIP if that is > signal close + MAX_CHASE (no chasing).
Stop = signal bar low (1R). Exit = 1.5R target / stop / 15-min time limit / session end, whichever first.
Stop and target in same bar -> assume stop. Every result reported twice: base slippage and worse fill.

LIMITS: 5m candles, no bid/ask. Real spread + queue position need a broker tick feed (next step).
"""
import sys
import warnings

import numpy as np
import pandas as pd
import yfinance as yf

from bot import save_result, summarize
from markets import MARKETS, symbols

warnings.filterwarnings("ignore")

OR_BARS, TIME_BARS, VOL_MULT, TARGET_R, MED_N = 3, 3, 1.5, 1.5, 20
MAX_CHASE = 0.002            # entry may be at most 0.2% above signal close
SLIP = {"base": 0.0005, "worse fill": 0.0015}   # per side


def day_trades(day, med, cost, slip):
    """day: one session of 5m bars. med: median volume per slot index. Returns 0 or 1 trade."""
    O, H, L, C, V = (day[k].to_numpy() for k in ("Open", "High", "Low", "Close", "Volume"))
    if len(day) < OR_BARS + 2:
        return []
    or_hi = H[:OR_BARS].max()
    vwap = np.cumsum((H + L + C) / 3 * V) / np.maximum(np.cumsum(V), 1)
    for k in range(OR_BARS, len(day) - 1):
        if not (C[k] > or_hi and C[k] > vwap[k] and k < len(med) and V[k] >= VOL_MULT * med[k] > 0):
            continue
        entry = O[k + 1] * (1 + slip)
        stop = L[k]
        if entry > C[k] * (1 + MAX_CHASE) or entry <= stop:
            return [dict(time=day.index[k], status="SKIP")]
        target = entry + TARGET_R * (entry - stop)
        last = min(k + TIME_BARS, len(day) - 1)
        px, why = C[last], "time"
        for j in range(k + 1, last + 1):
            if L[j] <= stop:
                px, why = min(O[j], stop), "stop"
                break
            if H[j] >= target:
                px, why = target, "target"
                break
        px *= 1 - slip
        return [dict(time=day.index[k], status="FILLED", why=why, entry=entry, exit=px,
                     r=(px - entry) / (entry - stop), ret=px * (1 - cost) / (entry * (1 + cost)) - 1)]
    return []


def replay(mkt):
    m = MARKETS[mkt]
    raw = yf.download(symbols(mkt), period="60d", interval="5m", group_by="ticker", progress=False, threads=True)
    out = {name: [] for name in SLIP}
    for s in symbols(mkt):
        try:
            df = raw[s].dropna(subset=["Close"]).tz_convert(m["tz"]).between_time(m["open"], m["close"],
                                                                                  inclusive="left")
        except (KeyError, TypeError):
            print(f"! no 5m data: {s}")
            continue
        days = [g for _, g in df.groupby(df.index.date)]
        vol = pd.DataFrame([g.Volume.to_numpy()[:80] for g in days])        # session x slot
        meds = vol.rolling(MED_N, min_periods=10).median().shift(1)          # prior sessions only
        for di, g in enumerate(days):
            med = meds.iloc[di].to_numpy()
            if np.isnan(med[:OR_BARS + 1]).all():
                continue
            for name, slip in SLIP.items():
                out[name] += [{**t, "sym": s} for t in day_trades(g, np.nan_to_num(med), m["icost"], slip)]
    for name, ts in out.items():
        df = pd.DataFrame(ts)
        if df.empty or "ret" not in df:
            print(f"[{mkt} intraday {name}] no trades")
            continue
        f = df[df.status == "FILLED"]
        res = summarize(f)
        print(f"[{mkt} intraday {name}] {res} | avg R {f.r.mean():.2f} | skipped {int((df.status == 'SKIP').sum())} "
              f"| exits {f.why.value_counts().to_dict()}")
        if name == "worse fill":
            save_result(f"{mkt}-intraday", res)   # gate on the pessimistic case


if __name__ == "__main__":
    a = [x.upper() for x in sys.argv[1:]] or ["IN"]
    for mkt in (list(MARKETS) if a == ["ALL"] else a):
        replay(mkt)
