#!/usr/bin/env python3
"""Quarantine intraday rows written before check_feed_freshness() existed.

The guard in gex_intraday.py stops new stale rows from being written. It does
nothing about rows already on file -- same as check_chain_integrity(), whose
four zeroed-Greek rows had to be removed by a separate migration.

Five rows exceed the 15-minute limit. Four are the 2026-09-23 incident, where
every symbol came back carrying a feed_ts from 03:35-03:56 UTC against a 13:14
capture -- 9.3 to 9.6 hours stale, structurally perfect, spot equal to
Tuesday's close to the cent. The fifth is IWM on 09-16 at 30.9 minutes, a
slow serve rather than a fault, removed under the same rule because a rule
applied selectively is not a rule.

The 9/23 rows are the dangerous ones. They are the only rows for that date,
so anything grouping by day reports a 9/23 "close" of 7764.6401 that nobody
traded on 9/23. Removing them turns a fabricated session into an honest gap.

SECOND RULE (2026-10-08): rows captured outside the session window.
gex_intraday.py now refuses to capture outside 09:00-17:15 ET, but four rows
from before that gate existed are still on file: a run GitHub deferred to
2026-10-06 22:57 UTC (18:57 ET) wrote post-market quotes under that session's
date. Same treatment, same reason -- anything grouping by day read them as
the 10/06 close. They go to quarantine/intraday_offhours_rows.csv.

Run --dry-run first. Removed rows go to history/quarantine/, never deleted.
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

import pandas as pd

from market_calendar import et_wall_clock

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")
QUAR = os.path.join(HIST, "quarantine")


def remove_lines(path, drop):
    """Delete the flagged rows by LINE, leaving every other byte untouched.

    Not s[~drop].to_csv(): a pandas round trip reformats columns it re-infers
    (elapsed_s comes back as 231702.0), which rewrites the file from the
    first such row to the end. The relay appends to this file every 15
    minutes, all day, every day; a commit that rewrites its tail collides
    with the relay's next append and can strand the relay's commits. Deleting
    lines in the middle does not.
    """
    with open(path, encoding="utf-8", newline="") as fh:
        lines = fh.readlines()
    if len(lines) != len(drop) + 1:
        sys.exit(f"{path}: {len(lines)} lines for {len(drop)} rows -- blank or "
                 f"multi-line records; refusing to delete by line number")
    keep = [lines[0]] + [ln for ln, d in zip(lines[1:], drop) if not d]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(keep)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-feed-lag", type=float, default=900,
                   help="seconds; must match gex_intraday's --max-feed-lag")
    a = p.parse_args()

    path = os.path.join(HIST, "intraday_state.csv")
    s = pd.read_csv(path)

    cap = pd.to_datetime(s["capture_ts"], utc=True).dt.tz_localize(None)
    feed = pd.to_datetime(s["feed_ts"], errors="coerce")
    lag = (cap - feed).dt.total_seconds()

    stale = lag > a.max_feed_lag
    unparseable = feed.isna()

    # Same window as the gate in gex_intraday.py, in Eastern time.
    et = pd.to_datetime(s["capture_ts"], utc=True).map(et_wall_clock).dt.time
    offhours = ((et < pd.Timestamp("09:00").time()) |
                (et > pd.Timestamp("17:15").time())) & ~stale

    print(f"{len(s)} rows, limit {a.max_feed_lag / 60:.0f} min")
    print(f"  stale       {int(stale.sum())} -> quarantine")
    print(f"  off-hours   {int(offhours.sum())} -> quarantine (outside 09:00-17:15 ET)")
    print(f"  unparseable {int(unparseable.sum())} -> KEPT (no evidence either way)")
    if offhours.any():
        print()
        print(s.loc[offhours, ["capture_ts", "feed_ts", "symbol", "spot", "regime"]]
              .to_string(index=False))

    if stale.any():
        out = s.loc[stale, ["capture_ts", "feed_ts", "symbol", "spot", "regime"]].copy()
        out["lag_min"] = (lag[stale] / 60).round(1)
        print()
        print(out.to_string(index=False))

        # Days that lose every row are now honest gaps rather than one
        # fabricated observation. Worth naming explicitly.
        day = cap.dt.date.astype(str)
        for d in sorted(set(day[stale])):
            before = int((day == d).sum())
            after = int(((day == d) & ~stale).sum())
            if after == 0:
                print(f"\n  NOTE {d}: all {before} row(s) removed -- that date now "
                      f"has no intraday coverage at all, which is the truth.")

    if a.dry_run:
        print("\nDRY RUN -- nothing written.")
        return 0

    drop = stale | offhours
    if drop.any():
        os.makedirs(QUAR, exist_ok=True)
        for mask, name in ((stale, "intraday_stale_rows.csv"),
                           (offhours, "intraday_offhours_rows.csv")):
            if mask.any():
                q = os.path.join(QUAR, name)
                s[mask].to_csv(q, mode="a", header=not os.path.exists(q), index=False)
                print(f"\n{int(mask.sum())} row(s) -> history/quarantine/{name}")
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        shutil.copy2(path, f"{path}.bak.{stamp}")
        remove_lines(path, drop)
        print(f"{len(s)} -> {int((~drop).sum())} rows. Backup .bak.{stamp}")
    else:
        print("\nnothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
