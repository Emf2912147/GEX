"""latest.json's per-symbol levels block (build_dashboard.levels_block)."""
import datetime as dt

import build_dashboard as bd


def test_levels_block_shape():
    lvl = lambda k, side, g, wall="": dict(
        strike=k, side=side, dist=k / 100 - 1, sig=(k - 100) / 4, grade=g,
        label={"A": "strong", "B": "solid"}[g], strength="steady", wall=wall, score=71.3)
    L = dict(spot=100.0, iv=0.2, sigma=4.0, lo=96.0, hi=104.0,
             session=dt.date(2026, 11, 24), n_sessions=20,
             support=[lvl(98, "S", "A", "put_wall")], resistance=[lvl(103, "R", "B")])
    b = bd.levels_block(L)
    assert b["session"] == "2026-11-24" and b["band_lo"] == 96.0 and b["sessions_used"] == 20
    s = b["support"][0]
    assert s == {"strike": 98, "grade": "A", "label": "strong", "strength": "steady",
                 "wall": "put_wall", "score": 71.3, "dist_pct": -2.0, "sigma": -0.5}
    assert b["resistance"][0]["wall"] is None and b["resistance"][0]["dist_pct"] == 3.0
