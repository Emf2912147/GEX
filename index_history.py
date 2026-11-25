#!/usr/bin/env python3
"""
index_history.py

Daily closes and current levels for the cash indices this repo reads --
VIX, VIX9D, VIX3M, VIX6M, VIX1Y, SPX, RUT, XSP, VXN.

SOURCE
    Yahoo's chart API, which carries every series above and needs no key.
    Availability was probed on 2026-11-22 before any of this was written:

        VIX 1257 bars   VIX9D 1255   VIX3M 1255   VIX6M 1255
        SPX 1255        RUT 1255     XSP 1255     VXN 1255
        VIX1Y ............ 1 bar   <- current level only, no history

    That probe also agreed with this repo's own record: ^GSPC's last close
    came back 6910.39, matching official_closes.json for 2026-11-20 to the
    cent.

VIX1Y IS THE EXCEPTION, AND IT STAYS ONE
    Yahoo serves VIX1Y's current level but no usable history, so its
    trailing-year and five-year percentiles cannot be computed. callers ask
    for a minimum bar count and get nothing rather than a percentile derived
    from a handful of points: that block feeds tranche timing, and a confident
    CHEAP/RICH label computed off one observation is worse than a blank one.

This module only reads public index levels. No key, no account, nothing that
needs hiding -- which is why it is committed, unlike the broker scripts.
"""

from __future__ import annotations

from datetime import datetime, timezone

import requests

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
HEADERS = {"User-Agent": "Mozilla/5.0 (GEX index history)"}
TIMEOUT_S = 20

# Yahoo's spelling for each index this repo asks about.
YAHOO = {
    "VIX": "^VIX", "VIX9D": "^VIX9D", "VIX3M": "^VIX3M",
    "VIX6M": "^VIX6M", "VIX1Y": "^VIX1Y", "VXN": "^VXN",
    "SPX": "^GSPC", "RUT": "^RUT", "XSP": "^XSP",
}

# Enough history to place a value in its own distribution. Below this a
# percentile is arithmetic, not information.
MIN_PERCENTILE_BARS = 60

def _chart(symbol: str, rng: str) -> dict | None:
    ysym = YAHOO.get(symbol.upper())
    if not ysym:
        return None
    r = requests.get(CHART_URL.format(sym=ysym), headers=HEADERS,
                     params={"range": rng, "interval": "1d"}, timeout=TIMEOUT_S)
    r.raise_for_status()
    res = ((r.json().get("chart") or {}).get("result") or [])
    return res[0] if res else None


def daily_closes(symbol: str, rng: str = "5y", min_bars: int = 2):
    """(dates, closes) oldest first, ISO date strings. (None, None) if short.

    The shape gex_intraday.fetch_history_closes returns. min_bars is the caller's honesty gate: ask
    for what your statistic actually needs.
    """
    q = _chart(symbol, rng)
    if not q:
        return None, None
    ts = q.get("timestamp") or []
    raw = ((q.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    dates, closes = [], []
    for t, c in zip(ts, raw):
        if c is None or not (c > 0):
            continue
        dates.append(datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"))
        closes.append(float(c))
    if len(closes) < min_bars:
        return None, None
    return dates, closes


def quote(symbol: str) -> dict:
    """{current_price, prev_day_close, last_trade_time} -- the field names
    gex_intraday.capture_vix reads.

    prev_day_close IS DERIVED FROM THE BARS, NOT FROM meta.chartPreviousClose.
    That meta field is the close preceding the REQUESTED RANGE, not the
    previous session: asked over 5 days it returns a close from about six
    sessions back. Caught 2026-11-22 against this repo's own record -- it gave
    SPX 7561.67 when the stored closes run 7416.59 / 7340.53 / 7323.63 /
    6910.39. capture_vix divides by this to get vix_chg_1d_pct, so that would
    have written week-long moves labelled as one-day moves straight into the
    term-structure state.

    The previous close is the SECOND-TO-LAST bar, in every case. current_price
    always belongs to the last bar's session -- today's still-forming one
    while the market is open, the last settled one when it is closed -- so the
    session before it is always bars[-2].

    The first attempt at this took "the last bar dated before today", which
    looks equivalent and is not: on a non-trading day the last bar IS the
    previous session, so current_price and prev_day_close came back identical
    (VIX 35.89 and 35.89) and every daily change would have been exactly zero.
    Caught on a Sunday, which is the only reason it was visible at all.
    """
    q = _chart(symbol, "1mo")
    meta = (q or {}).get("meta") or {}
    ts = (q or {}).get("timestamp") or []
    raw = (((q or {}).get("indicators") or {}).get("quote") or [{}])[0].get("close") or []

    bars = [(datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%d"), float(c))
            for s, c in zip(ts, raw) if c is not None and c > 0]

    prev = bars[-2][1] if len(bars) > 1 else None

    price = meta.get("regularMarketPrice")
    if price is None and bars:
        price = bars[-1][1]
    t = meta.get("regularMarketTime")
    return {
        "current_price": float(price) if price is not None else None,
        "prev_day_close": float(prev) if prev is not None else None,
        "last_trade_time": (datetime.fromtimestamp(t, timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%S") if t else None),
    }


def closes_and_asof(symbol: str, min_bars: int = MIN_PERCENTILE_BARS):
    """(closes, last_date) -- the shape vol_metrics._fetch_closes returns."""
    dates, closes = daily_closes(symbol, "5y", min_bars=min_bars)
    if not closes:
        return None, None
    return closes, dates[-1]


if __name__ == "__main__":
    for s in YAHOO:
        try:
            d, c = daily_closes(s)
            qq = quote(s)
            n = len(c) if c else 0
            tail = ", ".join(f"{d}={v:.2f}" for d, v in zip(d[-2:], c[-2:])) if c else "-"
            print(f"{s:<6} bars {n:>5}  quote {qq['current_price']}  "
                  f"prev {qq['prev_day_close']}  | last bars: {tail}")
        except Exception as e:
            print(f"{s:<6} FAILED {type(e).__name__}: {e}")
