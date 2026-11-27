"""credit_stress.py: the HYG/IEF z-score port of the thinkScript study."""
import json
import math
import statistics
from datetime import datetime, timedelta, timezone

import pytest

import credit_stress as cs


def _dates(n):
    d0 = datetime(2026, 1, 1)
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


@pytest.fixture(autouse=True)
def _clear_memo():
    cs._MEMO.clear()
    yield
    cs._MEMO.clear()


def test_zscore_matches_thinkscript_population_stdev():
    vals = [1 + 0.01 * math.sin(i / 3) + 0.001 * i for i in range(80)]
    z = cs.zscores(vals, 60)
    assert z[58] is None and z[59] is not None
    w = vals[20:80]
    expect = (vals[79] - statistics.fmean(w)) / statistics.pstdev(w)
    assert z[79] == pytest.approx(expect, rel=1e-12)


def test_flat_window_gives_none():
    assert cs.zscores([1.0] * 60, 60)[-1] is None


def test_ratio_aligns_on_date_and_skips_gaps():
    num = (["a", "b", "c"], [10.0, 11.0, 12.0])
    den = (["a", "c"], [5.0, 4.0])
    assert cs.ratio_series(num, den) == (["a", "c"], [2.0, 3.0])


def _series_with_drop(n=140, drop_sessions=3):
    vals = [1.0 + 0.002 * math.sin(i) for i in range(n - drop_sessions)]
    vals += [vals[-1] * (1 - 0.01 * (k + 1)) for k in range(drop_sessions)]
    return _dates(n), vals


def test_stress_state_counts_sessions_and_cross():
    d, r = _series_with_drop()
    b = cs.state(d, r)
    assert b["state"] == "stress" and b["z"] < -2
    assert b["sessions_below_threshold"] == 3
    assert b["last_cross_below"] == d[-3]
    assert b["trend"] == "deteriorating"
    assert len(b["history"]) == cs.HISTORY_POINTS
    assert b["history"][-1]["date"] == d[-1]


def test_calm_state():
    d = _dates(120)
    r = [1.0 + 0.002 * math.sin(i) for i in range(120)]
    b = cs.state(d, r)
    assert b["state"] in ("calm", "watch")
    assert b["sessions_below_threshold"] == 0 or b["z"] < -2


def test_too_short_is_none():
    assert cs.state(_dates(30), [1.0 + i * 1e-3 for i in range(30)]) is None


def _fetch_ok(sym):
    d, r = _series_with_drop()
    return (d, [100 * x for x in r]) if sym == "HYG" else (d, [100.0] * len(d))


def test_block_fetches_and_marks_fresh(tmp_path):
    now = datetime(2026, 11, 27, 14, tzinfo=timezone.utc)
    b = cs.credit_block(str(tmp_path), fetch=_fetch_ok, now=now)
    assert b["state"] == "stress" and b["stale"] is False
    assert b["fetched_utc"] == now.isoformat()
    json.dumps(b, allow_nan=False)                       # valid JSON for the app


def test_block_reuses_recent_and_keeps_stale_on_failure(tmp_path):
    now = datetime(2026, 11, 27, 14, tzinfo=timezone.utc)
    prev = {"z": -1.2, "state": "watch", "fetched_utc": (now - timedelta(hours=1)).isoformat(),
            "stale": False}
    (tmp_path / "latest.json").write_text(json.dumps({"market": {"credit_stress": prev}}))

    def boom(sym):
        raise RuntimeError("down")
    assert cs.credit_block(str(tmp_path), fetch=boom, now=now) == prev     # cached, no fetch
    cs._MEMO.clear()
    later = now + timedelta(hours=cs.MAX_AGE_H + 1)
    out = cs.credit_block(str(tmp_path), fetch=boom, now=later)
    assert out["stale"] is True and out["z"] == -1.2


def test_text_line():
    d, r = _series_with_drop()
    line = cs.text(cs.state(d, r))
    assert "STRESS" in line and "below -2" in line
    assert cs.text(None).endswith("unavailable")
