#!/usr/bin/env python3
"""NYSE trading calendar. Pure stdlib -- no pandas, no network, no new deps.

WHY THIS EXISTS
    The daily capture reads a Cboe file that reflects the PRIOR session's
    settled open interest. Cboe re-stamps that file with a fresh feed_ts every
    time it is served, including on weekends, so the feed_ts dedupe in
    gex_capture.already_captured() never fires on a weekend re-serve. Result:
    Friday's chain was written three times (Sat 9/19, Sun 9/20, Mon 9/21) under
    three different feed_dates, each carrying Friday's stale spot of 7650.50
    while the market actually closed Monday at 7764.70.

    A calendar is the only correct gate. The rule is one line:

        capture on day D  <=>  (D - 1 calendar day) was a trading session

    Saturday captures Friday. Tuesday captures Monday. Sunday and Monday do
    not run. If Monday is a holiday, Tuesday does not run either -- Friday's
    session was already captured on Saturday -- and Wednesday captures
    Tuesday. Good Friday is handled by the same rule: Friday's run fires
    because THURSDAY was a session, and it captures Thursday.
"""

from datetime import date, timedelta

# Ad-hoc, unscheduled NYSE closures. Add to this set as they occur; there is
# no algorithm for a state funeral or a hurricane.
AD_HOC_CLOSURES = {
    date(2012, 10, 29), date(2012, 10, 30),   # Hurricane Sandy
    date(2018, 12, 5),                        # G.H.W. Bush funeral
    date(2025, 1, 9),                         # Carter funeral
}


def _easter(year):
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year, month, weekday, n):
    """n-th `weekday` (Mon=0) of month; n=-1 means last."""
    if n == -1:
        d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 \
            else date(year, 12, 31)
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        return d
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(weeks=n - 1)


def _observed(d):
    """NYSE shifts a Saturday holiday to Friday, a Sunday holiday to Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def holidays(year):
    """The set of NYSE full-closure dates in `year`."""
    out = set()

    # New Year's Day is the one exception to the observed-Friday rule: when it
    # falls on a Saturday the NYSE does NOT close the preceding Dec 31.
    nyd = date(year, 1, 1)
    if nyd.weekday() != 5:
        out.add(_observed(nyd))

    out.add(_nth_weekday(year, 1, 0, 3))          # MLK Day
    out.add(_nth_weekday(year, 2, 0, 3))          # Washington's Birthday
    out.add(_easter(year) - timedelta(days=2))    # Good Friday
    out.add(_nth_weekday(year, 5, 0, -1))         # Memorial Day
    if year >= 2022:
        out.add(_observed(date(year, 6, 19)))     # Juneteenth
    out.add(_observed(date(year, 7, 4)))          # Independence Day
    out.add(_nth_weekday(year, 9, 0, 1))          # Labor Day
    out.add(_nth_weekday(year, 11, 3, 4))         # Thanksgiving
    out.add(_observed(date(year, 12, 25)))        # Christmas

    return {d for d in out if d.year == year} | {
        d for d in AD_HOC_CLOSURES if d.year == year
    }


def is_trading_day(d):
    """True if `d` is a full NYSE session."""
    if d.weekday() >= 5:
        return False
    return d not in holidays(d.year)


def prev_trading_day(d):
    """The most recent session strictly before `d`."""
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def should_capture(capture_date):
    """True if the daily job should run on `capture_date`.

    The file served this morning reflects yesterday. Run only when yesterday
    was a session; otherwise the feed serves a chain already on file.
    """
    return is_trading_day(capture_date - timedelta(days=1))


def session_for(capture_date):
    """The trading session the file served on `capture_date` belongs to.

    This is what feed_date SHOULD have been recording. Always the previous
    calendar day when should_capture() is True.
    """
    return capture_date - timedelta(days=1)


if __name__ == "__main__":
    # Self-test. Any failure here means do not deploy.
    assert _easter(2026) == date(2026, 4, 5), _easter(2026)
    assert date(2026, 4, 3) in holidays(2026), "Good Friday 2026"
    assert date(2026, 7, 3) in holidays(2026), "July 4 Sat -> Fri observed"
    assert date(2026, 1, 1) in holidays(2026), "New Year 2026 is a Thursday"
    assert date(2027, 1, 1) in holidays(2027), "Jan 1 2027 is a Friday"
    # Jan 1 2028 is a Saturday: closed that day anyway, but Dec 31 2027 stays
    # OPEN -- the one holiday NYSE does not shift backward.
    assert date(2028, 1, 1) not in holidays(2028), "Jan 1 Sat -> not observed"
    assert date(2027, 12, 31) not in holidays(2027), "Dec 31 2027 stays open"
    assert not is_trading_day(date(2026, 9, 7)), "Labor Day"

    # The exact failure this module was written for.
    assert should_capture(date(2026, 9, 19)), "Sat captures Fri"
    assert not should_capture(date(2026, 9, 20)), "Sun must not run"
    assert not should_capture(date(2026, 9, 21)), "Mon must not run"
    assert should_capture(date(2026, 9, 22)), "Tue captures Mon"
    assert session_for(date(2026, 9, 19)) == date(2026, 9, 18)
    assert session_for(date(2026, 9, 22)) == date(2026, 9, 21)

    # Labor Day week 2026: Mon 9/7 closed.
    assert should_capture(date(2026, 9, 5)), "Sat captures Fri 9/4"
    assert not should_capture(date(2026, 9, 8)), "Tue: Mon was a holiday"
    assert should_capture(date(2026, 9, 9)), "Wed captures Tue 9/8"

    # Good Friday week 2026: Fri 4/3 closed.
    assert should_capture(date(2026, 4, 3)), "Fri runs: Thu was a session"
    assert session_for(date(2026, 4, 3)) == date(2026, 4, 2)
    assert not should_capture(date(2026, 4, 4)), "Sat: Fri was a holiday"
    assert not should_capture(date(2026, 4, 6)), "Mon: Sun"
    assert should_capture(date(2026, 4, 7)), "Tue captures Mon 4/6"

    print("market_calendar: all self-tests passed")
