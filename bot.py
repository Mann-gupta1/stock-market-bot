"""Swing bot: 20-day breakout, multi-market. Run after each market's close.

  python bot.py backtest IN [years]          # per market; result gates LIVE-ELIGIBLE vs PAPER ONLY
  python bot.py scan [IN JP UK US ...]       # default: all markets
  python bot.py buy IN RELIANCE 1250 10 [STOP]   # record your buy (stop default = price - 2*ATR)
  python bot.py sell IN RELIANCE 1300            # record your sell, P&L -> trades.csv
Markets: IN JP UK US HK AU DE FR CA (see markets.py). Holdings in holdings.json.
"""
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from markets import MARKETS, inr_rate, symbols

warnings.filterwarnings("ignore")

RULES = "breakout20-v2"
BREAKOUT_N, EXIT_N, VOL_MULT, TREND_MA, INDEX_MA = 20, 10, 1.5, 50, 200
ATR_N, ATR_STOP = 14, 2.0
MAX_GAP_UP = 0.02
CAPITAL_INR, RISK_PCT, MAX_POS = 100_000, 0.01, 5
GATE = dict(min_trades=100, min_pf=1.2)  # backtest must beat this before LIVE-ELIGIBLE
HOLD_FILE, RESULTS = Path("holdings.json"), Path("results.json")


def load(syms, period, mkt):
    m = MARKETS[mkt]
    raw = yf.download(syms, period=period, auto_adjust=True, group_by="ticker", progress=False, threads=True)
    now = datetime.now(ZoneInfo(m["tz"]))
    out = {}
    for s in syms:
        try:
            df = raw[s].dropna(subset=["Close"])
        except KeyError:
            df = pd.DataFrame()
        if len(df) and df.index[-1].date() == now.date() and now.strftime("%H:%M") < m["close"]:
            df = df.iloc[:-1]  # today's candle still forming
        if df.empty:
            print(f"! no data: {s}")
            continue
        out[s] = df
    return out


def indicators(df, min_tv):
    d = df.copy()
    c, h, l, v = d.Close, d.High, d.Low, d.Volume
    d["sma50"] = c.rolling(TREND_MA).mean()
    d["hh"] = h.rolling(BREAKOUT_N).max().shift(1)    # prior N completed sessions
    d["ll"] = l.rolling(EXIT_N).min().shift(1)
    d["vavg"] = v.rolling(BREAKOUT_N).mean().shift(1)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(ATR_N).mean()  # ponytail: simple MA ATR, Wilder smoothing if it matters
    d["liquid"] = (c * v).rolling(BREAKOUT_N).mean() >= min_tv
    d["setup"] = d.liquid & (c > d.sma50) & (c > d.hh) & (v >= VOL_MULT * d.vavg)
    return d


def index_ok(idx_df, index):
    c = idx_df.Close
    return (c > c.rolling(INDEX_MA).mean()).reindex(index, method="ffill").fillna(False)


def simulate(d, ok, cost):
    """One stock. Signal at close i -> entry open i+1 -> exit by stop or trend. Returns trades."""
    O, L, C = (d[k].to_numpy() for k in ("Open", "Low", "Close"))
    hh, ll, atr = d.hh.to_numpy(), d.ll.to_numpy(), d.atr.to_numpy()
    sig = (d.setup & ok).to_numpy()
    idx, n, trades, i = d.index, len(d), [], 0
    while i < n - 1:
        if not sig[i] or O[i + 1] < hh[i] or O[i + 1] > C[i] * (1 + MAX_GAP_UP):
            i += 1
            continue
        fill = O[i + 1]
        stop = fill - ATR_STOP * atr[i]
        j, reason, px = i + 1, "open", C[-1]
        while j < n:
            if L[j] <= stop:
                reason, px = "stop", min(O[j], stop)  # gap-down fills below stop
                break
            if C[j] < ll[j]:
                reason, px = "trend", O[j + 1] if j + 1 < n else C[j]
                j = min(j + 1, n - 1)
                break
            j += 1
        j = min(j, n - 1)
        ret = px * (1 - cost) / (fill * (1 + cost)) - 1
        trades.append(dict(entry=idx[i + 1], exit=idx[j], fill=fill, px=px, ret=ret,
                           stop_pct=(fill - stop) / fill, reason=reason))
        i = j + 1
    return trades


def portfolio(trades):
    """Chronological, risk-sized, max MAX_POS concurrent. Realized-equity curve (fraction of start)."""
    eq, curve, open_pos = 1.0, [1.0], []
    for t in sorted(trades, key=lambda t: t["entry"]):
        for p in sorted((p for p in open_pos if p["exit"] <= t["entry"]), key=lambda p: p["exit"]):
            eq += p["pnl"]
            curve.append(eq)
        open_pos = [p for p in open_pos if p["exit"] > t["entry"]]
        if len(open_pos) >= MAX_POS:
            continue
        open_pos.append({**t, "pnl": min(RISK_PCT * eq / t["stop_pct"], eq / MAX_POS) * t["ret"]})
    for p in sorted(open_pos, key=lambda p: p["exit"]):
        eq += p["pnl"]
        curve.append(eq)
    return pd.Series(curve)


def max_dd(s):
    return float((s / s.cummax() - 1).min())


def summarize(tr):
    w, lo = tr[tr.ret > 0].ret.sum(), -tr[tr.ret <= 0].ret.sum()
    return dict(trades=len(tr), win=round(float((tr.ret > 0).mean()), 3), avg=round(float(tr.ret.mean()), 5),
                pf=round(float(w / lo) if lo else 99.0, 2))


def save_result(key, res):
    RESULTS.write_text(json.dumps({**load_results(), key: {**res, "date": str(datetime.now().date())}}, indent=1))


def load_results():
    return json.loads(RESULTS.read_text()) if RESULTS.exists() else {}


def eligible(key):
    r = load_results().get(key)
    return bool(r) and r["avg"] > 0 and r["trades"] >= GATE["min_trades"] and r["pf"] >= GATE["min_pf"]


def backtest(mkt, years):
    m = MARKETS[mkt]
    data = load(symbols(mkt) + [m["index"]], f"{years}y", mkt)
    idx = data.pop(m["index"])
    trades = []
    for s, df in data.items():
        d = indicators(df, m["min_tv"])
        trades += [{**x, "sym": s} for x in simulate(d, index_ok(idx, d.index), m["cost"])]
    if not trades:
        return print(f"{mkt}: no trades")
    tr = pd.DataFrame(trades)
    tr["year"] = tr.entry.dt.year
    print(tr.groupby("year").ret.agg(trades="count", win=lambda r: (r > 0).mean(), avg="mean").round(3).to_string())
    res = summarize(tr)
    eq = portfolio(trades)
    n = idx.Close[idx.index >= tr.entry.min()]
    print(f"\n{mkt} swing ALL: {res} | exits {tr.reason.value_counts().to_dict()}")
    print(f"PORTFOLIO ({RISK_PCT:.0%} risk/trade, max {MAX_POS} pos): {eq.iloc[-1] - 1:+.1%} | "
          f"max DD {max_dd(eq):.1%} (realized only)")
    print(f"INDEX {m['index']} buy&hold same span: {n.iloc[-1] / n.iloc[0] - 1:+.1%} | max DD {max_dd(n):.1%}")
    print("CAVEAT: universe = TODAY'S large caps (survivorship bias). Judge by year table.")
    save_result(f"{mkt}-swing", res)
    print(f"gate {GATE}: {'LIVE-ELIGIBLE' if eligible(f'{mkt}-swing') else 'PAPER ONLY'}")


def holdings():
    return json.loads(HOLD_FILE.read_text()) if HOLD_FILE.exists() else {}


def buy(mkt, t, price, qty, stop=None):
    s = t.upper() + MARKETS[mkt]["sfx"]
    if stop is None:
        stop = round(price - ATR_STOP * indicators(load([s], "1y", mkt)[s], 0).atr.iloc[-1], 2)
    HOLD_FILE.write_text(json.dumps({**holdings(), s: dict(mkt=mkt, entry=price, qty=qty, stop=stop,
                                                           date=str(datetime.now().date()))}, indent=1))
    print(f"BOUGHT {s} {qty} @ {price} {MARKETS[mkt]['ccy']}, stop {stop}")


def sell(mkt, t, price):
    s, h = t.upper() + MARKETS[mkt]["sfx"], holdings()
    if s not in h:
        return print(f"{s} not in holdings")
    p, rest = h[s], {k: v for k, v in h.items() if k != s}
    pnl = (price - p["entry"]) * p["qty"]
    fx = inr_rate(MARKETS[mkt]["ccy"])
    pd.DataFrame([dict(sym=s, mkt=mkt, entry=p["entry"], exit=price, qty=p["qty"], pnl_local=round(pnl, 2),
                       pnl_inr=round(pnl * fx, 0), ret=round(price / p["entry"] - 1, 4),
                       bought=p["date"], sold=str(datetime.now().date()))]
                 ).to_csv("trades.csv", mode="a", header=not Path("trades.csv").exists(), index=False)
    HOLD_FILE.write_text(json.dumps(rest, indent=1))
    print(f"SOLD {s} @ {price}: P&L {pnl:,.2f} {MARKETS[mkt]['ccy']} = Rs{pnl * fx:,.0f} ({price / p['entry'] - 1:+.1%})")


def scan_market(mkt, held):
    """Returns rows for one market."""
    m = MARKETS[mkt]
    mine = {s: h for s, h in held.items() if h["mkt"] == mkt}
    syms = symbols(mkt) + [s for s in mine if s not in symbols(mkt)]
    data = load(syms + [m["index"]], "2y", mkt)
    if m["index"] not in data:
        return [dict(mkt=mkt, sym=m["index"], status="DATA ISSUE", close=0, why="index missing")]
    idx = data.pop(m["index"])
    mkt_ok = bool(index_ok(idx, idx.index).iloc[-1])
    fx, live = inr_rate(m["ccy"]), eligible(f"{mkt}-swing")
    exp = load_results().get(f"{mkt}-swing", {}).get("avg")
    today = datetime.now(ZoneInfo(m["tz"])).date()
    rows = []
    for s, df in data.items():
        d = indicators(df, m["min_tv"])
        r = d.iloc[-1]
        age = (today - d.index[-1].date()).days
        base = dict(date=str(d.index[-1].date()), mkt=mkt, sym=s, close=round(float(r.Close), 2), rules=RULES)
        if age > 4 or r[["sma50", "hh", "atr"]].isna().any():
            rows.append({**base, "status": "DATA ISSUE", "why": f"last bar {age}d old or short history"})
        elif s in mine:
            h = mine[s]
            broke = "stop" if r.Close <= h["stop"] else "10d low" if r.Close < r.ll else None
            pl = f"P&L {r.Close / h['entry'] - 1:+.1%} = Rs{(r.Close - h['entry']) * h['qty'] * fx:,.0f}"
            rows.append({**base, "status": "SELL NOW" if broke else "HOLD",
                         "why": f"{pl}. closed below {broke} -> sell next open" if broke
                         else f"{pl}. stop {h['stop']}, trend-exit below {r.ll:.2f}"})
        elif r.setup and mkt_ok:
            risk = ATR_STOP * r.atr
            qty = int(min(CAPITAL_INR * RISK_PCT / (risk * fx), CAPITAL_INR / MAX_POS / (r.Close * fx)))
            value = qty * r.Close * fx
            if qty == 0:
                rows.append({**base, "status": "SKIP", "why": f"setup, but 1 share (Rs{r.Close * fx:,.0f}) "
                                                              f"breaks capital/risk limits"})
                continue
            rows.append({**base, "status": "BUY SETUP" if live else "BUY (PAPER ONLY)",
                         "exp_inr": round(exp * value) if exp is not None else None,
                         "why": f"broke 20d high {r.hh:.2f}, vol {r.Volume / r.vavg:.1f}x. Entry if next open in "
                                f"[{r.hh:.2f}, {r.Close * (1 + MAX_GAP_UP):.2f}]; stop fill-{risk:.2f}; qty {qty} "
                                f"(Rs{value:,.0f}); exp net Rs{exp * value:,.0f}/trade" if exp is not None else
                                f"broke 20d high {r.hh:.2f}; qty {qty}; no backtest yet -> run backtest {mkt}"})
        elif r.liquid and (r.hh - r.Close) / r.Close < 0.03:
            miss = [k for k, bad in [(f"close<=20d high {r.hh:.2f}", r.Close <= r.hh),
                                     (f"vol {r.Volume / r.vavg:.1f}x<{VOL_MULT}x", r.Volume < VOL_MULT * r.vavg),
                                     ("below 50DMA", r.Close <= r.sma50), ("index below 200DMA", not mkt_ok)] if bad]
            rows.append({**base, "status": "WAIT", "why": "missing: " + ", ".join(miss)})
    print(f"[{mkt}] index {'ABOVE' if mkt_ok else 'BELOW'} 200DMA | swing {'LIVE-ELIGIBLE' if live else 'PAPER ONLY'}")
    return rows


def scan(mkts):
    held = holdings()
    rows = [r for mkt in mkts for r in scan_market(mkt, held)]
    print(f"\nrules {RULES} | results dates / filings NOT checked - verify before entry\n")
    order = ("SELL NOW", "BUY SETUP", "BUY (PAPER ONLY)", "HOLD", "WAIT", "SKIP", "DATA ISSUE")
    rows = sorted(rows, key=lambda r: (order.index(r["status"]), -(r.get("exp_inr") or -1e18)))
    if not any(r["status"].startswith("BUY") for r in rows):
        print("Aaj koi fresh entry nahi.")
    for r in rows:
        print(f"{r['mkt']:<3}{r['sym']:<14}{r['status']:<17}{r['close']:<10}{r['why']}")
    log = pd.DataFrame([r for r in rows if r["status"] not in ("WAIT", "HOLD")])
    if len(log):
        log.to_csv("journal.csv", mode="a", header=not Path("journal.csv").exists(), index=False)


if __name__ == "__main__":
    a = sys.argv[1:] or ["scan"]
    if a[0] == "backtest":
        backtest(a[1].upper(), int(a[2]) if len(a) > 2 else 8)
    elif a[0] == "buy":
        buy(a[1].upper(), a[2], float(a[3]), int(a[4]), float(a[5]) if len(a) > 5 else None)
    elif a[0] == "sell":
        sell(a[1].upper(), a[2], float(a[3]))
    else:
        scan([x.upper() for x in a[1:]] or list(MARKETS))
