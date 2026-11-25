#!/usr/bin/env python3
"""
Daily GEX history -- shared storage and bookkeeping.

The daily capture itself is schwab_daily.py (on the PC, next to the Schwab
login): it pulls each symbol's chain from Schwab, patches open interest from
tastytrade, and writes through the helpers here. This module owns the layout,
the metric columns, and the per-session dedupe and spot-anchoring rules, so
every writer produces identical rows.

Why raw chains and not just the metrics:
    Methodology changes. The wall definition in gamma_exposure.py changed twice
    on the day this was written. If only derived numbers were stored, every
    historical row would be wrong and unrecoverable. Raw chains can be
    recomputed under any future definition; derived metrics are a cache.

Settled open interest changes once a day (OCC settlement overnight), so one
capture per session is the clean observation; re-running intraday adds
nothing to positioning.

Layout:
    history/
      raw/<feed-date>/<SYMBOL>.parquet     (or .csv.gz if pyarrow is absent)
      daily_metrics.csv                    append-only, one row per symbol/day
      capture.log
"""

import json
import os
from datetime import datetime, timezone

import pandas as pd

import market_calendar as cal

# Bumped whenever the derived-metric definitions change, so historical rows
# stay interpretable. Raw chains are unaffected by this.
SCHEMA_VERSION = 3

# GLD and TLT added 2026-10-08; SMH and XLF 2026-10-15; XLK 2026-10-16
# (the Robinhood Trade Agent's index scan). When this list
# changes, change DAILY_SYMBOLS in watchdog.py to match
# -- it checks the daily capture is complete by counting rows, and neither
# can import this module (it pulls in matplotlib, which the watchdog's runner
# does not install).
DEFAULT_SYMBOLS = ["SPX", "SPY", "QQQ", "IWM", "XLK", "SMH", "XLF", "GLD", "TLT"]

# Cash indexes. Served while the market is closed, the feed's `spot` for these
# IS the official close -- index settlement is fixed overnight. SPX matched
# Robinhood's official close to the cent on all 21 sessions served with the
# market closed, and missed by 18.84 on the one served mid-session. So the
# feed is the anchor for these whenever market_calendar.is_market_open() says
# the market was closed at feed_ts.
INDEX_SYMBOLS = {"SPX"}

METRIC_COLUMNS = [
    "feed_ts", "feed_date", "session_date", "capture_ts", "symbol", "spot",
    "feed_spot", "spot_source",
    "contracts_total", "contracts_dte", "contracts_window",
    "net_gex_full", "net_gex_window", "flip", "flip_pct_vs_spot",
    "call_wall", "put_wall", "wall_fallback", "regime",
    "max_dte", "wall_exclude", "grid_window", "plot_window",
    "iv_rescaled", "schema_version",
]


def log(path, msg):
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {msg}"
    print(line)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# --------------------------------------------------------------------------

def write_raw(df, outdir, symbol):
    """Parquet when pyarrow is available, gzipped CSV otherwise."""
    os.makedirs(outdir, exist_ok=True)
    try:
        path = os.path.join(outdir, f"{symbol}.parquet")
        df.to_parquet(path, compression="zstd", index=False)
        return path
    except (ImportError, ValueError):
        path = os.path.join(outdir, f"{symbol}.csv.gz")
        df.to_csv(path, index=False, compression="gzip")
        return path


def session_already_captured(metrics_path, symbol, session_date):
    """True if this (symbol, SESSION) is already on file.

    Keyed on the trading session, never on feed_ts. A feed can re-stamp feed_ts
    on every serve -- including weekend re-serves of the same settled chain -- so
    a feed_ts key silently admits duplicates. Worse, a late re-serve pairs the
    PRIOR session's open interest with a LIVE spot: on 2026-09-21 the file
    carried Friday's OI against Monday's 766.78 SPY quote while Friday closed
    at 762.79. Gamma computed on that pairing is wrong, not merely redundant.
    """
    if not os.path.exists(metrics_path):
        return False
    try:
        prior = pd.read_csv(metrics_path, usecols=["symbol", "session_date"],
                            dtype=str)
    except Exception:
        return False
    hit = prior[(prior["symbol"] == symbol) &
                (prior["session_date"] == str(session_date))]
    return len(hit) > 0


def official_close(outdir, symbol, session_date):
    """The official close from history/official_closes.json, or None.

    Usually None at capture time -- the file is refreshed by hand -- but when
    it is present it is exact and beats everything else.
    """
    path = os.path.join(outdir, "official_closes.json")
    try:
        with open(path, encoding="utf-8") as fh:
            v = json.load(fh).get("closes", {}).get(symbol, {}).get(str(session_date))
        return float(v) if v else None
    except (OSError, ValueError):
        return None


def session_close(outdir, symbol, session_date):
    """The symbol's last traded spot during `session_date`, from intraday_state.

    WHY THIS EXISTS
        The daily file carries the prior session's SETTLED open interest, but
        the chain's `spot` field is whatever the quote feed says at the moment the
        file is served -- which for a 11:30 UTC run is a PRE-MARKET quote from
        the following morning. Session 2026-09-24, captured 2026-09-25 11:39Z:

            SPX  7704.1299  vs true close 7704.1299   0.00%
            SPY    770.66   vs true close   765.94   +0.62%
            QQQ    746.74   vs true close   739.28   +1.01%
            IWM    283.12   vs true close   281.08   +0.73%

        SPX matches to the cent because index settlement is fixed overnight.
        The ETFs do not, so every spot-dependent metric -- flip, both walls,
        net GEX (which scales with spot squared) -- was anchored up to 1% away
        from the book it was measuring. The error scales with the overnight
        gap, so it is largest exactly when the reading matters most.

        The intraday capture already records the real close. Use it.
    """
    path = os.path.join(outdir, "intraday_state.csv")
    if not os.path.exists(path):
        return None
    try:
        st = pd.read_csv(path, usecols=["capture_ts", "symbol", "spot"])
    except Exception:
        return None
    st = st[st["symbol"] == symbol]
    if st.empty:
        return None
    day = pd.to_datetime(st["capture_ts"], utc=True).dt.date.astype(str)
    st = st[day == str(session_date)]
    if st.empty:
        return None
    return float(st.sort_values("capture_ts")["spot"].iloc[-1])


def _served_while_open(feed_ts):
    """True if the chain was stamped while the regular session was trading.

    feed_ts is naive UTC, as everywhere else in this repo. An unparseable
    stamp counts as open -- the safe answer, since it only costs the index
    its feed anchor, never gives it a wrong one.
    """
    try:
        ts = pd.Timestamp(feed_ts)
    except (TypeError, ValueError):
        return True
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return cal.is_market_open(ts.to_pydatetime())


def append_metrics(metrics_path, rows):
    df = pd.DataFrame(rows, columns=METRIC_COLUMNS)
    header = not os.path.exists(metrics_path)
    df.to_csv(metrics_path, mode="a", header=header, index=False)


# --------------------------------------------------------------------------
