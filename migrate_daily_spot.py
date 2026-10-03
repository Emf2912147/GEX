#!/usr/bin/env python3
"""Re-anchor daily_metrics.csv on each session's real close.

THE BUG
    The daily file carries the prior session's settled open interest, but
    Cboe's `spot` field is whatever the quote feed says when the file is
    served. An 11:30 UTC run is reading a PRE-MARKET quote from the next
    morning. Session 2026-09-24, captured 2026-09-25 11:39Z:

        SPX  7704.1299  vs close 7704.1299   0.00%
        SPY    770.66   vs close   765.94   +0.62%
        QQQ    746.74   vs close   739.28   +1.01%
        IWM    283.12   vs close   281.08   +0.73%

    SPX is immune -- index settlement is fixed overnight -- which is why this
    survived every earlier audit: the number you would sanity-check is the one
    that was always right.

    Spot is not a label. It sets the gamma_profile search grid, the plot
    window, both walls, flip_pct_vs_spot, and net GEX itself (dollar gamma
    scales with spot SQUARED, so a 1% spot error is a 2% GEX error). Every one
    of those was computed against the wrong anchor for the ETFs.

WHAT THIS DOES
    For each daily row, takes the session close from intraday_state.csv,
    reloads that row's stored raw chain, and recomputes every spot-dependent
    metric at the live defaults. Adds `feed_spot` (what Cboe said) and
    `spot_source` so the correction is visible rather than silent.

    Rows whose session has no intraday coverage keep the feed spot and are
    marked spot_source=feed. They are not guesses and are not discarded.

Run --dry-run first. Backs up before writing.
"""

import argparse
import glob
import inspect
import json
import os
import shutil
import sys
from datetime import datetime

import pandas as pd

import gamma_exposure as gx

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")

WALL_EXCLUDE = inspect.signature(gx.find_walls).parameters["exclude"].default


def raw_index():
    """(symbol, capture_ts) -> raw chain path.

    Keyed on the file's own columns, not its directory name: older raw folders
    are named by the fetch date and newer ones by the session date, so the path
    cannot be trusted to identify the row.
    """
    idx = {}
    for p in glob.glob(os.path.join(HIST, "raw", "*", "*.parquet")):
        try:
            h = pd.read_parquet(p, columns=["symbol", "capture_ts"])
        except Exception:
            continue
        if len(h):
            idx[(str(h["symbol"].iloc[0]), str(h["capture_ts"].iloc[0]))] = p
    return idx


def official_closes():
    """(symbol, session_date) -> official regular-hours close, or {} if absent.

    Pulled from Robinhood (get_equity_historicals / get_index_historicals) and
    committed as history/official_closes.json. These are exact. The intraday
    fallback below is good but not exact -- the last capture fires near 20:55
    UTC, almost an hour after the 16:00 ET close, so it catches after-hours
    drift. Benchmarked over 48 session-symbol pairs:

        feed spot (what was stored)   mean |err| 0.285%   max 1.09%
        last intraday capture          mean |err| 0.068%   max 0.25%
        official close                 exact

    SPX matched the official close on all 19 sessions to the cent -- index
    settlement is fixed overnight -- so only the ETFs are actually corrected.
    """
    path = os.path.join(HIST, "official_closes.json")
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        doc = json.load(fh)
    return {(sym, day): float(v)
            for sym, days in doc.get("closes", {}).items()
            for day, v in days.items()}


def session_closes():
    """(symbol, session_date) -> last intraday spot of that session."""
    path = os.path.join(HIST, "intraday_state.csv")
    st = pd.read_csv(path, usecols=["capture_ts", "symbol", "spot"])
    st["day"] = pd.to_datetime(st["capture_ts"], utc=True).dt.date.astype(str)
    st = st.sort_values("capture_ts")
    return {(r.symbol, r.day): float(r.spot)
            for r in st.groupby(["symbol", "day"]).last().reset_index().itertuples()}


def recompute(path, spot, max_dte, grid_window, plot_window):
    df = pd.read_parquet(path)
    df["dte"] = pd.to_numeric(df["dte"], errors="coerce")
    df = df[df["dte"] > 0]
    df["T"] = df["dte"] / 365.0
    chain = df[df["dte"] <= max_dte]
    if chain.empty:
        return None

    glo, ghi = spot * (1 - grid_window), spot * (1 + grid_window)
    _, _, flip = gx.gamma_profile(chain, glo, ghi)

    plo, phi = spot * (1 - plot_window), spot * (1 + plot_window)
    win = chain[(chain["strike"] >= plo) & (chain["strike"] <= phi)]
    full_ps, _, _ = gx.gex_by_strike(chain, spot)
    win_ps, calls, puts = gx.gex_by_strike(win, spot)
    cw, pw, fb = gx.find_walls(calls, puts, spot, WALL_EXCLUDE)
    total = float(full_ps.sum())

    return {
        "contracts_dte": len(chain),
        "contracts_window": len(win),
        "net_gex_full": round(total, 2),
        "net_gex_window": round(float(win_ps.sum()), 2),
        "flip": round(flip, 4) if flip is not None else "",
        "flip_pct_vs_spot": round(flip / spot - 1, 6) if flip is not None else "",
        "call_wall": cw if cw is not None else "",
        "put_wall": pw if pw is not None else "",
        "wall_fallback": int(fb),
        "regime": "positive" if total > 0 else "negative",
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-dte", type=float, default=30)
    p.add_argument("--grid-window", type=float, default=0.15)
    p.add_argument("--plot-window", type=float, default=0.10)
    a = p.parse_args()

    path = os.path.join(HIST, "daily_metrics.csv")
    d = pd.read_csv(path)
    raws = raw_index()
    official, intraday = official_closes(), session_closes()
    print(f"{len(official)} official closes on file")
    print(f"{len(d)} daily rows   {len(raws)} raw chains   "
          f"wall_exclude {WALL_EXCLUDE}\n")

    if "feed_spot" not in d.columns:
        d.insert(d.columns.get_loc("spot") + 1, "feed_spot", d["spot"])
        d.insert(d.columns.get_loc("feed_spot") + 1, "spot_source", "feed")

    fixed = no_close = no_raw = 0
    rows = []
    for i, r in d.iterrows():
        key = (str(r["symbol"]), str(r["session_date"]))
        # Official close first; the intraday capture is the fallback.
        close, src_label = official.get(key), "official_close"
        if close is None:
            close, src_label = intraday.get(key), "session_close"
        if close is None:
            no_close += 1
            continue
        feed = float(r["feed_spot"])
        drift = feed / close - 1
        src = raws.get((str(r["symbol"]), str(r["capture_ts"])))
        if src is None:
            no_raw += 1
            continue
        new = recompute(src, close, a.max_dte, a.grid_window, a.plot_window)
        if new is None:
            no_raw += 1
            continue
        d.at[i, "spot"] = round(close, 4)
        d.at[i, "spot_source"] = src_label
        for k, v in new.items():
            d.at[i, k] = v
        fixed += 1
        if abs(drift) > 0.001:
            rows.append((r["session_date"], r["symbol"], feed, close, drift * 100,
                         r["net_gex_window"], new["net_gex_window"]))

    print(f"re-anchored      {fixed}")
    print(f"no session close {no_close}  (session has no intraday coverage)")
    print(f"no raw chain     {no_raw}")

    if rows:
        print(f"\nrows whose spot moved more than 0.1%:")
        print(f"{'session':12} {'sym':4} {'feed spot':>10} {'close':>10} "
              f"{'drift':>7} {'netGEX old':>12} {'netGEX new':>12}")
        for s_, sym, f_, c_, dr, go, gn in sorted(rows, key=lambda x: -abs(x[4]))[:14]:
            print(f"{s_:12} {sym:4} {f_:>10,.2f} {c_:>10,.2f} {dr:>6.2f}% "
                  f"{go / 1e9:>11.2f}B {gn / 1e9:>11.2f}B")

    if a.dry_run:
        print("\nDRY RUN -- nothing written.")
        return 0

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    shutil.copy2(path, f"{path}.bak.{stamp}")
    d.to_csv(path, index=False)
    print(f"\nwritten. backup at daily_metrics.csv.bak.{stamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
