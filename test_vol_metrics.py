"""
Tests for vol_metrics.py and the latest.json vol enrichment.

    python -m pytest test_vol_metrics.py -q

Uses the stored 2026-10-16 chains in history/raw, so the expected values are
the ones the pipeline actually published.
"""

import json
import math
import os

import numpy as np
import pandas as pd
import pytest

import build_dashboard as bd
import vol_metrics as vm

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")
DATE = "2026-10-16"


def chain(symbol, date=DATE):
    path = os.path.join(HIST, "raw", date, f"{symbol}.parquet")
    if not os.path.exists(path):
        pytest.skip(f"{path} not in this checkout")
    df = pd.read_parquet(path)
    df = df[df["dte"] > 0].copy()
    df["T"] = df["dte"] / 365.0
    return df, float(df["spot"].iloc[0])


# ------------------------------------------------------------------
# Pure functions
# ------------------------------------------------------------------

def test_realized_vol_matches_hand_calculation():
    rng = np.random.default_rng(7)
    rets = rng.normal(0, 0.01, 20)
    closes = pd.Series(100 * np.exp(np.cumsum(np.r_[0, rets])))
    expected = np.std(rets, ddof=1) * math.sqrt(252)
    assert vm.realized_vol(closes) == pytest.approx(expected)


def test_realized_vol_needs_21_closes():
    assert vm.realized_vol(pd.Series(np.linspace(100, 110, 20))) is None


def test_constant_maturity_interpolates_total_variance():
    term = pd.DataFrame({"T": [10 / 365, 40 / 365], "atm_iv": [0.20, 0.10]})
    iv, extrapolated = vm.constant_maturity_iv(term, 30)
    v10, v40 = 0.20 ** 2 * 10 / 365, 0.10 ** 2 * 40 / 365
    var30 = v10 + (v40 - v10) * (20 / 30)
    assert iv == pytest.approx(math.sqrt(var30 / (30 / 365)))
    assert not extrapolated


def test_constant_maturity_flags_extrapolation():
    term = pd.DataFrame({"T": [10 / 365], "atm_iv": [0.2]})
    iv, extrapolated = vm.constant_maturity_iv(term, 30)
    assert iv == 0.2 and extrapolated


# ------------------------------------------------------------------
# Real chains
# ------------------------------------------------------------------

def test_spx_metrics_are_in_a_sane_range():
    df, spot = chain("SPX")
    out = vm.compute(df, spot, df[df["dte"] <= 30])
    assert 0.05 < out["atm_iv_30d"] < 0.6
    assert 0.05 < out["atm_iv_7d"] < 0.6
    assert out["skew_25d"] > 0                 # index puts bid over calls
    assert 0.5 < out["term_ratio"] < 1.5


def test_spx_atm_iv_agrees_with_intraday_capture():
    # gex_intraday.py measured 0.14065 at the close with its own method.
    df, spot = chain("SPX")
    out = vm.compute(df, spot, df[df["dte"] <= 30])
    assert out["atm_iv_30d"] == pytest.approx(0.1407, abs=0.003)


def test_vol_trigger_sits_above_the_flip_when_profile_has_positive_gamma():
    import gamma_exposure as gx
    for sym in ("SPX", "QQQ", "TLT", "GLD"):
        df, spot = chain(sym)
        c = df[df["dte"] <= 30]
        _, net, flip = gx.gamma_profile(c, spot * 0.85, spot * 1.15)
        trig = vm.vol_trigger(c, spot)
        assert trig is not None and flip is not None, sym
        assert trig > flip, sym


def test_vol_trigger_spx_value():
    df, spot = chain("SPX")
    assert vm.vol_trigger(df[df["dte"] <= 30], spot) == pytest.approx(7790.57, abs=0.5)


# ------------------------------------------------------------------
# latest.json enrichment
# ------------------------------------------------------------------

@pytest.fixture(scope="module")
def feed(tmp_path_factory):
    out = tmp_path_factory.mktemp("docs")
    bd.build_latest_json(HIST, str(out))
    return json.loads((out / "latest.json").read_text())


def test_existing_fields_are_untouched(feed):
    for sym, rec in feed.items():
        if sym in ("generated_utc", "market"):
            continue
        for key in ("spot", "flip", "call_wall", "put_wall", "regime",
                    "net_gex_full", "flip_pct_vs_spot"):
            assert key in rec, (sym, key)


def test_every_symbol_has_a_vol_block(feed):
    for sym, rec in feed.items():
        if sym in ("generated_utc", "market"):
            continue
        vol = rec["vol"]
        for key in ("atm_iv_7d", "atm_iv_30d", "term_ratio", "skew_25d",
                    "vol_trigger", "rv_20d", "iv_rv_ratio", "rv_source"):
            assert key in vol, (sym, key)


def test_rv_prefers_cboe_history_and_falls_back_to_captured_closes(feed):
    assert feed["SPX"]["vol"]["rv_source"] == "cboe_history:SPX"
    # No free Nasdaq-100 history at Cboe: QQQ uses this repo's own closes.
    assert feed["QQQ"]["vol"]["rv_source"] == "captured_closes"
    assert feed["QQQ"]["vol"]["rv_20d"] is not None


def test_recent_symbols_report_missing_rv_as_null(feed):
    # TLT was added 2026-10-08: not enough closes yet.
    assert feed["TLT"]["vol"]["rv_20d"] is None
    assert feed["TLT"]["vol"]["iv_rv_ratio"] is None


def test_market_vix_block(feed):
    vix = feed["market"]["vix"]
    assert vix["vix"] > 0
    assert "term_state" in vix
