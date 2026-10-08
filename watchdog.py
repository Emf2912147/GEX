#!/usr/bin/env python3
"""Fail loudly when the capture pipeline stops running.

WHY THIS EXISTS
    Every guard in this repo checks whether captured data is GOOD. None of
    them notice when no data is captured at all, because a workflow that never
    fires produces no log line, no bad row, and no failed run -- nothing to
    inspect. That has now happened twice:

        2026-09-23  intraday stopped after one 13:14 cycle, resumed 17:43 the
                    NEXT day. ~28 hours. Session 2026-09-23 is permanently
                    lost; Cboe serves no history.
        2026-10-05  intraday stopped at 18:36 after 13 cycles. The last 85
                    minutes of the session, including the close, are gone.

    Both times the final run logged "ok=4 failed=0". Nothing broke. GitHub's
    scheduler simply stopped firing -- its own docs call scheduled runs
    best-effort, and under load they drift or are dropped.

WHAT IT DOES
    Compares what SHOULD be on disk by now against what is, and exits 1 when
    they disagree. A failing workflow emails the repo owner, which is the only
    channel that reaches you without you going to look.

    Being a separate workflow matters: it does not share a process, a step, or
    a failure mode with the thing it watches.

HONEST LIMITATION
    The watchdog is itself a scheduled workflow, so the same scheduler that
    drops intraday runs can drop this one. It cannot detect its own absence.
    What it does buy is that TWO independent schedules must fail silently
    before an outage goes unnoticed, instead of one. If you want a guarantee
    rather than an improvement, the check has to run somewhere that is not
    GitHub Actions.
"""

import argparse
import datetime as dt
import os
import sys

import pandas as pd

from market_calendar import et_wall_clock, is_trading_day, should_capture

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")

# Eastern wall-clock time. These were UTC (13:30 / 20:00) until 2026-10-08,
# which is an hour early for the whole of winter.
OPEN_ET = dt.time(9, 30)
CLOSE_ET = dt.time(16, 0)
CADENCE_MIN = 15
# Actions drops and delays runs routinely, so demand well under the nominal
# rate. Observed good sessions land 20-27 cycles against a nominal 27; 60%
# of nominal flags a real outage without crying wolf over ordinary drift.
MIN_FRACTION = 0.60
# Longest acceptable silence mid-session. Four consecutive missed cycles.
MAX_SILENCE_MIN = 60

problems = []


def fail(msg):
    problems.append(msg)
    print(f"FAIL  {msg}")


def ok(msg):
    print(f"ok    {msg}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--now", help="ISO UTC override, for testing")
    a = p.parse_args()

    now = (dt.datetime.fromisoformat(a.now) if a.now
           else dt.datetime.now(dt.timezone.utc))
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    today = now.date()
    et_now = et_wall_clock(now)
    print(f"watchdog {now:%Y-%m-%d %H:%M} UTC ({today:%a})\n")

    s = pd.read_csv(os.path.join(HIST, "intraday_state.csv"))
    s["ts"] = pd.to_datetime(s["capture_ts"], utc=True)

    # ---- intraday ----------------------------------------------------------
    if not is_trading_day(today):
        ok(f"{today} is not an NYSE session -- intraday not expected")
    elif et_now.time() < OPEN_ET:
        ok("before the open -- intraday not expected yet")
    else:
        today_rows = s[s["ts"].dt.date == today]
        cycles = today_rows["capture_ts"].nunique()

        elapsed = (min(et_now.time(), CLOSE_ET).hour * 60
                   + min(et_now.time(), CLOSE_ET).minute
                   - OPEN_ET.hour * 60 - OPEN_ET.minute)
        expected = max(int(elapsed / CADENCE_MIN), 1)
        floor = max(int(expected * MIN_FRACTION), 1)

        if cycles < floor:
            fail(f"only {cycles} intraday cycles today, expected about "
                 f"{expected} by now (floor {floor})")
        else:
            ok(f"{cycles} intraday cycles today (expected ~{expected})")

        # A stall mid-session is the 2026-10-05 signature: plenty of cycles
        # banked, then silence. The count check alone would pass that.
        if len(today_rows):
            silence = (now - today_rows["ts"].max()).total_seconds() / 60
            if et_now.time() <= CLOSE_ET and silence > MAX_SILENCE_MIN:
                fail(f"no capture for {silence:.0f} min "
                     f"(last {today_rows['ts'].max():%H:%M} UTC) -- "
                     f"the scheduler has stalled mid-session")
            else:
                ok(f"last capture {silence:.0f} min ago")
        elif et_now.time() > dt.time(10, 30):
            fail("no intraday rows at all today")

    # ---- daily -------------------------------------------------------------
    d = pd.read_csv(os.path.join(HIST, "daily_metrics.csv"))
    if should_capture(today):
        session = str(today - dt.timedelta(days=1))
        got = len(d[d["session_date"].astype(str) == session])
        if now.time() < dt.time(12, 30):
            ok(f"daily for session {session} not due yet")
        elif got < 4:
            fail(f"daily capture for session {session}: {got}/4 symbols")
        else:
            ok(f"daily capture for session {session} complete")
    else:
        ok(f"no daily capture due on {today:%a}")

    # ---- staleness backstop -------------------------------------------------
    # Catches a multi-day outage even if the per-day logic above is somehow
    # satisfied -- the 2026-09-23 case, where the gap spanned sessions.
    last = s["ts"].max().date()
    gap = sum(1 for n in range(1, (today - last).days + 1)
              if is_trading_day(today - dt.timedelta(days=n - 1)) and n > 1)
    if gap >= 2:
        fail(f"last intraday capture was {last} -- {gap} sessions with nothing")
    else:
        ok(f"last intraday capture {last}")

    print()
    if problems:
        print(f"{len(problems)} PROBLEM(S):")
        for x in problems:
            print(f"  - {x}")
        print("\nCheck the Actions tab. Data not captured cannot be "
              "recovered -- Cboe serves no history.")
        return 1
    print("pipeline healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
