#!/usr/bin/env python3
"""
Backtest of the "Stock pattern lessons" course rules (Eugenio's trading
course), to decide which belong in the trading app.

Research only: reads daily OHLCV from Yahoo (yfinance) at run time and
writes results/ (JSON + Markdown). No price data is committed.

Weighting: the main indices decide the grade.
    INDEX  = SPY (1994+), QQQ (1999+), IWM (2000+), and ^GSPC 1985-1993
             (pre-SPY, so SPX history is added without double counting)
    BROAD  = ~75 liquid stocks and sector ETFs, 2005+ (cross-check only)

Every outcome is in ATR(14) units, 10 trading days forward, and DRIFT-
ADJUSTED (the ticker's own mean 10-day move is subtracted), so the long
upward drift of the indices cannot masquerade as a pattern edge.
Events are de-duplicated per ticker (no two events of the same kind
within 10 days), so overlapping windows do not inflate the statistics.

Grades (decided on the INDEX group, confirmed by BROAD):
    CREDIBLE      predicted direction, index t >= 2.5, same sign in both
                  halves (pre/post 2013), broad agrees (same sign, t >= 2),
                  effect >= the test's minimum meaningful size
    SLIGHT        predicted direction, index t >= 1.5 (or index weak but
                  broad t >= 3 in the predicted direction)
    NONE          no reliable effect
    CONTRADICTED  significant in the OPPOSITE direction (index t <= -2)
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import date, timedelta

import numpy as np
import pandas as pd

H = 10                      # forward horizon, trading days
DEDUPE = 10                 # min days between events of one kind, per ticker
SPLIT = pd.Timestamp("2013-01-01")

INDEX = {"SPY": "1994-01-01", "QQQ": "1999-04-01", "IWM": "2000-06-01"}
SPX_EARLY = ("^GSPC", "1985-01-01", "1993-12-31")
BROAD = [
    "XLK", "XLF", "XLV", "XLP", "XLE", "XLI", "XLY", "XLU", "XLB", "SMH", "TLT", "GLD", "DIA",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "V", "MA", "UNH",
    "XOM", "JNJ", "PG", "HD", "COST", "ABBV", "MRK", "PEP", "KO", "WMT", "BAC", "CVX", "LLY",
    "ORCL", "CRM", "ADBE", "AMD", "NFLX", "INTC", "CSCO", "QCOM", "TXN", "IBM", "GS", "MS",
    "C", "WFC", "CAT", "DE", "BA", "GE", "HON", "LMT", "UPS", "NKE", "MCD", "SBUX", "DIS",
    "T", "VZ", "PFE", "ABT", "TMO", "MU", "AMAT", "LRCX", "NOW", "INTU", "BKNG", "AXP",
]
BROAD_START = "2005-01-01"


# ======================================================================
# data
# ======================================================================

def load(data_dir: str | None) -> dict[str, tuple[str, pd.DataFrame]]:
    """{name: (group, df)}; df index = Timestamp, columns open high low close volume."""
    out = {}
    if data_dir:                                   # offline / tests
        for f in sorted(os.listdir(data_dir)):
            if f.endswith(".csv"):
                name = f[:-4]
                df = pd.read_csv(os.path.join(data_dir, f), parse_dates=["date"]).set_index("date")
                out[name] = ("INDEX" if name in INDEX or name == "SPX_EARLY" else "BROAD", df)
        return out

    import yfinance as yf

    def dl(sym, start, end=None):
        d = yf.download(sym, start=start, end=end, auto_adjust=True, progress=False, threads=False)
        if d is None or d.empty:
            return None
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        d = d.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
        return d[(d["high"] > 0) & (d["low"] > 0)]

    for sym, start in INDEX.items():
        d = dl(sym, start)
        if d is not None:
            out[sym] = ("INDEX", d)
    sym, start, end = SPX_EARLY
    d = dl(sym, start, end)
    if d is not None:
        out["SPX_EARLY"] = ("INDEX", d)
    for sym in BROAD:
        d = dl(sym, BROAD_START)
        if d is not None and len(d) > 500:
            out[sym] = ("BROAD", d)
    return out


def prep(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    pc = d["close"].shift()
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    d["tr"] = tr
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    d["fwd"] = (d["close"].shift(-H) - d["close"]) / d["atr"]
    d["fwd"] = d["fwd"] - d["fwd"].mean()               # drift-adjusted
    d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()
    return d


# ======================================================================
# statistics
# ======================================================================

def dedupe(events: list[dict], key=("ticker", "kind")) -> list[dict]:
    last, out = {}, []
    for e in sorted(events, key=lambda e: (e["ticker"], e["kind"], e["i"])):
        k = tuple(e[x] for x in key)
        if k in last and e["i"] - last[k] < DEDUPE:
            continue
        last[k] = e["i"]
        out.append(e)
    return out


def mean_t(x):
    x = np.asarray([v for v in x if v is not None and not np.isnan(v)], float)
    if len(x) < 3:
        return None, None, len(x)
    sd = x.std(ddof=1)
    return float(x.mean()), (float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else None), len(x)


def diff_t(a, b):
    """Welch t for mean(a) - mean(b)."""
    a = np.asarray([v for v in a if v is not None and not np.isnan(v)], float)
    b = np.asarray([v for v in b if v is not None and not np.isnan(v)], float)
    if len(a) < 5 or len(b) < 5:
        return None, None, len(a), len(b)
    d = a.mean() - b.mean()
    se = math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    return float(d), (float(d / se) if se > 0 else None), len(a), len(b)


def summarize(events, group, a_sel, b_sel=None, field="y"):
    """Effect = mean(a) - mean(b)  (or mean(a) if b_sel is None), with halves."""
    ev = [e for e in events if e["group"] == group]

    def calc(es):
        a = [e[field] for e in es if a_sel(e)]
        if b_sel is None:
            m, t, n = mean_t(a)
            return {"effect": m, "t": t, "n": n}
        b = [e[field] for e in es if b_sel(e)]
        d, t, na, nb = diff_t(a, b)
        return {"effect": d, "t": t, "n": na, "n_ref": nb,
                "mean_a": float(np.mean(a)) if a else None,
                "mean_b": float(np.mean(b)) if b else None}

    res = calc(ev)
    res["half1"] = calc([e for e in ev if e["date"] < SPLIT])
    res["half2"] = calc([e for e in ev if e["date"] >= SPLIT])
    return res


def grade(idx, broad, min_effect):
    t, eff = idx.get("t"), idx.get("effect")
    bt, beff = broad.get("t"), broad.get("effect")
    if t is None or eff is None:
        return "NONE", "too few index events"
    halves_ok = all((h.get("effect") or 0) > 0 for h in (idx["half1"], idx["half2"]))
    if t <= -2:
        return "CONTRADICTED", "index shows the opposite of the course rule"
    if (t >= 2.5 and halves_ok and eff >= min_effect
            and bt is not None and bt >= 2 and beff is not None and beff > 0):
        return "CREDIBLE", "significant on the indices, stable across both halves, confirmed on stocks"
    if t >= 1.5 and (bt is None or bt > -2):
        return "SLIGHT", "right direction on the indices but not strong or stable enough"
    if (bt is not None and bt >= 3) and t > 0:
        return "SLIGHT", "clear on stocks, weak on the indices"
    return "NONE", "no reliable effect on the indices"


# ======================================================================
# patterns
# ======================================================================

def pivots(d: pd.DataFrame, w: int, col: str, fn):
    v = d[col].to_numpy()
    out = []
    for i in range(w, len(v) - w):
        if v[i] == fn(v[i - w:i + w + 1]):
            out.append(i)
    return out


def t1_zone_touches(name, group, d, w=5, up=1.5, dn=0.5, horizon=H):
    """Law 1: each touch drains a zone. Support from pivot lows (and
    resistance from pivot highs, mirrored). Outcome y=1 if the zone HOLDS:
    price moves 1.5 ATR away before closing 0.5 ATR through it (10 days)."""
    ev = []
    hi, lo, cl, atr = (d[c].to_numpy() for c in ("high", "low", "close", "atr"))
    n = len(d)
    for side in ("S", "R"):
        piv = pivots(d, w, "low" if side == "S" else "high", np.min if side == "S" else np.max)
        confirm = {}
        for i in piv:
            confirm.setdefault(i + w, []).append(i)
        active = {}                       # pivot index -> [level, touches]
        for t in range(20, n - horizon):
            for i in confirm.get(t, []):
                active[i] = [lo[i] if side == "S" else hi[i], 0]
            a = atr[t]
            if not a or np.isnan(a):
                continue
            dead = []
            for i, (L, k) in active.items():
                if side == "S":
                    if cl[t] < L - 0.5 * a:
                        dead.append(i)
                        continue
                    touch = lo[t] <= L + 0.25 * a and lo[t - 1] > L + 0.25 * a and cl[t - 1] > L
                else:
                    if cl[t] > L + 0.5 * a:
                        dead.append(i)
                        continue
                    touch = hi[t] >= L - 0.25 * a and hi[t - 1] < L - 0.25 * a and cl[t - 1] < L
                if not touch:
                    continue
                k += 1
                active[i][1] = k
                held = 0
                for j in range(t + 1, t + horizon + 1):
                    if side == "S":
                        if cl[j] < L - dn * a:
                            break
                        if hi[j] >= L + up * a:
                            held = 1
                            break
                    else:
                        if cl[j] > L + dn * a:
                            break
                        if lo[j] <= L - up * a:
                            held = 1
                            break
                ev.append({"ticker": name, "group": group, "kind": f"touch{side}{i}",
                           "i": t, "date": d.index[t], "touch": k, "y": held})
            for i in dead:
                active.pop(i, None)
            # keep the active set small: drop zones > 4 ATR away
            for i in [i for i, (L, _) in active.items() if abs(cl[t] - L) > 4 * a]:
                active.pop(i)
    return ev


def t2_gap_breakout(name, group, d):
    """Law 2: gapping through a level beats grinding through it.
    Break of the 20-day high (low), gap = opened beyond it. y = continuation."""
    ev = []
    hi, lo, op, cl, fwd = (d[c].to_numpy() for c in ("high", "low", "open", "close", "fwd"))
    for t in range(21, len(d) - H):
        R, S = hi[t - 20:t].max(), lo[t - 20:t].min()
        if cl[t] > R and cl[t - 1] <= R:
            ev.append({"ticker": name, "group": group, "kind": "brk", "i": t, "date": d.index[t],
                       "gap": op[t] > R, "y": fwd[t]})
        elif cl[t] < S and cl[t - 1] >= S:
            ev.append({"ticker": name, "group": group, "kind": "brk", "i": t, "date": d.index[t],
                       "gap": op[t] < S, "y": -fwd[t]})
    return dedupe(ev)


def _r2(y):
    x = np.arange(len(y))
    c = np.corrcoef(x, y)[0, 1]
    return c * c


def t3_slope(name, group, d):
    """Law 3: the character of a 15-day move tells the trade.
    orderly (steady, R^2 >= .75, no range blow-out) -> continuation
    violent (ranges >= 1.5x normal, R^2 < .7)        -> no trend (stand aside)
    absorbed (last 5 days stall vs the first 10)     -> reversal
    y = continuation in the move's direction."""
    ev = []
    cl, tr, atr, fwd = (d[c].to_numpy() for c in ("close", "tr", "atr", "fwd"))
    lc = np.log(cl)
    atr60 = pd.Series(tr).rolling(60).mean().to_numpy()
    for t in range(80, len(d) - H):
        a = atr[t] / cl[t]
        z = (lc[t] - lc[t - 15]) / (a * math.sqrt(15)) if a > 0 else 0
        if abs(z) < 1.5:
            continue
        s = 1 if z > 0 else -1
        r2 = _r2(lc[t - 15:t + 1])
        volexp = tr[t - 14:t + 1].mean() / atr60[t - 15] if atr60[t - 15] else 1
        early = s * (lc[t - 5] - lc[t - 15])
        late = s * (lc[t] - lc[t - 5])
        if volexp >= 1.5 and r2 < 0.7:
            cls = "violent"
        elif late <= 0.25 * early:
            cls = "absorbed"
        elif r2 >= 0.75:
            cls = "orderly"
        else:
            cls = "other"
        ev.append({"ticker": name, "group": group, "kind": cls, "i": t, "date": d.index[t],
                   "cls": cls, "dir": "down" if s < 0 else "up", "y": s * fwd[t]})
    return dedupe(ev)


def t4_fib_close(name, group, d):
    """Law 4: after a 3+ ATR impulse to a 10-day extreme, how deep the next
    5 days retrace grades the move: < 23.6% (9-10/10), 23.6-50% (7/10),
    > 50% (4/10). y = continuation over the following 10 days."""
    ev = []
    hi, lo, cl, atr = (d[c].to_numpy() for c in ("high", "low", "close", "atr"))
    fwd = d["fwd"].to_numpy()
    n = len(d)
    for t in range(11, n - H - 6):
        a = atr[t]
        if hi[t] >= hi[t - 10:t + 1].max():
            base = lo[t - 10:t + 1].min()
            imp = hi[t] - base
            if imp >= 3 * a:
                r = (hi[t] - lo[t + 1:t + 6].min()) / imp
                ev.append({"ticker": name, "group": group, "kind": "fibU", "i": t, "date": d.index[t],
                           "bucket": _fib(r), "y": fwd[t + 5]})
        if lo[t] <= lo[t - 10:t + 1].min():
            top = hi[t - 10:t + 1].max()
            imp = top - lo[t]
            if imp >= 3 * a:
                r = (hi[t + 1:t + 6].max() - lo[t]) / imp
                ev.append({"ticker": name, "group": group, "kind": "fibD", "i": t, "date": d.index[t],
                           "bucket": _fib(r), "y": -fwd[t + 5]})
    return dedupe(ev)


def _fib(r):
    return "strong" if r < 0.236 else "mid" if r <= 0.5 else "weak"


def t5_phase(name, group, d):
    """Master Pattern. (a) breakouts out of a contraction (Bollinger width
    in its lowest 20% of the year within the last 5 days) vs other
    breakouts; (b) entering when extended > 2.5 ATR from the 20-day EMA vs
    0.5-1.5 ATR. y = continuation."""
    ev = []
    hi, lo, cl, atr, fwd, ema = (d[c].to_numpy() for c in ("high", "low", "close", "atr", "fwd", "ema20"))
    c = d["close"]
    bbw = (4 * c.rolling(20).std() / c.rolling(20).mean())
    pct = bbw.rolling(250, min_periods=200).rank(pct=True).to_numpy()
    for t in range(260, len(d) - H):
        R, S = hi[t - 20:t].max(), lo[t - 20:t].min()
        squeeze = np.nanmin(pct[t - 5:t]) <= 0.2
        if cl[t] > R and cl[t - 1] <= R:
            ev.append({"ticker": name, "group": group, "kind": "bo", "i": t, "date": d.index[t],
                       "squeeze": bool(squeeze), "y": fwd[t]})
        elif cl[t] < S and cl[t - 1] >= S:
            ev.append({"ticker": name, "group": group, "kind": "bo", "i": t, "date": d.index[t],
                       "squeeze": bool(squeeze), "y": -fwd[t]})
        ext = (cl[t] - ema[t]) / atr[t]
        if abs(ext) >= 2.5 or 0.5 <= abs(ext) <= 1.5:
            s = 1 if ext > 0 else -1
            ev.append({"ticker": name, "group": group, "kind": "ext" + ("X" if abs(ext) >= 2.5 else "N"),
                       "i": t, "date": d.index[t], "extended": abs(ext) >= 2.5, "y": s * fwd[t]})
    return dedupe(ev)


def t6_sweep(name, group, d):
    """Lesson 6: a sweep under a prior swing low that is reclaimed (close
    back above within a day) is a long signal; a break that holds below for
    2 days is a breakdown. Mirrored for swing highs. y = return in the
    signal's direction, from the entry close."""
    ev = []
    hi, lo, cl, atr, fwd = (d[c].to_numpy() for c in ("high", "low", "close", "atr", "fwd"))
    n = len(d)
    for side, col, fn in (("L", "low", np.min), ("H", "high", np.max)):
        piv = pivots(d, 5, col, fn)
        levels = []
        pi = 0
        for t in range(20, n - H - 3):
            while pi < len(piv) and piv[pi] + 5 <= t:
                levels.append(lo[piv[pi]] if side == "L" else hi[piv[pi]])
                levels = levels[-6:]
                pi += 1
            a = atr[t]
            for L in levels:
                if side == "L":
                    if lo[t] < L - 0.1 * a and cl[t - 1] > L:
                        if cl[t] > L:
                            ev.append(_ev(name, group, "sweep", t, d, fwd[t]))
                        elif cl[t + 1] > L:
                            ev.append(_ev(name, group, "sweep", t + 1, d, fwd[t + 1]))
                        elif cl[t] < L - 0.25 * a and cl[t + 1] < L and cl[t + 2] < L:
                            ev.append(_ev(name, group, "breakdown", t + 2, d, -fwd[t + 2]))
                else:
                    if hi[t] > L + 0.1 * a and cl[t - 1] < L:
                        if cl[t] < L:
                            ev.append(_ev(name, group, "sweep", t, d, -fwd[t]))
                        elif cl[t + 1] < L:
                            ev.append(_ev(name, group, "sweep", t + 1, d, -fwd[t + 1]))
                        elif cl[t] > L + 0.25 * a and cl[t + 1] > L and cl[t + 2] > L:
                            ev.append(_ev(name, group, "breakdown", t + 2, d, fwd[t + 2]))
    return dedupe(ev)


def _ev(name, group, kind, i, d, y):
    return {"ticker": name, "group": group, "kind": kind, "i": i, "date": d.index[i], "y": y}


def third_fridays(start: date, end: date):
    out, y, m = [], start.year, start.month
    while date(y, m, 1) <= end:
        d0 = date(y, m, 15)
        fri = d0 + timedelta(days=(4 - d0.weekday()) % 7)
        out.append(fri)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def t7_opex(name, group, d):
    """Lesson 8. For every day: trading days to the next monthly OPEX (k).
    pin window k = 1-4; shakeout window ~14 calendar days out (k = 7-10);
    quad-witching months shift both (shakeout k = 18-23, pin k = 8-11).
    Measures: |close-to-close move| / ATR (pin = compression), and false
    breakouts: a close beyond the 10-day range that is back inside within
    3 days (shakeout = more fakeouts)."""
    idx = d.index
    days = [x.date() for x in idx]
    fr = third_fridays(days[0], days[-1] + timedelta(days=40))
    # OPEX = last trading day <= third Friday
    opex = []
    j = 0
    for f in fr:
        while j + 1 < len(days) and days[j + 1] <= f:
            j += 1
        if days[j] <= f:
            opex.append(j)
    opex = sorted(set(opex))
    cl, hi, lo, atr = (d[c].to_numpy() for c in ("close", "high", "low", "atr"))
    ev = []
    nxt = 0
    for t in range(11, len(d) - 4):
        while nxt < len(opex) and opex[nxt] < t:
            nxt += 1
        if nxt >= len(opex):
            break
        k = opex[nxt] - t
        quad = days[opex[nxt]].month in (3, 6, 9, 12)
        move = abs(cl[t] - cl[t - 1]) / atr[t - 1] if atr[t - 1] else np.nan
        R, S = hi[t - 10:t].max(), lo[t - 10:t].min()
        fake = None
        if cl[t] > R:
            fake = float(any(cl[t + j2] <= R for j2 in (1, 2, 3)))
        elif cl[t] < S:
            fake = float(any(cl[t + j2] >= S for j2 in (1, 2, 3)))
        if quad:
            win = "pin" if 8 <= k <= 11 else "shake" if 18 <= k <= 23 else "other"
        else:
            win = "pin" if 1 <= k <= 4 else "shake" if 7 <= k <= 10 else "other"
        ev.append({"ticker": name, "group": group, "kind": "day", "i": t, "date": idx[t],
                   "quad": quad, "win": win, "k": k, "move": move, "fake": fake})
    return ev


# ======================================================================
# run
# ======================================================================


# ======================================================================
# placebo (random-walk surrogates with the same bar statistics)
# ======================================================================

def surrogate(df: pd.DataFrame, rng) -> pd.DataFrame:
    """Shuffle 10-day blocks of bars (each bar relative to the prior close)
    and rebuild prices: same volatility and gap/range profile, no patterns."""
    pc = df["close"].shift()
    rel = pd.DataFrame({"o": df["open"] / pc, "h": df["high"] / pc, "l": df["low"] / pc,
                        "c": df["close"] / pc, "v": df["volume"]}).iloc[1:]
    n = len(rel)
    starts = rng.integers(0, max(1, n - 10), size=n // 10 + 1)
    idx = np.concatenate([np.arange(s0, s0 + 10) for s0 in starts])[:n]
    r = rel.iloc[idx].to_numpy()
    c = np.empty(n + 1)
    c[0] = df["close"].iloc[0]
    o, h, l = (np.empty(n) for _ in range(3))
    for k in range(n):
        o[k], h[k], l[k] = c[k] * r[k, 0], c[k] * r[k, 1], c[k] * r[k, 2]
        c[k + 1] = c[k] * r[k, 3]
    out = pd.DataFrame({"open": np.r_[df["open"].iloc[0], o], "high": np.r_[df["high"].iloc[0], h],
                        "low": np.r_[df["low"].iloc[0], l], "close": c,
                        "volume": np.r_[df["volume"].iloc[0], r[:, 4]]}, index=df.index)
    return out


# ======================================================================
# tests
# ======================================================================

SPECS = [
    # name, lesson, claim, metric, events, filter, a, b, field, min effect
    ("Zone touch count", "5 (Law 1)", "First touch of a support/resistance zone holds more often than the 3rd+",
     "hold rate, 1st touch minus 3rd+", "t1", None, lambda e: e["touch"] == 1, lambda e: e["touch"] >= 3, "y", 0.04),
    ("Gap breakout vs grind", "5 (Law 2)", "Breaking a 20-day level on a gap continues better than grinding through",
     "10-day continuation (ATR), gap minus grind", "t2", None, lambda e: e["gap"], lambda e: not e["gap"], "y", 0.15),
    ("Move character: orderly", "5 (Law 3)", "Steep, orderly moves continue",
     "10-day continuation (ATR), orderly minus ordinary trends", "t3", None,
     lambda e: e["cls"] == "orderly", lambda e: e["cls"] == "other", "y", 0.15),
    ("Move character: absorbed", "5 (Law 3)", "Steep-then-stalling moves reverse",
     "10-day continuation (ATR), ordinary minus absorbed", "t3", None,
     lambda e: e["cls"] == "other", lambda e: e["cls"] == "absorbed", "y", 0.15),
    ("Move character: violent", "5 (Law 3)", "Wide, violent moves have no trend (stand aside)",
     "10-day continuation (ATR), ordinary minus violent", "t3", None,
     lambda e: e["cls"] == "other", lambda e: e["cls"] == "violent", "y", 0.15),
    ("Close strength vs Fibonacci", "5 (Law 4)",
     "Moves that hold above the 23.6% retracement continue better than those that lose 50%",
     "10-day continuation (ATR), strong minus weak", "t4", None,
     lambda e: e["bucket"] == "strong", lambda e: e["bucket"] == "weak", "y", 0.15),
    ("Breakout from contraction", "1 (Master Pattern)",
     "Breakouts out of a volatility contraction continue better than other breakouts",
     "10-day continuation (ATR), squeeze minus other", "t5", lambda e: e["kind"] == "bo",
     lambda e: e["squeeze"], lambda e: not e["squeeze"], "y", 0.15),
    ("Late entry (extended)", "1-2 (entries)",
     "Entering > 2.5 ATR from the 20-day EMA does worse than entering 0.5-1.5 ATR away",
     "10-day continuation (ATR), normal minus extended", "t5", lambda e: e["kind"].startswith("ext"),
     lambda e: not e["extended"], lambda e: e["extended"], "y", 0.15),
    ("Stop sweep and reclaim", "6", "A reclaimed sweep of a swing low/high reverses",
     "10-day move in the signal direction (ATR)", "t6", None, lambda e: e["kind"] == "sweep", None, "y", 0.15),
    ("Sweep vs clean breakdown", "6", "Reclaimed sweeps beat breakdowns that hold",
     "signal-direction move (ATR), sweep minus breakdown", "t6", None,
     lambda e: e["kind"] == "sweep", lambda e: e["kind"] == "breakdown", "y", 0.15),
    ("Pin window (monthly OPEX)", "8", "Price compresses in the 1-4 days before monthly expiration",
     "daily |move| / ATR, other days minus pin window", "t7", lambda e: not e["quad"],
     lambda e: e["win"] == "other", lambda e: e["win"] == "pin", "move", 0.03),
    ("Shakeout window (monthly OPEX)", "8", "Breakouts ~2 weeks before monthly expiration fail more often",
     "false-breakout rate, shakeout minus other days", "t7", lambda e: not e["quad"] and e["fake"] is not None,
     lambda e: e["win"] == "shake", lambda e: e["win"] == "other", "fake", 0.04),
    ("Pin window (quad witching)", "8", "Price compresses ~2 weeks before quad witching",
     "daily |move| / ATR, other days minus pin window", "t7", lambda e: e["quad"],
     lambda e: e["win"] == "other", lambda e: e["win"] == "pin", "move", 0.03),
    ("Shakeout window (quad witching)", "8", "Breakouts ~1 month before quad witching fail more often",
     "false-breakout rate, shakeout minus other days", "t7", lambda e: e["quad"] and e["fake"] is not None,
     lambda e: e["win"] == "shake", lambda e: e["win"] == "other", "fake", 0.04),
]

FNS = {"t1": t1_zone_touches, "t2": t2_gap_breakout, "t3": t3_slope, "t4": t4_fib_close,
       "t5": t5_phase, "t6": t6_sweep, "t7": t7_opex}


def events(data, keys=None, **kw):
    E = {k: [] for k in FNS}
    for name, (group, df) in data.items():
        d = prep(df)
        for k, fn in FNS.items():
            if keys is None or k in keys:
                E[k] += fn(name, group, d, **kw.get(k, {}))
    return E


def stats(E, specs=SPECS):
    out = {}
    for name, _, _, _, key, flt, a, b, field, _ in specs:
        ev = [e for e in E[key] if flt is None or flt(e)]
        out[name] = {g: summarize(ev, g, a, b, field=field) for g in ("INDEX", "BROAD")}
    return out


def excess(real, placebos):
    """Real effect minus the mean placebo effect, with a t that includes
    the placebo's own spread."""
    out = {}
    for name, groups in real.items():
        out[name] = {}
        for g, r in groups.items():
            def one(rr, path):
                eff, t = rr.get("effect"), rr.get("t")
                ps = [_get(p[name][g], path) for p in placebos]
                ps = [x for x in ps if x is not None]
                if eff is None or t is None or not ps or t == 0:
                    return {"effect": None, "t": None, "n": rr.get("n"), "raw": eff, "placebo": None}
                se = abs(eff / t)
                pm = float(np.mean(ps))
                psd = float(np.std(ps, ddof=1)) if len(ps) > 1 else 0.0
                se_c = math.sqrt(se * se + psd * psd / len(ps) + psd * psd)
                return {"effect": eff - pm, "t": (eff - pm) / se_c if se_c > 0 else None,
                        "n": rr.get("n"), "raw": eff, "placebo": pm}
            res = one(r, ())
            res["half1"] = one(r["half1"], ("half1",))
            res["half2"] = one(r["half2"], ("half2",))
            out[name][g] = res
    return out


def _get(d, path):
    for p in path:
        d = d.get(p, {})
    return d.get("effect")


def run(data, k_placebo=5, seed=11):
    real = stats(events(data))
    rng = np.random.default_rng(seed)
    placebos = []
    for _ in range(k_placebo):
        sur = {n: (g, surrogate(df, rng)) for n, (g, df) in data.items()}
        placebos.append(stats(events(sur)))
    X = excess(real, placebos)
    R = []
    for name, lesson, claim, metric, *_rest in SPECS:
        min_eff = _rest[-1]
        g, why = grade(X[name]["INDEX"], X[name]["BROAD"], min_eff)
        R.append({"test": name, "lesson": lesson, "claim": claim, "metric": metric,
                  "index": X[name]["INDEX"], "broad": X[name]["BROAD"], "grade": g, "why": why,
                  "raw_index": real[name]["INDEX"], "raw_broad": real[name]["BROAD"]})
    R.append({"test": "Open-interest wall shapes", "lesson": "7",
              "claim": "One-sided walls V-reverse; stacked walls chop; sloping walls give an ABC path",
              "metric": "needs daily open-interest history", "index": {}, "broad": {}, "grade": "UNTESTED",
              "why": "only 35 sessions of stored chains (2 monthly expirations): too few to test"})
    counts = {g: sum(1 for (gg, _) in data.values() if gg == g) for g in ("INDEX", "BROAD")}
    return R, counts


def robustness(data, k_placebo=3, seed=23):
    """Zone touches under other settings (placebo-adjusted)."""
    rows = []
    rng = np.random.default_rng(seed)
    surs = [{n: (g, surrogate(df, rng)) for n, (g, df) in data.items()} for _ in range(k_placebo)]
    spec = [x for x in SPECS if x[0] == "Zone touch count"]
    for w, up, dn in ((3, 1.5, 0.5), (8, 1.5, 0.5), (5, 1.0, 0.5), (5, 2.0, 0.75), (5, 1.5, 0.25)):
        kw = {"t1": {"w": w, "up": up, "dn": dn}}
        real = stats(events(data, keys={"t1"}, **kw), spec)
        pl = [stats(events(s, keys={"t1"}, **kw), spec) for s in surs]
        X = excess(real, pl)["Zone touch count"]
        rows.append({"test": f"zone touch: pivot {w}, hold {up} ATR, break {dn} ATR",
                     "index": X["INDEX"], "broad": X["BROAD"]})
    return rows


def fmt(x, p=3):
    return "–" if x is None else (f"{x:+.{p}f}" if isinstance(x, float) else str(x))


def report(R, counts, rob, outdir):
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "results.json"), "w") as f:
        json.dump({"counts": counts, "results": R, "robustness": rob}, f, indent=1, default=str)
    L = ["# Course pattern backtest", "",
         f"Index series: {counts['INDEX']} (SPY, QQQ, IWM, SPX 1985-93) · broad: {counts['BROAD']} tickers · "
         f"horizon {H} days · ATR units, drift-adjusted · effects are EXCESS over a random-walk placebo", "",
         "| Test | Lesson | Grade | Index excess (t, n) [raw / placebo] | halves | Broad excess (t, n) |",
         "|---|---|---|---|---|---|"]
    for r in R:
        i, b = r["index"], r["broad"]
        L.append(f"| {r['test']} | {r['lesson']} | **{r['grade']}** | {fmt(i.get('effect'))} "
                 f"(t {fmt(i.get('t'), 1)}, n {i.get('n', '–')}) [{fmt(i.get('raw'))} / {fmt(i.get('placebo'))}] | "
                 f"{fmt((i.get('half1') or {}).get('effect'))} / {fmt((i.get('half2') or {}).get('effect'))} | "
                 f"{fmt(b.get('effect'))} (t {fmt(b.get('t'), 1)}, n {b.get('n', '–')}) |")
    L += ["", "## Zone-touch robustness (placebo-adjusted)", "",
          "| Variant | Index excess (t, n) | halves | Broad excess (t, n) |", "|---|---|---|---|"]
    for r in rob:
        i, b = r["index"], r["broad"]
        L.append(f"| {r['test']} | {fmt(i.get('effect'))} (t {fmt(i.get('t'), 1)}, n {i.get('n')}) | "
                 f"{fmt(i['half1'].get('effect'))} / {fmt(i['half2'].get('effect'))} | "
                 f"{fmt(b.get('effect'))} (t {fmt(b.get('t'), 1)}, n {b.get('n')}) |")
    L += ["", "## Notes"] + [f"- **{r['test']}**: {r['claim']}. {r['metric']}. → {r['grade']}: {r['why']}." for r in R]
    with open(os.path.join(outdir, "results.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default=None, help="offline CSVs (tests)")
    p.add_argument("--out", default="results")
    p.add_argument("--placebo", type=int, default=5)
    a = p.parse_args()
    data = load(a.data_dir)
    print(f"loaded {len(data)} series")
    R, counts = run(data, k_placebo=a.placebo)
    rob = robustness(data)
    report(R, counts, rob, a.out)


if __name__ == "__main__":
    main()
