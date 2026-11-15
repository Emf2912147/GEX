"""
Tests for rsi_divergence.py (the RSI divergence gate, G10).

    python -m pytest test_rsi_divergence.py -q

Synthetic price paths check each rule on its own; a pandas reference checks the
RSI and the pivot rule against an independent implementation on random walks.
"""

import random
from datetime import date, timedelta

import pandas as pd
import pytest

import rsi_divergence as rd


def path(parts, start):
    p = [start]
    for n, target in parts:
        s = p[-1]
        for i in range(1, n + 1):
            p.append(s + (target - s) * i / n)
    return p


def bars(prices, pad=0.2):
    return [(x + pad, x - pad, x) for x in prices]


WARM = [100 + ((-1) ** i) * 0.3 for i in range(30)]
# sharp run to a first high, dip, slower climb to a HIGHER high: RSI lower
BEAR_PATH = WARM + path([(5, 108), (5, 104), (10, 109), (6, 104)], WARM[-1])
# the mirror image: a LOWER low with a higher RSI
BULL_PATH = [200 - (x - 100) for x in BEAR_PATH]


def run(prices, tf="1D"):
    b = bars(prices)
    return rd.detect([x[0] for x in b], [x[1] for x in b], [x[2] for x in b], tf)


# ----------------------------------------------------------------- indicators

def test_rsi_matches_pandas_wilder():
    rng = random.Random(7)
    closes = [100.0]
    for _ in range(300):
        closes.append(closes[-1] * (1 + rng.gauss(0, 0.01)))
    s = pd.Series(closes)
    d = s.diff()
    au = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    ad = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    ref = (100 - 100 / (1 + au / ad)).tolist()
    got = rd.rsi(closes)
    for g, r in zip(got, ref):
        if r != r:  # NaN
            assert g is None
        else:
            assert g == pytest.approx(r, abs=1e-9)


def test_rsi_flat_up_series_is_100():
    assert rd.rsi([float(i) for i in range(40)])[-1] == 100.0


def test_pivots_match_reference_on_random_walks():
    rng = random.Random(3)
    for k in (2, 3):
        highs, lows = [], []
        x = 100.0
        for _ in range(400):
            x *= 1 + rng.gauss(0, 0.01)
            highs.append(x * 1.002)
            lows.append(x * 0.998)
        ph, pl = rd.pivots(highs, lows, k)
        h, l = pd.Series(highs), pd.Series(lows)
        n = len(h)
        ref_h = [i for i in range(k, n - k)
                 if h[i] == h[i - k:i + k + 1].max() and h[i] > h[i - k:i].max()]
        ref_l = [i for i in range(k, n - k)
                 if l[i] == l[i - k:i + k + 1].min() and l[i] < l[i - k:i].min()]
        assert ph == ref_h and pl == ref_l


# ------------------------------------------------------------------ detection

def test_bearish_divergence_found():
    d = run(BEAR_PATH)
    assert d["type"] == "bear"
    assert d["price2"] > d["price1"] and d["rsi2"] < d["rsi1"]
    assert 5 <= d["spacing"] <= 16
    assert d["age"] == 3  # confirmed k=3 bars after the second high


def test_bullish_divergence_found():
    d = run(BULL_PATH)
    assert d["type"] == "bull"
    assert d["price2"] < d["price1"] and d["rsi2"] > d["rsi1"]


def test_no_signal_before_confirmation():
    # drop the last 3 bars: the second pivot is not confirmed yet
    assert run(BEAR_PATH[:-4])["type"] != "bear"


def test_spacing_outside_window_does_not_count(monkeypatch):
    # the same pattern is 15 bars apart; narrow the window to 5-10 and it is out
    monkeypatch.setitem(rd.TIMEFRAMES, "1D", {**rd.TIMEFRAMES["1D"], "window": (5, 10)})
    d = run(BEAR_PATH)
    assert d["type"] is None and "window" in d["reason"]


def test_stale_signal_expires():
    # 14 more bars drifting down after the confirmed divergence
    tail = path([(14, 100)], BEAR_PATH[-1])[1:]
    d = run(BEAR_PATH + tail)
    assert d["type"] is None


def test_broken_by_a_new_high_does_not_count():
    # price runs on to a new high above the second pivot: that pivot is replaced
    p = BEAR_PATH + path([(8, 112)], BEAR_PATH[-1])[1:]
    d = run(p)
    assert d["type"] != "bear" or d["price2"] > 109.2


def test_short_history_is_reported_not_guessed():
    d = run(BEAR_PATH[:10])
    assert d["type"] is None and "bars" in d["reason"]


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        rd.detect([1.0, 2.0], [1.0], [1.0, 2.0], "1D")


# --------------------------------------------------------------------- grading

def sig(tf, kind):
    return {"tf": tf, "type": kind, "spacing": 8, "age": 2,
            "rsi1": 40.0, "rsi2": 46.0}


def an(w=None, d=None, h=None):
    mk = lambda tf, v: None if v == "n/a" else sig(tf, v) if v else {"tf": tf, "type": None}
    return {"1W": mk("1W", w), "1D": mk("1D", d), "1H": mk("1H", h)}


@pytest.mark.parametrize("a,bias,grade", [
    (an("bull", "bull", "bull"), "up", "A"),
    (an("bear", "bear", "bear"), "down", "A"),
    (an("bull", "bull", None), "up", "B"),
    (an("bull", "bull", "n/a"), "up", "B"),
    (an("bull", None, None), "up", "C"),
    (an(None, "bull", None), "up", "C"),
    (an(None, "bull", "bull"), "up", "C"),
    (an(None, None, "bull"), "up", "D"),
    (an(None, None, None), "up", "D"),
    (an("bull", "bull", "bear"), "up", "C"),      # higher frames outweigh hourly
    (an("bull", "bear", None), "up", "D"),        # weekly vs daily disagree
    (an("bear", "bull", None), "up", "F"),        # weekly bearish vs daily bullish
    (an("bear", "bear", None), "up", "F"),
    (an("bull", "bull", "bull"), "down", "F"),
])
def test_grades(a, bias, grade):
    assert rd.grade(a, bias)["grade"] == grade


def test_weekly_negative_with_daily_positive_is_mixed_not_agreeing():
    # the case Eugenio described: both can be true at once
    g = rd.grade(an("bear", "bull", None), "up")
    assert g["aligned"] == ["1D"] and g["opposed"] == ["1W"]
    assert g["grade"] == "F"


def test_agreement_is_stronger_than_disparity():
    order = "ABCDF"
    agree = rd.grade(an("bull", "bull", "bull"), "up")["grade"]
    mixed = rd.grade(an("bull", "bull", "bear"), "up")["grade"]
    split = rd.grade(an("bull", "bear", "bull"), "up")["grade"]
    assert order.index(agree) < order.index(mixed) <= order.index(split)


def test_chop_has_no_grade():
    g = rd.grade(an("bull", "bull", "bull"), "chop")
    assert g["grade"] is None and "information" in g["note"]


def test_missing_data_is_named():
    g = rd.grade(an("bull", "bull", "n/a"), "up")
    assert g["missing"] == ["1H"] and "1H" in g["note"]


def test_ticket_line_mentions_every_timeframe():
    line = rd.ticket_line(an("bull", "bull", None), "up")
    assert line.startswith("RSI div B (up)")
    assert "1W bull" in line and "1D bull" in line and "1H none" in line


# -------------------------------------------------------------------- helpers

def test_from_robinhood_drops_interpolated():
    rows = [
        {"high_price": "2", "low_price": "1", "close_price": "1.5", "interpolated": True},
        {"high_price": "3", "low_price": "2", "close_price": "2.5"},
    ]
    assert rd.from_robinhood(rows) == [(3.0, 2.0, 2.5)]


def daily(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            x = 100 + len(out)
            out.append((d, x + 1, x - 1, x))
        d += timedelta(days=1)
    return out


def test_weekly_drops_the_week_in_progress():
    rows = daily(date(2026, 10, 5), 12)       # Mon Oct 5 .. Tue Oct 20
    assert rows[-1][0] == date(2026, 10, 20)
    assert len(rd.weekly_from_daily(rows)) == 2       # third week still open


def test_weekly_keeps_a_week_that_ended_friday():
    rows = daily(date(2026, 10, 5), 10)       # through Fri Oct 16
    assert rows[-1][0] == date(2026, 10, 16)
    assert len(rd.weekly_from_daily(rows)) == 2


def test_weekly_keeps_a_holiday_shortened_week_once_it_has_passed():
    rows = [r for r in daily(date(2026, 10, 5), 11) if r[0] != date(2026, 10, 9)]
    # Fri Oct 9 missing, last bar is Mon Oct 19: the first week is complete
    wk = rd.weekly_from_daily(rows)
    assert len(wk) == 2 and wk[0][2] == rows[3][3]


def test_weekly_aggregation_values():
    rows = daily(date(2026, 10, 5), 10)
    wk = rd.weekly_from_daily(rows)
    assert wk[0] == (max(r[1] for r in rows[:5]), min(r[2] for r in rows[:5]), rows[4][3])


def test_analyze_handles_missing_timeframes():
    b = bars(BEAR_PATH)
    a = rd.analyze({"1D": b})
    assert a["1W"] is None and a["1H"] is None and a["1D"]["type"] == "bear"
