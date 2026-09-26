#!/usr/bin/env python3
"""Recompute call_wall / put_wall in intraday_state.csv from stored chains.

The rows were written by the OLD find_walls, which ranked each side in
isolation and had no directional constraint, so a round-number strike with a
large two-sided OI pile could win both contests -- 235 rows carry identical
call and put walls. The new find_walls ranks on NET gamma and requires the
call wall above spot and the put wall below it.

Every intraday snapshot is stored as parquet, so the correct walls can be
replayed exactly rather than estimated. This script reproduces the live path
in gex_intraday.capture_symbol():

    dte   = (expiry - capture_ts) / 86400, keep dte > 0
    chain = dte <= --max-dte
    window= strike within +/- --plot-window of spot
    walls = find_walls(calls, puts, spot, --wall-exclude)   [unbinned]

Run --dry-run first. Writes a .bak.<stamp> before touching anything.

KNOWN IMPRECISION: the live code computed dte against datetime.now() with
sub-second precision; capture_ts is stored only to the second. A contract
sitting exactly on the --max-dte boundary can therefore fall on the other
side of the filter. This shifts a handful of rows at most and never changes
a wall by more than one strike increment.
"""

import argparse
import glob
import os
import shutil
import sys
from datetime import datetime

import pandas as pd

import gamma_exposure as gx

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")


def index_snapshots(histdir):
    """Map (symbol, capture_ts) -> parquet path. Keyed on the file's own
    columns rather than on its name, so a naming change can't silently
    mis-join a chain to the wrong state row."""
    idx = {}
    paths = glob.glob(os.path.join(histdir, "intraday", "*", "*.parquet"))
    paths += glob.glob(os.path.join(histdir, "intraday", "*", "*.csv.gz"))
    for p in paths:
        try:
            head = (pd.read_parquet(p, columns=["symbol", "capture_ts"])
                    if p.endswith(".parquet")
                    else pd.read_csv(p, usecols=["symbol", "capture_ts"], nrows=1))
        except Exception:
            continue
        if head.empty:
            continue
        idx[(str(head["symbol"].iloc[0]), str(head["capture_ts"].iloc[0]))] = p
    return idx


def walls_for(path, spot, capture_ts, max_dte, plot_window, wall_exclude):
    df = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
    now = pd.to_datetime(capture_ts, utc=True).tz_localize(None)
    df["expiry"] = pd.to_datetime(df["expiry"])
    df["dte"] = (df["expiry"] - now).dt.total_seconds() / 86400.0
    df = df[df["dte"] > 0]
    chain = df[df["dte"] <= max_dte]
    lo, hi = spot * (1 - plot_window), spot * (1 + plot_window)
    win = chain[(chain["strike"] >= lo) & (chain["strike"] <= hi)]
    if win.empty:
        return None
    _, calls, puts = gx.gex_by_strike(win, spot)
    return gx.find_walls(calls, puts, spot, wall_exclude)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-dte", type=float, default=30)
    p.add_argument("--plot-window", type=float, default=0.10)
    p.add_argument("--wall-exclude", type=float, default=0.01)
    a = p.parse_args()

    path = os.path.join(HIST, "intraday_state.csv")
    s = pd.read_csv(path)
    print(f"{len(s)} state rows")

    idx = index_snapshots(HIST)
    print(f"{len(idx)} stored snapshots indexed\n")

    changed = missing = same = failed = 0
    fell_back = 0
    samples = []
    new_c, new_p = s["call_wall"].astype(object).copy(), s["put_wall"].astype(object).copy()

    for i, r in s.iterrows():
        key = (str(r["symbol"]), str(r["capture_ts"]))
        src = idx.get(key)
        if src is None:
            missing += 1
            continue
        try:
            res = walls_for(src, float(r["spot"]), r["capture_ts"],
                            a.max_dte, a.plot_window, a.wall_exclude)
        except Exception as e:
            failed += 1
            if failed <= 3:
                print(f"  ! {key}: {e}")
            continue
        if res is None:
            failed += 1
            continue
        cw, pw, fb = res
        fell_back += bool(fb)
        old_c, old_p = float(r["call_wall"]), float(r["put_wall"])
        if cw != old_c or pw != old_p:
            changed += 1
            if len(samples) < 8:
                samples.append((r["capture_ts"], r["symbol"], float(r["spot"]),
                                old_c, old_p, cw, pw))
        else:
            same += 1
        new_c.at[i], new_p.at[i] = cw, pw

    print(f"changed   {changed}")
    print(f"unchanged {same}")
    print(f"no chain  {missing}   (state row predates or lost its snapshot)")
    print(f"failed    {failed}")
    print(f"fell_back {fell_back}   (a side had no strike of the expected sign)")

    if samples:
        print(f"\n{'capture_ts':26} {'sym':4} {'spot':>9} "
              f"{'old c/p':>17} -> {'new c/p':>17}")
        for ts, sym, sp, oc, op, nc, npw in samples:
            print(f"{ts:26} {sym:4} {sp:>9,.2f} "
                  f"{oc:>8,.1f}/{op:<8,.1f} -> {nc:>8,.1f}/{npw:<8,.1f}")

    degen_before = int((s["call_wall"] == s["put_wall"]).sum())
    degen_after = int((new_c == new_p).sum())
    print(f"\ncall_wall == put_wall: {degen_before} -> {degen_after}")

    if a.dry_run:
        print("\nDRY RUN -- nothing written.")
        return 0

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    shutil.copy2(path, f"{path}.bak.{stamp}")
    s["call_wall"], s["put_wall"] = new_c, new_p
    s.to_csv(path, index=False)
    print(f"\nwritten. backup at intraday_state.csv.bak.{stamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
