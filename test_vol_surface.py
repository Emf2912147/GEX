"""
Tests for vol_surface.py and its latest.json / spread-score wiring.

    python -m pytest test_vol_surface.py -q

Synthetic curves check the arithmetic; the stored 2026-11-13 chains check that
the real pipeline output says what the tape said (FOMC on 2026-12-09).
"""

import json
import math
import os
from datetime import date, timedelta

import pandas as pd
import pytest

import build_dashboard as bd
import sector_support_agent as ssa
import vol_metrics as vm
import vol_surface as vs

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")
DATE = "2026-11-13"
ASOF = date(2026, 11, 13)          # a Friday


def flat_points(asof=ASOF, vol=0.20, days=range(3, 130, 7), bumps=None):
    """A curve with constant forward vol on the variance clock, plus optional
    extra daily variance on given dates (bumps: {date: extra_vol})."""
    bumps = bumps or {}
    pts = []
    for d in days:
        e = asof + timedelta(days=d)
        if not vs.is_session(e):
            continue
        tt = vs.variance_time(asof, e)
        w = vol * vol * tt / vs.VARIANCE_YEAR
        for bd_, extra in bumps.items():
            if bd_ <= e:
                w += extra * extra / vs.VARIANCE_YEAR
        pts.append((e, float(d), math.sqrt(w / (d / 365.0))))
    return pts


# ------------------------------------------------------------------ clock

def test_market_holidays_2026_and_2027():
    h26, h27 = vs.market_holidays(2026), vs.market_holidays(2027)
    assert date(2026, 11, 26) in h26 and date(2026, 12, 25) in h26
    assert date(2026, 7, 3) in h26                    # July 4 on a Saturday
    assert date(2026, 4, 3) in h26                    # Good Friday
    assert date(2027, 6, 18) in h27                   # Juneteenth observed
    assert date(2027, 12, 24) in h27                  # Christmas observed
    assert len(h26) == 10


def test_trading_days_skip_weekend_and_thanksgiving():
    assert vs.trading_days(ASOF, date(2026, 11, 16)) == 1
    assert vs.trading_days(ASOF, date(2026, 11, 20)) == 5
    assert vs.trading_days(date(2026, 11, 25), date(2026, 11, 27)) == 1
    assert vs.variance_time(ASOF, date(2026, 11, 16)) == pytest.approx(1 + 2 * vs.CLOSED_DAY_WEIGHT)


# ------------------------------------------------------------------ surface

def test_flat_curve_is_fair_everywhere_with_no_events():
    s = vs.analyze(flat_points(), ASOF)
    assert s["events"] == [] and not s["noisy"]
    ranked = [r for r in s["rows"] if r["rich_rel"] is not None]
    assert len(ranked) >= 10
    assert all(abs(r["rich_rel"]) < 1e-3 and r["label"] == "fair" for r in ranked)
    assert all(r["fwd_iv"] == pytest.approx(0.20, abs=1e-3) for r in s["rows"])
    assert all(r["iv_t"] == pytest.approx(0.20, abs=1e-3) for r in s["rows"])


def test_a_weekend_is_not_read_as_cheap_or_rich():
    daily = [d for d in range(3, 40)]
    s = vs.analyze(flat_points(days=daily), ASOF)
    mondays = [r for r in s["rows"] if date.fromisoformat(r["expiration"]).weekday() == 0]
    assert mondays and all(r["fwd_iv"] == pytest.approx(0.20, abs=1e-3) for r in mondays)
    assert s["events"] == []


def test_event_day_is_flagged_and_marks_the_next_expiry_rich():
    event = date(2026, 12, 9)
    s = vs.analyze(flat_points(days=range(3, 60), bumps={event: 0.60}), ASOF)
    assert [e["expiration"] for e in s["events"]] == ["2026-12-09"]
    row = vs.row_for(s, event)
    assert row["event"] and row["fwd_iv"] > 0.5
    before = vs.row_for(s, date(2026, 12, 8))
    assert not before["event"] and before["rich_rel"] < row["rich_rel"]


def test_a_cheap_expiry_does_not_fake_an_event_on_the_next_one():
    pts = flat_points(days=range(3, 60))
    i = next(n for n, p in enumerate(pts) if p[1] >= 25)
    pts[i] = (pts[i][0], pts[i][1], pts[i][2] * 0.96)            # one expiry quoted 4% low
    s = vs.analyze(pts, ASOF)
    assert s["rows"][i + 1]["fwd_iv"] > 0.20 * vs.EVENT_RATIO       # the rebound is large...
    assert s["events"] == []                                        # ...and is not an event


def test_pick_expiry_buys_the_cheap_one_and_sells_the_rich_one():
    pts = flat_points()
    cheap, rich = pts[4], pts[6]
    pts[4] = (cheap[0], cheap[1], cheap[2] * 0.95)
    pts[6] = (rich[0], rich[1], rich[2] * 1.05)
    s = vs.analyze(pts, ASOF)
    lo, hi = cheap[1] - 8, rich[1] + 8
    buy, sell = vs.pick_expiry(s, lo, hi, "buy"), vs.pick_expiry(s, lo, hi, "sell")
    assert buy["expiration"] == cheap[0].isoformat() and buy["label"] == "cheap"
    assert sell["expiration"] == rich[0].isoformat() and sell["label"] == "rich"
    assert buy["edge_rel"] > 0.02 and sell["edge_rel"] > 0.02
    assert "cheapest to buy" in buy["reason"] and "richest to sell" in sell["reason"]


def test_pick_expiry_respects_the_allowed_set_and_declines_when_alone():
    pts = flat_points()
    pts[4] = (pts[4][0], pts[4][1], pts[4][2] * 0.95)
    s = vs.analyze(pts, ASOF)
    others = {r["expiration"] for r in s["rows"]} - {pts[4][0].isoformat()}
    p = vs.pick_expiry(s, 10, 80, "buy", allowed=others)
    assert p["expiration"] != pts[4][0].isoformat()
    assert vs.pick_expiry(s, pts[4][1], pts[4][1], "buy") is None       # one expiry: no comparison


def test_noisy_chain_is_not_ranked():
    pts = flat_points()
    pts = [(e, d, iv * (1.08 if i % 2 else 0.92)) for i, (e, d, iv) in enumerate(pts)]
    s = vs.analyze(pts, ASOF)
    assert s["noisy"]
    assert vs.pick_expiry(s, 14, 60, "buy") is None
    assert vs.score_adjustment(s, pts[5][0], "buy", cap=5) == (0.0, None)


def test_score_adjustment_sign_and_cap():
    pts = flat_points()
    pts[4] = (pts[4][0], pts[4][1], pts[4][2] * 0.90)
    s = vs.analyze(pts, ASOF)
    adj_buy, note = vs.score_adjustment(s, pts[4][0], "buy", cap=5)
    adj_sell, _ = vs.score_adjustment(s, pts[4][0], "sell", cap=5)
    assert adj_buy == 5 and adj_sell == -5 and "cheap" in note
    assert vs.score_adjustment(s, date(2030, 1, 1), "buy") == (0.0, None)


# ------------------------------------------------------------------ calendar gate

def _two_tenor(front_vol, back_vol):
    f, b = ASOF + timedelta(days=10), ASOF + timedelta(days=42)
    pts = [(f, 10.0, front_vol), (b, 42.0, back_vol)]
    return vs.analyze(pts, ASOF), f, b


def test_calendar_gate_states():
    s, f, b = _two_tenor(0.20, 0.20)
    g = vs.calendar_gate(s, f, b)
    assert g["pass"] is True and g["state"] == "fair" and g["ratio"] == pytest.approx(1.0, abs=0.10)

    s, f, b = _two_tenor(0.12, 0.20)                 # steep contango
    g = vs.calendar_gate(s, f, b)
    assert g["pass"] is False and g["state"] == "front_cheap" and g["ratio"] < vs.CAL_FRONT_CHEAP

    s, f, b = _two_tenor(0.30, 0.22)                 # near leg rich
    g = vs.calendar_gate(s, f, b)
    assert g["pass"] is True and g["state"] == "front_rich"
    g = vs.calendar_gate(s, f, b, stress=True)
    assert g["pass"] is False and g["state"] == "front_rich_stress"

    s, f, b = _two_tenor(0.60, 0.20)                 # negative forward variance
    g = vs.calendar_gate(s, f, b)
    assert g["pass"] is False and g["state"] == "inconsistent" and g["fwd_iv"] is None

    g = vs.calendar_gate(s, f, date(2030, 1, 1))
    assert g["pass"] is None and g["state"] == "unknown"


def test_forward_vol_matches_hand_calculation():
    s, f, b = _two_tenor(0.18, 0.21)
    rf, rb = vs.row_for(s, f), vs.row_for(s, b)
    w1, w2 = 0.18 ** 2 * 10 / 365, 0.21 ** 2 * 42 / 365
    want = math.sqrt((w2 - w1) / ((rb["_tt"] - rf["_tt"]) / vs.VARIANCE_YEAR))
    assert vs.calendar_gate(s, f, b)["fwd_iv"] == pytest.approx(want, abs=1e-4)


# ------------------------------------------------------------------ long-dated vol

def test_long_vol_state_and_ladder_read():
    base = [20 + (i % 50) * 0.1 for i in range(600)]            # 20.0 .. 24.9
    closes = {"vix": [18.0] * 600,
              "vix3m": base[:-1] + [20.2],                       # low in its range
              "vix6m": base[:-1] + [22.5],                       # middle
              "vix1y": base[:-1] + [24.8]}                       # high
    lv = vs.long_vol_state(closes)
    t = lv["tenors"]
    assert t["vix3m"]["state"] == "CHEAP" and t["vix6m"]["state"] == "NEUTRAL" and t["vix1y"]["state"] == "RICH"
    assert t["vix3m"]["pct_1y"] <= 30 <= t["vix6m"]["pct_1y"] <= 70 <= t["vix1y"]["pct_1y"]
    assert t["vix1y"]["ratio_to_vix"] == pytest.approx(24.8 / 18.0, abs=1e-3)
    read = vs.ladder_read(lv, days_to_expiry=215)                # Jun 2027 from Nov 2026
    assert read["tenor"] == "vix6m" and read["state"] == "NEUTRAL" and "VIX6M" in read["note"]
    assert vs.ladder_read(lv, 80)["tenor"] == "vix3m"
    assert vs.ladder_read({}, 200)["state"] == "UNKNOWN"


def test_percentile_needs_history():
    assert vs.percentile_rank([20.0] * 10, 20.0) is None
    assert vs.percentile_rank(list(range(1, 101)), 25) == 25.0


def test_long_vol_block_caches_and_survives_a_failed_fetch(tmp_path):
    from datetime import datetime, timezone
    calls = []

    def fetch(sym):
        calls.append(sym)
        return [20 + (i % 40) * 0.1 for i in range(400)], "2026-11-13"

    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    first = vm.long_vol_block(str(tmp_path), fetch=fetch, now=now)
    assert first["asof"] == "2026-11-13" and not first["stale"] and len(calls) == 4
    (tmp_path / "latest.json").write_text(json.dumps({"market": {"long_vol": first}}))

    again = vm.long_vol_block(str(tmp_path), fetch=fetch, now=now + timedelta(hours=2))
    assert again == first and len(calls) == 4                    # reused, no refetch

    def boom(sym):
        raise OSError("network down")

    late = vm.long_vol_block(str(tmp_path), fetch=boom, now=now + timedelta(hours=9))
    assert late["stale"] is True and late["tenors"] == first["tenors"]
    assert vm.long_vol_block(str(tmp_path / "none"), fetch=boom, now=now) is None


# ------------------------------------------------------------------ adapters

def _legs(surface_pts, spot=100.0, typ="call"):
    legs = []
    for e, d, iv in surface_pts:
        for k in (95.0, 100.0, 105.0, 110.0):
            legs.append({"expiration": e.isoformat(), "strike": k, "type": typ,
                         "implied_volatility": iv, "bid": 1.0, "ask": 1.1, "open_interest": 1000})
    return legs


def test_term_from_legs_round_trip():
    pts = flat_points(vol=0.25)
    term = vs.term_from_legs(_legs(pts), 100.0, ASOF)
    assert [t[0] for t in term] == [e.isoformat() for e, _, _ in pts]
    assert term[3][2] == pytest.approx(pts[3][2])
    s = vs.from_legs(_legs(pts), 100.0, ASOF)
    assert s["rows"] and not s["events"]


def _priced_legs(pts, spot=100.0):
    legs = []
    for e, d, iv in pts:
        T = d / 365.0
        for k in (98.0, 100.0, 102.0, 104.0, 106.0):
            v = ssa.call_value(spot, k, T, iv)
            legs.append({"expiration": e.isoformat(), "strike": k, "implied_volatility": iv,
                         "bid": round(v - 0.02, 2), "ask": round(v + 0.02, 2), "open_interest": 5000})
    return legs


def test_spread_score_prefers_the_cheap_expiry():
    pts = [p for p in flat_points(vol=0.30, days=range(3, 90, 7)) if True]
    in_window = [i for i, p in enumerate(pts) if 21 <= p[1] <= 45]
    a, b = in_window[0], in_window[-1]
    pts[a] = (pts[a][0], pts[a][1], pts[a][2] * 0.94)
    res = ssa.score("TEST", {"spot": 100.0, "legs": _priced_legs(pts)}, 98.0, 106.0, today=ASOF)
    rows = {r["expiration"]: r for r in res["top"]}
    cheap = pts[a][0].isoformat()
    assert res["expiry_selection"]["cheapest"]["expiration"] == cheap
    assert any(r["expiry_points"] == ssa.SURFACE_POINTS for r in res["top"] if r["expiration"] == cheap) \
        or res["best"]["expiration"] == cheap
    assert all(0 <= r["score"] <= 100 for r in res["top"])
    assert "expiry_label" in res["best"]


def test_spread_score_unchanged_when_the_chain_has_one_expiry():
    pts = [p for p in flat_points(vol=0.30, days=[28])]
    res = ssa.score("TEST", {"spot": 100.0, "legs": _priced_legs(pts)}, 98.0, 106.0, today=ASOF)
    assert res["best"]["expiry_points"] == 0.0 and res["best"]["expiry_note"] is None


# ------------------------------------------------------------------ stored chains

def stored(symbol):
    path = os.path.join(HIST, "raw", DATE, f"{symbol}.parquet")
    if not os.path.exists(path):
        pytest.skip(f"{path} not in this checkout")
    raw = pd.read_parquet(path)
    return raw, {"symbol": symbol, "spot": float(raw["spot"].iloc[0]), "session_date": DATE}


def test_spx_block_flags_the_december_fomc():
    raw, row = stored("SPX")
    b = vm.symbol_surface_block(raw, row)
    assert b["asof"] == DATE and not b["noisy"]
    assert "2026-12-09" in [e["expiration"] for e in b["events"]]
    assert set(b["windows"]) == {"7-14", "15-25", "20-35", "35-45", "45-60", "60-120"}
    assert b["windows"]["20-35"]["buy"]["expiration"] and b["windows"]["20-35"]["sell"]["expiration"]
    assert 0.10 < b["cm_iv"]["30"] < 0.25 and b["cm_iv"]["180"] > b["cm_iv"]["30"]
    for name in ("diagonal_35_45_over_7_14", "calendar_45_60_over_7_14"):
        g = b["calendars"][name]
        assert g["pass"] in (True, False) and g["ratio"] > 0 and g["front"] < g["back"]
    cols = b["expiries"]
    assert set(cols) == set(vm.SURFACE_COLUMNS) and len({len(v) for v in cols.values()}) == 1
    assert cols["event"][cols["expiration"].index("2026-12-09")] is True
    json.dumps(b)                                               # serialisable


def test_thin_chain_is_marked_noisy():
    raw, row = stored("XLF")
    b = vm.symbol_surface_block(raw, row)
    assert b["noisy"] is True
    assert all(w["buy"] is None and w["sell"] is None for w in b["windows"].values())
    assert b["events"] == []
    assert all(g["pass"] is None for g in b["calendars"].values())


def test_latest_json_carries_surface_and_keeps_existing_keys(tmp_path, monkeypatch):
    if not os.path.exists(os.path.join(HIST, "daily_metrics.csv")):
        pytest.skip("history not in this checkout")
    monkeypatch.setattr(vm, "_fetch_closes",
                        lambda sym: ([20 + (i % 40) * 0.1 for i in range(400)], "2026-11-13"))
    bd.build_latest_json(HIST, str(tmp_path))
    out = json.loads((tmp_path / "latest.json").read_text())
    spx = out["SPX"]
    for key in ("spot", "flip", "call_wall", "put_wall", "regime", "vol"):
        assert key in spx
    assert spx["surface"]["expiries"]["expiration"] and "windows" in spx["surface"]
    assert set(out["market"]["long_vol"]["tenors"]) == {"vix3m", "vix6m", "vix1y"}
    assert "vix" in out["market"]
