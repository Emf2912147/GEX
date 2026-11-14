#!/usr/bin/env python3
"""
Retention for the stored option-chain snapshots.

The repository keeps two kinds of thing, and they do not deserve the same
lifetime:

  DERIVED METRICS -- daily_metrics.csv, intraday_state.csv, vix_state.csv,
    realized_vol.csv, official_closes.json, sector/. Small, append-only, and
    the only record of what the pipeline actually computed. KEPT FOREVER.
    This script never touches them; PROTECTED below is asserted, not assumed.

  SETTLED CHAINS -- history/raw/<session>/<SYM>.parquet, one per session.
    Pruned after 90 days. The derived metrics are already computed from them
    and kept forever; the chains are replay material for formula changes.

  INTRADAY SNAPSHOTS -- history/intraday/<session>/<SYM>__<HHMMSS>.parquet,
    one per symbol every 15 minutes. PRUNED. Each capture's computed state
    vector is already in intraday_state.csv forever, so what the window costs
    is the ability to REPLAY a formula change over old sessions, not the
    record of what was seen.

WHY THIS EXISTS

    Measured 2026-11-14: history/intraday is 592 MB in 8,548 files over 47
    sessions -- 14 MB a session, about 3.5 GB a year. history/raw is 37 MB
    over 55 sessions -- 0.6 MB a session, about 150 MB a year. The intraday
    snapshots are 23x the cost per session, which is why they are the ones
    with a window.

WHAT PRUNING DOES AND DOES NOT FIX

    It caps the WORKING TREE and stops future growth. It does NOT shrink an
    existing clone: git keeps every blob it has ever been given, so the pack
    that is already ~580 MB stays that size after a prune. Only rewriting
    history (git filter-repo) removes those, and that rewrites every commit
    id, so it breaks existing clones and open pull requests. That is a
    deliberate decision for the repo owner, not something this script does.
    Until then, `git clone --depth 1` fetches one commit's worth.

THE COST OF PRUNING

    Past the window a formula change can no longer be REPLAYED over old
    sessions -- backfill_walls.py is exactly that kind of replay, and it can
    only reach back as far as the snapshots still on disk. The derived rows
    stay, so nothing already computed is lost; only the ability to recompute
    it differently. Widen --intraday-days before a backfill, not after.

SAFETY

  * Dry run unless --apply is passed. The dry run prints exactly what would go.
  * The cutoff is measured from the NEWEST session on disk, never from the
    clock: a wrong system date or a long gap cannot wipe the store.
  * A floor of retained sessions (MIN_KEEP) outranks the window, so a thin
    store is never emptied.
  * Only directories named YYYY-MM-DD directly under the two snapshot roots
    are considered, and a directory holding anything that is not a snapshot
    file is reported and skipped rather than deleted.

Usage:
    python prune_history.py                     # dry run, default windows
    python prune_history.py --apply             # delete
    python prune_history.py --raw-days 120 --intraday-days 45 --apply
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))

# Windows in CALENDAR days, counted back from the newest session on disk.
# 0 means keep forever.
#
# history/raw: 90 days, as the owner specified. It is only ~0.6 MB a session
# (~150 MB a year); history/intraday is the real bulk (14 MB a session,
# ~3.5 GB a year), so it gets the tighter window. Cost of both windows: a
# methodology change can only be replayed over sessions still on disk.
# Pass --raw-days 0 to keep settled chains forever.
RAW_DAYS = 90
INTRADAY_DAYS = 30

# Never leave fewer than this many sessions, whatever the window says.
# dealer_flows.py reads up to 20 prior sessions of history/raw and
# vol_surface.py wants neighbours either side, so the raw floor is above both.
MIN_KEEP = {"raw": 25, "intraday": 10}

SESSION_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SNAPSHOT_FILE = re.compile(r"^[A-Z0-9.^]+(__\d{6})?\.(parquet|csv\.gz)$")

# Pruning may only ever happen inside these, relative to --histdir.
PRUNABLE = ("raw", "intraday")

# Named here so a future edit that adds one of them to PRUNABLE fails loudly
# instead of quietly deleting the record of what the pipeline computed.
PROTECTED = ("daily_metrics.csv", "intraday_state.csv", "vix_state.csv",
             "realized_vol.csv", "official_closes.json", "capture.log",
             "intraday.log", "sector", "quarantine")


def _dir_size(path: str) -> int:
    return sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path)
               if os.path.isfile(os.path.join(path, f)))


def sessions(root: str) -> list[str]:
    """Session directories under `root`, oldest first."""
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root)
                  if SESSION_DIR.match(d) and os.path.isdir(os.path.join(root, d)))


def only_snapshots(path: str) -> tuple[bool, list[str]]:
    """(True, []) when every entry is a snapshot file, else (False, odd ones)."""
    odd = []
    for f in os.listdir(path):
        full = os.path.join(path, f)
        if not os.path.isfile(full) or not SNAPSHOT_FILE.match(f):
            odd.append(f)
    return (not odd), odd


def plan(histdir: str, kind: str, days: int, today: date | None = None) -> dict:
    """What a prune of `kind` would remove. Nothing is deleted here."""
    assert kind in PRUNABLE, f"{kind} is not prunable"
    root = os.path.join(histdir, kind)
    have = sessions(root)
    out = {"kind": kind, "root": root, "days": days, "sessions": len(have),
           "cutoff": None, "remove": [], "skipped": [], "bytes": 0,
           "kept_bytes": 0, "reason": None}
    if not have:
        out["reason"] = "nothing stored"
        return out
    if days <= 0:
        out["reason"] = "kept forever (window 0)"
        out["kept_bytes"] = sum(_dir_size(os.path.join(root, d)) for d in have)
        return out

    # Anchored on the newest session present, not on the clock.
    newest = date.fromisoformat(have[-1])
    if today is not None and today < newest:
        newest = today
    cutoff = (newest - timedelta(days=days)).isoformat()
    out["cutoff"] = cutoff

    floor = MIN_KEEP[kind]
    older = [d for d in have if d < cutoff]
    # The floor outranks the window.
    keepable = len(have) - len(older)
    if keepable < floor:
        older = older[:max(0, len(have) - floor)]
        out["reason"] = f"floor of {floor} sessions applied"

    for d in older:
        path = os.path.join(root, d)
        clean, odd = only_snapshots(path)
        if not clean:
            out["skipped"].append((d, odd))
            continue
        out["remove"].append(d)
        out["bytes"] += _dir_size(path)
    out["kept_bytes"] = sum(_dir_size(os.path.join(root, d)) for d in have
                            if d not in set(out["remove"]))
    return out


def apply_plan(p: dict) -> int:
    """Delete the session directories in `p`. Returns how many went."""
    gone = 0
    for d in p["remove"]:
        shutil.rmtree(os.path.join(p["root"], d))
        gone += 1
    return gone


def _mb(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


def report(p: dict, applied: bool) -> list[str]:
    verb = "removed" if applied else "would remove"
    lines = [f"{p['kind']}: {p['sessions']} sessions stored"
             + (f", keeping {p['days']} days (cutoff {p['cutoff']})" if p["cutoff"]
                else ", kept forever" if p["sessions"] else "")]
    if p["reason"]:
        lines.append(f"  note: {p['reason']}")
    if p["remove"]:
        lines.append(f"  {verb} {len(p['remove'])} sessions, {_mb(p['bytes'])} freed "
                     f"({p['remove'][0]} .. {p['remove'][-1]})")
    else:
        lines.append("  nothing older than the window")
    for d, odd in p["skipped"]:
        lines.append(f"  SKIPPED {d}: holds files that are not snapshots: {', '.join(odd[:4])}")
    lines.append(f"  remaining: {_mb(p['kept_bytes'])}")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1].strip(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--histdir", default=os.path.join(HERE, "history"))
    ap.add_argument("--raw-days", type=int, default=RAW_DAYS,
                    help=f"keep settled chains this many days (default {RAW_DAYS}; 0 = forever)")
    ap.add_argument("--intraday-days", type=int, default=INTRADAY_DAYS,
                    help=f"keep 15-minute snapshots this many days (default {INTRADAY_DAYS})")
    ap.add_argument("--apply", action="store_true", help="delete; otherwise dry run")
    args = ap.parse_args(argv)

    # The protected files live beside the prunable roots; this is the assertion
    # that keeps a future edit honest.
    for name in PROTECTED:
        assert name not in PRUNABLE, f"{name} is protected and must never be prunable"

    total = 0
    for kind, days in (("raw", args.raw_days), ("intraday", args.intraday_days)):
        p = plan(args.histdir, kind, days)
        if args.apply and p["remove"]:
            apply_plan(p)
        total += p["bytes"] if args.apply else 0
        print("\n".join(report(p, args.apply)))
    if not args.apply:
        print("\nDry run: nothing was deleted. Pass --apply to do it.")
    elif total:
        print(f"\nFreed {_mb(total)} from the working tree. The git pack is unchanged: "
              "already-committed blobs stay in history (see the module docstring).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
