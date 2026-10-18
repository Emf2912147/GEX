#!/usr/bin/env python3
"""
Which setups favour BUYING THE UNDERLYING over 3-6 months? Backtest for the
trading app's Stocks tab.

Research only: Yahoo daily history read at run time; results to
research/results/buy_underlying.{md,json}. No price data committed.

INDEX (decides): SPY 1993+, QQQ 1999+, IWM 2000+, DIA 1998+, SPX 1960-1992
BROAD (cross-check): ~75 of today's large caps, 2005+ -- SURVIVORSHIP-
         BIASED (today's winners), so only a direction check.

Outcomes per entry (close of the signal day):
    ret_H     forward return over H = 63 and 126 trading days
    excess_H  ret_H minus the ticker's average H-day return from ANY day
              (i.e. the edge over buying on a random day)
    mdd_H     worst drawdown from entry inside the window (risk)
Events of one setup are >= 21 days apart per ticker. Forward windows still
overlap, so t-statistics are deflated by sqrt(H / 21) (conservative).

Grades (on excess return, indices decide):
    CREDIBLE   index t >= 2.5 at 63 or 126 days, positive in both halves of
               the index history, broad not contradicting (t > -1)
    SLIGHT     index t >= 1.5
    NONE / NEGATIVE (index t <= -2)
"""

from __future__ import annotations

import argparse
import json
import math
import os

import numpy as np
import pandas as pd

HS = (63, 126)
GAP = 21
INDEX = {"SPY": "1993-02-01", "QQQ": "1999-04-01", "IWM": "2000-06-01", "DIA": "1998-02-01"}
SPX_EARLY = ("^GSPC", "1960-01-01", "1992-12-31")
BROAD = [
    "XLK", "XLF", "XLV", "XLP", "XLE", "XLI", "XLY", "XLU", "XLB", "SMH", "TLT", "GLD",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "V", "MA", "UNH",
    "XOM", "JNJ", "PG", "HD", "COST", "ABBV", "MRK", "PEP", "KO", "WMT", "BAC", "CVX", "LLY",
    "ORCL", "CRM", "ADBE", "AMD", "NFLX", "INTC", "CSCO", "QCOM", "TXN", "IBM", "GS", "MS",
    "C", "WFC", "CAT", "DE", "BA", "GE", "HON", "LMT", "UPS", "NKE", "MCD", "SBUX", "DIS",
    "T", "VZ", "PFE", "ABT", "TMO", "MU", "AMAT", "LRCX", "NOW", "INTU", "BKNG", "AXP",
]


def load(data_dir=None):
    out, vix = {}, None
    if data_dir:
        for f in sorted(os.listdir(data_dir)):
            if f.endswith(".csv"):
                n = f[:-4]
                df = pd.read_csv(os.path.join(data_dir, f), parse_dates=["date"]).set_index("date")
                if n in ("VIX", "VIX3M"):
                    vix = vix if vix is not None else pd.DataFrame(index=df.index)
                    vix[n] = df["close"]
                else:
                    out[n] = ("INDEX" if n in INDEX or n == "SPX_EARLY" else "BROAD", df)
        return out, vix
    import yfinance as yf

    def dl(sym, start, end=None):
        d = yf.download(sym, start=start, end=end, auto_adjust=True, progress=False, threads=False)
        if d is None or d.empty:
            return None
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        return d.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()

    for s, st in INDEX.items():
        d = dl(s, st)
        if d is not None:
            out[s] = ("INDEX", d)
    s, st, en = SPX_EARLY
    d = dl(s, st, en)
    if d is not None:
        out["SPX_EARLY"] = ("INDEX", d)
    for s in BROAD:
        d = dl(s, "2005-01-01")
        if d is not None and len(d) > 600:
            out[s] = ("BROAD", d)
    v = dl("^VIX", "1990-01-01")
    v3 = dl("^VIX3M", "2006-07-01")
    if v is not None:
        vix = pd.DataFrame({"VIX": v["close"]})
        if v3 is not None:
            vix["VIX3M"] = v3["close"]
    return out, vix


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def features(df: pd.DataFrame, vix: pd.DataFrame | None) -> pd.DataFrame:
    d = df.copy()
    c = d["close"]
    pc = c.shift()
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    d["sma50"], d["sma200"] = c.rolling(50).mean(), c.rolling(200).mean()
    d["hi252"] = c.rolling(252).max()
    d["hi20"] = c.rolling(20).max()
    d["max60"] = c.rolling(60).max()
    d["rsi"] = rsi(c)
    d["slope200"] = d["sma200"] - d["sma200"].shift(20)
    for h in HS:
        fwd = c.shift(-h) / c - 1
        d[f"ret{h}"] = fwd
        d[f"exc{h}"] = fwd - fwd.mean()
        lows = d["low"].rolling(h).min().shift(-h)
        d[f"mdd{h}"] = (lows / c - 1).clip(upper=0)
    if vix is not None:
        v = vix.reindex(d.index).ffill(limit=3)
        d["vix"] = v["VIX"]
        d["vix3m"] = v["VIX3M"] if "VIX3M" in v else np.nan
    else:
        d["vix"] = d["vix3m"] = np.nan
    return d


SETUPS = [
    ("Above the 200-day average", "trend filter: close > 200-day SMA",
     lambda d: d["close"] > d["sma200"]),
    ("Below the 200-day average", "close < 200-day SMA (for comparison)",
     lambda d: d["close"] < d["sma200"]),
    ("Dip in an uptrend", "above a rising 200-day SMA and 2+ ATR below the 20-day high",
     lambda d: (d["close"] > d["sma200"]) & (d["slope200"] > 0) & (d["hi20"] - d["close"] >= 2 * d["atr"])),
    ("Oversold in an uptrend", "RSI(14) <= 30 while above the 200-day SMA",
     lambda d: (d["rsi"] <= 30) & (d["close"] > d["sma200"])),
    ("Test of a rising 200-day", "within 0-2% above a rising 200-day SMA after being 7%+ above it in the last 60 days",
     lambda d: (d["close"] >= d["sma200"]) & (d["close"] <= d["sma200"] * 1.02) & (d["slope200"] > 0)
     & (d["max60"] >= d["sma200"] * 1.07)),
    ("Golden cross", "50-day SMA crosses above the 200-day",
     lambda d: (d["sma50"] > d["sma200"]) & (d["sma50"].shift() <= d["sma200"].shift())),
    ("New 52-week high", "close at a 252-day high",
     lambda d: d["close"] >= d["hi252"]),
    ("Index drawdown 15%+", "close 15%+ below its 52-week high",
     lambda d: d["close"] <= 0.85 * d["hi252"]),
    ("Drawdown 15%+ while VIX >= 30", "15%+ below the 52-week high with VIX at 30+",
     lambda d: (d["close"] <= 0.85 * d["hi252"]) & (d["vix"] >= 30)),
    ("VIX spike (>= 30)", "VIX closes at 30 or higher",
     lambda d: d["vix"] >= 30),
    ("VIX term inverted", "VIX above VIX3M by 5%+ (front-end fear)",
     lambda d: d["vix"] >= 1.05 * d["vix3m"]),
]


def events(data, vix):
    rows = []
    for name, (group, df) in data.items():
        d = features(df, vix)
        for sname, _, rule in SETUPS:
            try:
                m = rule(d).fillna(False).to_numpy()
            except Exception:
                continue
            last = -10 ** 9
            for i in np.flatnonzero(m):
                if i - last < GAP:
                    continue
                last = i
                r = {"ticker": name, "group": group, "setup": sname, "date": d.index[i]}
                ok = False
                for h in HS:
                    if not np.isnan(d[f"ret{h}"].iat[i]):
                        ok = True
                    r[f"ret{h}"] = d[f"ret{h}"].iat[i]
                    r[f"exc{h}"] = d[f"exc{h}"].iat[i]
                    r[f"mdd{h}"] = d[f"mdd{h}"].iat[i]
                if ok:
                    rows.append(r)
        # baseline: every 21st day (random-entry reference for drawdown / win rate)
        for i in range(250, len(d), GAP):
            r = {"ticker": name, "group": group, "setup": "_any_day", "date": d.index[i]}
            for h in HS:
                r[f"ret{h}"], r[f"exc{h}"], r[f"mdd{h}"] = (d[f"ret{h}"].iat[i], d[f"exc{h}"].iat[i],
                                                            d[f"mdd{h}"].iat[i])
            rows.append(r)
    return pd.DataFrame(rows)


def tstat(x, h):
    x = pd.Series(x).dropna()
    if len(x) < 8 or x.std() == 0:
        return None, len(x)
    t = x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))
    return float(t / math.sqrt(max(1.0, h / GAP))), len(x)


def summarize(E):
    out = []
    base = E[E["setup"] == "_any_day"]
    for sname, desc, _ in SETUPS:
        row = {"setup": sname, "rule": desc}
        for g in ("INDEX", "BROAD"):
            ev = E[(E["setup"] == sname) & (E["group"] == g)]
            b = base[base["group"] == g]
            for h in HS:
                x = ev[f"exc{h}"].dropna()
                t, n = tstat(x, h)
                med = ev["date"].median() if len(ev) else None
                halves = ([float(ev.loc[ev["date"] < med, f"exc{h}"].mean()),
                           float(ev.loc[ev["date"] >= med, f"exc{h}"].mean())] if len(ev) > 8 else [None, None])
                row[f"{g}_{h}"] = {
                    "n": n, "excess": float(x.mean()) if n else None, "t": t,
                    "ret": float(ev[f"ret{h}"].mean()) if n else None,
                    "win": float((ev[f"ret{h}"] > 0).mean()) if n else None,
                    "win_base": float((b[f"ret{h}"] > 0).mean()) if len(b) else None,
                    "mdd": float(ev[f"mdd{h}"].mean()) if n else None,
                    "mdd_base": float(b[f"mdd{h}"].mean()) if len(b) else None,
                    "halves": halves,
                }
        row["grade"], row["why"] = grade(row)
        out.append(row)
    return out


def grade(row):
    best = None
    for h in HS:
        i, b = row[f"INDEX_{h}"], row[f"BROAD_{h}"]
        if i["t"] is None:
            continue
        halves_ok = all(x is not None and not np.isnan(x) and x > 0 for x in i["halves"])
        broad_ok = b["t"] is None or b["t"] > -1
        if i["t"] >= 2.5 and halves_ok and broad_ok:
            return "CREDIBLE", f"beats a random entry at {h} days on the indices, both halves, stocks agree"
        if i["t"] <= -2:
            best = best or ("NEGATIVE", f"worse than a random entry at {h} days")
        if i["t"] >= 1.5 and best is None:
            best = ("SLIGHT", f"better than a random entry at {h} days, not strong/stable enough")
    return best or ("NONE", "no better than buying on a random day")


def pct(x, d=1):
    return "–" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:+.{d}f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir")
    ap.add_argument("--out", default="results")
    a = ap.parse_args()
    data, vix = load(a.data_dir)
    E = events(data, vix)
    S = summarize(E)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "buy_underlying.json"), "w") as f:
        json.dump(S, f, indent=1, default=str)
    L = ["# Buying the underlying: which setups beat a random entry?", "",
         "Indices: SPY, QQQ, IWM, DIA, SPX 1960-92 (decide). Broad: 75 of today's large caps 2005+ "
         "(survivorship-biased; direction check only). Excess = return minus the average return from any day. "
         "t deflated for overlapping windows.", "",
         "| Setup | Grade | Index 63d excess (t, n) | Index 126d excess (t, n) | 126d win% (any day) | 126d avg drawdown (any day) | Broad 126d excess (t) |",
         "|---|---|---|---|---|---|---|"]
    for r in S:
        i63, i126, b126 = r["INDEX_63"], r["INDEX_126"], r["BROAD_126"]
        L.append(f"| {r['setup']} | **{r['grade']}** | {pct(i63['excess'])} (t {i63['t'] and round(i63['t'], 1)}, n {i63['n']}) | "
                 f"{pct(i126['excess'])} (t {i126['t'] and round(i126['t'], 1)}, n {i126['n']}) | "
                 f"{pct(i126['win'], 0)} ({pct(i126['win_base'], 0)}) | {pct(i126['mdd'])} ({pct(i126['mdd_base'])}) | "
                 f"{pct(b126['excess'])} (t {b126['t'] and round(b126['t'], 1)}) |")
    L += ["", "Halves (index, 126d excess, earlier / later half of events):"]
    for r in S:
        h = r["INDEX_126"]["halves"]
        L.append(f"- {r['setup']}: {pct(h[0])} / {pct(h[1])} — {r['why']}")
    with open(os.path.join(a.out, "buy_underlying.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
