#!/usr/bin/env python3
"""Standing integrity check for history/. Read-only -- never writes.

Every check here exists because the corresponding defect actually occurred and
produced output that looked correct. None of them were caught by the pipeline
at the time; all were found by audit after the fact. The point of this script
is that nobody has to remember to look.

Exit code 0 = all pass, 1 = at least one FAIL. Run it monthly, after any
migration, and any time the numbers look surprising.
"""

import datetime as dt
import glob
import json
import os
import sys

import pandas as pd

from market_calendar import is_trading_day

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")
FEED_LAG_LIMIT_MIN = 15

# Sessions with no data that are permanent and understood. Cboe serves no
# history, so these can never be filled. Listing them here keeps the check
# actionable -- a known hole must not fail forever, or the failure stops
# meaning anything. Add a date ONLY after establishing why it is empty.
KNOWN_GAPS = {
    "2026-09-23",   # runner stopped ~28h; the single stale row was quarantined
}

results = []


def check(name, ok, detail=""):
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def main():
    d = pd.read_csv(os.path.join(HIST, "daily_metrics.csv"))
    s = pd.read_csv(os.path.join(HIST, "intraday_state.csv"))

    print(f"\ndaily_metrics.csv   {len(d):>5} rows, {d.session_date.nunique()} sessions")
    print(f"intraday_state.csv  {len(s):>5} rows\n")

    print("DAILY")
    # One session captured twice used to happen every weekend: Cboe re-stamps
    # feed_ts on each serve, so the old feed_ts dedupe never fired.
    check("no duplicate (symbol, session)",
          d.duplicated(["symbol", "session_date"]).sum() == 0,
          f"{d.duplicated(['symbol', 'session_date']).sum()} dupes")

    bad = [x for x in d.session_date.unique()
           if not is_trading_day(dt.date.fromisoformat(str(x)))]
    check("every session_date is an NYSE session", not bad, str(bad))

    # spot drives the grid, the walls, flip, and net GEX (which scales with
    # spot SQUARED). The Cboe feed's spot is a pre-market quote from the
    # morning AFTER the session -- up to 1.09% wrong on the ETFs.
    if "spot_source" in d.columns:
        counts = d.spot_source.value_counts().to_dict()
        check("spot anchored on a real close", counts.get("feed", 0) <= 5,
              f"{counts}")
    else:
        check("spot_source column present", False, "run migrate_daily_spot.py")

    print("\nINTRADAY")
    check("no zero-gamma rows", (s.net_gex_window == 0).sum() == 0,
          f"{(s.net_gex_window == 0).sum()} rows")
    # A strike cannot be both the ceiling and the floor. 235 rows once were.
    check("no degenerate walls", (s.call_wall == s.put_wall).sum() == 0,
          f"{(s.call_wall == s.put_wall).sum()} rows")
    inv = int((s.call_wall <= s.spot).sum() + (s.put_wall >= s.spot).sum())
    check("no wall on the wrong side of spot", inv == 0, f"{inv} rows")

    # 2026-09-23: a chain 9.6 HOURS stale passed every other guard.
    lag = ((pd.to_datetime(s.capture_ts, utc=True).dt.tz_localize(None)
            - pd.to_datetime(s.feed_ts, errors="coerce")).dt.total_seconds() / 60)
    stale = int((lag > FEED_LAG_LIMIT_MIN).sum())
    check(f"no chain staler than {FEED_LAG_LIMIT_MIN} min", stale == 0,
          f"{stale} rows, max {lag.max():.1f} min")
    check("regime agrees with net_gex sign",
          len(s[((s.net_gex_window > 0) & (s.regime != "positive"))
                | ((s.net_gex_window <= 0) & (s.regime != "negative"))]) == 0)

    print("\nCOVERAGE")
    s["day"] = pd.to_datetime(s.capture_ts, utc=True).dt.date
    days = sorted(s.day.unique())
    # A session with no rows is honest. A session with ONE row is usually a
    # stale pre-open serve wearing today's date -- far more dangerous.
    thin = [str(x) for x, n in s.groupby("day").capture_ts.nunique().items() if n < 5]
    check("no session with fewer than 5 cycles", not thin, str(thin))

    # A run that fires hours after the close writes real quotes under a
    # session date. Nothing is wrong with the chain; it is simply not a
    # session observation, and anything grouping by day will read the latest
    # such row as that day's close.
    late = s[pd.to_datetime(s.capture_ts, utc=True).dt.time > dt.time(21, 0)]
    check("no captures after 21:00 UTC", len(late) == 0,
          f"{len(late)} rows on {sorted({str(x) for x in late['day'].unique()})}"
          if len(late) else "")

    missing = [str(x) for x in pd.date_range(days[0], days[-1]).date
               if is_trading_day(x) and x not in set(days)]
    new_gaps = [x for x in missing if x not in KNOWN_GAPS]
    check("no unexplained missing sessions", not new_gaps, str(new_gaps))
    if set(missing) & KNOWN_GAPS:
        print(f"         known gaps, not counted: "
              f"{sorted(set(missing) & KNOWN_GAPS)}")

    # The daily capture for session D runs on D+1, so the most recent
    # intraday session legitimately has no daily row yet. Excluding it is not
    # a loosened standard -- flagging it was simply wrong, and a check that
    # fails every single day teaches you to ignore the checker.
    daily_sessions = set(d.session_date.astype(str))
    settled = days[:-1] if days else []
    gaps = [str(x) for x in settled if str(x) not in daily_sessions]
    check("every settled intraday session has a daily row", not gaps,
          str(gaps) + f" (newest session {days[-1]} excluded -- its daily "
                      f"capture runs tomorrow)" if days else "")

    # Freshness: is the pipeline still alive?
    age = (dt.date.today() - days[-1]).days
    check("pipeline ran within 4 days", age <= 4,
          f"last intraday session {days[-1]} ({age}d ago)")

    print("\nOFFICIAL CLOSES")
    p = os.path.join(HIST, "official_closes.json")
    if os.path.exists(p):
        doc = json.load(open(p))
        have = {(sym, day) for sym, days_ in doc["closes"].items() for day in days_}
        need = {(str(r.symbol), str(r.session_date)) for r in d.itertuples()}
        gap = sorted(need - have)
        check("official close on file for every daily row", not gap,
              f"{len(gap)} missing, e.g. {gap[:3]}")
        print(f"         pulled {doc.get('_pulled', '?')}")
    else:
        check("official_closes.json present", False, "see MONTHLY_REFRESH.md")

    failed = [n for ok, n, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
