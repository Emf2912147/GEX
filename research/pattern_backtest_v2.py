#!/usr/bin/env python3
"""
Options-era challenge to the course-pattern backtest (v1: pattern_backtest.py).

Hypothesis (Eugenio, 2026-10-18): listed options volume has grown several-
fold since 2020, so the course patterns are mainly options-driven and should
show up in the recent era, and be more reliable when coincident indicators
are at extremes.

Window   2020-10-01 .. today (indicators warmed up from 2019-10).
Weights  INDEX = SPY, QQQ, IWM decide; BROAD = ~75 liquid names cross-check.
Context  at each pattern signal (direction s = +1 bullish / -1 bearish):
           RSI(14)        aligned extreme: s=+1 & RSI<=30, s=-1 & RSI>=70
           Put/call       Cboe EQUITY put/call, 5-day mean, percentile vs the
                          prior 252 days: s=+1 & >= 90th (fear), s=-1 & <= 10th
           Divergence     RSI divergence within the last 5 days: s=+1 bullish
                          (lower price low, higher RSI low vs a swing low in the
                          prior 6-40 days), s=-1 bearish (mirror)
Outcome  s x (10-day forward move in ATR), drift-adjusted, as in v1.

Questions, per pattern x context:
  1. More reliable at extremes?   A - B  = (pattern & aligned extreme) - (pattern, no extreme)
  2. Adds to the indicator alone? A - C  = (pattern & aligned extreme) - (aligned-extreme days, any)
Plus: v1's placebo-adjusted grades re-run on the options era only, and the
indicators on their own.

Grades for a pattern x context (indices decide):
  CREDIBLE  A-B index t >= 2.5, both halves (pre/post Oct 2023) positive,
            A-C index t >= 2, broad A-B same sign with t >= 2
  SLIGHT    A-B index t >= 1.5 in the right direction (or index > 0 and broad t >= 3)
  NONE / CONTRADICTED (index A-B t <= -2)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import numpy as np
import pandas as pd
import requests

import pattern_backtest as v1

ERA = pd.Timestamp("2020-10-01")
WARM = "2019-10-01"
SPLIT2 = pd.Timestamp("2023-10-01")
PCR_URL = "https://cdn.cboe.com/data/us/options/market_statistics/daily/{d}_daily_options"


# ---------------------------------------------------------------- data

def load(data_dir=None):
    if data_dir:
        out = v1.load(data_dir)
        return {k: (g, df[df.index >= WARM]) for k, (g, df) in out.items() if k != "SPX_EARLY"}
    import yfinance as yf

    def dl(sym):
        d = yf.download(sym, start=WARM, auto_adjust=True, progress=False, threads=False)
        if d is None or d.empty:
            return None
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        d = d.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
        return d[(d["high"] > 0) & (d["low"] > 0)]

    out = {}
    for sym in v1.INDEX:
        d = dl(sym)
        if d is not None:
            out[sym] = ("INDEX", d)
    for sym in v1.BROAD:
        d = dl(sym)
        if d is not None and len(d) > 400:
            out[sym] = ("BROAD", d)
    return out


def fetch_pcr(dates) -> pd.DataFrame:
    """Cboe daily market statistics: put/call ratios and total volume."""
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (research)"

    def one(d):
        for attempt in range(3):
            try:
                r = s.get(PCR_URL.format(d=d.strftime("%Y-%m-%d")), timeout=20)
                if r.status_code != 200:
                    return None
                j = r.json()
                ratios = {x["name"]: float(x["value"]) for x in j.get("ratios", [])
                          if str(x.get("value", "")).replace(".", "", 1).isdigit()}
                tot = next((x for x in j.get("SUM OF ALL PRODUCTS", []) if x.get("name") == "VOLUME"), {})
                return {"date": d, "equity": ratios.get("EQUITY PUT/CALL RATIO"),
                        "index": ratios.get("INDEX PUT/CALL RATIO"),
                        "total": ratios.get("TOTAL PUT/CALL RATIO"),
                        "spx": ratios.get("SPX + SPXW PUT/CALL RATIO"),
                        "volume": tot.get("total")}
            except Exception:
                time.sleep(1 + attempt)
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = [r for r in pool.map(one, dates) if r]
    return pd.DataFrame(rows).set_index("date").sort_index()


# ---------------------------------------------------------------- indicators

def rsi(c: pd.Series, n=14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def divergences(d: pd.DataFrame, r: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """bull[t] / bear[t]: an RSI divergence printed within the last 5 days."""
    lo, hi, rv = d["low"].to_numpy(), d["high"].to_numpy(), r.to_numpy()
    n = len(d)
    piv_lo = [i for i in range(3, n - 3) if lo[i] == lo[i - 3:i + 4].min()]
    piv_hi = [i for i in range(3, n - 3) if hi[i] == hi[i - 3:i + 4].max()]
    bull, bear = np.zeros(n, bool), np.zeros(n, bool)
    for t in range(45, n):
        j = t - 4 + int(np.argmin(lo[t - 4:t + 1]))      # recent low
        prior = [p for p in piv_lo if t - 40 <= p <= t - 6 and p + 3 <= t]
        if prior:
            p = prior[-1]
            if lo[j] < lo[p] and rv[j] > rv[p] + 2:
                bull[t] = True
        j = t - 4 + int(np.argmax(hi[t - 4:t + 1]))
        prior = [p for p in piv_hi if t - 40 <= p <= t - 6 and p + 3 <= t]
        if prior:
            p = prior[-1]
            if hi[j] > hi[p] and rv[j] < rv[p] - 2:
                bear[t] = True
    return bull, bear


def context_frame(d: pd.DataFrame, pcr: pd.DataFrame | None) -> pd.DataFrame:
    c = pd.DataFrame(index=d.index)
    r = rsi(d["close"])
    c["rsi"] = r
    c["bull_div"], c["bear_div"] = divergences(d, r)
    if pcr is not None and len(pcr):
        e = pcr["equity"].astype(float).rolling(5).mean()
        pct = e.rolling(252, min_periods=150).apply(lambda x: (x[:-1] < x[-1]).mean(), raw=True)
        c["pcr_pct"] = pct.reindex(d.index).ffill(limit=3)
    else:
        c["pcr_pct"] = np.nan
    return c


def ctx_flags(row, s):
    rsi_v, pp = row["rsi"], row["pcr_pct"]
    f = {
        "rsi": (s > 0 and rsi_v <= 30) or (s < 0 and rsi_v >= 70),
        "rsi_counter": (s > 0 and rsi_v >= 70) or (s < 0 and rsi_v <= 30),
        "pcr": (not np.isnan(pp)) and ((s > 0 and pp >= 0.9) or (s < 0 and pp <= 0.1)),
        "pcr_counter": (not np.isnan(pp)) and ((s > 0 and pp <= 0.1) or (s < 0 and pp >= 0.9)),
        "div": bool(row["bull_div"]) if s > 0 else bool(row["bear_div"]),
    }
    f["any"] = f["rsi"] or f["pcr"] or f["div"]
    f["pcr_known"] = not np.isnan(pp)
    return f


# ---------------------------------------------------------------- patterns

PATTERNS = [
    # name, event key, filter, signal-direction transform
    ("First touch of a zone", "t1", lambda e: e["touch"] == 1, 1),
    ("3rd+ touch of a zone", "t1", lambda e: e["touch"] >= 3, 1),
    ("Gap breakout", "t2", lambda e: e["gap"], 1),
    ("Grind breakout", "t2", lambda e: not e["gap"], 1),
    ("Fibonacci strong close", "t4", lambda e: e["bucket"] == "strong", 1),
    ("Orderly trend (continuation)", "t3", lambda e: e["cls"] == "orderly", 1),
    ("Absorbed move (reversal)", "t3", lambda e: e["cls"] == "absorbed", -1),
    ("Breakout from contraction", "t5", lambda e: e["kind"] == "bo" and e["squeeze"], 1),
    ("Extended move (fade)", "t5", lambda e: e["kind"] == "extX", -1),
    ("Stop sweep and reclaim", "t6", lambda e: e["kind"] == "sweep", 1),
]
CONTEXTS = [("rsi", "RSI extreme"), ("pcr", "Put/call extreme"), ("div", "RSI divergence"),
            ("any", "Any of the three")]


def build(data, pcr):
    """Pattern signals with context, and context-alone baseline days."""
    sig, base = [], []
    E = {k: [] for k in v1.FNS}
    prepped = {}
    for name, (group, df) in data.items():
        d = v1.prep(df)
        cx = context_frame(d, pcr)
        prepped[name] = (group, d, cx)
        for k, fn in v1.FNS.items():
            if k == "t7":
                continue
            for e in fn(name, group, d):
                E[k].append((e, d, cx))
        # context-alone baseline: every day, both directions, deduped later
        fwd = d["fwd"].to_numpy()
        for i in range(len(d) - v1.H):
            if d.index[i] < ERA or np.isnan(fwd[i]):
                continue
            row = cx.iloc[i]
            for s in (1, -1):
                f = ctx_flags(row, s)
                for c, _ in CONTEXTS:
                    if f[c]:
                        base.append({"ticker": name, "group": group, "kind": f"{c}{s}", "i": i,
                                     "date": d.index[i], "ctx": c, "y": s * fwd[i]})
    for pname, key, flt, flip in PATTERNS:
        for e, d, cx in E[key]:
            if not flt(e):
                continue
            i = e.get("ctx_i", e["i"])
            if d.index[i] < ERA or i >= len(d) - v1.H:
                continue
            s = e["s"] * flip
            y = s * d["fwd"].iloc[i]
            if np.isnan(y):
                continue
            row = cx.iloc[i]
            sig.append({"ticker": e["ticker"], "group": e["group"], "kind": pname, "i": i,
                        "date": d.index[i], "pattern": pname, "y": float(y), **ctx_flags(row, s)})
    base = v1.dedupe(base)
    return sig, base


# ---------------------------------------------------------------- stats

def cell(sig, base, group, pattern, c):
    ev = [e for e in sig if e["group"] == group and e["pattern"] == pattern]
    if c == "pcr":
        ev = [e for e in ev if e["pcr_known"]]
    A = [e["y"] for e in ev if e[c]]
    B = [e["y"] for e in ev if not e[c]]
    C = [e["y"] for e in base if e["group"] == group and e["ctx"] == c]

    def halves(sel):
        out = []
        for lo, hi in ((ERA, SPLIT2), (SPLIT2, pd.Timestamp("2100-01-01"))):
            a = [e["y"] for e in ev if e[c] and lo <= e["date"] < hi]
            b = [e["y"] for e in ev if not e[c] and lo <= e["date"] < hi]
            out.append(v1.diff_t(a, b)[0])
        return out

    ab = v1.diff_t(A, B)
    ac = v1.diff_t(A, C)
    h = halves(None)
    return {"n_A": len(A), "n_B": len(B), "mean_A": _m(A), "mean_B": _m(B), "mean_C": _m(C),
            "win_A": _w(A), "win_B": _w(B),
            "A_minus_B": ab[0], "t_AB": ab[1], "A_minus_C": ac[0], "t_AC": ac[1],
            "half1": h[0], "half2": h[1]}


def _m(x):
    return float(np.mean(x)) if len(x) else None


def _w(x):
    return float(np.mean([v > 0 for v in x])) if len(x) else None


def grade_cell(i, b):
    t, tac = i.get("t_AB"), i.get("t_AC")
    if t is None or i["n_A"] < 15:
        return "NONE", "too few index signals at this extreme"
    if t <= -2:
        return "CONTRADICTED", "less reliable at the extreme on the indices"
    halves_ok = all((h or 0) > 0 for h in (i["half1"], i["half2"]))
    bt = b.get("t_AB")
    if t >= 2.5 and halves_ok and (tac or 0) >= 2 and bt is not None and bt >= 2 and (b["A_minus_B"] or 0) > 0:
        return "CREDIBLE", "more reliable at the extreme, adds to the indicator, stable, confirmed on stocks"
    if t >= 1.5 or ((bt or 0) >= 3 and t > 0):
        return "SLIGHT", "better at the extreme, but not strong or stable enough"
    return "NONE", "no reliable improvement at the extreme"


def indicator_alone(base, group):
    out = {}
    for c, label in CONTEXTS:
        y = [e["y"] for e in base if e["group"] == group and e["ctx"] == c]
        m, t, n = v1.mean_t(y)
        out[c] = {"label": label, "mean": m, "t": t, "n": n, "win": _w(y)}
    return out


# ---------------------------------------------------------------- report

def fmt(x, p=2):
    return "–" if x is None else (f"{x:+.{p}f}" if isinstance(x, float) else str(x))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--pcr-csv", default=None, help="offline put/call CSV (tests)")
    ap.add_argument("--out", default="results")
    ap.add_argument("--placebo", type=int, default=5)
    a = ap.parse_args()

    data = load(a.data_dir)
    idx_dates = sorted({d for (g, df) in data.values() if g == "INDEX" for d in df.index})
    if a.pcr_csv:
        pcr = pd.read_csv(a.pcr_csv, parse_dates=["date"]).set_index("date")
    else:
        pcr = fetch_pcr([pd.Timestamp(x).date() for x in idx_dates])
        pcr.index = pd.to_datetime(pcr.index)
    print(f"series {len(data)}, put/call days {len(pcr)}")

    sig, base = build(data, pcr)

    # options-era v1 grades (pattern alone, placebo-adjusted)
    v1.SPLIT = SPLIT2          # halves = Oct 2020-Sep 2023 vs Oct 2023-today
    R1, counts = v1.run(data, k_placebo=a.placebo)

    cells = []
    for pname, *_ in PATTERNS:
        for c, label in CONTEXTS:
            i = cell(sig, base, "INDEX", pname, c)
            b = cell(sig, base, "BROAD", pname, c)
            g, why = grade_cell(i, b)
            cells.append({"pattern": pname, "context": label, "index": i, "broad": b, "grade": g, "why": why})

    ind_i, ind_b = indicator_alone(base, "INDEX"), indicator_alone(base, "BROAD")

    vol = pcr["volume"].dropna()
    vol_year = vol.groupby(vol.index.year).mean().round(-3).astype(int).to_dict() if len(vol) else {}

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "results_v2.json"), "w") as f:
        json.dump({"cells": cells, "indicator_alone": {"INDEX": ind_i, "BROAD": ind_b},
                   "era_pattern_grades": R1, "options_volume_by_year": vol_year,
                   "pcr_days": len(pcr)}, f, indent=1, default=str)

    L = ["# Options-era challenge (Oct 2020 - today)", "",
         f"Index: SPY, QQQ, IWM · broad: {counts['BROAD']} tickers · put/call days: {len(pcr)} · "
         "10-day outcomes in ATR, drift-adjusted, in the signal's direction", "",
         "Average daily listed options volume (Cboe, all products): "
         + ", ".join(f"{y}: {v / 1e6:.1f}M" for y, v in vol_year.items()), "",
         "## 1. Patterns alone, options era only (placebo-adjusted, as v1)", "",
         "| Test | Grade | Index excess (t, n) | halves | Broad excess (t, n) |", "|---|---|---|---|---|"]
    for r in R1:
        i, b = r["index"], r["broad"]
        L.append(f"| {r['test']} | **{r['grade']}** | {fmt(i.get('effect'), 3)} (t {fmt(i.get('t'), 1)}, n {i.get('n', '–')}) | "
                 f"{fmt((i.get('half1') or {}).get('effect'), 3)} / {fmt((i.get('half2') or {}).get('effect'), 3)} | "
                 f"{fmt(b.get('effect'), 3)} (t {fmt(b.get('t'), 1)}, n {b.get('n', '–')}) |")
    L += ["", "## 2. Indicators alone (signal direction, 10-day move in ATR)", "",
          "| Indicator extreme | Index mean (t, n, win%) | Broad mean (t, n, win%) |", "|---|---|---|"]
    for c, label in CONTEXTS:
        i, b = ind_i[c], ind_b[c]
        L.append(f"| {label} | {fmt(i['mean'])} (t {fmt(i['t'], 1)}, n {i['n']}, {fmt((i['win'] or 0) * 100, 0)}%) | "
                 f"{fmt(b['mean'])} (t {fmt(b['t'], 1)}, n {b['n']}, {fmt((b['win'] or 0) * 100, 0)}%) |")
    L += ["", "## 3. Pattern x extreme", "",
          "A = pattern at an aligned extreme, B = pattern without it, C = the extreme alone.", "",
          "| Pattern | Context | Grade | Index A / B / C | A-B (t) | A-C (t) | halves A-B | n A | Broad A-B (t) |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in cells:
        i, b = r["index"], r["broad"]
        L.append(f"| {r['pattern']} | {r['context']} | **{r['grade']}** | {fmt(i['mean_A'])} / {fmt(i['mean_B'])} / {fmt(i['mean_C'])} | "
                 f"{fmt(i['A_minus_B'])} ({fmt(i['t_AB'], 1)}) | {fmt(i['A_minus_C'])} ({fmt(i['t_AC'], 1)}) | "
                 f"{fmt(i['half1'])} / {fmt(i['half2'])} | {i['n_A']} | {fmt(b['A_minus_B'])} ({fmt(b['t_AB'], 1)}) |")
    with open(os.path.join(a.out, "results_v2.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
