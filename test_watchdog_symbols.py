"""watchdog.symbol_gaps: per-symbol coverage of today's intraday cycles."""

import pandas as pd

from watchdog import symbol_gaps


def rows(counts, cycles=12):
    out = []
    for sym, n in counts.items():
        for i in range(n):
            out.append({"symbol": sym, "capture_ts": f"2026-10-20T{13 + i // 4:02d}:{(i % 4) * 15:02d}:00+00:00"})
    return pd.DataFrame(out)


def test_symbol_in_half_the_cycles_is_flagged():
    gaps = symbol_gaps(rows({"SPX": 12, "XLK": 6}), 12)
    assert len(gaps) == 1 and gaps[0].startswith("XLK captured in only 6 of 12")


def test_full_coverage_is_quiet():
    assert symbol_gaps(rows({"SPX": 12, "XLK": 11}), 12) == []


def test_symbol_missing_all_day_is_flagged_when_expected():
    gaps = symbol_gaps(rows({"SPX": 12}), 12, expected={"SPX", "TLT"})
    assert gaps == ["TLT captured in only 0 of 12 cycles today (Cboe serving its chain late or empty -- see history/intraday.log)"]


def test_too_few_cycles_to_judge():
    assert symbol_gaps(rows({"SPX": 4, "XLK": 1}), 4) == []
