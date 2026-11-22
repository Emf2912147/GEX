#!/usr/bin/env python3
"""
Volatility metrics derived from a captured option chain.

Everything here is computed from data the capture already stores (each
contract's IV, delta and open interest) plus the daily close history. Nothing
new is fetched and nothing new is written to history/raw/.

Per symbol, per capture:

    atm_iv_7d      ATM implied vol, constant 7-day maturity (decimal, 0.18)
    atm_iv_30d     ATM implied vol, constant 30-day maturity
    term_ratio     atm_iv_7d / atm_iv_30d. Above 1 the curve is inverted
                   (near-term stress); below 1 it is in contango (calm).
    skew_25d       IV of the 25-delta put minus IV of the 25-delta call, at
                   the expiry closest to 30 days (vol points, decimal).
                   Positive = puts bid over calls (normal for equity indices).
    vol_trigger    Price at which the dealer gamma profile recovers to
                   VOL_TRIGGER_FRACTION of its peak, scanning down from the
                   peak. Below it, long-gamma support is weak or gone. It sits
                   above the flip whenever the profile has a positive region.
                   None when the profile has no positive region in range.
    rv_20d         20-session realized vol from official closes (annualized).
    iv_rv_ratio    atm_iv_30d / rv_20d: implied vs realized, the vol-supply
                   read. Above 1 options are rich to recent movement.
    smile          (feed only) put/call IV by strike for the expiry nearest
                   7 days, +/-8% of spot, so the agent can price its spreads.

Method notes, read before trusting a number:

  - ATM IV uses out-of-the-money options only (puts below spot, calls above),
    interpolated linearly in strike to spot. ITM quotes are wider and their
    IVs noisier.
  - Constant maturity interpolates TOTAL VARIANCE (iv^2 * T) linearly in T
    between the two expiries that bracket the target, which is the standard
    way to avoid calendar arbitrage in the interpolation. Outside the listed
    range the nearest expiry is used and `*_extrapolated` is flagged.
  - Contracts with zero IV, zero open interest AND zero volume are ignored.
  - IV is whatever Cboe reports. It is a model value from mid quotes, not a
    traded price.
  - rv_20d needs 21 consecutive closes. Symbols added recently (GLD, TLT on
    2026-10-08; SMH, XLF, XLK mid-October) return None until they have them.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

import gamma_exposure as gx


VOL_TRIGGER_FRACTION = 0.25

RV_WINDOW = 20
TRADING_DAYS = 252

# Below this many usable OTM strikes an expiry's ATM IV is not trusted.
MIN_STRIKES_PER_SIDE = 2


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _usable(df: pd.DataFrame) -> pd.DataFrame:
    live = (df["iv"] > 0) & ((df["oi"] > 0) | (df["volume"] > 0))
    return df[live]


def _expiry_atm_iv(exp: pd.DataFrame, spot: float) -> float | None:
    """ATM IV for one expiry from OTM options, interpolated to spot."""
    puts = exp[(exp["type"] == "P") & (exp["strike"] <= spot)]
    calls = exp[(exp["type"] == "C") & (exp["strike"] >= spot)]
    if len(puts) < MIN_STRIKES_PER_SIDE or len(calls) < MIN_STRIKES_PER_SIDE:
        return None

    p = puts.sort_values("strike").iloc[-1]   # highest put strike <= spot
    c = calls.sort_values("strike").iloc[0]   # lowest call strike >= spot

    if c["strike"] == p["strike"]:
        return float((p["iv"] + c["iv"]) / 2.0)

    w = (spot - p["strike"]) / (c["strike"] - p["strike"])
    return float(p["iv"] * (1 - w) + c["iv"] * w)


def _atm_term(df: pd.DataFrame, spot: float) -> pd.DataFrame:
    """One row per expiry: T (years), atm_iv. Sorted by T."""
    rows = []
    for _, exp in _usable(df).groupby("expiry"):
        iv = _expiry_atm_iv(exp, spot)
        if iv is not None and iv > 0:
            rows.append({"T": float(exp["dte"].iloc[0]) / 365.0, "atm_iv": iv})
    return pd.DataFrame(rows, columns=["T", "atm_iv"]).sort_values("T")


def constant_maturity_iv(term: pd.DataFrame, days: float) -> tuple[float | None, bool]:
    """
    ATM IV at a constant maturity by linear interpolation of total variance.
    Returns (iv, extrapolated).
    """
    if term.empty:
        return None, False

    target = days / 365.0
    Ts = term["T"].values
    ivs = term["atm_iv"].values

    if target <= Ts[0]:
        return float(ivs[0]), target < Ts[0]
    if target >= Ts[-1]:
        return float(ivs[-1]), target > Ts[-1]

    i = int(np.searchsorted(Ts, target))
    t0, t1 = Ts[i - 1], Ts[i]
    v0, v1 = ivs[i - 1] ** 2 * t0, ivs[i] ** 2 * t1
    var = v0 + (v1 - v0) * (target - t0) / (t1 - t0)
    return float(math.sqrt(max(var, 0.0) / target)), False


def _iv_at_delta(side: pd.DataFrame, target_delta: float) -> float | None:
    """IV interpolated to a delta on one side (calls positive, puts negative)."""
    s = side[(side["delta"] != 0)].sort_values("delta")
    if len(s) < 2:
        return None
    d = s["delta"].values
    if not (d.min() <= target_delta <= d.max()):
        return None
    return float(np.interp(target_delta, d, s["iv"].values))


def skew_25d(df: pd.DataFrame, days: float = 30.0) -> float | None:
    """25-delta put IV minus 25-delta call IV at the expiry nearest `days`."""
    usable = _usable(df)
    if usable.empty:
        return None

    expiries = usable.groupby("expiry")["dte"].first()
    # Prefer expiries with enough quotes; walk outward from the target.
    for expiry in (expiries - days).abs().sort_values().index[:4]:
        exp = usable[usable["expiry"] == expiry]
        put_iv = _iv_at_delta(exp[exp["type"] == "P"], -0.25)
        call_iv = _iv_at_delta(exp[exp["type"] == "C"], 0.25)
        if put_iv is not None and call_iv is not None:
            return put_iv - call_iv
    return None


# ------------------------------------------------------------------
# IV smile for the trade expiry
# ------------------------------------------------------------------
#
# The agent prices its wall-anchored spreads from this, so its credit /
# width filter can run. Only the expiry nearest SMILE_TARGET_DTE is
# published (the agent's trade tenor), only strikes within SMILE_BAND of
# spot, and only IVs Cboe actually quotes on contracts with open interest
# or volume. A strike with no usable quote on one side is null on that
# side: the agent then reports the spread as unpriced rather than guess.

SMILE_TARGET_DTE = 7.0
SMILE_MIN_DTE = 2.0      # never pick an expiry about to settle
SMILE_BAND = 0.08        # +/- 8% of spot


def smile(df: pd.DataFrame, spot: float,
          target_dte: float = SMILE_TARGET_DTE,
          band: float = SMILE_BAND) -> dict | None:
    usable = _usable(df)
    usable = usable[usable["dte"] >= SMILE_MIN_DTE]
    if usable.empty:
        return None

    dtes = usable.groupby("expiry")["dte"].first()
    expiry = (dtes - target_dte).abs().idxmin()
    exp = usable[(usable["expiry"] == expiry)
                 & (usable["strike"] >= spot * (1 - band))
                 & (usable["strike"] <= spot * (1 + band))]
    if exp.empty:
        return None

    puts = exp[exp["type"] == "P"].groupby("strike")["iv"].first()
    calls = exp[exp["type"] == "C"].groupby("strike")["iv"].first()
    strikes = sorted(set(puts.index) | set(calls.index))

    def iv_list(side):
        return [round(float(side[k]), 4) if k in side.index else None for k in strikes]

    exp_ts = pd.Timestamp(expiry)
    if exp_ts.tzinfo is None:
        exp_ts = exp_ts.tz_localize("UTC")

    return {
        "expiry_utc": exp_ts.isoformat(),
        "dte": round(float(dtes[expiry]), 3),
        "strikes": [float(k) for k in strikes],
        "put_iv": iv_list(puts),
        "call_iv": iv_list(calls),
    }


def vol_trigger(chain: pd.DataFrame, spot: float,
                grid_window: float = 0.15,
                fraction: float = VOL_TRIGGER_FRACTION) -> float | None:
    """
    Price where the net dealer gamma profile recovers to `fraction` of its
    peak, scanning down from the peak. `chain` is the same DTE-filtered chain
    the capture uses for the flip.
    """
    grid, net, _ = gx.gamma_profile(chain, spot * (1 - grid_window),
                                    spot * (1 + grid_window))
    peak_i = int(np.argmax(net))
    peak = float(net[peak_i])
    if peak <= 0:
        return None

    level = fraction * peak
    for i in range(peak_i, 0, -1):
        lo, hi = net[i - 1], net[i]
        if lo < level <= hi:
            return float(grid[i - 1] + (grid[i] - grid[i - 1]) * (level - lo) / (hi - lo))
    return None  # profile never drops below the level inside the grid


def realized_vol(closes: pd.Series, window: int = RV_WINDOW) -> float | None:
    """Annualized close-to-close realized vol over the last `window` returns."""
    closes = pd.Series(closes, dtype=float).dropna()
    closes = closes[closes > 0]
    if len(closes) < window + 1:
        return None
    r = np.log(closes.values[-(window + 1):]).astype(float)
    rets = np.diff(r)
    return float(np.std(rets, ddof=1) * math.sqrt(TRADING_DAYS))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

VOL_COLUMNS = [
    "atm_iv_7d", "atm_iv_30d", "term_ratio", "skew_25d",
    "vol_trigger", "rv_20d", "iv_rv_ratio", "iv_extrapolated",
]


def compute(df: pd.DataFrame, spot: float, chain_30: pd.DataFrame,
            closes: pd.Series | None = None) -> dict:
    """
    df        the full parsed chain (all expiries, dte > 0, IV normalized)
    spot      the anchored spot the capture uses
    chain_30  the DTE-filtered chain the capture uses for flip and walls
    closes    prior official closes for the symbol, oldest first, INCLUDING
              this session's close as the last value (or None)
    """
    term = _atm_term(df, spot)
    iv7, ext7 = constant_maturity_iv(term, 7)
    iv30, ext30 = constant_maturity_iv(term, 30)
    rv = realized_vol(closes) if closes is not None else None
    trig = vol_trigger(chain_30, spot)

    def r(x, n=4):
        return round(x, n) if x is not None else None

    return {
        "atm_iv_7d": r(iv7),
        "atm_iv_30d": r(iv30),
        "term_ratio": r(iv7 / iv30) if iv7 and iv30 else None,
        "skew_25d": r(skew_25d(df)),
        "vol_trigger": r(trig),
        "rv_20d": r(rv),
        "iv_rv_ratio": r(iv30 / rv, 3) if iv30 and rv else None,
        "iv_extrapolated": int(bool(ext7 or ext30)),
    }


# --------------------------------------------------------------------------
# Agent feed (docs/latest.json) enrichment
# --------------------------------------------------------------------------
#
# build_dashboard.build_latest_json() calls these. Every field is ADDITIVE:
# the existing spot/flip/walls/regime keys are untouched, so an agent that
# does not know about them keeps working. Each block is built independently
# and a failure leaves that block out (or None) rather than breaking the feed
# the trading agent depends on.

import os as _os

# A realized_vol.csv row older than this (calendar days vs the session) is
# stale; fall back to the closes captured here.
RV_MAX_AGE_DAYS = 5


def _closes_from_metrics(metrics: pd.DataFrame, symbol: str) -> pd.Series:
    """This pipeline's own anchored closes, one per session, oldest first."""
    s = metrics[metrics["symbol"] == symbol]
    if s.empty or "session_date" not in s:
        return pd.Series(dtype=float)
    s = (s.sort_values("feed_ts")
          .drop_duplicates("session_date", keep="last")
          .sort_values("session_date"))
    return pd.to_numeric(s["spot"], errors="coerce")


def _rv_from_file(histdir: str, symbol: str, session_date: str):
    """(hv20, source, proxy) from realized_vol.csv when fresh, else None."""
    path = _os.path.join(histdir, "realized_vol.csv")
    if not _os.path.exists(path):
        return None
    rv = pd.read_csv(path)
    rv = rv[rv["symbol"] == symbol]
    if rv.empty:
        return None
    last = rv.sort_values("capture_ts").iloc[-1]
    try:
        age = (pd.Timestamp(session_date) - pd.Timestamp(str(last["rv_asof_date"]))).days
    except (ValueError, TypeError):
        return None
    if age > RV_MAX_AGE_DAYS or pd.isna(last.get("hv20")):
        return None
    proxy = str(last.get("rv_proxy")).lower() == "true"
    return float(last["hv20"]), f"cboe_history:{last['rv_source']}", proxy


def symbol_vol_block(histdir: str, raw: pd.DataFrame, row,
                     metrics: pd.DataFrame, max_dte: float = 30,
                     grid_window: float = 0.15) -> dict:
    """
    Vol fields for one symbol from the SAME raw chain the walls and flip came
    from, so every number in the symbol's record describes one snapshot.
    """
    df = raw.copy()
    df["dte"] = pd.to_numeric(df["dte"], errors="coerce")
    df = df[df["dte"] > 0].copy()
    df["T"] = df["dte"] / 365.0
    spot = float(row["spot"])
    chain = df[df["dte"] <= max_dte]

    out = compute(df, spot, chain, closes=None)   # RV handled below

    session = str(row.get("session_date", ""))
    from_file = _rv_from_file(histdir, str(row["symbol"]), session) if session else None
    if from_file is not None:
        rv, source, proxy = from_file
    else:
        rv = realized_vol(_closes_from_metrics(metrics, str(row["symbol"])))
        source, proxy = ("captured_closes", False) if rv is not None else (None, None)

    out["rv_20d"] = round(rv, 4) if rv is not None else None
    out["rv_source"] = source
    out["rv_proxy"] = proxy
    iv30 = out.get("atm_iv_30d")
    out["iv_rv_ratio"] = round(iv30 / rv, 3) if iv30 and rv else None
    out["smile"] = smile(df, spot)
    return out


def vix_block(histdir: str) -> dict | None:
    """Latest market-wide VIX / VIX9D term-structure read from vix_state.csv."""
    path = _os.path.join(histdir, "vix_state.csv")
    if not _os.path.exists(path):
        return None
    v = pd.read_csv(path)
    if v.empty:
        return None
    last = v.sort_values("capture_ts").iloc[-1]

    def num(key):
        val = last.get(key)
        return None if pd.isna(val) else float(val)

    return {
        "asof": str(last["feed_ts"]),
        "vix": num("vix"),
        "vix9d": num("vix9d"),
        "term_spread_9d_vs_30d": num("term_spread_9d_vs_30d"),
        "term_state": None if pd.isna(last.get("term_state")) else str(last["term_state"]),
        "vix_chg_1d_pct": num("vix_chg_1d_pct"),
        "vix_20d_change_pct": num("vix_20d_change_pct"),
    }


# --------------------------------------------------------------------------
# Volatility surface across expirations (vol_surface.py)
# --------------------------------------------------------------------------
#
# Additive like everything above. The per-symbol block is computed from the
# stored chain; the market-wide long-dated block reads Cboe's VIX3M / VIX6M /
# VIX1Y daily history fresh and stores nothing but its result in latest.json.

import vol_surface as vs

# DTE windows the decision table trades (calendar days) and the calendar /
# diagonal pairs (near window, far window) of cells U1/U2 and C1/C2.
SURFACE_WINDOWS = ((7, 14), (15, 25), (20, 35), (35, 45), (45, 60), (60, 120))
SURFACE_CALENDARS = {"diagonal_35_45_over_7_14": ((7, 14), (35, 45)),
                     "calendar_45_60_over_7_14": ((7, 14), (45, 60))}
SURFACE_TENORS = (7, 14, 30, 60, 90, 180)
SURFACE_MAX_DTE = 250

LONG_VOL_SYMBOLS = {"vix": "VIX", "vix3m": "VIX3M", "vix6m": "VIX6M", "vix1y": "VIX1Y"}
LONG_VOL_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{sym}_History.csv"
LONG_VOL_MAX_AGE_H = 6.0


def _session_date(raw: pd.DataFrame, row=None):
    """The session the stored chain describes. feed_ts is UTC; an evening
    feed (after 00:00 UTC) still belongs to the previous US session."""
    if row is not None:
        s = str(row.get("session_date", "") or "")
        if len(s) >= 10 and s[:4].isdigit():
            return pd.Timestamp(s[:10]).date()
    ts = pd.to_datetime(raw["feed_ts"].iloc[0], errors="coerce")
    if pd.isna(ts):
        ts = pd.to_datetime(raw["capture_ts"].iloc[0], utc=True).tz_localize(None)
    return (ts - pd.Timedelta(hours=5)).date()


def surface_points(df: pd.DataFrame, spot: float, asof) -> list[tuple]:
    """(expiry date, calendar days from the session date, ATM IV) per expiry."""
    pts = []
    for expiry, exp in _usable(df).groupby("expiry"):
        iv = _expiry_atm_iv(exp, spot)
        if iv is None or iv <= 0:
            continue
        e = pd.Timestamp(expiry).date()
        dte = (e - asof).days
        if dte > 0:
            pts.append((e, float(dte), float(iv)))
    return pts


SURFACE_COLUMNS = ("expiration", "dte", "atm_iv", "fwd_iv", "rich_rel", "label", "event")


def _columns(rows):
    """Rows as parallel arrays (same shape as the smile block): a third the
    size of a list of objects in a feed the agents fetch every run."""
    return {c: [r.get(c) for r in rows] for c in SURFACE_COLUMNS}


def _slim_pick(p):
    if p is None:
        return None
    return {k: p[k] for k in ("expiration", "dte", "rich_rel", "label", "edge_rel",
                              "event_premium", "reason")}


def symbol_surface_block(raw: pd.DataFrame, row, stress: bool = False) -> dict | None:
    """
    Term-structure block for one symbol from the stored chain:

        cm_iv      ATM IV at constant maturities (calendar days)
        expiries   parallel arrays, one entry per listed expiry out to 250
                   days: ATM IV, forward vol from the previous expiry,
                   richness against neighbours (rich_rel, label), event flag
        events     intervals whose forward vol marks a priced event
        windows    cheapest expiry to buy / richest to sell per DTE window
        calendars  the forward-vol gate for the calendar and diagonal cells
    `stress` is the market-wide backwardation read (VIX9D above VIX).
    """
    df = raw.copy()
    df["dte"] = pd.to_numeric(df["dte"], errors="coerce")
    df = df[df["dte"] > 0]
    if df.empty:
        return None
    spot = float(row["spot"])
    asof = _session_date(raw, row)
    surf = vs.analyze(surface_points(df, spot, asof), asof, max_dte=SURFACE_MAX_DTE)
    if not surf["rows"]:
        return None

    windows = {}
    for lo, hi in SURFACE_WINDOWS:
        windows[f"{lo}-{hi}"] = {"buy": _slim_pick(vs.pick_expiry(surf, lo, hi, "buy")),
                                 "sell": _slim_pick(vs.pick_expiry(surf, lo, hi, "sell"))}

    def leg(window, side):
        lo, hi = window
        p = vs.pick_expiry(surf, lo, hi, side)
        if p is not None:
            return p["expiration"]
        inside = [r for r in surf["rows"] if lo <= r["dte"] <= hi]
        mid = (lo + hi) / 2.0
        return min(inside, key=lambda r: abs(r["dte"] - mid))["expiration"] if inside else None

    calendars = {}
    for name, (near, far) in SURFACE_CALENDARS.items():
        f, b = leg(near, "sell"), leg(far, "buy")
        if f and b:
            g = vs.calendar_gate(surf, f, b, stress=stress)
            calendars[name] = {k: g.get(k) for k in ("front", "back", "pass", "state", "ratio",
                                                     "fwd_iv", "front_event", "reason")}

    return {
        "asof": surf["asof"],
        "noisy": surf["noisy"],
        "baseline_fwd": surf["baseline_fwd"],
        "cm_iv": {str(d): vs.constant_maturity(surf, d) for d in SURFACE_TENORS},
        "expiries": _columns(vs.public_rows(surf)),
        "events": surf["events"],
        "windows": windows,
        "calendars": calendars,
    }


def _fetch_closes(symbol: str):
    """Daily closes, oldest first, with the last date. (None, None) if short.

    Was Cboe's history CSV; blocked since 2026-11-20, so this reads
    index_history (Yahoo) instead.

    VIX1Y IS EXPECTED TO COME BACK EMPTY. Yahoo serves its current level but
    no usable history, so there is nothing to place it against. A percentile
    off a handful of points is arithmetic dressed as information, and this
    block drives tranche timing -- so VIX1Y is left absent and long_vol_block
    keeps its previous value marked stale, which is the honest state.
    """
    import index_history as ixh
    return ixh.closes_and_asof(symbol)


def long_vol_block(docsdir: str | None = None, fetch=None, now=None) -> dict | None:
    """
    VIX3M / VIX6M / VIX1Y against their own trailing year: the long-dated
    cheap / rich read for staged position tranches.

    The history files change once a day, so the block already in
    docs/latest.json is reused while it is under LONG_VOL_MAX_AGE_H old. A
    failed fetch keeps the previous block (marked stale) rather than dropping
    it. Nothing is written to history/.
    """
    import json as _json
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    fetch = fetch or _fetch_closes
    prev = None
    if docsdir:
        try:
            with open(_os.path.join(docsdir, "latest.json"), encoding="utf-8") as fh:
                prev = (_json.load(fh).get("market") or {}).get("long_vol")
        except (OSError, ValueError):
            prev = None
    if prev and prev.get("fetched_utc"):
        try:
            age = (now - datetime.fromisoformat(prev["fetched_utc"])).total_seconds() / 3600.0
            if 0 <= age < LONG_VOL_MAX_AGE_H:
                return prev
        except (ValueError, TypeError):
            pass
    try:
        closes, asof = {}, None
        for key, sym in LONG_VOL_SYMBOLS.items():
            c, last = fetch(sym)
            if not c:
                raise ValueError(f"{sym}: no usable history")
            closes[key] = c
            if key != "vix":
                asof = last if asof is None else min(asof, last or asof)
        out = vs.long_vol_state(closes)
        out.update({"asof": asof, "fetched_utc": now.isoformat(), "stale": False,
                    "source": "cboe daily history (read fresh, not stored)"})
        return out
    except Exception as ex:                          # network, schema, anything
        print(f"latest.json: long-vol fetch failed ({ex})")
        if prev:
            return {**prev, "stale": True}
        return None
