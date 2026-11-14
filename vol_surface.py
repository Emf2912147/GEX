#!/usr/bin/env python3
"""
Volatility surface across expirations (the term-structure axis).

Canonical copy. gex-trading-agent vendors this file unchanged as
ticker_analysis/vol_surface.py; the EMF-gex-agent imports it from this folder.
It needs nothing but the standard library, so it runs anywhere the agents do.

It answers four questions the single-number vol fields cannot:

  1. EXPIRY SELECTION  Inside a DTE window, which expiry is cheapest to buy
                       and which is richest to sell?            pick_expiry()
  2. CALENDAR GATE     Is the near leg rich or cheap against the FORWARD vol
                       between the two expiries (what a calendar or diagonal
                       really buys)?                            calendar_gate()
  3. EVENT DETECTION   Which expiries carry a jump in forward vol, i.e. a
                       priced event (FOMC, CPI, earnings)?      analyze()["events"]
  4. LADDER TIMING     Are long-dated index options cheap against their own
                       history (VIX3M / VIX6M / VIX1Y)?         long_vol_state()

Method, read before trusting a number:

  - Input is one ATM implied vol per expiry. Total variance w = iv^2 * T uses
    CALENDAR time (T = dte / 365), because that is the basis the IV is quoted
    on.
  - Everything BETWEEN expiries uses a VARIANCE clock: one unit per market
    session, CLOSED_DAY_WEIGHT per weekend day or market holiday. On a
    calendar clock every weekend looks like a cheap interval; on a pure
    trading clock every Monday expiry looks rich. Early closes count as full
    sessions.
  - Forward vol from expiry A to B = sqrt((w_B - w_A) / (tt_B - tt_A) * year),
    tt in variance-clock units.
    Negative forward variance means the quotes disagree (or the near expiry
    is extremely bid); it is reported as None, never clipped to zero.
  - rich_rel compares an expiry with a straight line fitted through the total
    variance of its NEIGHBOURS (leave-one-out, ordinary least squares in
    trading time). A straight line is a constant forward vol, so the residual
    is the bump that expiry carries over its neighbours: +0.03 = 3% richer.
    It removes the slope of the curve on purpose: contango alone does not
    make the near expiry "cheap".
  - An event is a short interval (<= EVENT_MAX_GAP trading days) whose forward
    vol is EVENT_RATIO times the median forward vol of the front of the curve
    and at least EVENT_MIN_PTS above it. With weekly expiries a one-day event
    is diluted over five days and may not show: "no event detected" is not
    "no event". The agents' own event calendar still applies.
  - A thin chain whose expiries scatter around their immediate neighbours
    (median roughness above NOISY_SURFACE) is marked noisy: no expiry is ranked, no
    event is flagged and the calendar gate reports "not measurable".

Every threshold below is a starting value chosen from 57 stored sessions
(2026-09-05 to 2026-11-13). None has been backtested against outcomes.
Treat the outputs as information for expiry choice, not as a trade signal.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

TRADING_DAYS = 252
# Variance clock: a weekend day or holiday carries this share of a session's
# variance. 0.1 equalises Monday and mid-week one-day forward vols on SPX daily
# expiries over the 55 stored sessions (0.0 leaves Mondays 10% rich, 0.2
# leaves them 7% cheap).
CLOSED_DAY_WEIGHT = 0.1
# One year on the variance clock (252 sessions + 113 closed days).
VARIANCE_YEAR = 252 + 113 * CLOSED_DAY_WEIGHT

# rich_rel beyond these is labelled rich / cheap against the neighbours.
RICH_REL = 0.02
CHEAP_REL = -0.02
# score_adjustment() ignores richness smaller than this (ordinary scatter:
# the middle 60% of expiries on the stored index chains sit inside it).
ADJ_DEAD_BAND = 0.01
# Two expiries within this much of each other count as a tie.
TIE_REL = 0.005

# Expiries closer than this many sessions are not ranked (too few hours left
# for a per-expiry IV to mean much).
MIN_RANK_BD = 3
# Neighbourhood for the leave-one-out fit, in trading days each side.
FIT_MIN_SPAN = 12
FIT_SPAN_FRAC = 0.5
FIT_MIN_POINTS = 3

# A chain whose expiries scatter this much around the line between their two
# immediate neighbours (median, as a share of IV) is too thin to rank.
NOISY_SURFACE = 0.012

# Event detection.
EVENT_MAX_GAP = 5          # interval length, trading days
EVENT_FRONT_BD = 45        # events are looked for, and the baseline (median
                           # forward vol) is taken, out to this many sessions
EVENT_RATIO = 1.20
EVENT_MIN_PTS = 0.025
EVENT_REBOUND = 0.90       # previous interval below this share of the baseline
                           # = the spike may only be a rebound from a cheap expiry
# An expiry is said to carry event premium when an event interval ends
# within this many trading days before it (inclusive).
EVENT_NEAR_BD = 5

# Calendar / diagonal gate: near-leg vol (trading-time) over the forward vol.
CAL_FRONT_CHEAP = 0.80
CAL_FRONT_RICH = 1.15

# Long-dated vol against its own trailing year.
LONG_VOL_CHEAP_PCT = 30
LONG_VOL_RICH_PCT = 70
LONG_VOL_WINDOW = 252
LONG_VOL_TENOR_DAYS = {"vix3m": 93, "vix6m": 184, "vix1y": 365}


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def _as_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    day = (h + l - 7 * m + 33 * month + 19) % 32
    return date(year, month, day)


def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(days=7 * (n - 1))


def _last_weekday(year, month, weekday):
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


_HOLIDAY_CACHE: dict = {}


def market_holidays(year: int) -> frozenset:
    """Full-day US equity market holidays (NYSE rules). Early closes count as
    ordinary sessions."""
    if year not in _HOLIDAY_CACHE:
        days = {
            _observed(date(year, 1, 1)),
            _nth_weekday(year, 1, 0, 3),                 # Martin Luther King Jr.
            _nth_weekday(year, 2, 0, 3),                 # Presidents
            _easter(year) - timedelta(days=2),           # Good Friday
            _last_weekday(year, 5, 0),                   # Memorial
            _observed(date(year, 6, 19)),                # Juneteenth
            _observed(date(year, 7, 4)),
            _nth_weekday(year, 9, 0, 1),                 # Labor
            _nth_weekday(year, 11, 3, 4),                # Thanksgiving
            _observed(date(year, 12, 25)),
        }
        # New Year's Day on a Saturday is not observed on the Friday before.
        if date(year, 1, 1).weekday() == 5:
            days.discard(date(year - 1, 12, 31))
        _HOLIDAY_CACHE[year] = frozenset(d for d in days if d.weekday() < 5)
    return _HOLIDAY_CACHE[year]


def is_session(d: date) -> bool:
    return d.weekday() < 5 and d not in market_holidays(d.year)


def trading_days(asof, expiry) -> int:
    """Market sessions after `asof` up to and including `expiry`."""
    a, e = _as_date(asof), _as_date(expiry)
    n, d = 0, a
    while d < e:
        d += timedelta(days=1)
        n += is_session(d)
    return n


def variance_time(asof, expiry) -> float:
    """
    Trading-time units from `asof` to `expiry`: 1.0 per market session and
    CLOSED_DAY_WEIGHT per weekend day or holiday. Markets price a little
    variance into closed days (news still arrives), so counting them as zero
    makes every Monday expiry look rich and counting them as full days makes
    it look cheap.
    """
    a, e = _as_date(asof), _as_date(expiry)
    t, d = 0.0, a
    while d < e:
        d += timedelta(days=1)
        t += 1.0 if is_session(d) else CLOSED_DAY_WEIGHT
    return t


# --------------------------------------------------------------------------
# Term structure
# --------------------------------------------------------------------------

def _fit_line(xs, ys, x):
    """Ordinary least squares y = a + b*x through the points, evaluated at x."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((v - mx) ** 2 for v in xs)
    if sxx <= 0:
        return None
    b = sum((u - mx) * (v - my) for u, v in zip(xs, ys)) / sxx
    return my + b * (x - mx)


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def forward_vol(w1: float, tt1: float, w2: float, tt2: float) -> float | None:
    """Forward vol between two expiries on the variance clock (tt = units
    from variance_time()), None if not defined."""
    if tt2 <= tt1:
        return None
    fv = (w2 - w1) / ((tt2 - tt1) / VARIANCE_YEAR)
    return math.sqrt(fv) if fv > 0 else None


def analyze(points, asof, max_dte: float = 400.0) -> dict:
    """
    points   iterable of (expiry, dte_calendar_days, atm_iv); expiry is a date
             or ISO string, atm_iv a decimal (0.18)
    asof     the date the quotes describe

    Returns {"asof", "rows": [...], "events": [...], "baseline_fwd"}; rows are
    sorted by expiry and carry:
        expiration  ISO date            dte      calendar days
        bd          market sessions     atm_iv   as quoted
        iv_t        the same vol on the variance clock
        fwd_iv      variance-clock forward vol from the previous expiry
                    (from `asof` for the first row); None if undefined
        rich_rel    relative richness against neighbouring expiries
        label       "rich" | "cheap" | "fair" | None
        event       True when the interval ending here is an event interval
        event_premium  rich AND an event interval ends within EVENT_NEAR_BD
    """
    asof = _as_date(asof)
    rows = []
    for expiry, dte, iv in points:
        try:
            dte, iv = float(dte), float(iv)
        except (TypeError, ValueError):
            continue
        if not (dte > 0 and iv > 0 and math.isfinite(iv)) or dte > max_dte:
            continue
        e = _as_date(expiry)
        bd = trading_days(asof, e)
        if bd < 1:
            continue
        w = iv * iv * dte / 365.0
        tt = variance_time(asof, e)
        rows.append({"expiration": e.isoformat(), "dte": round(dte, 2), "bd": bd,
                     "atm_iv": round(iv, 4), "_w": w, "_tt": tt,
                     "iv_t": round(math.sqrt(w / (tt / VARIANCE_YEAR)), 4)})
    rows.sort(key=lambda r: (r["bd"], r["dte"]))
    # One row per trading-day count (AM and PM settled expiries share a date).
    dedup = []
    for r in rows:
        if dedup and dedup[-1]["bd"] == r["bd"]:
            continue
        dedup.append(r)
    rows = dedup

    # Forward vol between consecutive expiries.
    prev_w, prev_tt, prev_bd = 0.0, 0.0, 0
    for r in rows:
        f = forward_vol(prev_w, prev_tt, r["_w"], r["_tt"])
        r["fwd_iv"] = None if f is None else round(f, 4)
        r["_gap"] = r["bd"] - prev_bd
        prev_w, prev_tt, prev_bd = r["_w"], r["_tt"], r["bd"]

    # Richness against the neighbours (leave-one-out straight line in w).
    for i, r in enumerate(rows):
        span = max(FIT_MIN_SPAN, FIT_SPAN_FRAC * r["bd"])
        nb = [o for j, o in enumerate(rows) if j != i and abs(o["bd"] - r["bd"]) <= span]
        r["rich_rel"], r["label"] = None, None
        if r["bd"] < MIN_RANK_BD:
            continue
        below = any(o["bd"] < r["bd"] for o in nb)
        above = any(o["bd"] > r["bd"] for o in nb)
        if len(nb) < FIT_MIN_POINTS or not (below and above):
            continue
        w_fit = _fit_line([o["_tt"] for o in nb], [o["_w"] for o in nb], r["_tt"])
        if w_fit is None or w_fit <= 0:
            continue
        iv_fit = math.sqrt(w_fit / (r["dte"] / 365.0))
        rel = r["atm_iv"] / iv_fit - 1.0
        r["rich_rel"] = round(rel, 4)
        r["label"] = "rich" if rel >= RICH_REL else "cheap" if rel <= CHEAP_REL else "fair"

    # Total variance is convex in time when the curve slopes up, so a straight
    # line through the neighbours sits a little above every interior point.
    # Centre on the surface's own median so "fair" means "like the others".
    rels = [r["rich_rel"] for r in rows if r["rich_rel"] is not None and r["dte"] >= 7]
    centre = _median(rels) if len(rels) >= 4 else 0.0
    for r in rows:
        if r["rich_rel"] is None:
            continue
        rel = r["rich_rel"] - centre
        r["rich_rel"] = round(rel, 4)
        r["label"] = "rich" if rel >= RICH_REL else "cheap" if rel <= CHEAP_REL else "fair"
    # Roughness: how far each expiry's total variance sits from the straight
    # line between its two IMMEDIATE neighbours, as a share of IV. A real
    # event is a step that disturbs two points; a thin chain disturbs them
    # all, so the median separates the two.
    rough = []
    for a, r, b in zip(rows, rows[1:], rows[2:]):
        if r["dte"] < 7 or b["_tt"] <= a["_tt"]:
            continue
        w_mid = a["_w"] + (b["_w"] - a["_w"]) * (r["_tt"] - a["_tt"]) / (b["_tt"] - a["_tt"])
        if w_mid > 0:
            rough.append(abs(math.sqrt(r["_w"] / w_mid) - 1.0))
    noise = _median(rough) if len(rough) >= 3 else None
    noisy = bool(noise is not None and noise > NOISY_SURFACE)

    # Events: short intervals whose forward vol stands out from the front.
    front = [r["fwd_iv"] for r in rows if r["fwd_iv"] is not None and r["bd"] <= EVENT_FRONT_BD]
    base = _median(front) if len(front) >= 4 else None
    events = []
    for i, r in enumerate(rows):
        r["event"] = False
        if (base is None or noisy or r["fwd_iv"] is None or r["_gap"] > EVENT_MAX_GAP
                or r["bd"] > EVENT_FRONT_BD):
            continue
        if r["fwd_iv"] >= EVENT_RATIO * base and r["fwd_iv"] - base >= EVENT_MIN_PTS:
            # A cheap (or mis-quoted) expiry just before makes the forward vol
            # out of it look high. When the previous interval is low, the
            # extra variance over BOTH intervals must still be event-sized.
            if i >= 1 and (rows[i - 1]["fwd_iv"] is None or rows[i - 1]["fwd_iv"] < EVENT_REBOUND * base):
                w0, t0 = (rows[i - 2]["_w"], rows[i - 2]["_tt"]) if i >= 2 else (0.0, 0.0)
                extra = (r["_w"] - w0) - base * base * (r["_tt"] - t0) / VARIANCE_YEAR
                need = (EVENT_RATIO ** 2 - 1.0) * base * base * (r["_tt"] - rows[i - 1]["_tt"]) / VARIANCE_YEAR
                if extra < need:
                    continue
            r["event"] = True
            events.append({"expiration": r["expiration"], "bd": r["bd"], "gap_bd": r["_gap"],
                           "fwd_iv": r["fwd_iv"], "baseline": round(base, 4),
                           "excess_pts": round(r["fwd_iv"] - base, 4)})
    ev_bd = [e["bd"] for e in events]
    for r in rows:
        near = any(r["bd"] - EVENT_NEAR_BD < b <= r["bd"] for b in ev_bd)
        r["event_premium"] = bool(near and r["label"] == "rich")
        r["spans_events"] = sum(1 for b in ev_bd if b <= r["bd"])

    return {"asof": asof.isoformat(), "rows": rows, "events": events,
            "baseline_fwd": None if base is None else round(base, 4),
            "noise": None if noise is None else round(noise, 4), "noisy": noisy}


def public_rows(surface: dict) -> list[dict]:
    """Rows without the working fields, safe to serialise."""
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in surface.get("rows", [])]


def constant_maturity(surface: dict, days: float) -> float | None:
    """ATM IV at a calendar tenor, total variance interpolated in trading time."""
    rows = surface.get("rows") or []
    if not rows:
        return None
    asof = _as_date(surface["asof"])
    target = variance_time(asof, asof + timedelta(days=int(round(days))))
    if target <= rows[0]["_tt"] or len(rows) == 1:
        return rows[0]["atm_iv"] if target >= rows[0]["_tt"] * 0.5 else None
    if target >= rows[-1]["_tt"]:
        return rows[-1]["atm_iv"] if target <= rows[-1]["_tt"] * 1.25 else None
    for a, b in zip(rows, rows[1:]):
        if a["_tt"] <= target <= b["_tt"]:
            f = (target - a["_tt"]) / (b["_tt"] - a["_tt"])
            w = a["_w"] + (b["_w"] - a["_w"]) * f
            return round(math.sqrt(w / (days / 365.0)), 4) if w > 0 else None
    return None


# --------------------------------------------------------------------------
# 1. Expiry selection
# --------------------------------------------------------------------------

def pick_expiry(surface: dict, dte_lo: float, dte_hi: float, side: str,
                allowed=None) -> dict | None:
    """
    The expiry inside [dte_lo, dte_hi] calendar days that is cheapest against
    its neighbours (side="buy": debit spreads, long options, the far leg of a
    calendar) or richest (side="sell": credit spreads, condors, the near leg).

    allowed   optional set of ISO expiries the caller can actually trade
              (liquid, not across earnings ...). Others are ignored.

    Returns None when fewer than two expiries in the window can be compared,
    or the chain is too thin to rank (surface["noisy"]): the caller then keeps
    its own rule. Otherwise:
        expiration, dte, rich_rel, label, event_premium, spans_events,
        edge_rel   how much cheaper (buy) or richer (sell) than the median
                   expiry of the window, as a fraction of IV
        ranked     every comparable expiry, best first
        reason     one plain sentence
    """
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    rows = [r for r in surface.get("rows", [])
            if dte_lo <= r["dte"] <= dte_hi and r.get("rich_rel") is not None
            and (allowed is None or r["expiration"] in allowed)]
    if len(rows) < 2 or surface.get("noisy"):
        return None
    mid = (dte_lo + dte_hi) / 2.0
    sign = 1.0 if side == "buy" else -1.0
    best_val = min(sign * r["rich_rel"] for r in rows)
    ties = [r for r in rows if sign * r["rich_rel"] - best_val <= TIE_REL]
    # Among ties, an expiry without event premium, then the one nearest the
    # middle of the window.
    best = min(ties, key=lambda r: (r["event_premium"] if side == "buy" else False,
                                    abs(r["dte"] - mid)))
    med = _median([r["rich_rel"] for r in rows])
    edge = sign * (med - best["rich_rel"])
    ranked = sorted(rows, key=lambda r: (sign * r["rich_rel"], abs(r["dte"] - mid)))

    verb = "cheapest to buy" if side == "buy" else "richest to sell"
    if abs(edge) < TIE_REL:
        reason = (f"{best['expiration']} ({best['dte']:.0f}d): the expiries in the "
                  f"{dte_lo:.0f}-{dte_hi:.0f} day window are priced alike; no expiry edge")
    else:
        rel = best["rich_rel"] * 100
        where = (f"{abs(rel):.1f}% {'below' if rel < 0 else 'above'} its neighbours"
                 if abs(rel) >= 0.05 else "in line with its neighbours")
        reason = (f"{best['expiration']} ({best['dte']:.0f}d) is the {verb} in the "
                  f"{dte_lo:.0f}-{dte_hi:.0f} day window: IV {where}, {abs(edge) * 100:.1f}% better "
                  f"than the window's median expiry")
    if best["event_premium"]:
        reason += ("; its richness is event premium (a priced event falls just before it), so "
                   + ("selling it means carrying that event" if side == "sell"
                      else "buying it pays for that event"))
    return {"expiration": best["expiration"], "dte": best["dte"], "rich_rel": best["rich_rel"],
            "label": best["label"], "event_premium": best["event_premium"],
            "spans_events": best["spans_events"], "edge_rel": round(edge, 4),
            "ranked": [{"expiration": r["expiration"], "dte": r["dte"], "rich_rel": r["rich_rel"],
                        "label": r["label"], "event_premium": r["event_premium"]} for r in ranked],
            "reason": reason}


def row_for(surface: dict, expiration) -> dict | None:
    iso = _as_date(expiration).isoformat()
    return next((r for r in surface.get("rows", []) if r["expiration"] == iso), None)


def score_adjustment(surface: dict, expiration, side: str, cap: float = 1.0) -> tuple[float, str | None]:
    """
    (-cap .. +cap, note) for an expiry on a buy or sell: +cap when it is
    RICH_REL*2 or more on the right side of its neighbours, -cap when it is
    that far on the wrong side, 0.0 within ADJ_DEAD_BAND of them. 0.0 and no
    note when the surface cannot rank it.
    """
    r = row_for(surface, expiration)
    if r is None or r.get("rich_rel") is None or surface.get("noisy"):
        return 0.0, None
    sign = -1.0 if side == "buy" else 1.0
    rel = sign * r["rich_rel"]
    # Nothing inside the dead band (half the fair range: ordinary scatter),
    # then linear up to the cap at twice RICH_REL.
    size = max(0.0, abs(rel) - ADJ_DEAD_BAND) / (2 * RICH_REL - ADJ_DEAD_BAND)
    x = math.copysign(min(1.0, size), rel)
    note = None
    if r["label"] in ("rich", "cheap"):
        note = (f"{r['expiration']} expiry is {r['label']} against neighbouring expiries "
                f"(IV {abs(r['rich_rel']) * 100:.1f}% {'above' if r['rich_rel'] > 0 else 'below'} them)")
        if r["event_premium"]:
            note += ": event premium"
    return round(x * cap, 3), note


# --------------------------------------------------------------------------
# 2. Calendar / diagonal gate
# --------------------------------------------------------------------------

def calendar_gate(surface: dict, front, back, stress: bool = False) -> dict:
    """
    Should the near leg be sold against the far leg?

    A calendar or diagonal sells variance out to the near expiry and buys it
    out to the far one; what is left is FORWARD variance between the two.
    The test is the near leg's vol against that forward vol:

        ratio = near iv_t / forward vol(near -> far)

        ratio <  CAL_FRONT_CHEAP   FAIL  the near leg is cheap against what is
                                         being bought (steep contango): the
                                         far leg rolls down the curve
        CHEAP <= ratio <= RICH      PASS  fairly priced; the edge is the pin
        ratio >  CAL_FRONT_RICH    PASS  the near leg is rich (favourable
                                         pricing), UNLESS `stress` is set
                                         (market-wide backwardation: the old
                                         "no calendars in backwardation" rule),
                                         in which case FAIL
    `stress` is the caller's own stress read (VIX9D above VIX, for example).
    Returns {"pass": bool|None, ...}; pass is None when it cannot be measured.
    """
    f, b = row_for(surface, front), row_for(surface, back)
    out = {"front": f and f["expiration"], "back": b and b["expiration"], "pass": None,
           "ratio": None, "fwd_iv": None, "state": "unknown", "front_event": None}
    if f is None or b is None or b["bd"] <= f["bd"]:
        out["reason"] = "forward vol not measurable: one of the two expiries has no usable ATM IV"
        return out
    if surface.get("noisy"):
        out["reason"] = ("forward vol not measurable: the chain is too thin, its expiries scatter "
                         "too much to compare")
        return out
    fwd = forward_vol(f["_w"], f["_tt"], b["_w"], b["_tt"])
    out["front_iv_t"], out["back_iv_t"] = f["iv_t"], b["iv_t"]
    ev = [r for r in surface["rows"] if r["event"] and r["bd"] <= f["bd"]
          and r["bd"] > f["bd"] - EVENT_NEAR_BD]
    out["front_event"] = bool(ev)
    if fwd is None:
        out.update({"pass": False, "state": "inconsistent",
                    "reason": "negative forward variance between the two expiries: the near leg is "
                              "extremely bid or the quotes disagree; do not price a calendar off it"})
        return out
    ratio = f["iv_t"] / fwd
    out.update({"fwd_iv": round(fwd, 4), "ratio": round(ratio, 3)})
    pct = f"near {f['iv_t'] * 100:.1f}% vs forward {fwd * 100:.1f}% (ratio {ratio:.2f})"
    if ratio < CAL_FRONT_CHEAP:
        out.update({"pass": False, "state": "front_cheap",
                    "reason": f"near leg cheap against the forward vol it is sold against: {pct}. "
                              "Steep contango: the far leg rolls down the curve"})
    elif ratio > CAL_FRONT_RICH:
        if stress:
            out.update({"pass": False, "state": "front_rich_stress",
                        "reason": f"near leg rich ({pct}) but the market is in stress backwardation: "
                                  "a short near leg gets run over"})
        else:
            out.update({"pass": True, "state": "front_rich",
                        "reason": f"near leg rich against the forward vol: {pct}. Favourable pricing"
                                  + ("; the richness is a priced event before the near expiry, "
                                     "so the calendar is short that event" if ev else "")})
    else:
        out.update({"pass": True, "state": "fair",
                    "reason": f"near leg fairly priced against the forward vol: {pct}. "
                              "The edge has to come from the pin, not the term structure"})
    return out


# --------------------------------------------------------------------------
# 4. Long-dated vol against its own history (ladder timing)
# --------------------------------------------------------------------------

def percentile_rank(history, x) -> float | None:
    """Share of `history` at or below x, 0-100."""
    vals = [float(v) for v in history if v is not None and float(v) > 0]
    if len(vals) < 60:
        return None
    return round(100.0 * sum(1 for v in vals if v <= x) / len(vals), 1)


def _state(pct) -> str:
    if pct is None:
        return "UNKNOWN"
    return "CHEAP" if pct <= LONG_VOL_CHEAP_PCT else "RICH" if pct >= LONG_VOL_RICH_PCT else "NEUTRAL"


def long_vol_state(closes: dict, window: int = LONG_VOL_WINDOW) -> dict:
    """
    closes   {"vix": [...], "vix3m": [...], "vix6m": [...], "vix1y": [...]},
             daily closes oldest first, the last value being the latest close.

    For each long tenor: level, percentile over the trailing `window` sessions
    and over the trailing 5 years, and CHEAP / NEUTRAL / RICH. Also the ratio
    of each tenor to VIX with its own percentile (how steep the curve is).
    """
    out = {"tenors": {}, "window_sessions": window}
    vix = [float(v) for v in closes.get("vix") or []]
    for name in ("vix3m", "vix6m", "vix1y"):
        s = [float(v) for v in closes.get(name) or []]
        if not s:
            continue
        last = s[-1]
        p1 = percentile_rank(s[-window:], last)
        p5 = percentile_rank(s[-5 * window:], last) if len(s) >= 2 * window else None
        t = {"level": round(last, 2), "pct_1y": p1, "pct_5y": p5, "state": _state(p1),
             "tenor_days": LONG_VOL_TENOR_DAYS[name]}
        n = min(len(s), len(vix))
        if n >= 60 and vix[-1] > 0:
            ratios = [a / b for a, b in zip(s[-n:], vix[-n:]) if b > 0]
            t["ratio_to_vix"] = round(last / vix[-1], 3)
            t["ratio_pct_1y"] = percentile_rank(ratios[-window:], last / vix[-1])
        out["tenors"][name] = t
    return out


def ladder_read(long_vol: dict, days_to_expiry: float) -> dict:
    """
    The long-vol read for a position with `days_to_expiry` left: the listed
    tenor nearest to it, and one plain sentence. Information for timing a
    staged long-dated tranche; never a gate.
    """
    tenors = (long_vol or {}).get("tenors") or {}
    if not tenors:
        return {"tenor": None, "state": "UNKNOWN", "note": "long-dated vol history not available"}
    name = min(tenors, key=lambda k: abs(tenors[k]["tenor_days"] - days_to_expiry))
    t = tenors[name]
    label = name.upper()
    if t["state"] == "UNKNOWN":
        note = f"{label} {t['level']}: not enough history to rank"
    else:
        word = {"CHEAP": "cheap: a favourable time to add a long-dated tranche",
                "RICH": "rich: hold the next tranche unless the thesis needs it now",
                "NEUTRAL": "neither cheap nor rich: no timing edge from vol"}[t["state"]]
        note = (f"{label} {t['level']} is at the {t['pct_1y']:.0f}th percentile of its last year: "
                f"long-dated vol is {word}")
    return {"tenor": name, "state": t["state"], "level": t["level"], "pct_1y": t["pct_1y"], "note": note}


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------

def term_from_legs(legs, spot: float, asof) -> list[tuple]:
    """
    (expiry, dte, atm_iv) per expiry from option legs in the agents' dict form
    ({"expiration", "strike", "implied_volatility", ...}; calls, puts or both).
    ATM IV is the mean IV of the listed strike nearest spot on each side
    present, at most 3% from spot.
    """
    asof = _as_date(asof)
    by = {}
    for l in legs or []:
        try:
            iv = float(l.get("implied_volatility") or 0)
            k = float(l["strike"])
        except (TypeError, ValueError, KeyError):
            continue
        if iv <= 0 or not l.get("expiration"):
            continue
        typ = str(l.get("type") or l.get("option_type") or "").lower()[:1] or "?"
        by.setdefault(str(l["expiration"])[:10], {}).setdefault(typ, []).append((abs(k - spot), iv))
    out = []
    for exp, sides in by.items():
        ivs = []
        for rows in sides.values():
            d, iv = min(rows)
            if d <= 0.03 * spot:
                ivs.append(iv)
        if not ivs:
            continue
        dte = (date.fromisoformat(exp) - asof).days
        if dte > 0:
            out.append((exp, dte, sum(ivs) / len(ivs)))
    return sorted(out)


def from_legs(legs, spot: float, asof, max_dte: float = 400.0) -> dict:
    return analyze(term_from_legs(legs, spot, asof), asof, max_dte=max_dte)
