#!/usr/bin/env python3
"""NYSE trading calendar. Pure stdlib -- no pandas, no network, no new deps.

WHY THIS EXISTS
    The daily capture reads a chain that reflects the PRIOR session's
    settled open interest. The original feed re-stamped that file with a fresh
    feed_ts every time it was served, including on weekends, so the feed_ts dedupe in
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

from datetime import date, datetime, timedelta, timezone

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


def early_closes(year):
    """NYSE 13:00 ET early-close sessions in `year`.

    The day after Thanksgiving, Christmas Eve and July 3 -- each only when it
    is itself a trading day (Dec 24 2027 is the observed Christmas holiday,
    July 3 2026 the observed Independence Day, so neither is an early close).
    Added 2026-11-27: the pipeline ran a full 16:00 day on that Friday and
    wrote three hours of frozen post-close rows.
    """
    cands = {
        _nth_weekday(year, 11, 3, 4) + timedelta(days=1),   # Black Friday
        date(year, 12, 24),
        date(year, 7, 3),
    }
    return {d for d in cands if is_trading_day(d)}


def session_close(d):
    """Regular-session close on `d` as a naive ET time: 13:00 on an early
    close, 16:00 otherwise."""
    from datetime import time as _time
    return _time(13, 0) if d in early_closes(d.year) else _time(16, 0)


def close_minute(d):
    """session_close(d) in minutes past midnight ET (780 or 960)."""
    t = session_close(d)
    return t.hour * 60 + t.minute


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


def _nth_sunday(year, month, n):
    d = date(year, month, 1)
    while d.weekday() != 6:
        d += timedelta(days=1)
    return d + timedelta(weeks=n - 1)


def et_offset(d):
    """US Eastern's offset from UTC on calendar day `d`: -4h (EDT) or -5h (EST).

    The NYSE runs on Eastern wall-clock time, so every session boundary in this
    repo moves by an hour against UTC twice a year. DST runs from the second
    Sunday of March to the first Sunday of November; both switches happen on
    a Sunday at 02:00, so for any trading day the date alone decides it.
    Stdlib only, like the rest of this module -- no tzdata needed on Windows.
    """
    in_dst = _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1)
    return timedelta(hours=-4 if in_dst else -5)


def et_wall_clock(utc_dt):
    """The Eastern wall-clock time at `utc_dt`, as a NAIVE datetime.

    Compare it with naive Eastern times (09:30, 16:00, ...) -- never with an
    aware datetime. `utc_dt` may be aware (any zone) or naive UTC.
    """
    if utc_dt.tzinfo is not None:
        utc_dt = utc_dt.astimezone(timezone.utc).replace(tzinfo=None)
    return utc_dt + et_offset(utc_dt.date())


def is_market_open(utc_dt):
    """True if the NYSE regular session is trading at `utc_dt` (09:30-16:00 ET).

    A cash index quoted while the market is CLOSED is the settled close of the
    last session; quoted while it is open, it is a live print. That is the
    whole difference between an exact and a wrong daily SPX anchor: across 22
    sessions the morning feed matched the official close on all 21 served
    with the market closed, and missed by 18.84 points on the one served
    mid-session (the 10/06 capture GitHub deferred to 13:53 ET on 10/07).
    """
    et = et_wall_clock(utc_dt)
    c = session_close(et.date())
    return (is_trading_day(et.date()) and
            datetime(et.year, et.month, et.day, 9, 30) <= et
            < datetime(et.year, et.month, et.day, c.hour, c.minute))


def should_capture(capture_date):
    """True if the daily job should run on `capture_date`: NYSE trading days only.

    Changed 2026-11-26. The rule used to be "yesterday was a session", which
    ran Saturday mornings (for Friday) and the morning after a holiday's eve.
    That suited Cboe's file. Broker data does not: settled open interest only
    advances on OCC business days, so a Saturday Schwab pull (2026-11-21) came
    back with 44-78% of near-dated contracts at zero OI, and the Thanksgiving
    morning pull (2026-11-26) carried Tuesday's OI unchanged under Wednesday's
    date. Capture on the next trading morning instead -- Monday for Friday,
    the Friday after Thanksgiving for the Wednesday before it.
    """
    return is_trading_day(capture_date)


def previous_session(d):
    """The last NYSE trading day strictly before `d`."""
    p = d - timedelta(days=1)
    while not is_trading_day(p):
        p -= timedelta(days=1)
    return p


def session_for(capture_date):
    """The trading session whose settled open interest a capture on
    `capture_date` carries: the previous trading day."""
    return previous_session(capture_date)


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

    # Weekends: Monday captures Friday; Saturday and Sunday never run.
    assert not should_capture(date(2026, 9, 19)), "Sat must not run"
    assert not should_capture(date(2026, 9, 20)), "Sun must not run"
    assert should_capture(date(2026, 9, 21)), "Mon captures Fri"
    assert session_for(date(2026, 9, 21)) == date(2026, 9, 18)
    assert session_for(date(2026, 9, 22)) == date(2026, 9, 21), "Tue captures Mon"

    # Labor Day week 2026: Mon 9/7 closed -> Tue captures Fri 9/4.
    assert not should_capture(date(2026, 9, 7)), "Labor Day: no run"
    assert should_capture(date(2026, 9, 8))
    assert session_for(date(2026, 9, 8)) == date(2026, 9, 4)

    # Good Friday 2026: Fri 4/3 closed -> Mon 4/6 captures Thu 4/2.
    assert not should_capture(date(2026, 4, 3)), "Good Friday: no run"
    assert session_for(date(2026, 4, 6)) == date(2026, 4, 2)

    # Thanksgiving 2026 -- the failure that changed the rule: Thu 11/26 must
    # not run; Fri 11/27 captures Wed 11/25.
    assert not should_capture(date(2026, 11, 26)), "Thanksgiving: no run"
    assert should_capture(date(2026, 11, 27))
    assert session_for(date(2026, 11, 27)) == date(2026, 11, 25)

    # Eastern time. 2026: DST ends Sun 11/1, starts again Sun 2027-03-14.
    assert et_offset(date(2026, 10, 30)) == timedelta(hours=-4), "Fri before: EDT"
    assert et_offset(date(2026, 11, 2)) == timedelta(hours=-5), "Mon after: EST"
    assert et_offset(date(2027, 3, 12)) == timedelta(hours=-5)
    assert et_offset(date(2027, 3, 15)) == timedelta(hours=-4)
    assert et_wall_clock(datetime(2026, 10, 8, 13, 30, tzinfo=timezone.utc)) \
        == datetime(2026, 10, 8, 9, 30), "summer open is 13:30 UTC"
    assert et_wall_clock(datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)) \
        == datetime(2026, 11, 2, 9, 30), "winter open is 14:30 UTC"
    assert et_wall_clock(datetime(2026, 10, 9, 0, 21)) \
        == datetime(2026, 10, 8, 20, 21), "naive input is UTC; crosses midnight"

    assert not is_market_open(datetime(2026, 10, 8, 11, 54)), "07:54 ET pre-open"
    assert is_market_open(datetime(2026, 10, 7, 17, 53)), "13:53 ET mid-session"
    assert not is_market_open(datetime(2026, 10, 7, 20, 30)), "16:30 ET after close"
    assert not is_market_open(datetime(2026, 10, 10, 15, 0)), "Saturday"
    assert is_market_open(datetime(2026, 11, 2, 14, 45)), "09:45 EST = 14:45 UTC"
    assert not is_market_open(datetime(2026, 11, 2, 14, 15)), "09:15 EST, pre-open"

    # Early closes (13:00 ET).
    assert early_closes(2026) == {date(2026, 11, 27), date(2026, 12, 24)}, early_closes(2026)
    assert early_closes(2025) == {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)}
    assert early_closes(2027) == {date(2027, 11, 26)}, "Dec 24 2027 is a holiday"
    assert close_minute(date(2026, 11, 27)) == 780 and close_minute(date(2026, 11, 30)) == 960
    assert is_market_open(datetime(2026, 11, 27, 17, 45)), "12:45 ET on Black Friday"
    assert not is_market_open(datetime(2026, 11, 27, 18, 15)), "13:15 ET on Black Friday"
    assert is_market_open(datetime(2026, 11, 30, 18, 15)), "13:15 ET on an ordinary Monday"

    print("market_calendar: all self-tests passed")
