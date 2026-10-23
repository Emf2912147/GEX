"""
Tests for setup_quality.py -- the trade-quality vetoes and score.

    python -m pytest test_setup_quality.py -q

Synthetic bars only, so the expected outcomes are exact.
"""

import math

import setup_quality as sq


def bars(closes, start=0, spread=0.5):
    out = []
    for i, c in enumerate(closes):
        out.append({"begins_at": f"2024-01-01T{(start + i) % 24:02d}:00:00Z",
                    "open_price": c, "high_price": c + spread,
                    "low_price": c - spread, "close_price": c})
    return out


def wave(n, base=100.0, amp=8.0, period=40, drift=0.0):
    return [base + drift * i + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


def test_rsi_levels_are_per_symbol():
    # a slow trender runs to RSI extremes; a choppy name never gets there
    trender = sq.calibrate_rsi(wave(800, amp=10, period=80))
    chopper = sq.calibrate_rsi(wave(800, amp=10, period=8))
    assert trender["rsi_calibrated"] and chopper["rsi_calibrated"]
    assert trender["overbought"] > chopper["overbought"]
    assert trender["oversold"] < chopper["oversold"]


def test_short_history_falls_back_to_30_70():
    c = sq.calibrate_rsi(wave(100))
    assert not c["rsi_calibrated"]
    assert (c["oversold"], c["overbought"]) == (30.0, 70.0)


def test_veto_a_no_bear_when_oversold():
    closes = wave(760) + [100 - 2 * i for i in range(1, 16)]      # hard selloff into the end
    q = sq.quality("bear", "T", bars(closes), bars(wave(200)), 85, 60)
    assert not q["tradeable"]
    assert any(v.startswith("VETO A") for v in q["vetoes"])


def test_veto_b_overextended_without_divergence():
    closes = wave(760) + [100 + 2 * i for i in range(1, 16)]      # hard rally into the end
    rising = [100 + 0.3 * i for i in range(200)]                  # hourly: no lower RSI high
    q = sq.quality("bear", "T", bars(closes), bars(rising), 135, 100)
    assert q["overextended"] and q["divergence"] is None
    assert any(v.startswith("VETO B") for v in q["vetoes"])
    assert q["points"]["extension_divergence"] == 0


def test_bearish_divergence_detected():
    # strong push to a high, pullback, then a slow grind to a marginally higher high
    up1 = [100 + 2.0 * i for i in range(20)]
    down = [138 - 1.5 * i for i in range(10)]
    up2 = [123 + 0.9 * i for i in range(18)]
    tail = [up2[-1] - 0.8 * i for i in range(1, 6)]
    d = sq.divergence(bars(up1 + down + up2 + tail, spread=0.1), "bear")
    assert d is not None
    assert d["price"][1] >= d["price"][0] and d["rsi"][1] < d["rsi"][0]


def test_bull_is_the_mirror():
    closes = wave(760) + [100 + 2 * i for i in range(1, 16)]
    q = sq.quality("bull", "T", bars(closes), bars(wave(200)), 120, 150)
    assert any(v.startswith("VETO A") and "overbought" in v for v in q["vetoes"])


def test_room_needs_two_atr():
    closes = wave(760)
    q = sq.quality("bear", "T", bars(closes), bars(wave(200)), 100, 99.9)
    assert q["points"]["room"] == 0
    q = sq.quality("bear", "T", bars(closes), bars(wave(200)), 100, 50)
    assert q["points"]["room"] == sq.QUALITY_WEIGHTS["room"]


def test_weights_sum_to_100():
    assert sum(sq.QUALITY_WEIGHTS.values()) == 100
