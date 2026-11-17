"""
Tests for flow_intensity.py (vanna/charm flow intensity score and grade).

    python -m pytest test_flow_intensity.py -q
"""

from datetime import date, timedelta

import pandas as pd
import pytest

import flow_intensity as fi

SESSION = "2026-11-13"


def chain(spot=100.0, session=date(2026, 11, 13), iv=0.2, oi_scale=1.0):
    """A small symmetric chain: puts and calls around spot, three expiries
    (one of them the Nov 20 monthly), enough strikes for an ATM IV."""
    rows = []
    for exp in (date(2026, 11, 20), date(2026, 11, 27), date(2026, 12, 11)):
        dte = (exp - session).days + 0.3
        for k in range(80, 121, 5):
            for t in ("C", "P"):
                otm = (t == "P" and k < spot) or (t == "C" and k > spot)
                rows.append({"symbol": "TST", "spot": spot,
                             "expiry": f"{exp} 21:00:00", "strike": float(k), "type": t,
                             "dte": dte, "iv": iv, "oi": (2000.0 if otm else 300.0) * oi_scale,
                             "volume": 10.0, "feed_ts": f"{session} 22:00:00",
                             "capture_ts": f"{session}T22:00:00+00:00"})
    return pd.DataFrame(rows)


def history_rows(n, charm=30.0, vpot=-100.0, step=1.0, symbol="TST"):
    d, out = date(2026, 8, 3), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append({"session_date": d.isoformat(), "symbol": symbol,
                        "charm_bp_day": charm + step * (len(out) % 10),
                        "vanna_bp_per_volpt": 20.0, "iv30": 0.18, "rv20": 0.2,
                        "vol_gap_pts": -2.0, "vanna_potential_bp": vpot - step * (len(out) % 10),
                        "sessions_to_opex": 10, "opex_date": "2026-11-20", "opex_oi_share": 0.2})
        d += timedelta(days=1)
    return pd.DataFrame(out, columns=fi.COLUMNS)


def today(charm=35.0, vpot=-105.0, share=0.2, s_to=10, session=SESSION):
    return {"session_date": session, "symbol": "TST", "charm_bp_day": charm,
            "vanna_bp_per_volpt": 20.0, "iv30": 0.18, "rv20": 0.2, "vol_gap_pts": -2.0,
            "vanna_potential_bp": vpot, "sessions_to_opex": s_to,
            "opex_date": "2026-11-20", "opex_oi_share": share}


# ------------------------------------------------------------- calendar

def test_monthly_opex_is_third_friday():
    assert fi.monthly_opex(date(2026, 11, 13)) == date(2026, 11, 20)
    assert fi.monthly_opex(date(2026, 11, 20)) == date(2026, 11, 20)
    assert fi.monthly_opex(date(2026, 11, 23)) == date(2026, 12, 18)


def test_monthly_opex_moves_off_a_holiday():
    # April 2025: the third Friday (Apr 18) was Good Friday
    assert fi.monthly_opex(date(2025, 4, 1)) == date(2025, 4, 17)


def test_sessions_between_counts_trading_days():
    assert fi.sessions_between(date(2026, 11, 13), date(2026, 11, 20)) == 5
    assert fi.sessions_between(date(2026, 11, 20), date(2026, 11, 20)) == 0
    # Thanksgiving week: Nov 26 closed
    assert fi.sessions_between(date(2026, 11, 20), date(2026, 11, 27)) == 4


# ------------------------------------------------------------- scoring

def test_pct_rank_counts_ties_half():
    assert fi.pct_rank([1, 2, 3, 4], 4) == pytest.approx(87.5)
    assert fi.pct_rank([1, 2, 3, 4], 0) == 0


@pytest.mark.parametrize("score,grade", [(95, "A"), (80, "A"), (79.9, "B"), (60, "B"),
                                         (45, "C"), (25, "D"), (5, "F"), (0, "F")])
def test_grade_bands(score, grade):
    assert fi.grade_for(score)[0] == grade


def test_no_grade_until_history_builds():
    b = fi.score_row(today(), history_rows(fi.MIN_HISTORY - 1))
    assert b["score"] is None and b["grade"] is None
    assert "building history" in b["note"]


def test_record_flows_score_near_the_top():
    b = fi.score_row(today(charm=500.0, vpot=-5000.0), history_rows(40))
    assert b["score"] > 95 and b["grade"] == "A"


def test_tiny_flows_score_near_the_bottom():
    b = fi.score_row(today(charm=0.5, vpot=-0.5), history_rows(40))
    assert b["score"] < 5 and b["grade"] == "F"


def test_intensity_ranks_size_not_sign():
    # a large negative charm (dealer selling) is as intense as a large positive one
    up = fi.score_row(today(charm=500.0), history_rows(40))
    down = fi.score_row(today(charm=-500.0), history_rows(40))
    assert up["charm_pct"] == down["charm_pct"]
    assert down["direction"] == "dealer selling"


def test_direction():
    h = history_rows(20)
    assert fi.score_row(today(charm=30, vpot=50), h)["direction"] == "dealer buying"
    assert fi.score_row(today(charm=-30, vpot=-50), h)["direction"] == "dealer selling"
    assert fi.score_row(today(charm=30, vpot=-50), h)["direction"] == "mixed"


def test_charm_only_when_vanna_cannot_be_scored():
    b = fi.score_row(today(vpot=None), history_rows(20))
    assert b["score"] == b["charm_pct"] and "charm only" in b["note"]
    h = history_rows(20)
    h["vanna_potential_bp"] = None
    b = fi.score_row(today(), h)
    assert b["score"] == b["charm_pct"] and "0 of 15" in b["note"]


def test_rolloff_flag_needs_size_and_proximity():
    h = history_rows(20)
    assert fi.score_row(today(share=0.5, s_to=3), h)["rolloff_risk"] is True
    assert fi.score_row(today(share=0.5, s_to=9), h)["rolloff_risk"] is False
    assert fi.score_row(today(share=0.1, s_to=2), h)["rolloff_risk"] is False


def test_lookback_caps_the_ranking_pool():
    # 200 old sessions of huge flows then 60 small ones: only the last 60 count
    old = history_rows(200, charm=1000.0, vpot=-9000.0)
    new = history_rows(260).tail(60)
    h = pd.concat([old, new]).reset_index(drop=True)
    h["session_date"] = [f"2025-{i // 28 + 1:02d}-{i % 28 + 1:02d}" for i in range(len(h))]
    b = fi.score_row(today(charm=500.0, vpot=-5000.0, session="2026-01-01"), h)
    assert b["history_sessions"] == fi.LOOKBACK and b["grade"] == "A"


def test_composite_averages_spx_qqq_iwm():
    blocks = {"SPX": {"score": 80, "direction": "mixed"},
              "QQQ": {"score": 60, "direction": "mixed"},
              "IWM": {"score": None, "direction": "dealer buying"},
              "SPY": {"score": 0, "direction": "dealer selling"}}
    c = fi.composite(blocks)
    assert c["score"] == 70 and c["grade"] == "B" and c["symbols"] == ["SPX", "QQQ"]
    assert fi.composite({}) is None


# ------------------------------------------------------------- chain and storage

def test_session_row_from_a_chain(tmp_path):
    r = fi.session_row(chain(), "TST", SESSION, str(tmp_path), None)
    assert r["iv30"] == pytest.approx(0.2, abs=0.01)
    assert r["rv20"] is None and r["vanna_potential_bp"] is None   # no RV source
    assert r["opex_date"] == "2026-11-20" and r["sessions_to_opex"] == 5
    assert 0.3 < r["opex_oi_share"] < 0.4                          # 1 of 3 expiries
    assert r["charm_bp_day"] > 0                                    # OTM puts dominate


def test_vol_gap_is_clipped(tmp_path):
    rv = pd.DataFrame([{"symbol": "TST", "capture_ts": "2026-11-14T14:00:00+00:00",
                        "rv_asof_date": SESSION, "hv20": 0.60}])
    rv.to_csv(tmp_path / "realized_vol.csv", index=False)
    r = fi.session_row(chain(iv=0.2), "TST", SESSION, str(tmp_path), None)
    assert r["vol_gap_pts"] == -fi.GAP_CLIP
    assert r["vanna_potential_bp"] == pytest.approx(r["vanna_bp_per_volpt"] * -fi.GAP_CLIP, rel=1e-3)


def test_stale_rv_file_is_not_used(tmp_path):
    rv = pd.DataFrame([{"symbol": "TST", "capture_ts": "2026-10-01T14:00:00+00:00",
                        "rv_asof_date": "2026-10-01", "hv20": 0.25}])
    rv.to_csv(tmp_path / "realized_vol.csv", index=False)
    assert fi.session_row(chain(), "TST", SESSION, str(tmp_path), None)["rv20"] is None


def test_upsert_replaces_the_same_session(tmp_path):
    fi.upsert(str(tmp_path), [today(charm=1.0), today(charm=2.0, session="2026-11-12")])
    fi.upsert(str(tmp_path), [today(charm=9.0)])
    h = fi.load_history(str(tmp_path))
    assert len(h) == 2
    assert h.loc[h["session_date"] == SESSION, "charm_bp_day"].item() == 9.0


def test_block_read_only_mode_does_not_write(tmp_path):
    fi.upsert(str(tmp_path), history_rows(20).to_dict("records"))
    before = (tmp_path / fi.HISTORY_FILE).read_text()
    b = fi.block(str(tmp_path), chain(), "TST", SESSION, None, write=False)
    assert b is not None and b["grade"] is not None
    assert (tmp_path / fi.HISTORY_FILE).read_text() == before


def test_block_write_mode_stores_today(tmp_path):
    fi.upsert(str(tmp_path), history_rows(20).to_dict("records"))
    fi.block(str(tmp_path), chain(), "TST", SESSION, None, write=True)
    h = fi.load_history(str(tmp_path))
    assert SESSION in set(h["session_date"])


def test_stored_history_file_is_well_formed():
    h = fi.load_history("history")
    if h.empty:
        pytest.skip("no stored history")
    assert list(h.columns) == fi.COLUMNS
    assert not h.duplicated(["session_date", "symbol"]).any()
