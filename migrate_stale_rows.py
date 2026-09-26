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

Run --dry-run first. Removed rows go to history/quarantine/, never deleted.
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")
QUAR = os.path.join(HIST, "quarantine")


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

    print(f"{len(s)} rows, limit {a.max_feed_lag / 60:.0f} min")
    print(f"  stale       {int(stale.sum())} -> quarantine")
    print(f"  unparseable {int(unparseable.sum())} -> KEPT (no evidence either way)")

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

    if stale.any():
        os.makedirs(QUAR, exist_ok=True)
        q = os.path.join(QUAR, "intraday_stale_rows.csv")
        s[stale].to_csv(q, mode="a", header=not os.path.exists(q), index=False)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        shutil.copy2(path, f"{path}.bak.{stamp}")
        s[~stale].to_csv(path, index=False)
        print(f"\n{int(stale.sum())} row(s) -> history/quarantine/intraday_stale_rows.csv")
        print(f"{len(s)} -> {int((~stale).sum())} rows. Backup .bak.{stamp}")
    else:
        print("\nnothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
