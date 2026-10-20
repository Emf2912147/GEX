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
