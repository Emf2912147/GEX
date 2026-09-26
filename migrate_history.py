#!/usr/bin/env python3
"""One-time repair of history/ written before the trading calendar existed.

Run with --dry-run first. Nothing is deleted: every row removed from a live
file is written to history/quarantine/ so the decision stays reversible and
auditable.

WHAT IT FIXES

  1. daily_metrics.csv -- calendar duplicates.
     The Cboe file re-stamps feed_ts on every serve, so already_captured()
     never fired on a weekend re-serve and one session got written up to three
     times. Rows are regrouped by the session they ACTUALLY describe --
     prev_trading_day(capture date) -- and the earliest capture per
     (symbol, session) is kept, which is exactly what the new Saturday/Tuesday
     schedule will produce going forward.

  2. daily_metrics.csv -- a real session_date column, backfilled for history.
     feed_date recorded the day the file was FETCHED, which is one day ahead
     of the session it describes.

  3. intraday_state.csv -- the four zero-gamma rows from 2026-09-10 and
     2026-09-11 that predate check_chain_integrity(). Identified structurally
     (net_gex_window == 0 AND flip pinned to the grid floor), never by date.
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

import pandas as pd

from market_calendar import prev_trading_day, is_trading_day

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")
QUAR = os.path.join(HIST, "quarantine")

GRID_WINDOW = 0.15          # gamma_profile search grid; floor is 1 - this


def _capture_date(ts):
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()


# ---------------------------------------------------------------- daily ----

def fix_daily(dry_run):
    path = os.path.join(HIST, "daily_metrics.csv")
    d = pd.read_csv(path)
    before = len(d)

    # The session a row describes = the last real session before it was pulled.
    d["session_date"] = [str(prev_trading_day(_capture_date(t)))
                         for t in d["capture_ts"]]

    d = d.sort_values("capture_ts", kind="mergesort")
    key = ["symbol", "session_date"]

    # Report disagreement BEFORE collapsing -- if two captures of the same
    # session differ on spot, that is worth a human look, not a silent drop.
    conflicts = []
    for (sym, sess), g in d.groupby(key):
        if len(g) > 1 and g["spot"].nunique() > 1:
            conflicts.append((sym, sess, g["spot"].tolist(),
                              g["capture_ts"].tolist()))

    dupes = d[d.duplicated(key, keep="first")]
    kept = d[~d.duplicated(key, keep="first")].copy()

    # session_date belongs next to feed_date, not bolted on the end.
    cols = list(kept.columns)
    cols.insert(cols.index("feed_date") + 1,
                cols.pop(cols.index("session_date")))
    kept = kept[cols]

    print(f"daily_metrics.csv: {before} rows -> {len(kept)} "
          f"({len(dupes)} calendar duplicates)")
    if len(dupes):
        print(dupes[["capture_ts", "feed_date", "session_date", "symbol",
                     "spot"]].to_string(index=False))
    if conflicts:
        print("\n  !! same session captured with DIFFERENT spot "
              "(earliest kept, rest quarantined):")
        for sym, sess, spots, ts in conflicts:
            print(f"     {sym} session {sess}: {spots}  from {ts}")

    if not dry_run:
        _quarantine(dupes, "daily_metrics_duplicates.csv")
        kept.to_csv(path, index=False)
    return len(dupes)


# ------------------------------------------------------------- intraday ----

def fix_intraday(dry_run):
    path = os.path.join(HIST, "intraday_state.csv")
    s = pd.read_csv(path)
    before = len(s)

    # Structural signature of a zeroed-gamma chain. Both conditions together
    # cannot occur on a real chain: a chain that sums to exactly 0.0 has no
    # zero crossing, so gamma_profile returns the floor of its search grid.
    flip_ratio = s["flip"] / s["spot"]
    corrupt = (s["net_gex_window"] == 0) & (flip_ratio < (1 - GRID_WINDOW) + 1e-6)

    # Reported separately: not fatal, but the agent should not read them raw.
    degenerate_walls = (s["call_wall"] == s["put_wall"]) & ~corrupt
    blank_metrics = s[["atm_iv_30", "rr25", "skew_state"]].isna().any(axis=1) & ~corrupt

    print(f"\nintraday_state.csv: {before} rows")
    print(f"  corrupt (zero gamma, pinned flip): {int(corrupt.sum())} -> quarantine")
    if corrupt.any():
        print(s.loc[corrupt, ["capture_ts", "symbol", "spot", "flip",
                              "net_gex_window", "regime"]].to_string(index=False))
    print(f"  degenerate walls (call_wall == put_wall): "
          f"{int(degenerate_walls.sum())} -> KEPT, see find_walls fix")
    print(f"  blank metric fields: {int(blank_metrics.sum())} -> KEPT")

    # Incomplete cycles are reported, never deleted: the rows that DID land
    # are good data, and dropping them to make cycles uniform destroys more
    # than it fixes.
    clean = s[~corrupt]
    per_cycle = clean.groupby("capture_ts").size()
    short = per_cycle[per_cycle < 4]
    print(f"  incomplete cycles (<4 symbols): {len(short)} -> KEPT, listed below")
    for ts, n in short.items():
        got = clean.loc[clean.capture_ts == ts, "symbol"].tolist()
        missing = [x for x in ("SPX", "SPY", "QQQ", "IWM") if x not in got]
        print(f"     {ts}  {n}/4  missing {missing}")

    if not dry_run:
        _quarantine(s[corrupt], "intraday_corrupt_rows.csv")
        s[~corrupt].to_csv(path, index=False)
    return int(corrupt.sum())


# ------------------------------------------------------------------------

def _quarantine(df, name):
    if df.empty:
        return
    os.makedirs(QUAR, exist_ok=True)
    out = os.path.join(QUAR, name)
    df.to_csv(out, mode="a", header=not os.path.exists(out), index=False)
    print(f"  -> {len(df)} row(s) appended to history/quarantine/{name}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    a = p.parse_args()

    if not a.dry_run and not a.no_backup:
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        for f in ("daily_metrics.csv", "intraday_state.csv"):
            shutil.copy2(os.path.join(HIST, f),
                         os.path.join(HIST, f"{f}.bak.{stamp}"))
        print(f"backups written with suffix .bak.{stamp}\n")

    fix_daily(a.dry_run)
    fix_intraday(a.dry_run)

    if a.dry_run:
        print("\nDRY RUN -- nothing written. Re-run without --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
