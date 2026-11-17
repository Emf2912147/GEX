#!/usr/bin/env python3
"""
Opex / flow-intensity outcome log (requested by Eugenio, 2026-11-17).

Records, for every graded session, what the vanna/charm read was
(flow_intensity.py) and what the market did next, so the January 2027
re-test has data instead of opinions. Nothing here feeds a trade.

history/flow_outcomes.csv, one row per session and symbol (SPX, SPY, QQQ,
IWM, SMH), rebuilt in full on every daily capture so forward returns fill
in as sessions pass:

    score, grade, direction, charm_pct, vanna_pct, front_charm_share,
    sessions_to_opex, opex_date            the read at that session's close
    rolloff_flag        rolloff_risk: <= 5 sessions to the front monthly and
                        >= 50% of the charm bid ends with it
    post_opex           one of the first 5 sessions after a monthly expiry
    vanna_selling_high  vanna potential is dealer SELLING (IV below realized)
                        and its size is >= the 60th percentile
    vanna_buying_high   the same for dealer buying (IV above realized)
    charm_bid_high      charm is dealer buying at >= the 60th percentile
    ret_1d, ret_5d      close-to-close log return to 1 and 5 sessions later
    rv_5d               annualized realized vol of the next 5 daily returns
    ret_to_opex         close to the front monthly's close (if passed)

Closes are the pipeline's own anchored session closes (daily_metrics.csv,
one per session). A forward column stays blank until its session exists.

`python flow_outcomes.py` rebuilds the file and prints the summary:
for each flag, n readings, the number of distinct monthly cycles they came
from (the honest sample size: readings inside one cycle are not
independent), mean and share-positive 5-day return, and mean 5-day realized
vol, each against all other graded readings. `--opex` adds the per-expiry
table: for every monthly expiry, the move from 5 sessions before it to the
expiry, and from the expiry to 5 sessions after.
"""

from __future__ import annotations

import argparse
import math
import os
from datetime import timedelta

import numpy as np
import pandas as pd

import flow_intensity as fi

OUTFILE = "flow_outcomes.csv"
SYMBOLS = ("SPX", "SPY", "QQQ", "IWM", "SMH")
HIGH_PCT = 60.0
POST_OPEX_SESSIONS = 5
FLAGS = ("rolloff_flag", "post_opex", "vanna_selling_high", "vanna_buying_high", "charm_bid_high")
COLUMNS = ["session_date", "symbol", "score", "grade", "direction", "charm_pct", "vanna_pct",
           "front_charm_share", "sessions_to_opex", "opex_date", *FLAGS,
           "close", "ret_1d", "ret_5d", "rv_5d", "ret_to_opex"]


def closes(histdir: str) -> dict[str, pd.Series]:
    """{symbol: Series of session closes indexed by 'YYYY-MM-DD', oldest first}."""
    m = pd.read_csv(os.path.join(histdir, "daily_metrics.csv"), dtype={"feed_ts": str})
    out = {}
    for sym, g in m.groupby("symbol"):
        g = (g.sort_values("feed_ts").drop_duplicates("session_date", keep="last")
              .sort_values("session_date"))
        s = pd.to_numeric(g["spot"], errors="coerce")
        s.index = g["session_date"].astype(str)
        out[str(sym)] = s.dropna()
    return out


def last_opex_before(d):
    """The most recent monthly expiry strictly before date `d`."""
    o = fi.monthly_opex(d.replace(day=1))
    if o >= d:
        prev_month = d.replace(day=1) - timedelta(days=1)
        o = fi.monthly_opex(prev_month.replace(day=1))
    return o


def forward(px: pd.Series, session: str, n: int):
    """Log return from `session` to n sessions later (None if not there yet)."""
    if session not in px.index:
        return None
    i = px.index.get_loc(session)
    if i + n >= len(px):
        return None
    return float(math.log(px.iloc[i + n] / px.iloc[i]))


def forward_rv(px: pd.Series, session: str, n: int = 5):
    if session not in px.index:
        return None
    i = px.index.get_loc(session)
    if i + n >= len(px):
        return None
    r = np.diff(np.log(px.iloc[i:i + n + 1].to_numpy(float)))
    return float(np.std(r, ddof=1) * math.sqrt(252))


def build(histdir: str) -> pd.DataFrame:
    hist = fi.load_history(histdir)
    px_all = closes(histdir)
    rows = []
    for sym in SYMBOLS:
        h = hist[hist["symbol"] == sym].sort_values("session_date")
        px = px_all.get(sym, pd.Series(dtype=float))
        for session in h["session_date"].astype(str):
            b = fi.score_from_history(hist, sym, session)
            if not b or b.get("score") is None:
                continue
            d = pd.Timestamp(session).date()
            prev = last_opex_before(d)
            post = prev is not None and 1 <= fi.sessions_between(prev, d) <= POST_OPEX_SESSIONS
            vp, vpct = b.get("vanna_potential_usd"), b.get("vanna_pct")
            cpct = b.get("charm_pct")
            opex = b.get("opex_date")
            to_opex = None
            if opex and session in px.index and opex in px.index:
                to_opex = float(math.log(px[opex] / px[session]))
            rows.append({
                "session_date": session, "symbol": sym,
                "score": b["score"], "grade": b["grade"], "direction": b["direction"],
                "charm_pct": cpct, "vanna_pct": vpct,
                "front_charm_share": b.get("front_charm_share"),
                "sessions_to_opex": b.get("sessions_to_opex"), "opex_date": opex,
                "rolloff_flag": bool(b.get("rolloff_risk")),
                "post_opex": bool(post),
                "vanna_selling_high": bool(vp is not None and vp < 0 and vpct is not None and vpct >= HIGH_PCT),
                "vanna_buying_high": bool(vp is not None and vp > 0 and vpct is not None and vpct >= HIGH_PCT),
                "charm_bid_high": bool(b["charm_usd_day"] > 0 and cpct is not None and cpct >= HIGH_PCT),
                "close": float(px[session]) if session in px.index else None,
                "ret_1d": forward(px, session, 1),
                "ret_5d": forward(px, session, 5),
                "rv_5d": forward_rv(px, session, 5),
                "ret_to_opex": to_opex,
            })
    return pd.DataFrame(rows, columns=COLUMNS)


def write(histdir: str) -> pd.DataFrame:
    df = build(histdir)
    df.to_csv(os.path.join(histdir, OUTFILE), index=False, float_format="%.6g")
    return df


def _cycle(session: str) -> str:
    return fi.monthly_opex(pd.Timestamp(session).date() + timedelta(days=1)).isoformat()


def summary(df: pd.DataFrame) -> pd.DataFrame:
    """Per flag: flagged readings vs every other graded reading, 5-day horizon."""
    d = df.dropna(subset=["ret_5d"]).copy()
    out = []
    for flag in FLAGS:
        on, off = d[d[flag]], d[~d[flag]]
        out.append({
            "flag": flag, "n": len(on), "cycles": on["session_date"].map(_cycle).nunique(),
            "ret_5d_mean_%": on["ret_5d"].mean() * 100 if len(on) else np.nan,
            "up_5d_%": (on["ret_5d"] > 0).mean() * 100 if len(on) else np.nan,
            "rv_5d_mean_%": on["rv_5d"].mean() * 100 if len(on) else np.nan,
            "others_n": len(off),
            "others_ret_5d_%": off["ret_5d"].mean() * 100 if len(off) else np.nan,
            "others_rv_5d_%": off["rv_5d"].mean() * 100 if len(off) else np.nan,
        })
    return pd.DataFrame(out)


def opex_table(histdir: str) -> pd.DataFrame:
    """Per monthly expiry and symbol: 5 sessions into it, 5 sessions out of it,
    and whether the roll-off flag was up 5 sessions before."""
    px_all = closes(histdir)
    hist = fi.load_history(histdir)
    rows = []
    for sym in SYMBOLS:
        px = px_all.get(sym)
        if px is None or px.empty:
            continue
        days = list(px.index)
        opexes = sorted({fi.monthly_opex(pd.Timestamp(s).date()).isoformat() for s in days})
        for o in opexes:
            if o not in px.index:
                continue
            i = days.index(o)
            pre = days[i - 5] if i >= 5 else None
            flag = None
            if pre is not None:
                b = fi.score_from_history(hist, sym, pre)
                flag = None if not b or b.get("score") is None else bool(b["rolloff_risk"])
            rows.append({
                "opex": o, "symbol": sym, "rolloff_flag_5_before": flag,
                "into_opex_%": (math.log(px[o] / px[pre]) * 100) if pre else None,
                "after_opex_%": (math.log(px[days[i + 5]] / px[o]) * 100) if i + 5 < len(days) else None,
            })
    return pd.DataFrame(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--histdir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "history"))
    ap.add_argument("--opex", action="store_true", help="also print the per-expiry table")
    a = ap.parse_args(argv)
    df = write(a.histdir)
    print(f"{OUTFILE}: {len(df)} graded readings, {df['ret_5d'].notna().sum()} with a 5-day outcome")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(summary(df).round(2).to_string(index=False))
        if a.opex:
            print()
            print(opex_table(a.histdir).round(2).to_string(index=False))
    print("Small samples: count cycles, not readings. Information only until the Jan-2027 re-test.")


if __name__ == "__main__":
    main()
