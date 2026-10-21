"""gex_intraday.main: a stale symbol gets one late retry and is a refusal, not a failure."""

import sys

import gex_intraday as gi


def run(monkeypatch, tmp_path, behaviour, symbols=("SPX", "XLK")):
    calls = []

    def fake_capture(sym, args, logpath, now):
        calls.append(sym)
        outcome = behaviour[sym].pop(0)
        if outcome == "stale":
            raise gi.StaleFeedError(f"feed_ts is 33.2 min behind the capture")
        return None, {"spot": 1.0, "net_gex_window": 0.0, "flip": 1.0,
                      "call_wall": 2.0, "put_wall": 0.5, "regime": "positive",
                      "rr25": 0.0, "skew_state": "FLAT", "session_pc_volume": 1.0}

    monkeypatch.setattr(gi, "capture_symbol", fake_capture)
    monkeypatch.setattr(gi.time, "sleep", lambda s: calls.append(f"sleep {s:.0f}"))
    monkeypatch.setattr(sys, "argv", ["gex_intraday.py", "--symbols", *symbols,
                                      "--outdir", str(tmp_path), "--dry-run",
                                      "--ignore-calendar", "--no-vix", "--no-rv"])
    return gi.main(), calls


def test_stale_symbol_recovers_on_the_late_retry(monkeypatch, tmp_path):
    code, calls = run(monkeypatch, tmp_path, {"SPX": ["ok"], "XLK": ["stale", "ok"]})
    assert code == 0
    assert calls == ["SPX", "XLK", "sleep 60", "XLK"]


def test_still_stale_is_a_refusal_not_a_red_run(monkeypatch, tmp_path):
    code, calls = run(monkeypatch, tmp_path, {"SPX": ["ok"], "XLK": ["stale", "stale"]})
    assert code == 0
    assert calls.count("XLK") == 2


def test_every_symbol_stale_still_goes_red(monkeypatch, tmp_path):
    code, _ = run(monkeypatch, tmp_path, {"SPX": ["stale", "stale"], "XLK": ["stale", "stale"]})
    assert code == 1


def test_xlk_accepts_up_to_30_minutes_others_15():
    assert gi.feed_lag_limit("XLK", 900) == 1800
    assert gi.feed_lag_limit("xlk", 900) == 1800
    assert gi.feed_lag_limit("SPY", 900) == 900


def test_freshness_check_with_the_xlk_limit():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 21, 20, 52, tzinfo=timezone.utc)
    # 20 min behind: refused at 15, accepted at XLK's 30
    assert gi.check_feed_freshness("XLK", "2026-10-21 20:32:00", now,
                                   gi.feed_lag_limit("XLK", 900), 300) == 1200
    import pytest
    with pytest.raises(gi.StaleFeedError):
        gi.check_feed_freshness("SPY", "2026-10-21 20:32:00", now,
                                gi.feed_lag_limit("SPY", 900), 300)
    with pytest.raises(gi.StaleFeedError):
        gi.check_feed_freshness("XLK", "2026-10-21 20:12:00", now,
                                gi.feed_lag_limit("XLK", 900), 300)


def test_integrity_limits_match_the_capture_limits():
    import check_integrity as ci
    for sym, secs in gi.SYMBOL_MAX_FEED_LAG.items():
        assert ci.SYMBOL_FEED_LAG_LIMIT_MIN[sym] * 60 == secs
