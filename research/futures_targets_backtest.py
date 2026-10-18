#!/usr/bin/env python3
"""
Which profit-taking rule? Exits for the trading app's Futures tab.

Same entries and stop as the app (futures_stops_backtest.py, test 2):
trend side (above a rising / below a falling 50-day), stop 0.25 ATR past the
nearest swing pivot 0.5-3 ATR away, one entry per 5 days. Exit rules:

    fixed 1.5R / 2R / 3R      all out at the target; time exit 20 days
    2R, 40 days               all out at 2R; time exit 40 days
    half 1.5R + trail         half at 1.5R, stop to entry, rest trails 0.25 ATR
                              past each newly confirmed swing pivot; 40 days
    half 2R + trail           same with 2R
    trail only                no target, swing-pivot trail; 60 days
    chandelier 3 ATR          no target, 3 ATR from the best close; 60 days

Result in R (the initial risk), FUT decides, ETF cross-check, beyond the
random-walk placebo (same bars, shuffled). Stop first when a bar touches
both. Results to research/results/futures_targets.{md,json}.
"""

from __future__ import annotations

import argparse
import json
import math
import os

import numpy as np
import pandas as pd

from futures_stops_backtest import ETF, FUT, LOOK, W, load, pivots, prep, surrogate

BUF = 0.25
N_PLACEBO = 3
RULES = ["fixed 1.5R", "fixed 2R", "fixed 3R", "2R, 40 days", "half 1.5R + trail",
         "half 2R + trail", "trail only", "chandelier 3 ATR"]


def entries(d):
    lows, highs = pivots(d)
    cl, atr, s50 = d["close"].to_numpy(), d["atr"].to_numpy(), d["sma50"].to_numpy()
    s50p = d["sma50"].shift(10).to_numpy()
    out, last = [], -99
    for i in range(60, len(d) - 2):
        if i - last < 5 or np.isnan(atr[i]) or np.isnan(s50p[i]):
            continue
        a = atr[i]
        if cl[i] > s50[i] > s50p[i]:
            piv = [q for (_, cj, q) in lows if i - LOOK <= cj <= i and 0.5 * a <= cl[i] - q <= 3 * a]
            if piv:
                out.append((i, 1, max(piv) - BUF * a))
                last = i
        elif cl[i] < s50[i] < s50p[i]:
            piv = [q for (_, cj, q) in highs if i - LOOK <= cj <= i and 0.5 * a <= q - cl[i] <= 3 * a]
            if piv:
                out.append((i, -1, min(piv) + BUF * a))
                last = i
    return out, lows, highs


def simulate(d, i, side, stop0, rule, lows, highs):
    o, hi, lo, cl, atr = (d[c].to_numpy() for c in ("open", "high", "low", "close", "atr"))
    n = len(d)
    e = cl[i]
    risk = (e - stop0) * side
    if risk <= 0:
        return None
    fixed = {"fixed 1.5R": (1.5, 20), "fixed 2R": (2.0, 20), "fixed 3R": (3.0, 20), "2R, 40 days": (2.0, 40)}
    half = {"half 1.5R + trail": 1.5, "half 2R + trail": 2.0}
    horizon = fixed[rule][1] if rule in fixed else 40 if rule in half else 60
    if i + horizon >= n:
        return None
    stop, size, booked, best = stop0, 1.0, 0.0, cl[i]
    piv_list = lows if side == 1 else highs
    took_half = False
    for k in range(i + 1, i + horizon + 1):
        # stop first
        if (side == 1 and lo[k] <= stop) or (side == -1 and hi[k] >= stop):
            fill = min(o[k], stop) if side == 1 else max(o[k], stop)
            return booked + size * (fill - e) * side / risk
        if rule in fixed:
            t = e + side * fixed[rule][0] * risk
            if (side == 1 and hi[k] >= t) or (side == -1 and lo[k] <= t):
                return fixed[rule][0]
        if rule in half and not took_half:
            t = e + side * half[rule] * risk
            if (side == 1 and hi[k] >= t) or (side == -1 and lo[k] <= t):
                booked += 0.5 * half[rule]
                size, took_half = 0.5, True
                stop = e
        if rule in half and took_half or rule == "trail only":
            conf = [q for (_, cj, q) in piv_list if i < cj <= k]
            if conf:
                p = conf[-1]
                new = p - side * BUF * atr[k]
                if (new - stop) * side > 0:
                    stop = new
        if rule == "chandelier 3 ATR":
            best = max(best, cl[k]) if side == 1 else min(best, cl[k])
            new = best - side * 3 * atr[k]
            if (new - stop) * side > 0:
                stop = new
    return booked + size * (cl[i + horizon] - e) * side / risk


def run(data, placebo_seed=None):
    rows = []
    for name, (group, df) in data.items():
        d = prep(df if placebo_seed is None else surrogate(df, placebo_seed))
        ents, lows, highs = entries(d)
        for (i, side, stop0) in ents:
            for rule in RULES:
                R = simulate(d, i, side, stop0, rule, lows, highs)
                if R is not None:
                    rows.append({"t": name, "g": group, "rule": rule, "date": d.index[i], "R": R})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir")
    ap.add_argument("--out", default="results")
    a = ap.parse_args()
    data = load(a.data_dir)
    T = run(data)
    P = pd.concat([run(data, 200 + k) for k in range(N_PLACEBO)], ignore_index=True)
    out, L = [], ["# Profit-taking rules for futures trend entries", "",
                  "Entries and stop as in the app (trend side, stop 0.25 ATR past the swing pivot). "
                  "Result in R. Placebo = same bars shuffled. Excess = avg R minus placebo avg R.", "",
                  "| Exit rule | Group | n | avg R | t | win% | placebo avg R | excess | 1st half | 2nd half |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
    for rule in RULES:
        for g in ("FUT", "ETF"):
            s = T[(T["rule"] == rule) & (T["g"] == g)]
            if len(s) < 30:
                continue
            R = s["R"]
            t = float(R.mean() / (R.std(ddof=1) / math.sqrt(len(R))))
            p = P[(P["rule"] == rule) & (P["g"] == g)]["R"]
            med = s["date"].median()
            rec = {"rule": rule, "group": g, "n": int(len(s)), "avg_R": float(R.mean()), "t": t,
                   "win": float((R > 0).mean()), "placebo_R": float(p.mean()) if len(p) else None,
                   "halves": [float(s.loc[s["date"] < med, "R"].mean()), float(s.loc[s["date"] >= med, "R"].mean())]}
            rec["excess"] = rec["avg_R"] - (rec["placebo_R"] or 0)
            out.append(rec)
            L.append(f"| {rule} | {g} | {rec['n']} | {rec['avg_R']:+.3f} | {t:+.1f} | {rec['win'] * 100:.0f}% | "
                     f"{rec['placebo_R']:+.3f} | {rec['excess']:+.3f} | {rec['halves'][0]:+.3f} | {rec['halves'][1]:+.3f} |")
    L += ["", "## By instrument (avg R)", "", "| Instrument | " + " | ".join(RULES) + " |",
          "|---|" + "---|" * len(RULES)]
    by = {}
    for tkr in sorted(T["t"].unique()):
        cells = []
        by[tkr] = {}
        for rule in RULES:
            v = T[(T["t"] == tkr) & (T["rule"] == rule)]["R"].mean()
            by[tkr][rule] = float(v)
            cells.append(f"{v:+.3f}")
        L.append(f"| {tkr} | " + " | ".join(cells) + " |")
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "futures_targets.json"), "w") as f:
        json.dump({"rules": out, "by_instrument": by}, f, indent=1, default=str)
    with open(os.path.join(a.out, "futures_targets.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
