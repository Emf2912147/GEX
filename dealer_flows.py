#!/usr/bin/env python3
"""
Dealer vanna and charm flows from a captured option chain.

Karsan's Axis B (directional pressure): as time passes (charm) and as
implied vol moves (vanna), the delta of the options dealers hold
changes, and dealers trade the underlying to stay hedged. Under the
same convention as the GEX numbers (dealers LONG calls, SHORT puts,
an assumption, not observed positioning) both effects are dealer
BUYING in normal conditions: a structural bid. What carries
information is how strong that bid is against its own recent level,
for example the roll-off after a monthly expiration.

Per symbol (contracts with 0.5 to 30 days to expiry, the GEX window):

    charm_bp_day         dealer hedge buying from one calendar day of
                         time decay, in basis points of the window's
                         open-interest notional (OI x 100 x spot).
                         Positive = dealers buy.
    vanna_bp_per_volpt   dealer hedge buying if implied vol FALLS one
                         vol point, same units. If IV rises, the same
                         amount is selling.
    charm_support_ratio  charm_bp_day / median of the previous up to
                         20 sessions (needs 10). Below 1 the bid is
                         weaker than normal (post-expiration roll-off).
    history_sessions     how many prior sessions the ratio used.

Black-Scholes greeks at each contract's Cboe IV; r = 4%, dividend
yields as in the agent's pricing. Normalising by OI notional rather
than by net GEX keeps the numbers stable near the gamma flip, where
net GEX goes to zero.

Validation (2026-10-17): greeks match finite differences of
Black-Scholes delta to 1e-7. Over 64 SPX/SPY/QQQ/IWM sessions the
support ratio did not yet show reliable forward-return power; the
agent displays these numbers but does not trade on them until a
re-test on more history.
"""

from __future__ import annotations

import math
import os

import numpy as np
import pandas as pd

RATE = 0.04
DIVIDEND_YIELD = {"TLT": 0.02, "SPX": 0.005, "QQQ": 0.005}
MIN_DTE = 0.5
MAX_DTE = 30.0
HISTORY_SESSIONS = 20
MIN_HISTORY = 10

_erf = np.vectorize(math.erf, otypes=[float])


def _cdf(x):
    return 0.5 * (1.0 + _erf(np.asarray(x, dtype=float) / math.sqrt(2.0)))


def _pdf(x):
    x = np.asarray(x, dtype=float)
    return np.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S, K, T, iv, q):
    vol_t = iv * np.sqrt(T)
    d1 = (np.log(S / K) + (RATE - q + 0.5 * iv * iv) * T) / vol_t
    return d1, d1 - vol_t


def vanna(S, K, T, iv, q=0.0):
    """dDelta/dSigma (per 1.00 of vol), identical for calls and puts."""
    d1, d2 = _d1_d2(S, K, T, iv, q)
    return -np.exp(-q * T) * _pdf(d1) * d2 / iv


def charm(S, K, T, iv, is_call, q=0.0):
    """dDelta/dt per YEAR of calendar time passing (= -dDelta/dT)."""
    d1, d2 = _d1_d2(S, K, T, iv, q)
    root_t = np.sqrt(T)
    common = np.exp(-q * T) * _pdf(d1) * (2 * (RATE - q) * T - d2 * iv * root_t) / (2 * T * iv * root_t)
    d_call = -q * np.exp(-q * T) * _cdf(d1) + common
    d_put = q * np.exp(-q * T) * _cdf(-d1) + common
    return -np.where(is_call, d_call, d_put)


def flows(raw: pd.DataFrame, symbol: str) -> dict | None:
    """charm_bp_day and vanna_bp_per_volpt for one stored chain."""
    df = raw.copy()
    df["dte"] = pd.to_numeric(df["dte"], errors="coerce")
    df = df[(df["dte"] >= MIN_DTE) & (df["dte"] <= MAX_DTE) & (df["iv"] > 0) & (df["oi"] > 0)]
    if df.empty:
        return None

    S = float(df["spot"].iloc[0])
    q = DIVIDEND_YIELD.get(symbol.upper(), 0.0)
    T = df["dte"].to_numpy(float) / 365.0
    K = df["strike"].to_numpy(float)
    iv = df["iv"].to_numpy(float)
    is_call = (df["type"] == "C").to_numpy()
    oi = df["oi"].to_numpy(float)

    sign = np.where(is_call, 1.0, -1.0)          # dealers long calls, short puts
    weight = sign * oi * 100.0 * S
    notional = float(np.sum(oi * 100.0 * S))
    if notional <= 0:
        return None

    dealer_delta_per_day = float(np.sum(weight * charm(S, K, T, iv, is_call, q))) / 365.0
    dealer_delta_per_volpt = float(np.sum(weight * vanna(S, K, T, iv, q))) * 0.01

    # Hedge trade = minus the change in dealer delta.
    return {
        "charm_bp_day": -dealer_delta_per_day / notional * 1e4,
        "vanna_bp_per_volpt": dealer_delta_per_volpt / notional * 1e4,
    }


def _history(histdir: str, symbol: str, session: str) -> list[float]:
    """charm_bp_day for up to HISTORY_SESSIONS stored sessions before `session`."""
    rawdir = os.path.join(histdir, "raw")
    if not os.path.isdir(rawdir):
        return []
    sessions = sorted(d for d in os.listdir(rawdir) if d < session)
    values = []
    for d in reversed(sessions):
        path = os.path.join(rawdir, d, f"{symbol}.parquet")
        if not os.path.exists(path):
            continue
        f = flows(pd.read_parquet(path), symbol)
        if f is not None:
            values.append(f["charm_bp_day"])
        if len(values) >= HISTORY_SESSIONS:
            break
    return values


def flows_block(histdir: str, raw: pd.DataFrame, symbol: str, session: str) -> dict | None:
    """The feed's "flows" block for one symbol (None if the chain is unusable)."""
    today = flows(raw, symbol)
    if today is None:
        return None

    prior = _history(histdir, symbol, session) if session else []
    ratio = None
    if len(prior) >= MIN_HISTORY:
        median = float(np.median(prior))
        if median > 0:
            ratio = today["charm_bp_day"] / median

    return {
        "charm_bp_day": round(today["charm_bp_day"], 2),
        "vanna_bp_per_volpt": round(today["vanna_bp_per_volpt"], 2),
        "charm_support_ratio": round(ratio, 3) if ratio is not None else None,
        "history_sessions": len(prior),
        "convention": "dealers long calls / short puts (assumed)",
    }
