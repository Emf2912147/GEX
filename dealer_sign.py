#!/usr/bin/env python3
"""dealer_sign.py - how much of the gamma regime is assumption rather than data.

WHY THIS EXISTS
---------------
Open interest counts contract PAIRS.  It does not say which side the dealer is
on, and dealer gamma depends entirely on that.  gamma_exposure.py resolves it
by convention, in one line (gamma_exposure.py:194, :296):

    sign = np.where(is_call, 1.0, -1.0)

"Dealers are long every call and short every put" -- customers overwrite calls
and buy puts.  A defensible story for index products in aggregate, applied
uniformly to every strike, tenor and symbol, with nothing behind it from the
store.  That one line sets the sign of every strike's contribution, which sets
where cumulative gamma crosses zero, which is the flip, which is the regime,
which selects the cell.  The decision table sits on it.

This module measures the same quantity instead, using net buying pressure
(Bollen & Whaley 2004, "Does Net Buying Pressure Affect the Shape of Implied
Volatility Functions?").  When customers buy a strike in size the dealer must
sell it and charges up for the risk, so that strike's IV richens against the
rest of its expiry.  Per contract, session over session:

    OI grew AND the strike richened vs its own expiry's ATM move
        -> openers were buyers paying up   -> DEALER SHORT
    OI grew AND the strike cheapened vs ATM
        -> openers were sellers hitting bids -> DEALER LONG

Subtracting the expiry's ATM move is what makes it work: without it, any
market-wide vol move reads as heavy customer buying everywhere.

Contract-level readings are noisy, so they are pooled into buckets (type x
moneyness in expected-move units) and correlated across sessions.  A bucket
overrides the convention only when that correlation is significant; otherwise
the naive prior is kept.  The module is allowed to conclude "no signal", and
on most buckets it does.

WHAT THE OUTPUT IS FOR
----------------------
Not a better flip.  The GAP between the naive and signed flip is the product:
a measure of how much of the regime call is resting on the convention.  Where
they agree the flip is robust to the assumption; where they disagree the
regime call deserves less confidence.  That is the skill's existing rule for
two disagreeing GEX readings -- "that disagreement IS the finding, say so and
lower confidence" -- applied to the sign model.

INFORMATION ONLY.  Never a gate, never a resize, never flips a cell.  Same
standing as G7, G9 and the Flow line.

WHAT IT IS NOT
--------------
Not signed trade data.  Cboe/OCC open-close volume is the real answer and is
not reachable from here.  This infers direction from daily EOD snapshots with
no trade prints: a proxy, meaningful only in aggregate.

USAGE
-----
    python dealer_sign.py SPX --verify      # naive path == pipeline? (correctness)
    python dealer_sign.py SPX --line        # the state-vector line
    python dealer_sign.py SPX --signs       # per-bucket estimate + significance
    python dealer_sign.py SPX --report      # naive vs signed flip/walls
    python dealer_sign.py --summary         # all symbols, FDR-corrected
    python dealer_sign.py --backfill        # build history/sign_metrics.csv
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime

import numpy as np
import pandas as pd

import gamma_exposure as gx

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")
RAW = os.path.join(HIST, "raw")
DAILY = os.path.join(HIST, "daily_metrics.csv")

HISTORY_FILE = "sign_metrics.csv"
COLUMNS = ["session_date", "symbol", "type", "zbucket", "net_doi", "wdiv", "contracts"]

# Pipeline parameters, matched to gex_capture.py so the naive path reproduces
# daily_metrics.csv exactly.  --verify asserts this.
MAX_DTE = 30.0
GRID_WINDOW = 0.15
WALL_EXCLUDE = 0.004
POINTS = 241

# The flow measurement looks wider than the GEX window: front and next
# monthly, the span flow_intensity.py uses.
FLOW_MAX_DTE = 65.0
MIN_DTE = 0.5
MAX_GAP_DAYS = 5
T_CRIT = 2.0          # |t| before a bucket may override the naive sign
MIN_SESSIONS = 20     # bucket-sessions before the test is attempted
DM_ALERT = 0.5        # |DM| flip shift worth flagging: G3's stand-aside band

# Moneyness in units of the contract's own expected move, z = ln(K/S)/(iv*sqrt(T)),
# so a bucket means the same thing across symbols and tenors.
Z_EDGES = [-np.inf, -2.0, -0.5, 0.5, 2.0, np.inf]
Z_LABELS = ["z<-2", "-2..-0.5", "atm", "0.5..2", "z>2"]


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def _raw_dir(histdir: str | None = None) -> str:
    return os.path.join(histdir, "raw") if histdir else RAW


def sessions_available(symbol: str, histdir: str | None = None) -> list[str]:
    d = _raw_dir(histdir)
    if not os.path.isdir(d):
        return []
    return [s for s in sorted(os.listdir(d))
            if os.path.exists(os.path.join(d, s, f"{symbol}.parquet"))]


def all_symbols(histdir: str | None = None) -> list[str]:
    d = _raw_dir(histdir)
    out = set()
    for s in os.listdir(d):
        for f in os.listdir(os.path.join(d, s)):
            if f.endswith(".parquet"):
                out.add(f[:-8])
    return sorted(out)


def prep(df: pd.DataFrame, max_dte: float = MAX_DTE) -> pd.DataFrame:
    """Prepare a raw chain exactly as gex_capture.py prepares it."""
    df = gx.normalize_iv(df, quiet=True)
    df = df.copy()
    df["T"] = df["dte"] / 365.0
    return df[df["dte"] <= max_dte].copy()


def load_session(symbol: str, session: str, max_dte: float = MAX_DTE,
                 histdir: str | None = None) -> pd.DataFrame:
    path = os.path.join(_raw_dir(histdir), session, f"{symbol}.parquet")
    return prep(pd.read_parquet(path), max_dte)


def _spot(df: pd.DataFrame) -> float:
    return float(df["spot"].iloc[0])


def atm_iv(df: pd.DataFrame) -> float:
    """ATM IV of the nearest expiry, for daily-move math (DM = ATM IV / 16)."""
    if df.empty:
        return float("nan")
    spot = _spot(df)
    near = df[df["dte"] == df["dte"].min()]
    if near.empty:
        return float("nan")
    d = near.assign(dist=(near["strike"] - spot).abs()).sort_values("dist")
    return float(d["iv"].head(4).mean())


# --------------------------------------------------------------------------
# signed profile / walls -- identical math to gamma_exposure, sign injected
# --------------------------------------------------------------------------

def naive_sign(df: pd.DataFrame) -> np.ndarray:
    return np.where((df["type"] == "C").values, 1.0, -1.0)


def add_moneyness(df: pd.DataFrame, spot: float) -> pd.DataFrame:
    t = np.maximum(df["T"].to_numpy(), 1e-6)
    sig = np.maximum(df["iv"].to_numpy(), 1e-6)
    z = np.log(np.maximum(df["strike"].to_numpy(), 1e-9) / spot) / (sig * np.sqrt(t))
    return df.assign(z=z, zbucket=pd.cut(z, bins=Z_EDGES, labels=Z_LABELS,
                                         right=False).astype(str))


def model_sign(df: pd.DataFrame, spot: float, signs: pd.DataFrame | None) -> np.ndarray:
    """Dealer sign per row.  signs=None reproduces the naive convention."""
    nv = naive_sign(df)
    if signs is None or signs.empty:
        return nv
    d = add_moneyness(df, spot)
    lut = {(r["type"], r["zbucket"]): r["sign"] for _, r in signs.iterrows()}
    got = np.array([lut.get((t, z), np.nan) for t, z in zip(d["type"], d["zbucket"])],
                   dtype=float)
    return np.where(np.isnan(got), nv, got)


def profile(df: pd.DataFrame, lo: float, hi: float, signs=None, points: int = POINTS):
    """Net dealer gamma across a grid of hypothetical spots -> (grid, net, flip).

    Mirrors gamma_exposure.gamma_profile(), including recomputing BS gamma at
    every grid point, with the sign taken from the model.
    """
    spot = _spot(df)
    grid = np.linspace(lo, hi, points)
    size = df["oi"].values.astype(float)
    K, T, iv = df["strike"].values, df["T"].values, df["iv"].values
    w = model_sign(df, spot, signs) * size * gx.CONTRACT_SIZE

    net = np.empty_like(grid)
    for i, S in enumerate(grid):
        net[i] = np.sum(w * gx.bs_gamma(S, K, T, iv)) * S * S * 0.01

    flip = None
    for i in range(len(grid) - 1):
        a, b = net[i], net[i + 1]
        if a == 0:
            flip = float(grid[i])
            break
        if (a < 0 < b) or (a > 0 > b):
            flip = float(grid[i] - a * (grid[i + 1] - grid[i]) / (b - a))
            break
    return grid, net, flip


def by_strike(df: pd.DataFrame, signs=None):
    spot = _spot(df)
    g = model_sign(df, spot, signs) * df["gamma"].values * df["oi"].values \
        * gx.CONTRACT_SIZE * spot * spot * 0.01
    t = df.assign(gex=g)
    calls = t[t["type"] == "C"].groupby("strike")["gex"].sum().sort_index()
    puts = t[t["type"] == "P"].groupby("strike")["gex"].sum().sort_index()
    return t.groupby("strike")["gex"].sum().sort_index(), calls, puts


def state(df: pd.DataFrame, signs=None) -> dict:
    spot = _spot(df)
    lo, hi = spot * (1 - GRID_WINDOW), spot * (1 + GRID_WINDOW)
    _, _, flip = profile(df, lo, hi, signs)
    per, calls, puts = by_strike(df, signs)
    cw, pw, fell = gx.find_walls(calls, puts, spot, exclude=WALL_EXCLUDE)
    return {"spot": spot, "flip": flip, "call_wall": cw, "put_wall": pw,
            "net_gex": float(per.sum()), "fell_back": fell}


# --------------------------------------------------------------------------
# correctness
# --------------------------------------------------------------------------

def verify(symbol: str, n: int = 6, histdir: str | None = None) -> float:
    """Naive path vs daily_metrics.csv.  A mismatch means this module is wrong."""
    daily = os.path.join(histdir, "daily_metrics.csv") if histdir else DAILY
    dm = pd.read_csv(daily)
    dm = dm[dm["symbol"] == symbol]
    sess = sessions_available(symbol, histdir)[-n:]
    print(f"\n{symbol}  naive path vs daily_metrics.csv")
    print(f"  {'session':12} {'flip(mine)':>12} {'flip(feed)':>12} {'d':>8}   walls")
    worst = 0.0
    for s in sess:
        row = dm[dm["session_date"] == s]
        if row.empty:
            continue
        row = row.iloc[0]
        st = state(load_session(symbol, s, histdir=histdir), None)
        d = abs((st["flip"] or np.nan) - row["flip"])
        worst = max(worst, 0.0 if math.isnan(d) else d)
        ok = ("ok" if st["call_wall"] == row["call_wall"] else "CW DIFF") + "/" + \
             ("ok" if st["put_wall"] == row["put_wall"] else "PW DIFF")
        print(f"  {s:12} {st['flip']:>12,.2f} {row['flip']:>12,.2f} {d:>8.2f}   {ok}")
    print(f"  worst flip difference: {worst:.4f} "
          f"{'- reproduces the pipeline' if worst < 1.0 else '- DOES NOT MATCH'}")
    return worst


# --------------------------------------------------------------------------
# net buying pressure: per-session bucket rows
# --------------------------------------------------------------------------

def _atm_iv_by_expiry(df: pd.DataFrame, spot: float) -> pd.Series:
    d = df.assign(dist=(df["strike"] - spot).abs())
    nearest = d.groupby("expiry")["dist"].transform("min")
    return d[d["dist"] <= nearest + 1e-9].groupby("expiry")["iv"].mean()


def pair_rows(prev: pd.DataFrame, cur: pd.DataFrame, symbol: str,
              session: str) -> list[dict]:
    """Bucket-level OI flow and relative IV change between two prepared chains.

    Joined on the INTERSECTION of (expiry, strike, type), per the pipeline's own
    data-integrity rule: a contract that appeared has a level, not a delta, and
    one that vanished is not negative flow.
    """
    key = ["expiry", "strike", "type"]
    p = prev[(prev["iv"] > 0) & (prev["dte"] >= MIN_DTE)]
    c = cur[(cur["iv"] > 0) & (cur["dte"] >= MIN_DTE)]
    if p.empty or c.empty:
        return []
    sp, sc = _spot(p), _spot(c)
    ap, ac = _atm_iv_by_expiry(p, sp), _atm_iv_by_expiry(c, sc)

    m = (p[key + ["oi", "iv"]].rename(columns={"oi": "oi0", "iv": "iv0"})
         .merge(c[key + ["oi", "iv", "T"]].rename(columns={"oi": "oi1", "iv": "iv1"}),
                on=key, how="inner"))
    if m.empty:
        return []
    m["a0"], m["a1"] = m["expiry"].map(ap), m["expiry"].map(ac)
    m = m.dropna(subset=["a0", "a1"])
    if m.empty:
        return []
    m["doi"] = m["oi1"] - m["oi0"]
    m["div"] = (m["iv1"] - m["iv0"]) - (m["a1"] - m["a0"])
    m = add_moneyness(m.assign(iv=m["iv1"]), sc)
    m = m[m["doi"] != 0]
    if m.empty:
        return []

    rows = []
    for (ty, zb), g in m.groupby(["type", "zbucket"]):
        w = g["doi"].abs()
        if w.sum() <= 0:
            continue
        rows.append({"session_date": session, "symbol": symbol, "type": ty,
                     "zbucket": zb, "net_doi": float(g["doi"].sum()),
                     "wdiv": float(np.average(g["div"], weights=w)),
                     "contracts": int(len(g))})
    return rows


def prev_session(symbol: str, session: str, histdir: str | None = None) -> str | None:
    """Most recent stored session strictly before `session`, within the gap limit."""
    earlier = [s for s in sessions_available(symbol, histdir) if s < session]
    if not earlier:
        return None
    p = earlier[-1]
    try:
        if (datetime.fromisoformat(session) - datetime.fromisoformat(p)).days > MAX_GAP_DAYS:
            return None
    except ValueError:
        return None
    return p


def session_rows(histdir: str, raw: pd.DataFrame, symbol: str,
                 session: str) -> list[dict]:
    """This session's bucket rows, pairing `raw` against the previous capture."""
    p = prev_session(symbol, session, histdir)
    if p is None:
        return []
    prev = load_session(symbol, p, FLOW_MAX_DTE, histdir)
    return pair_rows(prev, prep(raw, FLOW_MAX_DTE), symbol, session)


# --------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------

def load_history(histdir: str) -> pd.DataFrame:
    path = os.path.join(histdir, HISTORY_FILE)
    if not os.path.exists(path):
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(path, dtype={"session_date": str, "symbol": str,
                                    "type": str, "zbucket": str})


def upsert(histdir: str, rows: list[dict]) -> None:
    if not rows:
        return
    hist = load_history(histdir)
    new = pd.DataFrame(rows, columns=COLUMNS).drop_duplicates(
        ["session_date", "symbol", "type", "zbucket"], keep="last")
    if not hist.empty:
        keys = set(zip(new["session_date"], new["symbol"], new["type"], new["zbucket"]))
        hist = hist[[k not in keys for k in zip(hist["session_date"], hist["symbol"],
                                                hist["type"], hist["zbucket"])]]
        new = pd.concat([hist, new], ignore_index=True)
    new.sort_values(["session_date", "symbol", "type", "zbucket"]).to_csv(
        os.path.join(histdir, HISTORY_FILE), index=False)


def backfill(histdir: str | None = None, symbols: list[str] | None = None) -> int:
    """Build sign_metrics.csv from every stored chain pair.  Run once."""
    hd = histdir or HIST
    rows = []
    for sym in (symbols or all_symbols(hd)):
        sess = sessions_available(sym, hd)
        for ps, cs in zip(sess, sess[1:]):
            if (datetime.fromisoformat(cs) - datetime.fromisoformat(ps)).days > MAX_GAP_DAYS:
                continue
            try:
                prev = load_session(sym, ps, FLOW_MAX_DTE, hd)
                cur = load_session(sym, cs, FLOW_MAX_DTE, hd)
            except Exception:
                continue
            rows.extend(pair_rows(prev, cur, sym, cs))
    upsert(hd, rows)
    return len(rows)


# --------------------------------------------------------------------------
# estimation
# --------------------------------------------------------------------------

def signs_from_history(hist: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Per-bucket dealer sign, only where the correlation is significant.

    Positive correlation between net OI change and weighted relative IV change
    = OI grows when the bucket richens = customers buying to open = DEALER
    SHORT, so sign = -corr.  Buckets failing the bar keep the naive prior.
    """
    cols = ["type", "zbucket", "naive", "sign", "corr", "t", "n", "significant"]
    if hist is None or hist.empty:
        return pd.DataFrame(columns=cols)
    f = hist[hist["symbol"] == symbol]
    if f.empty:
        return pd.DataFrame(columns=cols)
    out = []
    for (ty, zb), g in f.groupby(["type", "zbucket"]):
        nv = 1.0 if str(ty).upper().startswith("C") else -1.0
        n = len(g)
        corr, t = np.nan, np.nan
        if n >= MIN_SESSIONS and g["net_doi"].std() > 0 and g["wdiv"].std() > 0:
            corr = float(np.corrcoef(g["net_doi"], g["wdiv"])[0, 1])
            if abs(corr) < 1.0:
                t = corr * math.sqrt((n - 2) / max(1e-12, 1 - corr * corr))
        sig = bool(np.isfinite(t) and abs(t) >= T_CRIT)
        out.append({"type": ty, "zbucket": zb, "naive": nv,
                    "sign": float(-corr) if sig else nv,
                    "corr": corr, "t": t, "n": n, "significant": sig})
    return pd.DataFrame(out).sort_values(["type", "zbucket"]).reset_index(drop=True)


def estimate_signs(symbol: str, histdir: str | None = None) -> pd.DataFrame:
    """Signs for `symbol`, from sign_metrics.csv, rebuilding it if absent."""
    hd = histdir or HIST
    hist = load_history(hd)
    if hist.empty or symbol not in set(hist["symbol"]):
        backfill(hd, [symbol])
        hist = load_history(hd)
    return signs_from_history(hist, symbol)


# --------------------------------------------------------------------------
# the feed block
# --------------------------------------------------------------------------

def _regime(spot: float, flip: float | None) -> str:
    if flip is None or not np.isfinite(flip):
        return "UNKNOWN"
    return "POS" if spot > flip else "NEG"


def block(histdir: str, raw: pd.DataFrame, symbol: str, session: str,
          metrics: pd.DataFrame | None = None, write: bool = True) -> dict | None:
    """The feed's "sign_model" block.

    With write=True (the daily capture) this session's bucket rows are upserted
    into history/sign_metrics.csv first, so the estimate always includes today.
    Information only: it never changes any other field in the feed.
    """
    rows = session_rows(histdir, raw, symbol, session)
    hist = load_history(histdir)
    if write and rows:
        upsert(histdir, rows)
        hist = load_history(histdir)
    elif rows:
        hist = pd.concat(
            [hist[~((hist["symbol"] == symbol) & (hist["session_date"] == session))],
             pd.DataFrame(rows, columns=COLUMNS)], ignore_index=True)

    signs = signs_from_history(hist, symbol)
    chain = prep(raw, MAX_DTE)
    if chain.empty:
        return None
    n, s = state(chain, None), state(chain, signs)
    if n["flip"] is None or s["flip"] is None:
        return None

    spot = n["spot"]
    iv = atm_iv(chain)
    dm = spot * (iv / 16.0) if np.isfinite(iv) and iv > 0 else float("nan")
    d = s["flip"] - n["flip"]
    ddm = d / dm if np.isfinite(dm) and dm else float("nan")
    rn, rs = _regime(spot, n["flip"]), _regime(spot, s["flip"])
    agrees = rn == rs
    k = int(signs["significant"].sum()) if not signs.empty else 0

    if not agrees:
        label = f"regime disagrees: naive {rn} vs signed {rs} - lower confidence"
    elif np.isfinite(ddm) and abs(ddm) >= DM_ALERT:
        label = f"regime agrees ({rn}) but flip moves {abs(ddm):.2f} DM - check G3"
    else:
        label = f"regime agrees ({rn}), flip stable"

    return {
        "flip_naive": round(float(n["flip"]), 4),
        "flip_signed": round(float(s["flip"]), 4),
        "delta_pts": round(float(d), 4),
        "delta_pct": round(float(d / spot * 100), 4),
        "delta_dm": (round(float(ddm), 3) if np.isfinite(ddm) else None),
        "regime_naive": rn,
        "regime_signed": rs,
        "agrees": agrees,
        "buckets_significant": k,
        "bucket_sessions": int(signs["n"].max()) if not signs.empty else 0,
        "label": label,
        "info_only": True,
    }


def line(symbol: str, session: str | None = None, histdir: str | None = None) -> str:
    """One line for the Mode A state vector or a ticket."""
    hd = histdir or HIST
    sess = sessions_available(symbol, hd)
    if not sess:
        return f"Sign model: n/a ({symbol} not in the stored chains)"
    session = session or sess[-1]
    raw = pd.read_parquet(os.path.join(_raw_dir(hd), session, f"{symbol}.parquet"))
    b = block(hd, raw, symbol, session, write=False)
    if b is None:
        return f"Sign model: no flip in window ({symbol} {session})"
    dm = f"{b['delta_dm']:+.2f} DM" if b["delta_dm"] is not None else "DM n/a"
    return (f"Sign model: flip {b['flip_signed']:,.2f} signed vs "
            f"{b['flip_naive']:,.2f} naive ({b['delta_pts']:+,.2f}, {dm}) - "
            f"{b['label']} - {b['buckets_significant']} bucket(s) significant "
            f"(info only, never a gate)")


# --------------------------------------------------------------------------
# research / reporting
# --------------------------------------------------------------------------

def _p_from_t(t: float) -> float:
    """Two-sided p for a correlation t-stat.  Normal approximation: at df 38-52
    it is slightly liberal in the tail (t=2.0 -> 0.046 here vs 0.051 exact),
    fine for ranking under Benjamini-Hochberg, not a precise p."""
    if not np.isfinite(t):
        return np.nan
    return math.erfc(abs(t) / math.sqrt(2.0))


def bh_fdr(pvals: list[float], q: float = 0.10) -> list[bool]:
    """Benjamini-Hochberg: which hypotheses survive at false-discovery rate q."""
    idx = [i for i, p in enumerate(pvals) if np.isfinite(p)]
    m = len(idx)
    keep = [False] * len(pvals)
    if m == 0:
        return keep
    order = sorted(idx, key=lambda i: pvals[i])
    kmax = -1
    for rank, i in enumerate(order, start=1):
        if pvals[i] <= q * rank / m:
            kmax = rank
    for rank, i in enumerate(order, start=1):
        if rank <= kmax:
            keep[i] = True
    return keep


def print_signs(symbol: str, signs: pd.DataFrame):
    print(f"\n{symbol}  net-buying-pressure test  (+1 dealer long, -1 dealer short)")
    print(f"  {'type':5} {'bucket':10} {'naive':>6} {'corr':>7} {'t':>7} {'n':>5} "
          f"{'sign used':>10}  note")
    for _, r in signs.iterrows():
        note = "significant" if r["significant"] else "no signal - naive kept"
        if r["significant"] and np.sign(r["sign"]) != np.sign(r["naive"]):
            note += ", FLIPS"
        c = f"{r['corr']:.3f}" if np.isfinite(r["corr"]) else "  n/a"
        t = f"{r['t']:.2f}" if np.isfinite(r["t"]) else "  n/a"
        print(f"  {r['type']:5} {r['zbucket']:10} {r['naive']:>6.0f} {c:>7} {t:>7} "
              f"{int(r['n']):>5} {r['sign']:>10.2f}  {note}")
    k = int(signs["significant"].sum())
    print(f"  {k}/{len(signs)} buckets clear |t| >= {T_CRIT}"
          f"{'; no usable signal on this store' if k == 0 else ''}")


def report(symbol: str, signs: pd.DataFrame, session: str | None = None,
           histdir: str | None = None):
    hd = histdir or HIST
    sess = sessions_available(symbol, hd)
    if not sess:
        print(f"{symbol}: no stored chains")
        return
    session = session or sess[-1]
    df = load_session(symbol, session, histdir=hd)
    n, s = state(df, None), state(df, signs)
    sp = n["spot"]
    print(f"\n{symbol}  session {session}  spot {sp:,.2f}")
    print(f"  {'':12} {'naive':>14} {'signed':>14}")
    for k in ("flip", "call_wall", "put_wall"):
        a = np.nan if n[k] is None else n[k]
        b = np.nan if s[k] is None else s[k]
        print(f"  {k:12} {a:>14,.2f} {b:>14,.2f}")
    print(f"  {'net gex $':12} {n['net_gex']:>14.3e} {s['net_gex']:>14.3e}")
    rn, rs = _regime(sp, n["flip"]), _regime(sp, s["flip"])
    print(f"  {'regime':12} {rn:>14} {rs:>14}"
          f"{'   <-- DISAGREES' if rn != rs else ''}")


def summary(symbols: list[str] | None = None, q: float = 0.10,
            histdir: str | None = None):
    """Pool every bucket test across symbols, control FDR, show the flip impact.

    One bucket surviving in one symbol is a coin flip; the same bucket surviving
    with the same sign across symbols is a finding.  SPX and SPY are the same
    underlying captured twice -- they corroborate each other's data, not each
    other's economics.
    """
    hd = histdir or HIST
    syms = symbols or all_symbols(hd)
    rows, keep = [], {}
    for sym in syms:
        sess = sessions_available(sym, hd)
        if not sess:
            continue
        sg = estimate_signs(sym, hd)
        if sg.empty:
            continue
        for _, r in sg.iterrows():
            rows.append({"symbol": sym, "type": r["type"], "zbucket": r["zbucket"],
                         "corr": r["corr"], "t": r["t"], "n": r["n"],
                         "p": _p_from_t(r["t"])})
        keep[sym] = (sess[-1], sg)

    d = pd.DataFrame(rows)
    if d.empty:
        print("no tests ran")
        return d
    d["survives_fdr"] = bh_fdr(d["p"].tolist(), q)

    print(f"\n=== net-buying-pressure test, {len(d)} hypotheses, "
          f"Benjamini-Hochberg FDR q={q} ===")
    s = d[d["survives_fdr"]].sort_values("p")
    if s.empty:
        print("  nothing survives correction - treat every bucket result as noise")
    else:
        print(f"  {'symbol':7} {'type':5} {'bucket':10} {'corr':>7} {'t':>7} {'n':>4} "
              f"{'p':>9}  reading")
        for _, r in s.iterrows():
            read = ("dealer SHORT (customers bought)" if r["corr"] > 0
                    else "dealer LONG (customers sold)")
            print(f"  {r['symbol']:7} {r['type']:5} {r['zbucket']:10} {r['corr']:>7.3f} "
                  f"{r['t']:>7.2f} {int(r['n']):>4} {r['p']:>9.2e}  {read}")
        print("\n  replication across symbols (same bucket, same sign):")
        any_rep = False
        for (ty, zb), g in s.groupby(["type", "zbucket"]):
            if len(g) < 2:
                continue
            any_rep = True
            same = (np.sign(g["corr"]) == np.sign(g["corr"].iloc[0])).all()
            print(f"    {ty} {zb:10} in {len(g)} symbols "
                  f"({', '.join(g['symbol'])}) - signs {'agree' if same else 'DISAGREE'}")
        if not any_rep:
            print("    none - every survivor is a single-symbol result")

    print("\n=== flip impact, latest stored session ===")
    print(f"  {'symbol':7} {'spot':>10} {'naive':>10} {'signed':>10} {'delta':>8} "
          f"{'%':>7} {'DM':>6}  regime")
    for sym, (sess, sg) in keep.items():
        try:
            df = load_session(sym, sess, histdir=hd)
            n, sgs = state(df, None), state(df, sg)
            if n["flip"] is None or sgs["flip"] is None:
                continue
            sp, iv = n["spot"], atm_iv(df)
            dm = sp * (iv / 16.0) if np.isfinite(iv) and iv > 0 else np.nan
            delta = sgs["flip"] - n["flip"]
            rn, rs = _regime(sp, n["flip"]), _regime(sp, sgs["flip"])
            tag = f"{rn}->{rs}" + ("  DISAGREES" if rn != rs else "")
            ddm = delta / dm if np.isfinite(dm) and dm else np.nan
            print(f"  {sym:7} {sp:>10,.2f} {n['flip']:>10,.2f} {sgs['flip']:>10,.2f} "
                  f"{delta:>+8.2f} {delta / sp * 100:>+7.2f} {ddm:>+6.2f}  {tag}")
        except Exception as exc:                     # pragma: no cover
            print(f"  {sym:7} error: {exc}")
    print(f"\n  DM = daily move = ATM IV / 16.  G3 stands aside inside {DM_ALERT} DM of")
    print("  the flip, so a shift near or above that can move a trade across the gate.")
    return d


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--verify", action="store_true", help="naive path == pipeline?")
    ap.add_argument("--signs", action="store_true", help="per-bucket estimate")
    ap.add_argument("--report", action="store_true", help="naive vs signed flip/walls")
    ap.add_argument("--line", action="store_true", help="state-vector line")
    ap.add_argument("--block", action="store_true", help="the feed block, as JSON")
    ap.add_argument("--summary", action="store_true", help="all symbols, FDR-corrected")
    ap.add_argument("--backfill", action="store_true", help="build sign_metrics.csv")
    ap.add_argument("--session")
    ap.add_argument("--q", type=float, default=0.10, help="FDR level")
    a = ap.parse_args(argv)

    if a.backfill:
        print(f"sign_metrics.csv: {backfill(HIST)} rows written")
        return

    if a.summary:
        summary([a.symbol.upper()] if a.symbol else None, a.q)
        return

    syms = all_symbols() if a.all else ([a.symbol.upper()] if a.symbol else None)
    if not syms:
        ap.error("give a symbol, --all, --summary or --backfill")
    if not (a.verify or a.signs or a.report or a.line or a.block):
        a.line = True

    for sym in syms:
        if not sessions_available(sym):
            print(f"{sym}: no stored chains")
            continue
        if a.verify:
            verify(sym)
        if a.line:
            print(line(sym, a.session))
        if a.block:
            sess = a.session or sessions_available(sym)[-1]
            raw = pd.read_parquet(os.path.join(RAW, sess, f"{sym}.parquet"))
            print(json.dumps({sym: block(HIST, raw, sym, sess, write=False)}, indent=2))
        if a.signs or a.report:
            sg = estimate_signs(sym)
            if a.signs:
                print_signs(sym, sg)
            if a.report:
                report(sym, sg, a.session)


if __name__ == "__main__":
    main()
