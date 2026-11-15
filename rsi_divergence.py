#!/usr/bin/env python3
"""
RSI divergence across timeframes (G10, the RSI divergence gate).

Canonical copy. It needs nothing but the standard library, so the agents can
vendor it byte for byte, as they do vol_surface.py.

Definitions (the standard ones):

  BULLISH   price makes a LOWER LOW while RSI(14) makes a HIGHER LOW.
  BEARISH   price makes a HIGHER HIGH while RSI(14) makes a LOWER HIGH.

A divergence is read between two CONSECUTIVE swing pivots of the same kind. A
swing high (low) is a bar whose high (low) is the extreme of the k bars on each
side and strictly beyond the k bars before it. A pivot is only known k bars
after it printed, so nothing here looks ahead: the signal exists from the
confirmation bar of the second pivot.

Timeframes are graded together, longer frames weighing more:

    1W weight 3   1D weight 2   1H weight 1

The grade is about agreement with the trade's direction (up bias wants bullish,
down bias wants bearish):

    A  all three timeframes show the divergence the trade wants
    B  weekly and daily show it, hourly shows nothing
    C  partial support, nothing material against it
    D  no support, or the timeframes disagree
    F  the divergence against the trade outweighs the one for it

"Chop" has no grade: a divergence is information at the edge of a range, not a
reason to trade it.

Calibration (read before trusting any number below). SPY, QQQ and IWM daily
bars 2016-01-04..2026-11-13 and hourly bars 2025-12-22..2026-11-13 (Robinhood,
regular hours, interpolated bars dropped), weekly bars built from the daily.
The window for each timeframe is the span from the 10th to the 90th percentile
of the spacing between pivots that produced a divergence, the middle 80% of
what the tape actually produces:

    timeframe  pivot k   spacing window (bars)   median spacing (bull / bear)
    1H         3         4 - 14                  7 / 9
    1D         3         5 - 16                  7 / 9
    1W         2         4 - 12                  6 / 7.5

What the forward returns say, honestly:

  - BULLISH divergence on the daily chart (n=46 events, about 28 independent
    months because the three ETFs signal together): 20-day return +2.4% against
    a +1.2% baseline. The 5- and 10-day returns are close to the baseline.
  - BEARISH divergence on the daily chart (n=124): no downside edge. Prices
    rose after 62-70% of signals, in line with the baseline. In a bull market a
    bearish divergence is a weak warning, not a sell signal.
  - Splitting by spacing did NOT produce a reliable ranking: inside the window
    the forward returns do not line up with spacing. So the window selects the
    most probable pattern; it is not a quality score.
  - Hourly has only 76 events from 11 months and weekly only 57 from 10 years.
    Neither timeframe has enough events to calibrate returns at all. Their
    windows come from spacing alone.

So the grade measures how many timeframes AGREE, which is what the pattern is
defined by. It is not a probability of profit, and it is advisory: it appears on
every ticket and never blocks or resizes a trade. Every number above is a
starting value, not a backtested edge.

One rule that was not in the backtest: a divergence whose second pivot has been
undercut (bullish) or exceeded (bearish) since is BROKEN and no longer counts,
because the next pivot has already replaced it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

RSI_PERIOD = 14

# k = bars on each side of a pivot; window = allowed bars between the two
# pivots; fresh = bars after confirmation that the signal stays live;
# weight = vote in the grade.
TIMEFRAMES = {
    "1H": {"k": 3, "window": (4, 14), "fresh": 14, "weight": 1},
    "1D": {"k": 3, "window": (5, 16), "fresh": 10, "weight": 2},
    "1W": {"k": 2, "window": (4, 12), "fresh": 4, "weight": 3},
}
ORDER = ("1W", "1D", "1H")  # longest first

EVIDENCE = {
    "bull": "daily 20d return +2.4% vs +1.2% baseline (n=46, ~28 independent); "
            "5d/10d near baseline",
    "bear": "no downside edge in 2016-2026 (n=124): prices rose after 62-70% "
            "of signals",
}


# ---------------------------------------------------------------- indicators

def rsi(closes, period: int = RSI_PERIOD):
    """Wilder RSI. Same length as `closes`; None until `period` changes exist.
    Seeded like an exponential average with alpha 1/period from the first
    change, which is what charting packages show once a few bars have passed."""
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    alpha = 1.0 / period
    au = ad = None
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        up, dn = max(ch, 0.0), max(-ch, 0.0)
        if au is None:
            au, ad = up, dn
        else:
            au = alpha * up + (1 - alpha) * au
            ad = alpha * dn + (1 - alpha) * ad
        if i >= period:
            out[i] = 100.0 if ad == 0 else 100.0 - 100.0 / (1.0 + au / ad)
    return out


def pivots(highs, lows, k: int):
    """Confirmed swing pivots as (high_indexes, low_indexes). A pivot at i is
    the extreme of bars i-k..i+k and strictly beyond bars i-k..i-1."""
    n = len(highs)
    ph, pl = [], []
    for i in range(k, n - k):
        if highs[i] == max(highs[i - k:i + k + 1]) and highs[i] > max(highs[i - k:i]):
            ph.append(i)
        if lows[i] == min(lows[i - k:i + k + 1]) and lows[i] < min(lows[i - k:i]):
            pl.append(i)
    return ph, pl


# ---------------------------------------------------------------- detection

def detect(highs, lows, closes, tf: str):
    """Live divergence on one timeframe, from COMPLETED bars (oldest first).

    Returns a dict. `type` is "bull", "bear" or None; `reason` explains a None
    when a pattern was found but did not qualify."""
    cfg = TIMEFRAMES[tf]
    k, (wmin, wmax), fresh = cfg["k"], cfg["window"], cfg["fresh"]
    n = len(closes)
    base = {"tf": tf, "type": None, "bars": n, "reason": "no divergence"}
    if not (len(highs) == len(lows) == n):
        raise ValueError("highs, lows and closes must be the same length")
    if n < RSI_PERIOD + 2 * k + 2:
        return {**base, "reason": f"only {n} bars, need {RSI_PERIOD + 2 * k + 2}"}
    r = rsi(closes)
    ph, pl = pivots(highs, lows, k)
    found, notes = [], []
    for kind, piv in (("bear", ph), ("bull", pl)):
        if len(piv) < 2:
            continue
        a, b = piv[-2], piv[-1]
        if r[a] is None or r[b] is None:
            continue
        if kind == "bear":
            diverges = highs[b] > highs[a] and r[b] < r[a]
            broken = max(highs[b + 1:], default=highs[b]) > highs[b]
            p1, p2 = highs[a], highs[b]
        else:
            diverges = lows[b] < lows[a] and r[b] > r[a]
            broken = min(lows[b + 1:], default=lows[b]) < lows[b]
            p1, p2 = lows[a], lows[b]
        if not diverges:
            continue
        spacing, age = b - a, (n - 1) - (b + k)
        if not (wmin <= spacing <= wmax):
            notes.append(f"{kind} outside the {wmin}-{wmax} bar window ({spacing})")
            continue
        if age > fresh:
            notes.append(f"{kind} is stale ({age} bars old, limit {fresh})")
            continue
        if broken:
            notes.append(f"{kind} broken: price took out the second pivot")
            continue
        found.append({
            "tf": tf, "type": kind, "bars": n, "spacing": spacing,
            "age": age, "confirmed_index": b + k,
            "price1": p1, "price2": p2,
            "price_change_pct": (p2 / p1 - 1) * 100,
            "rsi1": r[a], "rsi2": r[b],
            "rsi_change": r[b] - r[a],
            "rsi_now": r[-1],
            "reason": "",
        })
    if not found:
        return {**base, "reason": "; ".join(notes) or "no divergence"}
    return max(found, key=lambda f: f["confirmed_index"])


def analyze(frames):
    """frames: {"1H": bars, "1D": bars, "1W": bars}; a missing or None entry is
    a timeframe with no data. Each bars is a list of (high, low, close), oldest
    first, completed bars only. Returns {tf: detect() result or None}."""
    out = {}
    for tf in TIMEFRAMES:
        bars = frames.get(tf)
        if not bars:
            out[tf] = None
            continue
        out[tf] = detect([b[0] for b in bars], [b[1] for b in bars],
                         [b[2] for b in bars], tf)
    return out


# ------------------------------------------------------------------- grading

def grade(analysis, bias: str):
    """Grade the divergence picture against the trade's bias ("up"/"down").

    Returns {"grade", "aligned", "opposed", "missing", "weight_aligned",
    "weight_opposed", "note"}. Chop returns grade None."""
    bias = (bias or "").lower()
    missing = [tf for tf in ORDER if analysis.get(tf) is None]
    if bias not in ("up", "down"):
        return {"grade": None, "aligned": [], "opposed": [], "missing": missing,
                "weight_aligned": 0, "weight_opposed": 0,
                "note": "chop: divergence is information, not a grade"}
    want = "bull" if bias == "up" else "bear"
    aligned, opposed = [], []
    for tf in ORDER:
        s = analysis.get(tf)
        if not s or not s.get("type"):
            continue
        (aligned if s["type"] == want else opposed).append(tf)
    wa = sum(TIMEFRAMES[t]["weight"] for t in aligned)
    wo = sum(TIMEFRAMES[t]["weight"] for t in opposed)
    net = wa - wo
    if net < 0:
        g, note = "F", "divergence against the trade outweighs the one for it"
    elif wo > 0:
        if net >= 3:
            g, note = "C", "higher timeframes outweigh a disagreeing one"
        else:
            g, note = "D", "timeframes disagree"
    elif wa == 6:
        g, note = "A", "weekly, daily and hourly all agree"
    elif wa == 5:
        g, note = "B", "weekly and daily agree, hourly shows nothing"
    elif wa >= 2:
        g, note = "C", "partial agreement, nothing against it"
    elif wa == 1:
        g, note = "D", "hourly only"
    else:
        g, note = "D", "no divergence on any timeframe"
    if missing and g in ("A", "B"):
        note += f" (no data: {', '.join(missing)})"
    elif missing:
        note += f"; no data for {', '.join(missing)}"
    return {"grade": g, "aligned": aligned, "opposed": opposed,
            "missing": missing, "weight_aligned": wa, "weight_opposed": wo,
            "note": note}


def ticket_line(analysis, bias: str) -> str:
    """One line for the ticket, e.g.
    'RSI div B (up): 1W bull 7w RSI 38>44 | 1D bull 3d | 1H none  [weekly and
    daily agree, hourly shows nothing]'"""
    g = grade(analysis, bias)
    parts = []
    for tf in ORDER:
        s = analysis.get(tf)
        if s is None:
            parts.append(f"{tf} no data")
        elif not s.get("type"):
            parts.append(f"{tf} none")
        else:
            parts.append(f"{tf} {s['type']} {s['spacing']} bars apart "
                         f"RSI {s['rsi1']:.0f}>{s['rsi2']:.0f}, "
                         f"confirmed {s['age']} bars ago")
    head = f"RSI div {g['grade']} ({bias})" if g["grade"] else f"RSI div ({bias})"
    return f"{head}: " + " | ".join(parts) + f"  [{g['note']}]"


# ---------------------------------------------------------------- bar helpers

def from_robinhood(bars):
    """Rows from get_equity_historicals -> [(high, low, close)], dropping the
    interpolated bars the feed synthesizes to fill gaps (they carry no
    information and flatten the RSI)."""
    out = []
    for b in bars:
        if b.get("interpolated"):
            continue
        out.append((float(b["high_price"]), float(b["low_price"]),
                    float(b["close_price"])))
    return out


def _day(d):
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def weekly_from_daily(daily, asof=None):
    """Daily rows [(date, high, low, close)] -> completed weekly
    [(high, low, close)]. A week is complete once its Friday has passed, or the
    last bar we have is that Friday; the week in progress is dropped, because a
    half-formed weekly bar changes the RSI and would let a pivot print early."""
    weeks, order = {}, []
    for d, h, l, c in daily:
        d = _day(d)
        key = d.isocalendar()[:2]
        if key not in weeks:
            weeks[key] = []
            order.append(key)
        weeks[key].append((d, h, l, c))
    asof = _day(asof) if asof is not None else _day(daily[-1][0])
    out = []
    for key in order:
        rows = weeks[key]
        friday = rows[0][0] - timedelta(days=rows[0][0].weekday()) + timedelta(days=4)
        if rows[-1][0] < friday and asof <= friday:
            continue  # still in progress
        out.append((max(r[1] for r in rows), min(r[2] for r in rows), rows[-1][3]))
    return out


# ------------------------------------------------------------------------ CLI

def _read_csv(path):
    import csv
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            low = {k.strip().lower(): v for k, v in row.items()}
            rows.append((low.get("date") or low.get("begins_at") or low.get("time"),
                         float(low.get("high") or low["high_price"]),
                         float(low.get("low") or low["low_price"]),
                         float(low.get("close") or low["close_price"])))
    return rows


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="RSI divergence grade from bar CSVs")
    p.add_argument("--daily", help="CSV with date,high,low,close (completed bars)")
    p.add_argument("--hourly", help="CSV with time,high,low,close (completed bars)")
    p.add_argument("--bias", choices=["up", "down", "chop"], required=True)
    a = p.parse_args(argv)
    frames = {}
    if a.daily:
        daily = _read_csv(a.daily)
        frames["1D"] = [(h, l, c) for _, h, l, c in daily]
        frames["1W"] = weekly_from_daily(daily)
    if a.hourly:
        frames["1H"] = [(h, l, c) for _, h, l, c in _read_csv(a.hourly)]
    print(ticket_line(analyze(frames), a.bias))


if __name__ == "__main__":
    main()
