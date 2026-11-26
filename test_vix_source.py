"""gex_intraday VIX capture: tastytrade snapshot, Yahoo fallback, header migration."""
import datetime as dt
import sys
import types

import pandas as pd

import gex_intraday as gi

NOW = dt.datetime(2026, 11, 25, 21, 30, tzinfo=dt.timezone.utc)
Q = lambda px, pc: {"current_price": px, "prev_day_close": pc, "last_trade_time": "2026-11-25T21:15:00"}
SNAP = {"indices": {"VIX": Q(28.29, 26.36), "VIX9D": Q(29.61, 26.68),
                    "VIX3M": Q(26.29, 25.47), "VVIX": Q(115.38, 114.07)},
        "futures": [{"symbol": "/VXZ26", "expiry": "2026-12-16", "price": 24.825},
                    {"symbol": "/VXF27", "expiry": "2027-01-20", "price": 24.125}]}

OLD_HEADER = ("feed_ts,feed_date,capture_ts,elapsed_s,vix,vix_chg_1d_pct,vix_20d_change_pct,"
              "vix_20d_high,vix9d,vix9d_chg_1d_pct,term_spread_9d_vs_30d,term_state,schema_version\n")
OLD_ROW = "2026-11-24T21:15:00,2026-11-24,2026-11-24T21:30:00+00:00,900,26.36,0.01,,False,26.68,0.02,0.32,backwardation(short-rich),1\n"


def run(monkeypatch, tmp_path, snap):
    (tmp_path / "vix_state.csv").write_text(OLD_HEADER + OLD_ROW)
    monkeypatch.setitem(sys.modules, "tastytrade_vix",
                        types.SimpleNamespace(snapshot=lambda: snap) if snap else None)
    monkeypatch.setattr(gi.ixh, "quote", lambda s: Q(28.0 if s == "VIX" else 29.0, 27.0))
    monkeypatch.setattr(gi, "fetch_20d_change", lambda sym, px: (None, None))
    args = types.SimpleNamespace(outdir=str(tmp_path), force=True, dry_run=False)
    row = gi.capture_vix(args, str(tmp_path / "log.txt"), NOW)
    return row, pd.read_csv(tmp_path / "vix_state.csv", dtype=str, keep_default_na=False)


def test_tastytrade_row_carries_the_vx_curve(monkeypatch, tmp_path):
    row, v = run(monkeypatch, tmp_path, SNAP)
    assert row["vix_source"] == "tastytrade" and row["vix"] == 28.29
    assert row["vx1"] == 24.825 and row["vx2"] == 24.125
    assert row["vx_slope_1_2"] == -0.7 and row["vx_term_state"] == "backwardation"
    assert row["vx1_basis"] == round(24.825 - 28.29, 4)
    assert row["vix3m"] == 26.29 and row["vvix"] == 115.38
    assert list(v.columns) == gi.VIX_COLUMNS


def test_old_rows_survive_the_header_migration_unchanged(monkeypatch, tmp_path):
    _, v = run(monkeypatch, tmp_path, SNAP)
    first = v.iloc[0]
    old = dict(zip(OLD_HEADER.strip().split(","), OLD_ROW.strip().split(",")))
    assert all(first[k] == val for k, val in old.items())
    assert first["vix_source"] == "" and first["vx1"] == ""


def test_no_tastytrade_falls_back_to_yahoo_without_a_curve(monkeypatch, tmp_path):
    row, v = run(monkeypatch, tmp_path, None)
    assert row["vix_source"] == "yahoo" and row["vix"] == 28.0
    assert pd.isna(row.get("vx1")) if "vx1" in row else True
    assert v.iloc[-1]["vx1"] == ""


def test_curve_needs_two_contracts():
    assert gi.vx_curve({"futures": SNAP["futures"][:1]}) == {}
    assert gi.vx_curve(None) == {}
