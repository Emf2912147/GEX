"""
Tests for prune_history.py.

    python -m pytest test_prune_history.py -q

The point of these is not the arithmetic -- it is that the script cannot eat
the derived metrics, cannot empty a thin store, and cannot be fooled by a
wrong clock.
"""

import os
from datetime import date, timedelta

import pytest

import prune_history as ph


def build(tmp_path, kind, dates, files=("SPX.parquet", "QQQ.parquet"), size=1000):
    root = tmp_path / "history" / kind
    for d in dates:
        p = root / d
        p.mkdir(parents=True)
        for f in files:
            (p / f).write_bytes(b"x" * size)
    return str(tmp_path / "history")


def days_back(n, anchor="2026-11-13"):
    return (date.fromisoformat(anchor) - timedelta(days=n)).isoformat()


def test_window_is_measured_from_the_newest_session_not_the_clock(tmp_path):
    """A machine offline for a year must not wake up and delete everything."""
    old = [days_back(n) for n in (120, 100, 95)]
    recent = [days_back(n) for n in range(30, -1, -1)]       # clears the floor; newest is day 0
    hist = build(tmp_path, "raw", sorted(old + recent))
    p = ph.plan(hist, "raw", 90, today=date(2029, 1, 1))     # clock years ahead
    assert p["cutoff"] == days_back(90)
    assert p["remove"] == sorted(old)


def test_floor_outranks_the_window(tmp_path):
    """A thin store keeps MIN_KEEP sessions however old they are."""
    dates = sorted(days_back(n) for n in range(300, 280, -1))   # 20 old sessions
    hist = build(tmp_path, "raw", dates)
    p = ph.plan(hist, "raw", 90)
    assert len(dates) - len(p["remove"]) == ph.MIN_KEEP["raw"] or not p["remove"]
    assert p["reason"] and "floor" in p["reason"]
    remaining = len(dates) - len(p["remove"])
    assert remaining >= min(ph.MIN_KEEP["raw"], len(dates))


def test_a_session_holding_anything_odd_is_skipped_not_deleted(tmp_path):
    old = days_back(200)
    hist = build(tmp_path, "intraday", [old] + [days_back(n) for n in range(12, 0, -1)])
    (tmp_path / "history" / "intraday" / old / "notes.txt").write_text("mine")
    p = ph.plan(hist, "intraday", 30)
    assert p["remove"] == []
    assert p["skipped"] and p["skipped"][0][0] == old
    assert "notes.txt" in p["skipped"][0][1]
    ph.apply_plan(p)
    assert (tmp_path / "history" / "intraday" / old / "notes.txt").exists()


def test_derived_metrics_survive_an_apply(tmp_path):
    """The whole point: pruning snapshots never touches the computed record."""
    hist = build(tmp_path, "intraday",
                 [days_back(n) for n in (200, 150)] + [days_back(n) for n in range(12, 0, -1)])
    build(tmp_path, "raw", [days_back(n) for n in (200,)] + [days_back(n) for n in range(30, 0, -1)])
    h = tmp_path / "history"
    for name in ("daily_metrics.csv", "intraday_state.csv", "vix_state.csv",
                 "realized_vol.csv", "official_closes.json", "intraday.log"):
        (h / name).write_text("keep me")
    (h / "sector").mkdir()
    (h / "sector" / "XLK.csv").write_text("keep me too")

    assert ph.main(["--histdir", str(h), "--apply"]) == 0

    for name in ("daily_metrics.csv", "intraday_state.csv", "vix_state.csv",
                 "realized_vol.csv", "official_closes.json", "intraday.log"):
        assert (h / name).read_text() == "keep me", name
    assert (h / "sector" / "XLK.csv").exists()
    assert not (h / "intraday" / days_back(200)).exists()     # snapshots did go
    assert (h / "intraday" / days_back(1)).exists()


def test_dry_run_deletes_nothing(tmp_path):
    hist = build(tmp_path, "intraday",
                 [days_back(n) for n in (300, 299, 298)] + [days_back(n) for n in range(12, 0, -1)])
    before = sorted(os.listdir(os.path.join(hist, "intraday")))
    assert ph.main(["--histdir", hist]) == 0
    assert sorted(os.listdir(os.path.join(hist, "intraday"))) == before


def test_non_date_directories_are_ignored(tmp_path):
    hist = build(tmp_path, "raw", [days_back(200)] + [days_back(n) for n in range(30, 0, -1)])
    odd = tmp_path / "history" / "raw" / "quarantine"
    odd.mkdir()
    (odd / "SPX.parquet").write_bytes(b"x")
    p = ph.plan(hist, "raw", 90)
    assert "quarantine" not in p["remove"]
    ph.apply_plan(p)
    assert odd.exists()


def test_apply_frees_what_the_dry_run_promised(tmp_path):
    old = [days_back(n) for n in (200, 190, 180)]
    recent = [days_back(n) for n in range(12, 0, -1)]
    hist = build(tmp_path, "intraday", sorted(old + recent), size=4096)
    p = ph.plan(hist, "intraday", 30)
    assert p["bytes"] == len(old) * 2 * 4096          # two files per session
    assert ph.apply_plan(p) == len(old)
    assert sorted(os.listdir(os.path.join(hist, "intraday"))) == sorted(recent)


def test_only_the_two_snapshot_roots_are_prunable():
    assert ph.PRUNABLE == ("raw", "intraday")
    with pytest.raises(AssertionError):
        ph.plan("history", "sector", 30)
