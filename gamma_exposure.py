#!/usr/bin/env python3
"""
Gamma exposure (GEX) chart builder.

Pulls a full option chain from Cboe's free delayed-quote endpoint, computes
dealer gamma exposure by strike, and finds the gamma flip (zero-gamma) level.

    pip install requests pandas numpy matplotlib

    python gamma_exposure.py SPX
    python gamma_exposure.py SPY --max-dte 7
    python gamma_exposure.py QQQ --max-dte 45 --plot-window 0.08

Data notes / known limits -- read these before trusting a number:
  - Cboe delays this feed ~15 minutes. It is free and needs no API key.
    This is NOT a real-time read.
  - Open interest is settled overnight by the OCC, so intraday it reflects
    YESTERDAY'S CLOSE. On 0DTE-heavy days that is a material distortion.
    --use-volume weights by today's volume instead, but volume measures
    flow, not inventory. The two answer different questions.
  - Sign convention: dealers assumed long calls / short puts. This is the
    standard public convention, not a measurement of real positioning.
  - The gamma profile holds each contract's IV fixed as spot is varied.
    Real vol surfaces move with spot, so the flip is an approximation.
  - Monthly SPX (3rd Friday, root SPX not SPXW) settles AM at the open.
    DTE here assumes a 4:00pm ET expiry for everything -- slightly long
    for those lines.
"""

import argparse
import math
import re
import sys
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                                    # pragma: no cover
    _ET = None

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
# Cboe prefixes cash indices with an underscore.
INDEX_SYMBOLS = {"SPX", "NDX", "RUT", "VIX", "XSP", "DJX"}

OPT_RE = re.compile(r"^(?P<root>[A-Z0-9^]+?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
CONTRACT_SIZE = 100

# Calls plot above zero, puts below, so sign/position carries series identity
# independently of hue. That redundancy is what makes a green/red pair legible
# to a red-green colorblind reader (the pair alone sits at deutan dE 7.7).
COLOR_CALL = "#2e9e5b"
COLOR_PUT = "#c0392b"
COLOR_FLIP = "#8e44ad"
COLOR_LINE = "#2c3e50"


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def fetch_chain(symbol: str) -> dict:
    sym = symbol.upper()
    cboe_sym = f"_{sym}" if sym in INDEX_SYMBOLS else sym
    url = CBOE_URL.format(sym=cboe_sym)
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "gex-script/1.0"})
    except requests.exceptions.RequestException as e:
        sys.exit(
            f"Could not reach Cboe: {e}\n"
            "If you are behind a corporate proxy or a sandbox with restricted\n"
            "egress, this endpoint has to be reachable for the script to work."
        )
    if r.status_code == 404:
        sys.exit(f"Cboe has no chain for '{sym}'. Check the ticker.")
    r.raise_for_status()
    return r.json()


def _expiry_dt(ymd: str) -> datetime:
    """Expiry at 4:00pm ET on the listed date, returned as UTC."""
    d = datetime.strptime(ymd, "%y%m%d")
    if _ET is not None:
        return d.replace(hour=16, tzinfo=_ET).astimezone(timezone.utc)
    # Fallback if zoneinfo/tzdata is unavailable: assume EDT (UTC-4).
    return d.replace(hour=20, tzinfo=timezone.utc)


def parse_chain(payload: dict) -> tuple[pd.DataFrame, float, str]:
    data = payload.get("data", {})
    spot = data.get("current_price") or data.get("close")
    if not spot:
        sys.exit("No spot price in the Cboe response.")

    asof = payload.get("timestamp", "unknown")
    rows = []
    for o in data.get("options", []):
        m = OPT_RE.match(o.get("option", ""))
        if not m:
            continue
        try:
            expiry = _expiry_dt(m.group("ymd"))
        except ValueError:
            continue
        rows.append({
            "expiry": expiry,
            "strike": int(m.group("strike")) / 1000.0,
            "type": m.group("cp"),
            "oi": o.get("open_interest") or 0,
            "volume": o.get("volume") or 0,
            "iv": o.get("iv") or 0.0,
            "gamma": o.get("gamma") or 0.0,
            "delta": o.get("delta") or 0.0,
        })

    if not rows:
        sys.exit("Parsed zero contracts. Cboe may have changed their schema.")

    df = pd.DataFrame(rows)
    now = datetime.now(timezone.utc)
    df["dte"] = (df["expiry"] - now).dt.total_seconds() / 86400.0
    df = df[df["dte"] > 0].copy()
    if df.empty:
        sys.exit("Every contract in the feed has already expired.")
    df["T"] = df["dte"] / 365.0
    return df, float(spot), asof


def normalize_iv(df: pd.DataFrame, quiet: bool = False) -> pd.DataFrame:
    """
    Cboe has shipped IV as a decimal (0.18) historically, but a schema change to
    percent (18.0) would silently corrupt every Black-Scholes gamma downstream.
    Detect and correct rather than trust.
    """
    live = df.loc[df["iv"] > 0, "iv"]
    if live.empty:
        print("WARNING: no positive IV values in the feed; the gamma profile "
              "and flip level will be meaningless.", file=sys.stderr)
        return df
    med = float(live.median())
    if med > 3.0:
        df = df.copy()
        df["iv"] = df["iv"] / 100.0
        if not quiet:
            print(f"note: IV looked like percent (median {med:.1f}); "
                  "divided by 100.", file=sys.stderr)
    elif not quiet and not (0.01 <= med <= 3.0):
        print(f"WARNING: median IV is {med:.4f}, which is outside any sane "
              "range. Check the feed before trusting the flip.", file=sys.stderr)
    return df


# --------------------------------------------------------------------------
# Black-Scholes gamma (needed to reprice the curve at hypothetical spots)
# --------------------------------------------------------------------------

def _norm_pdf(x):
    return np.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_gamma(S, K, T, sigma, r=0.043, q=0.012):
    """Vectorized Black-Scholes gamma. Same for calls and puts."""
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    sigma = np.asarray(sigma, dtype=float)

    valid = (T > 0) & (sigma > 0) & (S > 0) & (K > 0)
    if not np.any(valid):
        return np.zeros(np.broadcast(S, K, T, sigma).shape, dtype=float)

    with np.errstate(divide="ignore", invalid="ignore"):
        sqrtT = np.sqrt(T)
        d1 = (np.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
        g = np.exp(-q * T) * _norm_pdf(d1) / (S * sigma * sqrtT)

    out = np.where(valid, g, 0.0)
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)


# --------------------------------------------------------------------------
# GEX
# --------------------------------------------------------------------------

def dollar_gex(gamma, oi, spot, is_call):
    """Dollar gamma per 1% move in the underlying, signed by convention."""
    sign = np.where(is_call, 1.0, -1.0)
    return sign * gamma * oi * CONTRACT_SIZE * spot * spot * 0.01


def gex_by_strike(df, spot, use_volume=False, bin_size=None):
    """
    Per-strike dollar GEX, split into calls and puts.

    bin_size buckets strikes to the nearest multiple before aggregating. SPX
    lists 5/10/25-point increments in the same chain and open interest piles up
    on the round numbers, so the raw plot is a picket fence. Binning to 25 or 50
    makes the walls legible. It does not change the totals.
    """
    size = df["volume"] if use_volume else df["oi"]
    g = dollar_gex(df["gamma"].values, size.values, spot, (df["type"] == "C").values)
    tmp = df.assign(gex=g)
    if bin_size and bin_size > 0:
        tmp = tmp.assign(strike=(tmp["strike"] / bin_size).round() * bin_size)
    per_strike = tmp.groupby("strike")["gex"].sum().sort_index()
    calls = tmp[tmp["type"] == "C"].groupby("strike")["gex"].sum().sort_index()
    puts = tmp[tmp["type"] == "P"].groupby("strike")["gex"].sum().sort_index()
    return per_strike, calls, puts


def find_walls(calls, puts, spot, exclude=0.01):
    """
    Call/put walls, ignoring strikes within `exclude` of spot.

    Gamma peaks at the money, so a plain idxmax/idxmin on gamma-weighted GEX
    just finds the ATM strike -- especially once strikes are binned, which
    gathers the tight near-money increments into one bucket. That is gamma
    density, not a wall. Excluding a band around spot leaves the structural
    open-interest concentrations that dealers actually hedge against.

    Returns (call_wall, put_wall, fell_back) -- fell_back is True when the
    exclusion emptied a side and the unfiltered strike was used instead.
    """
    lo, hi = spot * (1 - exclude), spot * (1 + exclude)
    c_out = calls[(calls.index < lo) | (calls.index > hi)]
    p_out = puts[(puts.index < lo) | (puts.index > hi)]
    fell_back = False

    # A side with nothing meaningful left outside the band (empty, or all zero)
    # falls back rather than reporting an arbitrary strike.
    if len(c_out) and c_out.max() > 0:
        call_wall = c_out.idxmax()
    else:
        call_wall = calls.idxmax() if len(calls) else None
        fell_back = True
    if len(p_out) and p_out.min() < 0:
        put_wall = p_out.idxmin()
    else:
        put_wall = puts.idxmin() if len(puts) else None
        fell_back = True
    return call_wall, put_wall, fell_back


def gamma_profile(df, lo, hi, points=241, use_volume=False):
    """
    Net GEX across a grid of hypothetical spot prices -> gamma flip.

    `df` should be the FULL chain within the DTE filter, not the plotted strike
    window. Truncating the book to a narrow band around spot biases the flip and
    can hide it entirely.
    """
    grid = np.linspace(lo, hi, points)
    size = (df["volume"] if use_volume else df["oi"]).values.astype(float)
    is_call = (df["type"] == "C").values
    K = df["strike"].values
    T = df["T"].values
    iv = df["iv"].values
    sign = np.where(is_call, 1.0, -1.0)
    w = sign * size * CONTRACT_SIZE

    net = np.empty_like(grid)
    for i, S in enumerate(grid):
        g = bs_gamma(S, K, T, iv)
        net[i] = np.sum(w * g) * S * S * 0.01

    flip = None
    for i in range(len(grid) - 1):
        a, b = net[i], net[i + 1]
        if a == 0:
            flip = float(grid[i])
            break
        if (a < 0 < b) or (a > 0 > b):
            flip = float(grid[i] + (grid[i + 1] - grid[i]) * (-a) / (b - a))
            break
    return grid, net, flip


# --------------------------------------------------------------------------
# Plot
# --------------------------------------------------------------------------

def plot(symbol, asof, spot, calls, puts, grid, net, flip, outfile, weight_label,
         bin_size=None, call_wall=None, put_wall=None):
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(13, 11), gridspec_kw={"height_ratios": [1.35, 1]}
    )

    b = 1e9
    strikes = np.union1d(calls.index.values, puts.index.values)
    if bin_size and bin_size > 0:
        width = bin_size * 0.85
    elif len(strikes) > 1:
        width = float(np.median(np.diff(strikes))) * 0.85
    else:
        width = 1.0

    if len(calls):
        ax1.bar(calls.index, calls.values / b, width=width, color=COLOR_CALL,
                alpha=0.85, label="Call gamma")
    if len(puts):
        ax1.bar(puts.index, puts.values / b, width=width, color=COLOR_PUT,
                alpha=0.85, label="Put gamma")
    ax1.axvline(spot, color="black", lw=1.6, ls="--", label=f"Spot {spot:,.2f}")
    if flip is not None:
        ax1.axvline(flip, color=COLOR_FLIP, lw=1.8, label=f"Gamma flip {flip:,.2f}")
    ax1.axhline(0, color="#444", lw=0.8)
    # Headroom so the wall annotations are not clipped by the axes.
    ymin, ymax = ax1.get_ylim()
    ax1.set_ylim(ymin - 0.14 * (ymax - ymin), ymax + 0.14 * (ymax - ymin))

    if call_wall is not None and call_wall in calls.index:
        ax1.annotate(f"Call wall {call_wall:,.0f}",
                     xy=(call_wall, calls.loc[call_wall] / b),
                     xytext=(0, 12), textcoords="offset points",
                     ha="center", fontsize=9, color=COLOR_CALL, weight="bold")
    if put_wall is not None and put_wall in puts.index:
        ax1.annotate(f"Put wall {put_wall:,.0f}",
                     xy=(put_wall, puts.loc[put_wall] / b),
                     xytext=(0, -20), textcoords="offset points",
                     ha="center", fontsize=9, color=COLOR_PUT, weight="bold")

    binned = f", binned to {bin_size:g}" if bin_size else ""
    ax1.set_title(
        f"{symbol} gamma exposure by strike   |   Cboe delayed (~15m), {asof}\n"
        f"weighted by {weight_label}{binned}",
        fontsize=13, weight="bold")
    ax1.set_ylabel("$Bn gamma per 1% move")
    ax1.legend(fontsize=9)
    ax1.grid(alpha=0.25)

    ax2.plot(grid, net / b, color=COLOR_LINE, lw=2)
    ax2.fill_between(grid, net / b, 0, where=(net >= 0), color=COLOR_CALL, alpha=0.20)
    ax2.fill_between(grid, net / b, 0, where=(net < 0), color=COLOR_PUT, alpha=0.20)
    ax2.axhline(0, color="#444", lw=0.9)
    ax2.axvline(spot, color="black", lw=1.6, ls="--")
    if flip is not None:
        ax2.axvline(flip, color=COLOR_FLIP, lw=1.8)
        ax2.annotate(f"flip {flip:,.2f}", xy=(flip, 0), xytext=(6, 14),
                     textcoords="offset points", color=COLOR_FLIP,
                     fontsize=10, weight="bold")
    ax2.set_title("Net dealer gamma profile vs. spot  (full chain, IV held fixed)",
                  fontsize=12, weight="bold")
    ax2.set_xlabel("Underlying price")
    ax2.set_ylabel("$Bn gamma per 1% move")
    ax2.grid(alpha=0.25)

    fig.tight_layout()
    fig.savefig(outfile, dpi=140)
    print(f"chart -> {outfile}")


# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Build a gamma exposure chart.")
    p.add_argument("symbol", nargs="?", default="SPX")
    p.add_argument("--max-dte", type=float, default=30,
                   help="only include expiries within this many days (default 30)")
    p.add_argument("--plot-window", "--strike-window", type=float, default=0.10,
                   dest="plot_window",
                   help="plot strikes within +/- this fraction of spot (default 0.10)")
    p.add_argument("--grid-window", type=float, default=0.15,
                   help="hypothetical-spot range for the gamma profile, +/- this "
                        "fraction of spot (default 0.15). Widen if the flip comes "
                        "back 'none in range'.")
    p.add_argument("--bin", type=float, default=None, dest="bin_size",
                   help="bucket strikes to the nearest N points in the bar chart "
                        "(e.g. 25 or 50 for SPX). Makes walls legible; does not "
                        "change totals or the flip.")
    p.add_argument("--wall-exclude", type=float, default=0.01,
                   help="ignore strikes within +/- this fraction of spot when "
                        "picking the call/put walls (default 0.01). Gamma peaks "
                        "at the money, so without this the 'wall' is just the "
                        "ATM strike. Set 0 to disable.")
    p.add_argument("--use-volume", action="store_true",
                   help="weight by today's volume instead of open interest")
    p.add_argument("--csv", default=None,
                   help="also write per-strike GEX to this CSV path")
    p.add_argument("--stamp", action="store_true",
                   help="append a UTC timestamp to the output filename "
                        "(useful for scheduled runs)")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    sym = args.symbol.upper()
    payload = fetch_chain(sym)
    df, spot, asof = parse_chain(payload)
    df = normalize_iv(df)

    # DTE filter applies to everything.
    chain = df[df["dte"] <= args.max_dte].copy()
    if chain.empty:
        sys.exit(f"No contracts within {args.max_dte:g} DTE. Raise --max-dte.")

    weight_label = "today's volume" if args.use_volume else "open interest (prior close)"
    size_col = "volume" if args.use_volume else "oi"
    if chain[size_col].sum() == 0:
        sys.exit(f"Every contract has zero {size_col}. "
                 f"{'Volume is empty before the open.' if args.use_volume else ''}")

    # The gamma profile uses the FULL chain -- truncating it biases the flip.
    glo, ghi = spot * (1 - args.grid_window), spot * (1 + args.grid_window)
    grid, net, flip = gamma_profile(chain, glo, ghi, use_volume=args.use_volume)

    # The bar chart uses the narrower plot window, for legibility only.
    plo, phi = spot * (1 - args.plot_window), spot * (1 + args.plot_window)
    windowed = chain[(chain["strike"] >= plo) & (chain["strike"] <= phi)]
    if windowed.empty:
        sys.exit("No contracts inside the plot window. Widen --plot-window.")

    per_strike, calls, puts = gex_by_strike(windowed, spot, args.use_volume,
                                            args.bin_size)
    full_per_strike, _, _ = gex_by_strike(chain, spot, args.use_volume)

    total_full = full_per_strike.sum()
    total_win = per_strike.sum()

    print(f"\n{sym}  spot {spot:,.2f}   as of {asof}  (Cboe, ~15 min delayed)")
    print(f"weighting      {weight_label}")
    print(f"contracts      {len(chain):,} within {args.max_dte:g} DTE "
          f"({len(windowed):,} inside the +/-{args.plot_window:.0%} plot window)")
    print(f"net GEX full   {total_full/1e9:>10,.2f} $Bn / 1%")
    print(f"net GEX window {total_win/1e9:>10,.2f} $Bn / 1%")
    if flip is not None:
        print(f"gamma flip     {flip:>10,.2f}   ({flip/spot - 1:+.2%} vs spot)")
    else:
        print(f"gamma flip          none within +/-{args.grid_window*100:g}% of spot "
              "-- widen --grid-window")
    call_wall, put_wall, fell_back = find_walls(calls, puts, spot, args.wall_exclude)
    excl = f" (>{args.wall_exclude*100:g}% from spot)" if args.wall_exclude else ""
    if call_wall is not None:
        print(f"call wall      {call_wall:>10,.2f}{excl}")
    if put_wall is not None:
        print(f"put wall       {put_wall:>10,.2f}{excl}")
    if fell_back:
        print("               (exclusion band emptied a side; fell back to ATM)")
    regime = "positive (mean-reverting)" if total_full > 0 else "negative (trend-amplifying)"
    print(f"regime         {regime}\n")

    top = per_strike.reindex(per_strike.abs().sort_values(ascending=False).index).head(10)
    binned_note = f", binned to {args.bin_size:g}" if args.bin_size else ""
    print(f"largest strikes by |GEX| (plot window{binned_note}):")
    for k, v in top.items():
        print(f"  {k:>10,.2f}   {v/1e9:>8,.2f} $Bn")
    print()

    stamp = datetime.now(timezone.utc).strftime("_%Y%m%d_%H%M") if args.stamp else ""
    out = args.out or f"{sym}_gex{stamp}.png"
    plot(sym, asof, spot, calls, puts, grid, net, flip, out, weight_label,
         args.bin_size, call_wall, put_wall)

    if args.csv:
        full_per_strike.rename("gex_usd").to_frame().to_csv(args.csv)
        print(f"csv   -> {args.csv}")


if __name__ == "__main__":
    main()
