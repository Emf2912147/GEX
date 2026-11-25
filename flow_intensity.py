#!/usr/bin/env python3
"""
Vanna / charm flow intensity: how strong the dealer hedging flows are today,
as a 0-100 score and an A-F grade (requested by Eugenio, 2026-11-17).

What is measured (user rule 2026-11-17): every expiry from 0.5 to
HORIZON_DTE (65) calendar days. That window ALWAYS holds the front monthly
and the next one (the next monthly is never more than 64 days from a
session, checked 2026-2030), so in the run-up to an expiration the
following month's exposure is counted instead of appearing all at once 30
days out. A fixed horizon also means no step on expiration day: contracts
roll in one expiry at a time.

Flows are in DOLLARS of underlying the dealers trade (dealers assumed long
calls / short puts, Black-Scholes greeks from dealer_flows.py):

    charm_usd_day         dealer hedge trade from one calendar day of time
                          decay. Positive = dealers BUY.
    vanna_usd_per_volpt   dealer hedge trade if implied vol FALLS one point.
    front_* / next_*      the same split at the front monthly: front = every
                          expiry on or before it; next = everything after it
                          in the window (the next monthly and its weeklies).
                          front + next = total.

Why dollars and not basis points of open-interest notional: any notional
base jumps when a large quarterly (SPX December holds ~60% of the open
interest within 65 days) enters it, which shrank every reading by up to half
with no change in the flows. The percentile ranking below already makes
symbols comparable.

On its own the vanna number is a sensitivity, not a flow: nothing happens
unless implied vol moves. This module turns it into an expected flow with
an explicit, simple assumption: implied vol drifts toward realized vol.

    vol_gap_pts           (30d ATM IV - 20d realized vol) x 100, clipped to
                          +/-10 points. Positive = IV rich, room to fall.
    vanna_potential_usd   vanna_usd_per_volpt x vol_gap_pts. Positive =
                          dealer buying if IV closes the gap; negative =
                          dealer selling (IV below realized, likely to rise).

Intensity ranks each flow's SIZE against the symbol's own stored history
(the same session and the up-to-60 before it), so "strong" means strong for
this market, not big in dollars:

    charm_pct            percentile of |charm_usd_day|
    vanna_pct            percentile of |vanna_potential_usd|
    score                0.5 x charm_pct + 0.5 x vanna_pct   (0-100)

    Grade  A >= 80 very strong   B 60-79 strong   C 40-59 normal
           D 20-39 weak          F < 20 very weak
    Fewer than MIN_HISTORY prior sessions: no grade ("building history").

Direction is reported separately: both flows positive = dealer buying
(a supportive bid), both negative = dealer selling, otherwise mixed.

Opex roll-off: the share of today's charm flow that comes from the front
monthly and its weeklies (front_charm_share) and how many sessions away the
front monthly is. Flagged at <= 5 sessions and >= 50%: most of today's
charm bid ends with that expiration (the post-opex "window of weakness").
What remains is the next month's piece, shown separately.

Status: INFORMATION ONLY. The equal weights and the IV-toward-RV
assumption are starting choices, not fitted. dealer_flows' own support
ratio showed no reliable forward-return power over 64 sessions
(2026-10-17), and this score has not been tested either; it describes
the pressure dealers are under, not where price goes next.

Storage: history/flow_metrics.csv, one row per session and symbol, kept
forever (raw chains are pruned after 90 days, so the percentile history
has to live in a derived file). The daily capture upserts the newest
session; `python flow_intensity.py --backfill` rebuilds it from every
stored raw chain.
"""

from __future__ import annotations

import argparse
import math
import os
from datetime import date, timedelta

import numpy as np
import pandas as pd

import dealer_flows as dfl
import market_calendar as mc
import vol_metrics as vm

HISTORY_FILE = "flow_metrics.csv"
COLUMNS = ["session_date", "symbol", "charm_usd_day", "vanna_usd_per_volpt",
           "iv30", "rv20", "vol_gap_pts", "vanna_potential_usd",
           "sessions_to_opex", "opex_date", "opex_oi_share",
           "next_opex_date", "next_oi_share",
           "front_charm_usd_day", "front_vanna_usd_per_volpt",
           "next_charm_usd_day", "next_vanna_usd_per_volpt", "window_max_dte"]
HORIZON_DTE = 65.0     # calendar days; always holds the front and next monthly
ROLLOFF_SESSIONS = 5   # roll-off flag: front monthly this close ...
ROLLOFF_SHARE = 0.5    # ... and carrying at least this share of the charm bid
LOOKBACK = 60          # prior sessions ranked against
MIN_HISTORY = 15       # prior sessions needed before a grade is given
GAP_CLIP = 10.0        # vol points
RV_MAX_AGE_DAYS = 5
WEIGHTS = {"charm": 0.5, "vanna": 0.5}
GRADES = [(80, "A", "very strong"), (60, "B", "strong"), (40, "C", "normal"),
          (20, "D", "weak"), (0, "F", "very weak")]
COMPOSITE = ("SPX", "QQQ", "IWM")


# ----------------------------------------------------------------- calendar

def monthly_opex(d: date) -> date:
    """The monthly expiration on or after `d`: third Friday, or the trading
    day before it when that Friday is a market holiday."""
    for k in range(3):
        y, m = d.year + (d.month - 1 + k) // 12, (d.month - 1 + k) % 12 + 1
        first = date(y, m, 1)
        fri = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
        while not mc.is_trading_day(fri):
            fri -= timedelta(days=1)
        if fri >= d:
            return fri
    raise ValueError(d)  # pragma: no cover


def sessions_between(a: date, b: date) -> int:
    """Trading sessions after `a` up to and including `b` (0 if b <= a)."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if mc.is_trading_day(d):
            n += 1
    return n


# ----------------------------------------------------------------- one session

def _iv30(raw: pd.DataFrame, spot: float) -> float | None:
    df = raw.copy()
    df["dte"] = pd.to_numeric(df["dte"], errors="coerce")
    df = df[df["dte"] > 0]
    term = vm._atm_term(df, spot)
    iv, _ = vm.constant_maturity_iv(term, 30.0)
    return iv


def _rv20(histdir: str, metrics: pd.DataFrame | None, symbol: str, session: str) -> float | None:
    """20d realized vol as of `session`: realized_vol.csv (index_history daily closes)
    when it has a reading within RV_MAX_AGE_DAYS, else this pipeline's own
    captured closes up to the session."""
    path = os.path.join(histdir, "realized_vol.csv")
    if os.path.exists(path):
        rv = pd.read_csv(path)
        rv = rv[(rv["symbol"] == symbol) & (rv["rv_asof_date"].astype(str) <= session)]
        rv = rv.dropna(subset=["hv20"])
        if not rv.empty:
            last = rv.sort_values(["rv_asof_date", "capture_ts"]).iloc[-1]
            age = (pd.Timestamp(session) - pd.Timestamp(str(last["rv_asof_date"]))).days
            if age <= RV_MAX_AGE_DAYS:
                return float(last["hv20"])
    if metrics is None:
        return None
    m = metrics[metrics["session_date"].astype(str) <= session]
    return vm.realized_vol(vm._closes_from_metrics(m, symbol))


def next_monthly(opex: date) -> date:
    """The monthly expiration after `opex`."""
    return monthly_opex(opex + timedelta(days=1))


def two_month_flows(raw: pd.DataFrame, symbol: str, front: date, nxt: date) -> dict | None:
    """Charm and vanna in dollars over every expiry from 0.5 to HORIZON_DTE
    days, split at the front monthly (see the module docstring)."""
    df = raw.copy()
    df["dte"] = pd.to_numeric(df["dte"], errors="coerce")
    keep = ((df["dte"] >= dfl.MIN_DTE) & (df["dte"] <= HORIZON_DTE)
            & (df["iv"] > 0) & (df["oi"] > 0))
    df = df[keep]
    if df.empty:
        return None
    exp = pd.to_datetime(df["expiry"]).dt.date
    S = float(df["spot"].iloc[0])
    q = dfl.DIVIDEND_YIELD.get(symbol.upper(), 0.0)
    T = df["dte"].to_numpy(float) / 365.0
    K = df["strike"].to_numpy(float)
    iv = df["iv"].to_numpy(float)
    is_call = (df["type"] == "C").to_numpy()
    oi = df["oi"].to_numpy(float)
    weight = np.where(is_call, 1.0, -1.0) * oi * 100.0 * S
    # hedge trade = minus the change in dealer delta (as in dealer_flows)
    charm_usd = -(weight * dfl.charm(S, K, T, iv, is_call, q)) / 365.0
    vanna_usd = (weight * dfl.vanna(S, K, T, iv, q)) * 0.01
    is_front = (exp <= front).to_numpy()
    total_oi = float(oi.sum())
    return {
        "charm_usd_day": float(charm_usd.sum()),
        "vanna_usd_per_volpt": float(vanna_usd.sum()),
        "front_charm_usd_day": float(charm_usd[is_front].sum()),
        "front_vanna_usd_per_volpt": float(vanna_usd[is_front].sum()),
        "next_charm_usd_day": float(charm_usd[~is_front].sum()),
        "next_vanna_usd_per_volpt": float(vanna_usd[~is_front].sum()),
        "opex_oi_share": float(oi[(exp == front).to_numpy()].sum()) / total_oi,
        "next_oi_share": float(oi[(exp == nxt).to_numpy()].sum()) / total_oi,
        "window_max_dte": float(df["dte"].max()),
    }


def session_row(raw: pd.DataFrame, symbol: str, session: str, histdir: str,
                metrics: pd.DataFrame | None) -> dict | None:
    """One stored row of flow metrics for a symbol's chain."""
    sday = pd.Timestamp(session).date()
    # the book is end-of-day: on expiration day the monthly is already gone,
    # so the front is the following one
    opex = monthly_opex(sday + timedelta(days=1))
    nxt = next_monthly(opex)
    f = two_month_flows(raw, symbol, opex, nxt)
    if f is None:
        return None
    spot = float(raw["spot"].iloc[0])
    iv30 = _iv30(raw, spot)
    rv20 = _rv20(histdir, metrics, symbol, session)
    gap = None
    if iv30 and rv20:
        gap = max(-GAP_CLIP, min(GAP_CLIP, (iv30 - rv20) * 100.0))
    r3 = lambda v: round(v)  # noqa: E731  (whole dollars)
    return {
        "session_date": session, "symbol": symbol,
        "charm_usd_day": r3(f["charm_usd_day"]),
        "vanna_usd_per_volpt": r3(f["vanna_usd_per_volpt"]),
        "iv30": round(iv30, 5) if iv30 else None,
        "rv20": round(rv20, 5) if rv20 else None,
        "vol_gap_pts": round(gap, 3) if gap is not None else None,
        "vanna_potential_usd": r3(f["vanna_usd_per_volpt"] * gap) if gap is not None else None,
        "sessions_to_opex": sessions_between(sday, opex),
        "opex_date": opex.isoformat(),
        "opex_oi_share": round(f["opex_oi_share"], 4),
        "next_opex_date": nxt.isoformat(),
        "next_oi_share": round(f["next_oi_share"], 4),
        "front_charm_usd_day": r3(f["front_charm_usd_day"]),
        "front_vanna_usd_per_volpt": r3(f["front_vanna_usd_per_volpt"]),
        "next_charm_usd_day": r3(f["next_charm_usd_day"]),
        "next_vanna_usd_per_volpt": r3(f["next_vanna_usd_per_volpt"]),
        "window_max_dte": round(f["window_max_dte"], 1),
    }


# ----------------------------------------------------------------- storage

def load_history(histdir: str) -> pd.DataFrame:
    path = os.path.join(histdir, HISTORY_FILE)
    if not os.path.exists(path):
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path, dtype={"session_date": str, "opex_date": str})
    return df


def upsert(histdir: str, rows: list[dict]) -> None:
    hist = load_history(histdir)
    new = pd.DataFrame(rows, columns=COLUMNS).drop_duplicates(["session_date", "symbol"], keep="last")
    if not hist.empty:
        keys = set(zip(new["session_date"], new["symbol"]))
        hist = hist[[k not in keys for k in zip(hist["session_date"], hist["symbol"])]]
        new = pd.concat([hist, new], ignore_index=True) if not new.empty else hist
    new = new.sort_values(["session_date", "symbol"])
    new.to_csv(os.path.join(histdir, HISTORY_FILE), index=False)


# ----------------------------------------------------------------- scoring

def pct_rank(values, x) -> float:
    """Share of `values` at or below `x`, 0-100 (ties count half)."""
    v = np.asarray([a for a in values if a is not None and not pd.isna(a)], float)
    if not len(v):
        return float("nan")
    return float(((v < x).sum() + 0.5 * (v == x).sum()) / len(v) * 100.0)


def grade_for(score: float | None):
    if score is None or pd.isna(score):
        return None, None
    for floor, g, word in GRADES:
        if score >= floor:
            return g, word
    return "F", "very weak"  # pragma: no cover


def _direction(charm: float, vanna_pot: float | None) -> str:
    signs = [np.sign(charm)] + ([np.sign(vanna_pot)] if vanna_pot is not None else [])
    signs = [s for s in signs if s != 0]
    if signs and all(s > 0 for s in signs):
        return "dealer buying"
    if signs and all(s < 0 for s in signs):
        return "dealer selling"
    return "mixed"


def _num(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v) else float(v)


def _bucket(today: dict, which: str) -> dict | None:
    """Front-monthly or next-monthly piece of today's flows (same units as the
    totals, so front + next = total). None for rows stored before 2026-11-17."""
    ch = _num(today.get(f"{which}_charm_usd_day"))
    va = _num(today.get(f"{which}_vanna_usd_per_volpt"))
    if ch is None or va is None:
        return None
    gap = _num(today.get("vol_gap_pts"))
    return {"expiry": today.get("opex_date" if which == "front" else "next_opex_date"),
            "charm_usd_day": round(ch), "vanna_usd_per_volpt": round(va),
            "vanna_potential_usd": round(va * gap) if gap is not None else None,
            "oi_share_monthly": _num(today.get("opex_oi_share" if which == "front" else "next_oi_share"))}


def score_row(today: dict, prior: pd.DataFrame) -> dict:
    """Intensity for one session against the symbol's prior sessions
    (oldest first; only the last LOOKBACK are used). The ranking pool
    includes today, so a record reading scores near 100."""
    prior = prior.tail(LOOKBACK)
    n = len(prior)
    charm = float(today["charm_usd_day"])
    vpot = today.get("vanna_potential_usd")
    vpot = None if vpot is None or pd.isna(vpot) else float(vpot)

    charm_pool = list(prior["charm_usd_day"].abs()) + [abs(charm)]
    charm_pct = pct_rank(charm_pool, abs(charm))
    vanna_pct, vanna_hist = None, 0
    if vpot is not None:
        vp = prior["vanna_potential_usd"].dropna().abs()
        vanna_hist = len(vp)
        if len(vp) >= MIN_HISTORY:
            vanna_pct = pct_rank(list(vp) + [abs(vpot)], abs(vpot))

    score = None
    if n >= MIN_HISTORY:
        if vanna_pct is not None:
            score = WEIGHTS["charm"] * charm_pct + WEIGHTS["vanna"] * vanna_pct
        else:  # no IV/RV for vanna: charm alone, said so in the note
            score = charm_pct
    g, word = grade_for(score)

    opex_share = today.get("opex_oi_share")
    s_to = today.get("sessions_to_opex")
    fch = _num(today.get("front_charm_usd_day"))
    front_share = fch / charm if fch is not None and charm > 0 else None
    rolloff = bool(front_share is not None and s_to is not None and not pd.isna(s_to)
                   and int(s_to) <= ROLLOFF_SESSIONS and front_share >= ROLLOFF_SHARE)

    note = None
    if n < MIN_HISTORY:
        note = f"building history ({n} of {MIN_HISTORY} sessions)"
    elif vpot is None:
        note = "vanna not scored (no IV/RV reading); score is charm only"
    elif vanna_pct is None:
        note = (f"vanna not scored yet ({vanna_hist} of {MIN_HISTORY} sessions with an IV/RV reading); "
                "score is charm only")

    return {
        "session_date": today["session_date"],
        "score": round(score, 1) if score is not None else None,
        "grade": g, "label": word,
        "direction": _direction(charm, vpot),
        "charm_usd_day": round(charm),
        "charm_pct": round(charm_pct, 1) if score is not None else None,
        "vanna_usd_per_volpt": round(float(today["vanna_usd_per_volpt"])),
        "vol_gap_pts": None if today.get("vol_gap_pts") is None or pd.isna(today.get("vol_gap_pts"))
                       else round(float(today["vol_gap_pts"]), 2),
        "vanna_potential_usd": None if vpot is None else round(vpot),
        "vanna_pct": round(vanna_pct, 1) if vanna_pct is not None else None,
        "opex_date": today.get("opex_date"),
        "sessions_to_opex": None if s_to is None else int(s_to),
        "opex_oi_share": None if opex_share is None or pd.isna(opex_share) else round(float(opex_share), 3),
        "front_charm_share": None if front_share is None else round(front_share, 3),
        "rolloff_risk": rolloff,
        "front": _bucket(today, "front"),
        "next": _bucket(today, "next"),
        "history_sessions": n,
        "note": note,
        "status": "information only (untested)",
    }


def score_from_history(hist: pd.DataFrame, symbol: str, session: str | None = None) -> dict | None:
    """Score `symbol` on `session` (default: its newest stored session)."""
    h = hist[hist["symbol"] == symbol].sort_values("session_date")
    if h.empty:
        return None
    if session is None:
        session = str(h["session_date"].iloc[-1])
    today = h[h["session_date"].astype(str) == session]
    if today.empty:
        return None
    prior = h[h["session_date"].astype(str) < session]
    return score_row(today.iloc[-1].to_dict(), prior)


def composite(blocks: dict) -> dict | None:
    """Market read: the mean score of SPX, QQQ and IWM (the ones graded)."""
    scores = [blocks[s]["score"] for s in COMPOSITE
              if blocks.get(s) and blocks[s].get("score") is not None]
    if not scores:
        return None
    sc = float(np.mean(scores))
    g, word = grade_for(sc)
    dirs = {blocks[s]["direction"] for s in COMPOSITE if blocks.get(s)}
    return {"score": round(sc, 1), "grade": g, "label": word,
            "direction": dirs.pop() if len(dirs) == 1 else "mixed",
            "symbols": [s for s in COMPOSITE if blocks.get(s) and blocks[s].get("score") is not None]}


# ----------------------------------------------------------------- entry points

def block(histdir: str, raw: pd.DataFrame, symbol: str, session: str,
          metrics: pd.DataFrame | None, write: bool = True) -> dict | None:
    """The feed's "flow_intensity" block. With write=True (the daily capture)
    today's row is upserted into history/flow_metrics.csv first."""
    row = session_row(raw, symbol, session, histdir, metrics)
    if row is None:
        return None
    hist = load_history(histdir)
    if write:
        upsert(histdir, [row])
        hist = load_history(histdir)
    else:
        hist = pd.concat([hist[~((hist["symbol"] == symbol) & (hist["session_date"] == session))],
                          pd.DataFrame([row], columns=COLUMNS)], ignore_index=True)
    return score_from_history(hist, symbol, session)


def backfill(histdir: str) -> int:
    metrics_path = os.path.join(histdir, "daily_metrics.csv")
    metrics = pd.read_csv(metrics_path, dtype={"feed_ts": str}) if os.path.exists(metrics_path) else None
    rawdir = os.path.join(histdir, "raw")
    rows = []
    for folder in sorted(os.listdir(rawdir)):
        for fn in sorted(os.listdir(os.path.join(rawdir, folder))):
            if not fn.endswith(".parquet"):
                continue
            sym = fn[:-8]
            try:
                raw = pd.read_parquet(os.path.join(rawdir, folder, fn))
                # early folders are named by capture date; the chain's own
                # feed time says which session it describes
                sday = vm._session_date(raw)
                while not mc.is_trading_day(sday):   # late feeds land on a weekend/holiday
                    sday -= timedelta(days=1)
                session = sday.isoformat()
                r = session_row(raw, sym, session, histdir, metrics)
            except Exception as ex:  # pragma: no cover - report and continue
                print(f"skip {session} {sym}: {ex}")
                continue
            if r is not None:
                rows.append(r)
    upsert(histdir, rows)
    return len(load_history(histdir))


def usd(x) -> str:
    """$ amount for display: +$1.2bn, -$350m."""
    if x is None or pd.isna(x):
        return "n/a"
    sign = "+" if x >= 0 else "-"
    x = abs(float(x))
    return f"{sign}${x / 1e9:.1f}bn" if x >= 1e9 else f"{sign}${x / 1e6:.0f}m"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--histdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "history"))
    ap.add_argument("--backfill", action="store_true", help="rebuild flow_metrics.csv from history/raw")
    ap.add_argument("--symbols", default="SPX,SPY,QQQ,IWM,SMH")
    a = ap.parse_args(argv)
    if a.backfill:
        print(f"{HISTORY_FILE}: {backfill(a.histdir)} rows")
    hist = load_history(a.histdir)
    blocks = {s: score_from_history(hist, s) for s in a.symbols.split(",")}
    for s, b in blocks.items():
        if b is None:
            print(f"{s}: no data")
            continue
        fr, nx = b.get("front") or {}, b.get("next") or {}
        print(f"{s} {b['session_date']}: score {b['score']} grade {b['grade']} ({b['label']}), {b['direction']}; "
              f"charm {usd(b['charm_usd_day'])}/day (p{b['charm_pct']}), "
              f"vanna {usd(b['vanna_potential_usd'])} at gap {b['vol_gap_pts']} pts (p{b['vanna_pct']}); "
              f"front {fr.get('expiry')} charm {usd(fr.get('charm_usd_day'))}, "
              f"next {nx.get('expiry')} charm {usd(nx.get('charm_usd_day'))} "
              f"vanna/pt {usd(nx.get('vanna_usd_per_volpt'))}; "
              f"opex in {b['sessions_to_opex']} sessions, roll-off {b['rolloff_risk']}"
              + (f"  [{b['note']}]" if b["note"] else ""))
    c = composite(blocks)
    if c:
        print(f"MARKET (SPX/QQQ/IWM): score {c['score']} grade {c['grade']} ({c['label']}), {c['direction']}")


if __name__ == "__main__":
    main()
