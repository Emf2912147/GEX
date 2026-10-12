#!/usr/bin/env python3
"""
Support and resistance inside the one-standard-deviation band, graded.

For each symbol on the GEX Monitor:

  band      spot +/- 1 sigma, sigma = spot x ATM IV30 x sqrt(30/365). The
            ~30-day horizon matches the <= 30 DTE book the walls are built
            from. ATM IV30 is computed from the settled chain with the same
            method as gex_intraday._atm_iv (expiry nearest 30 DTE, ATM call
            and put averaged).

  levels    strike bins inside the band (same bins as the strike bars, and
            outside the +/-0.4% wall-exclusion band around spot) that are a
            local peak of option density, plus the call and put walls
            whenever they sit in the band. Below spot = support, above =
            resistance. Up to LEVELS_PER_SIDE per side: the wall first, then
            the best scores; shown nearest-first.

  density   how much option positioning sits at the strike, against the
            bins within +/-10% of spot (the wall window):
              0.6 x sqrt(|net dollar gamma| / largest in the window)
            + 0.4 x sqrt(total open interest / largest in the window)
            Gamma is what the walls rank on, so the larger of the two walls
            scores 1.0 on that part -- a call side much heavier than the put
            side makes resistance grade stronger than support, as it should;
            open interest catches round-number contract piles
            whose gamma is still small because they sit further out. The
            0.6 / 0.4 split mirrors the sector monitor's execution score.
            Square roots keep a level half the wall's size from reading as
            half as important.
  rank      0.6 / 0.4 blend of the same two quantities as percentiles across
            the window -- used only to decide whether a bin is a level at all
            (candidates must be above the median bin).

  persistence  the sector monitor's wall-stability method
            (sector_candidates.wall_stability), applied to a level instead of
            a single wall. Over the last WALL_LOOKBACK sessions before today,
            a session counts as HELD if a bin within one bin of the level
            (about 1% -- the sector tolerance -- on the ETFs) ranked in the
            top quartile that session with the same gamma sign. Each session is
            weighted by the expirations between it and today with the sector
            weights (x0.5 per monthly, x0.25 per quarterly crossed).
              strong   held >= 80% of weighted sessions, or 6+ in a row
              steady   held >= 50%, or 3+ in a row
              shifting less
              fresh    not a level last session (the sector's "moved")
              new      fewer than 3 prior sessions of history

  grade     0-100 = 50 x persistence + 50 x density, graded with the sector
            trade-quality cut-offs: A >= 75 strong, B >= 60 solid,
            C >= 45 moderate, D weak. A level with no history ("new") scores
            persistence at 0.5 and cannot grade above B.

Reads only what is already stored (history/raw and daily_metrics.csv); it
adds no data to the repo.
"""

import glob
import math
import os

import numpy as np
import pandas as pd

import gamma_exposure as gx
from sector_candidates import (GRADES, WALL_LOOKBACK, WALL_MIN_HISTORY,
                               expirations_between, session_weight)

SIGMA_DAYS = 30            # calendar days; matches the <= 30 DTE book
MAX_DTE = 30
WINDOW = 0.10              # density is ranked against bins within +/-10%
EXCLUDE = 0.004            # same thin band the walls ignore
GAMMA_W, OI_W = 0.6, 0.4
HELD_PCT = 0.75            # top quartile of density counts as "a level"
LEVEL_MIN_RANK = 0.50      # below the median bin is not worth listing
LEVELS_PER_SIDE = 3
PERSIST_W, DENSITY_W = 50, 50
GRADE_LABEL = {"A": "strong", "B": "solid", "C": "moderate", "D": "weak"}


# --------------------------------------------------------------------------
# One session's density profile
# --------------------------------------------------------------------------

def atm_iv(chain, spot, target_dte=30.0, tol=12.0):
    """gex_intraday._atm_iv: ATM IV near target DTE, call and put averaged."""
    near = chain[(chain["dte"] - target_dte).abs() <= tol]
    if near.empty:
        return np.nan
    exp = near.loc[(near["dte"] - target_dte).abs().idxmin(), "expiry"]
    s = near[(near["expiry"] == exp) & (near["iv"] > 0)]
    out = []
    for cp in ("C", "P"):
        side = s[s["type"] == cp]
        if len(side):
            out.append(float(side.loc[(side["strike"] - spot).abs().idxmin(), "iv"]))
    return float(np.mean(out)) if out else np.nan


def profile(raw, spot, bin_size):
    """DataFrame indexed by binned strike within +/-WINDOW of spot:
    net (dollar gamma), oi, g_pct, oi_pct, density (0-1)."""
    df = raw.copy()
    df["dte"] = pd.to_numeric(df["dte"], errors="coerce")
    df = df[(df["dte"] > 0) & (df["dte"] <= MAX_DTE)]
    df = df[(df["strike"] >= spot * (1 - WINDOW)) & (df["strike"] <= spot * (1 + WINDOW))]
    if df.empty:
        return None
    net, _, _ = gx.gex_by_strike(df, spot, False, bin_size)
    oi = (df.assign(k=(df["strike"] / bin_size).round() * bin_size)
          .groupby("k")["oi"].sum())
    p = pd.DataFrame({"net": net, "oi": oi.reindex(net.index).fillna(0.0)})
    # rank: is this bin a level at all (persistence test)
    p["rank"] = (GAMMA_W * p["net"].abs().rank(pct=True)
                 + OI_W * p["oi"].rank(pct=True))
    # density: how big it is next to the biggest bin in the window (the
    # ATM exclusion band left out, as the walls leave it out)
    a = p["net"].abs()
    out_band = (p.index.to_series() - spot).abs().values > spot * EXCLUDE
    ref = a[out_band].max() if out_band.any() else 0.0
    p["g_rel"] = (a / ref).clip(upper=1.0) if ref else 0.0
    p["oi_rel"] = p["oi"] / (p["oi"].max() or 1.0)
    p["density"] = GAMMA_W * np.sqrt(p["g_rel"]) + OI_W * np.sqrt(p["oi_rel"])
    return p


# --------------------------------------------------------------------------
# History: one settled chain per session, matched to daily_metrics
# --------------------------------------------------------------------------

def session_chains(histdir, metrics, symbol):
    """[(session date, spot, raw chain)] oldest first, one per session.

    Raw folders are not reliably named by session (early ones carry the fetch
    date, and weekend re-serves left duplicate folders). daily_metrics is the
    clean record -- duplicates were quarantined -- so a raw file is used only
    if its feed_ts matches a metrics row, and that row supplies the session
    date and the official-close spot."""
    m = metrics[metrics["symbol"] == symbol]
    if m.empty:
        return []
    by_feed = {str(r["feed_ts"]): r for _, r in m.iterrows()}
    out = {}
    for path in glob.glob(os.path.join(histdir, "raw", "*", f"{symbol}.parquet")):
        try:
            raw = pd.read_parquet(path)
        except Exception:
            continue
        if raw.empty:
            continue
        r = by_feed.get(str(raw["feed_ts"].iloc[0]))
        if r is None:
            continue
        sess = pd.to_datetime(r.get("session_date") or r.get("feed_date")).date()
        out[sess] = (sess, float(r["spot"]), raw)
    return [out[k] for k in sorted(out)]


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------

def _held(p, level, sign, bin_size):
    if p is None:
        return False
    near = p[(p.index - level).__abs__() <= bin_size * 1.01]
    near = near[np.sign(near["net"]) == sign]
    return bool(len(near)) and float(near["rank"].max()) >= HELD_PCT


def persistence(level, sign, bin_size, prior, today):
    """prior: [(session, profile)] oldest first, excluding today."""
    prior = prior[-WALL_LOOKBACK:]
    n = len(prior)
    same = [_held(p, level, sign, bin_size) for _, p in prior]
    weights = [session_weight(s, today) for s, _ in prior]
    streak = 1
    for ok in reversed(same):
        if not ok:
            break
        streak += 1
    wsum = sum(weights)
    share = (sum(w for w, ok in zip(weights, same) if ok) / wsum) if wsum else 0.0
    out = dict(days=n, held=sum(same), streak=streak, share=share,
               held_post=None, days_post=None, last_exp=None)
    if prior:
        exps = expirations_between(prior[0][0], today)
        if exps:
            le = exps[-1][0]
            post = [ok for ok, (s, _) in zip(same, prior) if s >= le]
            out.update(last_exp=le, held_post=sum(post), days_post=len(post))
    if n < WALL_MIN_HISTORY:
        out["strength"], pts = "new", 0.5
    elif not same[-1]:
        out["strength"], pts = "fresh", share * 0.5
    elif share >= 0.8 or streak >= 6:
        out["strength"], pts = "strong", max(share, 0.8)
    elif share >= 0.5 or streak >= 3:
        out["strength"], pts = "steady", max(share, 0.5)
    else:
        out["strength"], pts = "shifting", share
    out["pts"] = pts
    return out


def levels(histdir, metrics, symbol, bin_size, row):
    """Graded support/resistance inside +/-1 sigma, or None."""
    chains = session_chains(histdir, metrics, symbol)
    if not chains:
        return None
    today, spot, raw = chains[-1]
    # The card's settled row is the authority; if its session has no raw
    # file, there is nothing current to grade.
    row_sess = pd.to_datetime(row.get("session_date") or row.get("feed_date")).date()
    if today != row_sess:
        return None
    spot = float(row["spot"])
    chain = raw.copy()
    chain["dte"] = pd.to_numeric(chain["dte"], errors="coerce")
    iv = atm_iv(chain[chain["dte"] > 0], spot)
    if not iv or math.isnan(iv):
        return None
    sigma = spot * iv * math.sqrt(SIGMA_DAYS / 365.0)
    lo, hi = spot - sigma, spot + sigma

    p = profile(raw, spot, bin_size)
    if p is None or len(p) < 3:
        return None
    prior = [(s, profile(r, sp, bin_size)) for s, sp, r in chains[:-1]]

    # local density peaks inside the band, outside the ATM exclusion
    d = p["density"]
    peak = (d >= d.shift(1).fillna(-1)) & (d >= d.shift(-1).fillna(-1))
    cand = p[peak & (p.index >= lo) & (p.index <= hi)
             & ((p.index - spot).__abs__() > spot * EXCLUDE)
             & (p["rank"] >= LEVEL_MIN_RANK)]
    # The call and put walls are always levels when they sit in the band,
    # even when a neighbouring bin with more open interest out-peaks them.
    wall_bins = {}
    for col in ("call_wall", "put_wall"):
        try:
            w = round(float(row.get(col)) / bin_size) * bin_size
        except (TypeError, ValueError):
            continue
        if w in p.index and lo <= w <= hi and abs(w - spot) > spot * EXCLUDE:
            wall_bins[w] = col
    cand = p.loc[sorted(set(cand.index) | set(wall_bins))]

    rows = []
    for k, c in cand.iterrows():
        sign = 1.0 if c["net"] > 0 else -1.0
        per = persistence(k, sign, bin_size, prior, today)
        score = PERSIST_W * per["pts"] + DENSITY_W * float(c["density"])
        grade = next(g for cut, g in GRADES if score >= cut)
        if per["strength"] == "new" and grade == "A":
            grade = "B"
        rows.append(dict(
            strike=float(k), side="R" if k > spot else "S",
            dist=k / spot - 1, sig=(k - spot) / sigma,
            dmoves=(k - spot) / (spot * iv / 16.0),
            net=float(c["net"]), oi=float(c["oi"]),
            density=float(c["density"]), g_rel=float(c["g_rel"]),
            oi_rel=float(c["oi_rel"]), score=round(score, 1), grade=grade,
            label=GRADE_LABEL[grade], wall=wall_bins.get(k, ""), **per))

    def pick(side):
        r = sorted((x for x in rows if x["side"] == side),
                   key=lambda x: (not x["wall"], -x["score"]))[:LEVELS_PER_SIDE]
        return sorted(r, key=lambda x: abs(x["dist"]))

    return dict(spot=spot, iv=iv, sigma=sigma, lo=lo, hi=hi, session=today,
                n_sessions=len(prior), resistance=pick("R"), support=pick("S"))
