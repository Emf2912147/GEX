#!/usr/bin/env python3
"""
Where do stops get run? Stop placement for the trading app's Futures tab.

Research only: Yahoo daily history read at run time; results to
research/results/futures_stops.{md,json}. No price data committed.

FUT  (decide): ES=F NQ=F RTY=F GC=F ZF=F ZB=F (front month, unadjusted:
     roll gaps are small next to an ATR but not zero)
ETF  (cross-check, no rolls, longer): SPY QQQ IWM GLD IEF TLT

Test 1 - "stop runs" (wick-and-reverse at obvious levels)
    Levels: 5-bar swing lows (for long stops) / highs (short stops) still
    intact, and round numbers. A level placed b ATR beyond the pivot
    (b = 0, 0.1, 0.25, 0.5, 1.0) is followed to its FIRST touch (<= 40 days).
      wick     the touch day closes back on the original side of the level
      reclaim  within 5 days price closes back beyond the pivot itself
    Control: a level the same distance from price on the same day, shifted
    0.5-1.5 ATR, more than 0.3 ATR from any pivot or round number.
    A stop run shows up as anchored wick/reclaim rates ABOVE control; the
    buffer where the excess disappears is the buffer to use.

Test 2 - stop rules in R
    Trend entries (close above a rising 50-day for longs, below a falling
    one for shorts; a pivot 0.5-3 ATR away), one per 5 days. Stop: pivot
    + b ATR, or a plain 1.5 / 2 / 3 ATR stop. Target 2R, time exit 20 days.
    Reports hit rate, whipsaw (stopped, then price reaches the 2R target
    within the next 20 days) and average R.
"""

from __future__ import annotations

import argparse
import json
import math
import os

import numpy as np
import pandas as pd

FUT = {"ES=F": 50.0, "NQ=F": 250.0, "RTY=F": 50.0, "GC=F": 50.0, "ZF=F": 1.0, "ZB=F": 1.0}
ETF = {"SPY": 10.0, "QQQ": 10.0, "IWM": 10.0, "GLD": 10.0, "IEF": 1.0, "TLT": 5.0}
BUFFERS = (0.0, 0.1, 0.25, 0.5, 1.0)
ATR_STOPS = (1.5, 2.0, 3.0)
W = 5               # pivot half-width
LOOK = 60           # pivots considered: confirmed in the last 60 days
H_TOUCH = 40
RNG = np.random.default_rng(7)
N_PLACEBO = 3


def load(data_dir=None):
    out = {}
    if data_dir:
        for f in sorted(os.listdir(data_dir)):
            if f.endswith(".csv"):
                n = f[:-4]
                df = pd.read_csv(os.path.join(data_dir, f), parse_dates=["date"]).set_index("date")
                out[n] = ("FUT" if n in FUT else "ETF", df)
        return out
    import yfinance as yf
    for group, syms in (("FUT", FUT), ("ETF", ETF)):
        for s in syms:
            d = yf.download(s, start="2000-01-01", auto_adjust=False, progress=False, threads=False)
            if d is None or d.empty:
                continue
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            d = d.rename(columns=str.lower)[["open", "high", "low", "close"]].dropna()
            d = d[(d["high"] >= d["low"]) & (d["close"] > 0)]
            if len(d) > 1000:
                out[s] = (group, d)
    return out


def surrogate(df, seed):
    """Same bars, random order: keeps the return / range distribution and
    drift, destroys any memory of price levels (the placebo)."""
    r = np.random.default_rng(seed)
    pc = df["close"].shift().to_numpy()
    rel = (df[["open", "high", "low", "close"]].to_numpy() / pc[:, None])[1:]
    rel = rel[r.permutation(len(rel))]
    c0 = float(df["close"].iloc[0])
    closes = c0 * np.cumprod(rel[:, 3])
    prev = np.r_[c0, closes[:-1]]
    bars = rel * prev[:, None]
    out = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=df.index[1:])
    return out


def prep(df):
    d = df.copy()
    pc = d["close"].shift()
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    d["sma50"] = d["close"].rolling(50).mean()
    return d


def pivots(d):
    """Confirmed pivots: (index of pivot, confirm index, price) for lows and highs."""
    lo, hi = d["low"].to_numpy(), d["high"].to_numpy()
    lows, highs = [], []
    for i in range(W, len(d) - W):
        if lo[i] == lo[i - W:i + W + 1].min():
            lows.append((i, i + W, lo[i]))
        if hi[i] == hi[i - W:i + W + 1].max():
            highs.append((i, i + W, hi[i]))
    return lows, highs


def first_touch(d, start, level, side):
    """side 'below' = level under price (long stop). Returns (k, wick, reclaim_ref_close_series)."""
    lo, hi, cl = d["low"].to_numpy(), d["high"].to_numpy(), d["close"].to_numpy()
    end = min(len(d), start + H_TOUCH + 1)
    for k in range(start + 1, end):
        if side == "below" and lo[k] <= level:
            return k, bool(cl[k] > level)
        if side == "above" and hi[k] >= level:
            return k, bool(cl[k] < level)
    return None, None


def reclaimed(d, k, pivot, side, days=5):
    cl = d["close"].to_numpy()
    seg = cl[k:k + days + 1]
    if len(seg) == 0:
        return None
    return bool((seg > pivot).any()) if side == "below" else bool((seg < pivot).any())


def near_any(x, levels, tol):
    return any(abs(x - v) <= tol for v in levels)


def test1(name, group, d, rstep):
    rows = []
    lows, highs = pivots(d)
    atr, cl = d["atr"].to_numpy(), d["close"].to_numpy()
    for side, piv in (("below", lows), ("above", highs)):
        for (pi, ci, p) in piv:
            if ci >= len(d) - 2 or np.isnan(atr[ci]):
                continue
            a = atr[ci]
            if side == "below" and not (cl[ci] > p + 0.5 * a):
                continue
            if side == "above" and not (cl[ci] < p - 0.5 * a):
                continue
            other = [q for (_, cj, q) in piv if ci - LOOK <= cj <= ci]
            for b in BUFFERS:
                lvl = p - b * a if side == "below" else p + b * a
                k, wick = first_touch(d, ci, lvl, side)
                if k is None:
                    continue
                rec = reclaimed(d, k, p, side)
                # matched control
                for _ in range(10):
                    sh = RNG.uniform(0.5, 1.5) * a * RNG.choice([-1, 1])
                    c = lvl + sh
                    if (side == "below" and c >= cl[ci] - 0.3 * a) or (side == "above" and c <= cl[ci] + 0.3 * a):
                        continue
                    rn = round(c / rstep) * rstep
                    if near_any(c, other + [p], 0.3 * a) or abs(c - rn) <= 0.3 * a:
                        continue
                    kc, wc = first_touch(d, ci, c, side)
                    if kc is None:
                        break
                    rc = reclaimed(d, kc, c + (p - lvl), side)   # same offset to "its" pivot
                    rows.append({"t": name, "g": group, "kind": "pivot", "b": b, "side": side,
                                 "date": d.index[k], "wick": wick, "rec": rec, "cwick": wc, "crec": rc})
                    break
    # round numbers: next round level below / above price, every 10 days
    for i in range(60, len(d) - 2, 10):
        a = atr[i]
        if np.isnan(a):
            continue
        for side in ("below", "above"):
            rn = (math.floor(cl[i] / rstep) * rstep) if side == "below" else (math.ceil(cl[i] / rstep) * rstep)
            if abs(cl[i] - rn) < 0.5 * a or abs(cl[i] - rn) > 4 * a:
                continue
            k, wick = first_touch(d, i, rn, side)
            if k is None:
                continue
            for _ in range(10):
                c = rn + RNG.uniform(0.3, 0.7) * rstep * RNG.choice([-1, 1])
                if (side == "below" and c >= cl[i] - 0.3 * a) or (side == "above" and c <= cl[i] + 0.3 * a):
                    continue
                kc, wc = first_touch(d, i, c, side)
                if kc is None:
                    break
                rows.append({"t": name, "g": group, "kind": "round", "b": 0.0, "side": side,
                             "date": d.index[k], "wick": wick, "rec": None, "cwick": wc, "crec": None})
                break
    return rows


def test2(name, group, d):
    rows = []
    lows, highs = pivots(d)
    o, hi, lo, cl, atr, s50 = (d[c].to_numpy() for c in ("open", "high", "low", "close", "atr", "sma50"))
    s50_prev = d["sma50"].shift(10).to_numpy()
    last = -99
    n = len(d)
    for i in range(60, n - 25):
        if i - last < 5 or np.isnan(atr[i]) or np.isnan(s50_prev[i]):
            continue
        a = atr[i]
        if cl[i] > s50[i] and s50[i] > s50_prev[i]:
            side, piv = 1, [q for (_, cj, q) in lows if i - LOOK <= cj <= i and 0.5 * a <= cl[i] - q <= 3 * a]
            ref = max(piv) if piv else None
        elif cl[i] < s50[i] and s50[i] < s50_prev[i]:
            side, piv = -1, [q for (_, cj, q) in highs if i - LOOK <= cj <= i and 0.5 * a <= q - cl[i] <= 3 * a]
            ref = min(piv) if piv else None
        else:
            continue
        if ref is None:
            continue
        last = i
        e = cl[i]
        rules = {f"pivot+{b}ATR": ref - side * b * a for b in BUFFERS}
        rules.update({f"{m}ATR": e - side * m * a for m in ATR_STOPS})
        for rule, stop in rules.items():
            risk = (e - stop) * side
            if risk <= 0:
                continue
            tgt = e + side * 2 * risk
            R, hit, whip = None, False, False
            for k in range(i + 1, min(n, i + 21)):
                # stop first if both in one bar (conservative)
                if (side == 1 and lo[k] <= stop) or (side == -1 and hi[k] >= stop):
                    fill = (min(o[k], stop) if side == 1 else max(o[k], stop))
                    R, hit = (fill - e) * side / risk, True
                    seg_hi, seg_lo = hi[k + 1:k + 21], lo[k + 1:k + 21]
                    whip = bool((seg_hi >= tgt).any()) if side == 1 else bool((seg_lo <= tgt).any())
                    break
                if (side == 1 and hi[k] >= tgt) or (side == -1 and lo[k] <= tgt):
                    R = 2.0
                    break
            if R is None:
                k = min(n - 1, i + 20)
                R = (cl[k] - e) * side / risk
            rows.append({"t": name, "g": group, "rule": rule, "date": d.index[i], "side": side,
                         "R": R, "hit": hit, "whip": whip, "risk_atr": risk / a})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir")
    ap.add_argument("--out", default="results")
    a = ap.parse_args()
    data = load(a.data_dir)
    r1, r2, p1, p2 = [], [], [], []
    for name, (group, df) in data.items():
        d = prep(df)
        step = FUT.get(name) or ETF.get(name) or 10.0
        r1 += test1(name, group, d, step)
        r2 += test2(name, group, d)
        for k in range(N_PLACEBO):
            sd = prep(surrogate(df, 100 + k))
            p1 += test1(name, group, sd, step)
            p2 += test2(name, group, sd)
    T1, T2 = pd.DataFrame(r1), pd.DataFrame(r2)
    P1, P2 = pd.DataFrame(p1), pd.DataFrame(p2)

    out = {"test1": [], "test2": []}
    L = ["# Stop runs and stop placement (daily bars)", "",
         "FUT decides (ES, NQ, RTY, GC, ZF, ZB front month since 2000); ETF cross-check "
         "(SPY, QQQ, IWM, GLD, IEF, TLT).", "",
         "## Test 1: first touch of a level - wick (closes back) and reclaim (back past the pivot in 5 days)",
         "Anchored = pivot + buffer or a round number; control = same-distance level away from pivots "
         "and round numbers. Excess = anchored minus control (points); t from a two-proportion test.", "",
         "| Level | Group | n | wick | control | excess (t) | placebo excess | reclaim | control | excess (t) | placebo excess |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]

    def prop_t(x, y):
        x, y = pd.Series(x).dropna().astype(float), pd.Series(y).dropna().astype(float)
        if len(x) < 30 or len(y) < 30:
            return None, None
        p = (x.sum() + y.sum()) / (len(x) + len(y))
        se = math.sqrt(p * (1 - p) * (1 / len(x) + 1 / len(y))) or 1
        return float(x.mean() - y.mean()), float((x.mean() - y.mean()) / se)

    for kind, b in [("pivot", b) for b in BUFFERS] + [("round", 0.0)]:
        for g in ("FUT", "ETF"):
            s = T1[(T1["kind"] == kind) & (T1["b"] == b) & (T1["g"] == g)] if len(T1) else T1
            if s is None or len(s) == 0:
                continue
            ew, tw = prop_t(s["wick"], s["cwick"])
            er, tr = prop_t(s["rec"], s["crec"])
            q = P1[(P1["kind"] == kind) & (P1["b"] == b) & (P1["g"] == g)] if len(P1) else P1
            pw = prop_t(q["wick"], q["cwick"])[0] if len(q) else None
            pr = prop_t(q["rec"], q["crec"])[0] if len(q) else None
            lab = f"pivot + {b} ATR" if kind == "pivot" else "round number"
            rec = {"level": lab, "group": g, "n": int(len(s)),
                   "wick": float(s["wick"].mean()), "cwick": float(s["cwick"].mean()), "wick_excess": ew, "wick_t": tw,
                   "rec": float(s["rec"].dropna().astype(float).mean()) if s["rec"].notna().any() else None,
                   "crec": float(s["crec"].dropna().astype(float).mean()) if s["crec"].notna().any() else None,
                   "rec_excess": er, "rec_t": tr,
                   "placebo_wick_excess": pw, "placebo_rec_excess": pr}
            out["test1"].append(rec)
            f = lambda x: "–" if x is None else f"{x * 100:.0f}%"
            g2 = lambda x, t: "–" if x is None else f"{x * 100:+.1f} ({t:+.1f})"
            h = lambda x: "–" if x is None else f"{x * 100:+.1f}"
            L.append(f"| {lab} | {g} | {rec['n']} | {f(rec['wick'])} | {f(rec['cwick'])} | {g2(ew, tw or 0)} | {h(pw)} | "
                     f"{f(rec['rec'])} | {f(rec['crec'])} | {g2(er, tr or 0)} | {h(pr)} |")

    L += ["", "## Test 2: stop rules on trend entries (target 2R, 20-day time exit)", "",
          "| Stop rule | Group | n | stopped | whipsaw (stopped, then 2R hit) | avg R | t | placebo avg R | placebo stopped | avg stop (ATR) |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for rule in [f"pivot+{b}ATR" for b in BUFFERS] + [f"{m}ATR" for m in ATR_STOPS]:
        for g in ("FUT", "ETF"):
            s = T2[(T2["rule"] == rule) & (T2["g"] == g)] if len(T2) else T2
            if len(s) < 30:
                continue
            R = s["R"]
            t = float(R.mean() / (R.std(ddof=1) / math.sqrt(len(R)))) if R.std() > 0 else 0.0
            q = P2[(P2["rule"] == rule) & (P2["g"] == g)] if len(P2) else P2
            pR = float(q["R"].mean()) if len(q) else None
            pS = float(q["hit"].mean()) if len(q) else None
            rec = {"rule": rule, "group": g, "n": int(len(s)), "stopped": float(s["hit"].mean()),
                   "placebo_R": pR, "placebo_stopped": pS,
                   "whipsaw": float(s["whip"].mean()), "avg_R": float(R.mean()), "t": t,
                   "risk_atr": float(s["risk_atr"].mean()),
                   "halves": [float(s.loc[s["date"] < s["date"].median(), "R"].mean()),
                              float(s.loc[s["date"] >= s["date"].median(), "R"].mean())]}
            out["test2"].append(rec)
            L.append(f"| {rule} | {g} | {rec['n']} | {rec['stopped'] * 100:.0f}% | {rec['whipsaw'] * 100:.0f}% | "
                     f"{rec['avg_R']:+.3f} | {t:+.1f} | {pR if pR is None else f'{pR:+.3f}'} | "
                     f"{pS if pS is None else f'{pS * 100:.0f}%'} | {rec['risk_atr']:.2f} |")
    # per-instrument best rule (FUT)
    L += ["", "## Test 2 by instrument (avg R)", "",
          "| Instrument | " + " | ".join([f"p+{b}" for b in BUFFERS] + [f"{m}ATR" for m in ATR_STOPS]) + " |",
          "|---|" + "---|" * (len(BUFFERS) + len(ATR_STOPS))]
    out["by_instrument"] = {}
    for t in sorted(T2["t"].unique()) if len(T2) else []:
        cells, rec = [], {}
        for rule in [f"pivot+{b}ATR" for b in BUFFERS] + [f"{m}ATR" for m in ATR_STOPS]:
            s = T2[(T2["t"] == t) & (T2["rule"] == rule)]["R"]
            v = float(s.mean()) if len(s) else None
            rec[rule] = v
            cells.append("–" if v is None else f"{v:+.3f}")
        out["by_instrument"][t] = rec
        L.append(f"| {t} | " + " | ".join(cells) + " |")

    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "futures_stops.json"), "w") as f:
        json.dump(out, f, indent=1, default=str)
    with open(os.path.join(a.out, "futures_stops.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
