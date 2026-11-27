"""
Credit stress: the HYG / IEF ratio against its own trailing 60 sessions.

A port of Eugenio's thinkScript study:

    def r = close("HYG") / close("IEF");
    def z = (r - Average(r, 60)) / StDev(r, 60);
    plot Lo = -2;     # z below -2 painted red

High yield (HYG) underperforming duration-matched Treasuries (IEF) is the
market pricing more default risk. The ratio strips out the rate move both
share, so what is left is the credit spread's direction. z < -2 means the
ratio is two standard deviations under its 60-session mean: credit is
stressed relative to its own recent history.

thinkScript's StDev is the population standard deviation (divide by n), so
this uses the same; a sample stdev would put z about 0.8% closer to zero at
n = 60 and move the -2 crossing by a session now and then.

Information only. Not a gate in any agent until it has been back-tested;
the block says so.

Nothing is written to history/. The closes are read fresh (tastytrade
first, Yahoo second, via index_history) and the computed block is published
in docs/latest.json, which is also the cache: a block younger than
MAX_AGE_H is reused, and a failed fetch keeps the previous block marked
stale rather than dropping it.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone

NUM, DEN = "HYG", "IEF"
LEN = 60                 # thinkScript input len
STRESS_Z = -2.0          # the study's Lo line
WATCH_Z = -1.0
TREND_LOOKBACK = 5       # sessions, for the trend label
TREND_STEP = 0.5         # z move over TREND_LOOKBACK that counts as a trend
HISTORY_POINTS = 60      # z values carried in the feed for the sparkline
MAX_AGE_H = 6.0
FETCH_RANGE = "1y"       # 60 for the first z, 60 more for the history

_MEMO: dict = {}         # one fetch per process (dashboard + feed both ask)


def ratio_series(num: tuple, den: tuple) -> tuple[list, list]:
    """Align two (dates, closes) series on date; return (dates, num/den)."""
    nd, nc = num
    dd, dc = den
    dmap = dict(zip(dd, dc))
    dates, ratios = [], []
    for d, c in zip(nd, nc):
        v = dmap.get(d)
        if v and c and v > 0 and c > 0:
            dates.append(d)
            ratios.append(c / v)
    return dates, ratios


def zscores(values: list, n: int = LEN) -> list:
    """Rolling z of each value against the n values ending at it (itself
    included, as Average/StDev do). None until n values exist or if flat."""
    out = []
    for i in range(len(values)):
        if i + 1 < n:
            out.append(None)
            continue
        w = values[i + 1 - n:i + 1]
        mean = sum(w) / n
        var = sum((x - mean) ** 2 for x in w) / n          # population, as TOS
        sd = math.sqrt(var)
        out.append((values[i] - mean) / sd if sd > 0 else None)
    return out


def state(dates: list, ratios: list, n: int = LEN) -> dict | None:
    """The credit-stress block from aligned dates and ratios."""
    z = zscores(ratios, n)
    pts = [(d, r, v) for d, r, v in zip(dates, ratios, z) if v is not None]
    if not pts:
        return None
    d_now, r_now, z_now = pts[-1]
    w = ratios[-n:]
    mean = sum(w) / n
    zs = [v for _, _, v in pts]

    def ago(k):
        return zs[-1 - k] if len(zs) > k else None

    z5 = ago(TREND_LOOKBACK)
    z20 = ago(20)
    chg5 = z_now - z5 if z5 is not None else None
    if chg5 is None:
        trend = "unknown"
    elif chg5 <= -TREND_STEP:
        trend = "deteriorating"
    elif chg5 >= TREND_STEP:
        trend = "improving"
    else:
        trend = "steady"

    below = 0
    for v in reversed(zs):
        if v < STRESS_Z:
            below += 1
        else:
            break
    last_cross = None                      # newest session z crossed under -2
    for i in range(len(pts) - 1, 0, -1):
        if pts[i][2] < STRESS_Z <= pts[i - 1][2]:
            last_cross = pts[i][0]
            break

    if z_now < STRESS_Z:
        label = "stress"
    elif z_now < WATCH_Z:
        label = "watch"
    else:
        label = "calm"

    tail = pts[-HISTORY_POINTS:]
    return {
        "pair": f"{NUM}/{DEN}",
        "len": n,
        "threshold": STRESS_Z,
        "asof": d_now,
        "ratio": round(r_now, 6),
        "ratio_mean": round(mean, 6),
        "ratio_vs_mean_pct": round((r_now / mean - 1) * 100, 3),
        "z": round(z_now, 3),
        "z_5d_ago": round(z5, 3) if z5 is not None else None,
        "z_20d_ago": round(z20, 3) if z20 is not None else None,
        "z_change_5d": round(chg5, 3) if chg5 is not None else None,
        "z_min_20d": round(min(zs[-20:]), 3),
        "state": label,
        "trend": trend,
        "sessions_below_threshold": below,
        "last_cross_below": last_cross,
        "history": [{"date": d, "z": round(v, 3)} for d, _, v in tail],
        "note": "information only -- not a gate until back-tested",
    }


def _fetch(symbol: str):
    import index_history as ixh
    return ixh.daily_closes(symbol, FETCH_RANGE, min_bars=LEN + 1)


def credit_block(docsdir: str | None = None, fetch=None, now=None) -> dict | None:
    """The block for docs/latest.json market.credit_stress (see module doc)."""
    now = now or datetime.now(timezone.utc)
    if fetch is None and "block" in _MEMO:
        return _MEMO["block"]
    fetch = fetch or _fetch
    prev = None
    if docsdir:
        try:
            with open(os.path.join(docsdir, "latest.json"), encoding="utf-8") as fh:
                prev = (json.load(fh).get("market") or {}).get("credit_stress")
        except (OSError, ValueError):
            prev = None
    if prev and prev.get("fetched_utc") and not prev.get("stale"):
        try:
            age = (now - datetime.fromisoformat(prev["fetched_utc"])).total_seconds() / 3600
            if 0 <= age < MAX_AGE_H:
                _MEMO["block"] = prev
                return prev
        except (ValueError, TypeError):
            pass
    try:
        series = {}
        for sym in (NUM, DEN):
            d, c = fetch(sym)
            if not c:
                raise ValueError(f"{sym}: no usable history")
            series[sym] = (d, c)
        dates, ratios = ratio_series(series[NUM], series[DEN])
        out = state(dates, ratios)
        if out is None:
            raise ValueError(f"fewer than {LEN} aligned sessions")
        out.update({"fetched_utc": now.isoformat(), "stale": False,
                    "source": "index_history daily closes, tastytrade first (read fresh, not stored)"})
    except Exception as ex:                          # network, schema, anything
        print(f"credit stress: fetch failed ({ex})")
        out = {**prev, "stale": True} if prev else None
    _MEMO["block"] = out
    return out


def text(b: dict | None) -> str:
    """One line for reports and the agent."""
    if not b:
        return "Credit (HYG/IEF z60): unavailable"
    chg = b.get("z_change_5d")
    s = (f"Credit (HYG/IEF z60): {b['z']:+.2f} {b['state'].upper()}, {b['trend']}"
         + (f" ({chg:+.2f} over 5d)" if chg is not None else "")
         + f"; ratio {b['ratio_vs_mean_pct']:+.2f}% vs 60d mean")
    if b.get("sessions_below_threshold"):
        s += f"; {b['sessions_below_threshold']} session(s) below -2"
    if b.get("stale"):
        s += " [stale]"
    return s


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    blk = credit_block(os.path.join(here, "docs"))
    print(text(blk))
    if blk:
        for h in blk["history"][-10:]:
            print(f"  {h['date']}  {h['z']:+.2f}")
