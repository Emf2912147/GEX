"""
Tests for flow_outcomes.py (opex / flow-intensity outcome log).

    python -m pytest test_flow_outcomes.py -q
"""

import math
from datetime import date, timedelta

import pandas as pd
import pytest

import flow_intensity as fi
import flow_outcomes as fo


def sessions(start, n):
    d, out = start, []
    while len(out) < n:
        if fi.mc.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.fixture
def store(tmp_path):
    """45 sessions Sep 1 - Nov 2 2026 for SPX: flow history plus closes
    rising 1% a session."""
    days = sessions(date(2026, 9, 1), 45)
    rows, mrows = [], []
    for i, d in enumerate(days):
        opex = fi.monthly_opex(d + timedelta(days=1))
        s_to = fi.sessions_between(d, opex)
        charm = 100.0 + (i % 7) * 10
        rows.append({"session_date": d.isoformat(), "symbol": "SPX", "charm_usd_day": charm,
                     "vanna_usd_per_volpt": 50.0, "iv30": 0.15, "rv20": 0.2, "vol_gap_pts": -5.0,
                     "vanna_potential_usd": -250.0 - (i % 5) * 20, "sessions_to_opex": s_to,
                     "opex_date": opex.isoformat(), "opex_oi_share": 0.3,
                     "next_opex_date": fi.next_monthly(opex).isoformat(), "next_oi_share": 0.3,
                     "front_charm_usd_day": charm * 0.7, "front_vanna_usd_per_volpt": 20.0,
                     "next_charm_usd_day": charm * 0.3, "next_vanna_usd_per_volpt": 30.0,
                     "window_max_dte": 60.0})
        mrows.append({"feed_ts": f"{d} 22:00:00", "session_date": d.isoformat(), "symbol": "SPX",
                      "spot": 1000.0 * 1.01 ** i})
    pd.DataFrame(rows, columns=fi.COLUMNS).to_csv(tmp_path / fi.HISTORY_FILE, index=False)
    pd.DataFrame(mrows).to_csv(tmp_path / "daily_metrics.csv", index=False)
    return tmp_path, days


def test_last_opex_before():
    assert fo.last_opex_before(date(2026, 11, 23)) == date(2026, 11, 20)
    assert fo.last_opex_before(date(2026, 11, 20)) == date(2026, 10, 16)
    assert fo.last_opex_before(date(2026, 11, 2)) == date(2026, 10, 16)
    assert fo.last_opex_before(date(2027, 1, 5)) == date(2026, 12, 18)


def test_forward_return_and_rv():
    px = pd.Series([100.0, 101, 102, 103, 104, 105, 106],
                   index=[f"2026-10-0{i}" for i in range(1, 8)])
    assert fo.forward(px, "2026-10-01", 5) == pytest.approx(math.log(105 / 100))
    assert fo.forward(px, "2026-10-03", 5) is None          # not there yet
    assert fo.forward(px, "2026-09-30", 1) is None          # unknown session
    assert fo.forward_rv(px, "2026-10-01") > 0


def test_build_rows_flags_and_outcomes(store):
    path, days = store
    df = fo.write(str(path))
    assert (path / fo.OUTFILE).exists()
    assert list(df.columns) == fo.COLUMNS
    assert len(df) == 45 - fi.MIN_HISTORY                   # graded sessions only
    # closes rise 1% a session: every complete 5-day outcome is +5 x log(1.01)
    done = df.dropna(subset=["ret_5d"])
    assert done["ret_5d"].round(6).eq(round(5 * math.log(1.01), 6)).all()
    assert df["ret_5d"].isna().sum() == 5                   # last 5 not filled yet
    # roll-off flag only within 5 sessions of the front monthly (70% of charm)
    assert (df.loc[df["rolloff_flag"], "sessions_to_opex"] <= 5).all()
    assert df["rolloff_flag"].any()
    # vanna potential is always dealer selling here
    assert not df["vanna_buying_high"].any()
    # post-opex: the first 5 sessions after a monthly (Sep 18 and Oct 16 here)
    post = df.loc[df["post_opex"], "session_date"].tolist()
    assert ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"] == \
        [d for d in post if d >= "2026-10-01"]
    assert all(d <= "2026-09-25" for d in post if d < "2026-10-01")
    assert "2026-10-26" not in post


def test_ret_to_opex_fills_once_expiry_passed(store):
    path, days = store
    df = fo.build(str(path))
    r = df[df["session_date"] == "2026-10-09"].iloc[0]
    assert r["opex_date"] == "2026-10-16"
    assert r["ret_to_opex"] == pytest.approx(5 * math.log(1.01))
    assert df[df["opex_date"] == "2026-11-20"]["ret_to_opex"].isna().all()


def test_summary_counts_cycles(store):
    path, _ = store
    s = fo.summary(fo.build(str(path))).set_index("flag")
    assert set(s.index) == set(fo.FLAGS)
    assert s.loc["rolloff_flag", "cycles"] >= 1
    assert s.loc["rolloff_flag", "n"] + s.loc["rolloff_flag", "others_n"] == 25


def test_opex_table(store):
    path, _ = store
    t = fo.opex_table(str(path))
    oct16 = t[(t["opex"] == "2026-10-16") & (t["symbol"] == "SPX")].iloc[0]
    assert oct16["into_opex_%"] == pytest.approx(5 * math.log(1.01) * 100)
    assert oct16["after_opex_%"] == pytest.approx(5 * math.log(1.01) * 100)
    assert bool(oct16["rolloff_flag_5_before"]) is True


def test_stored_outcome_file_is_well_formed():
    import os
    p = os.path.join("history", fo.OUTFILE)
    if not os.path.exists(p):
        pytest.skip("no stored log")
    df = pd.read_csv(p)
    assert list(df.columns) == fo.COLUMNS
    assert not df.duplicated(["session_date", "symbol"]).any()
