"""bear-rank (single-stock bear scan, 2026-11-10) on the repo's own sector data.

Synthetic falling ETF bars make every industry regime eligible on the trend
side; the gamma side and every per-name rule use the stored captures.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sector_support_agent as ssa  # noqa: E402


def falling_bars(d, sector):
    for etf in ssa.needed_etfs(sector):
        with open(os.path.join(d, f"{etf}.json"), "w") as fh:
            json.dump([{"close_price": 100 - 0.5 * i} for i in range(30)], fh)


def main():
    d = tempfile.mkdtemp()
    for sector in ("XLK", "XLF"):
        falling_bars(d, sector)
        r = ssa.bear_rank(sector, d)
        assert "error" not in r, r
        assert r["side"] == "bear"
        for x in r["ranked"]:
            if x["ticker"] in ssa.NEVER_TRADE:
                assert not x["eligible"]
            if x["eligible"]:
                assert x["spot"] < x["call_wall"] and x["put_wall"] < x["spot"]
                assert x["room_from_resistance"] >= ssa.MIN_ROOM
                assert x["call_wall_strength"] not in ("moved", "new", "")
                assert x["band"][0] < x["call_wall"] < x["band"][1]
                assert (x["resistance"], x["target"]) == (x["call_wall"], x["put_wall"])
        w = r["watch"]
        eligible = [x for x in r["ranked"] if x["eligible"]]
        if eligible:
            near = [x for x in eligible if x["to_call_wall"] <= ssa.NEAR_WALL]
            assert w["ticker"] == (near or eligible)[0]["ticker"]
        else:
            assert w is None
        # no trend bars: no regime can be bearish, nothing is eligible
        assert not any(x["eligible"] for x in ssa.bear_rank(sector, None)["ranked"])
    print("ok")


if __name__ == "__main__":
    main()
