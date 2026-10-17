"""
Tests for dealer_flows.py (vanna / charm) and the latest.json "flows" block.

    python -m pytest test_dealer_flows.py -q
"""

import json
import math
import os

import numpy as np
import pandas as pd
import pytest

import build_dashboard as bd
import dealer_flows as dfl

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")


def _bs_delta(S, K, T, iv, is_call, q):
    d1 = (math.log(S / K) + (dfl.RATE - q + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    n = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    return math.exp(-q * T) * (n if is_call else n - 1)


@pytest.mark.parametrize("is_call", [True, False])
def test_greeks_match_finite_differences(is_call):
    S, K, T, iv, q, h = 100.0, 95.0, 20 / 365, 0.2, 0.01, 1e-5
    fd_vanna = (_bs_delta(S, K, T, iv + h, is_call, q) - _bs_delta(S, K, T, iv - h, is_call, q)) / (2 * h)
    fd_charm = -(_bs_delta(S, K, T + h, iv, is_call, q) - _bs_delta(S, K, T - h, iv, is_call, q)) / (2 * h)
    assert float(dfl.vanna(S, K, T, iv, q)) == pytest.approx(fd_vanna, rel=1e-5)
    assert float(dfl.charm(S, K, T, iv, np.array(is_call), q)) == pytest.approx(fd_charm, rel=1e-5)


def test_short_otm_put_decay_is_dealer_buying():
    # One OTM put that dealers are short: time decay shrinks its delta,
    # so dealers buy back hedges (positive charm flow), and a vol drop
    # does the same (positive vanna flow).
    raw = pd.DataFrame({"spot": [100.0], "strike": [95.0], "type": ["P"],
                        "dte": [10.0], "iv": [0.2], "oi": [1000.0]})
    f = dfl.flows(raw, "TEST")
    assert f["charm_bp_day"] > 0
    assert f["vanna_bp_per_volpt"] > 0


def test_expiring_and_far_contracts_are_excluded():
    raw = pd.DataFrame({"spot": [100.0, 100.0], "strike": [95.0, 95.0], "type": ["P", "P"],
                        "dte": [0.2, 60.0], "iv": [0.2, 0.2], "oi": [1000.0, 1000.0]})
    assert dfl.flows(raw, "TEST") is None


def _chain(symbol, date="2026-10-16"):
    path = os.path.join(HIST, "raw", date, f"{symbol}.parquet")
    if not os.path.exists(path):
        pytest.skip(f"{path} not in this checkout")
    return pd.read_parquet(path)


def test_qqq_post_expiration_rolloff_on_2026_10_16():
    block = dfl.flows_block(HIST, _chain("QQQ"), "QQQ", "2026-10-16")
    assert block["history_sessions"] == 20
    assert block["charm_support_ratio"] == pytest.approx(0.425, abs=0.01)


def test_short_history_gives_no_ratio():
    block = dfl.flows_block(HIST, _chain("TLT"), "TLT", "2026-10-16")
    assert block["history_sessions"] < dfl.MIN_HISTORY
    assert block["charm_support_ratio"] is None
    assert block["charm_bp_day"] > 0


def test_feed_carries_flows_block(tmp_path):
    bd.build_latest_json(HIST, str(tmp_path))
    feed = json.loads((tmp_path / "latest.json").read_text())
    for sym, rec in feed.items():
        if sym in ("generated_utc", "market"):
            continue
        assert "flows" in rec, sym
        assert rec["flows"]["convention"].startswith("dealers long calls")
