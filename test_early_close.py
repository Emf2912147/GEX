"""Early-close (13:00 ET) sessions: calendar, intraday window, watchdog."""
from datetime import date, datetime, time

import market_calendar as cal


def test_early_close_dates():
    assert cal.early_closes(2026) == {date(2026, 11, 27), date(2026, 12, 24)}
    assert date(2026, 7, 3) not in cal.early_closes(2026)      # observed holiday
    assert cal.early_closes(2027) == {date(2027, 11, 26)}      # Dec 24 2027 closed


def test_session_close_and_open():
    assert cal.session_close(date(2026, 11, 27)) == time(13, 0)
    assert cal.session_close(date(2026, 11, 30)) == time(16, 0)
    assert cal.is_market_open(datetime(2026, 11, 27, 17, 59))      # 12:59 ET
    assert not cal.is_market_open(datetime(2026, 11, 27, 18, 0))   # 13:00 ET
    assert cal.is_trading_day(date(2026, 11, 27))                  # still a session
