#!/usr/bin/env python3
"""
Daily GEX history capture.

Snapshots the full Cboe option chain for several symbols, stores the RAW
contracts, and appends a row of derived metrics per symbol per day.

    python gex_capture.py                      # SPX SPY QQQ IWM
    python gex_capture.py --symbols SPX SPY
    python gex_capture.py --dry-run            # fetch + compute, write nothing

Why raw chains and not just the metrics:
    Methodology changes. The wall definition in gamma_exposure.py changed twice
    on the day this was written. If only derived numbers were stored, every
    historical row would be wrong and unrecoverable. Raw chains can be
    recomputed under any future definition; derived metrics are a cache.

Run it once each morning, AFTER the overnight OCC settlement lands in the Cboe
file (observed around 03:40 ET). A 7:00am ET run captures the prior session's
settled open interest, which is the clean daily observation. Re-running
intraday adds nothing to positioning -- OI does not change until the next
settlement -- so this deliberately captures once per feed timestamp and skips
duplicates.

Layout:
    history/
      raw/<feed-date>/<SYMBOL>.parquet     (or .csv.gz if pyarrow is absent)
      daily_metrics.csv                    append-only, one row per symbol/day
      capture.log
"""

import argparse
import gzip
import io
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd

import market_calendar as cal

import gamma_exposure as gx

# Bumped whenever the derived-metric definitions change, so historical rows
# stay interpretable. Raw chains are unaffected by this.
SCHEMA_VERSION = 3

# GLD and TLT added 2026-10-08. When this list changes, change DAILY_SYMBOLS in
# watchdog.py and session_runner.py to match -- both check the daily capture
# is complete by counting rows, and neither can import this module (it pulls
# in matplotlib, which the watchdog's runner does not install).
DEFAULT_SYMBOLS = ["SPX", "SPY", "QQQ", "IWM", "GLD", "TLT"]

# Cash indexes. Served while the market is closed, Cboe's `spot` for these
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

    Keyed on the trading session, never on feed_ts. Cboe re-stamps feed_ts on
    every serve -- including weekend re-serves of the same settled chain -- so
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
        Cboe's `spot` field is whatever the quote feed says at the moment the
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
    """True if Cboe stamped this file while the regular session was trading.

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


def capture_symbol(symbol, args, logpath):
    """Fetch, store raw, compute metrics. Returns a metrics dict or None."""
    payload = gx.fetch_chain(symbol)
    df, spot, feed_ts = gx.parse_chain(payload)

    iv_median_before = float(df.loc[df["iv"] > 0, "iv"].median()) if (df["iv"] > 0).any() else 0.0
    df = gx.normalize_iv(df, quiet=True)
    iv_rescaled = iv_median_before > 3.0

    now = datetime.now(timezone.utc)
    session_date = cal.session_for(now.date())

    metrics_path = os.path.join(args.outdir, "daily_metrics.csv")
    if session_already_captured(metrics_path, symbol, session_date) and not args.force:
        log(logpath, f"{symbol}: session {session_date} already captured, skipping")
        return None

    capture_ts = now.isoformat(timespec="seconds")
    # feed_date is the date stamped on the Cboe file. A morning run's file
    # reflects the PRIOR session's settled open interest -- it is not the
    # trading session date. Kept verbatim rather than guessed at.
    feed_date = str(feed_ts)[:10]

    # feed_date is the date Cboe stamped on the FILE. session_date is the
    # trading session that file describes -- always the prior calendar day,
    # because the calendar gate in main() only lets this run when that day
    # was a session. Everything downstream should key on session_date.
    session_date = str(session_date)

    # Anchor on the best close available, in this order:
    #   1. official_closes.json        exact
    #   2. the feed, for SPX            exact -- if served with the market
    #                                   closed (index settlement is fixed)
    #   3. last intraday capture        ETFs only; good to ~0.07% when the
    #                                   session was fully captured
    #   4. the feed, for ETFs           a pre-market quote; up to 1.09% off
    # Step 2 was missing until 2026-10-08: SPX fell through to step 3, so on
    # 10/05 and 10/07, when the session's last capture was mid-afternoon, an
    # exact feed spot was overwritten with a stale one (7777.04 for a 7773.95
    # close, 7804.85 for 7801.77).
    feed_spot = spot
    close, spot_source = official_close(args.outdir, symbol, session_date), "official_close"
    if close is None and symbol in INDEX_SYMBOLS and not _served_while_open(feed_ts):
        close, spot_source = feed_spot, "index_close"
    if close is None:
        close, spot_source = session_close(args.outdir, symbol, session_date), "session_close"
    if close is not None and close > 0:
        if abs(close / feed_spot - 1) > 0.0005:
            log(logpath, f"{symbol}: spot {feed_spot:.4f} from the feed is "
                         f"{(feed_spot / close - 1) * 100:+.2f}% off the "
                         f"{session_date} close {close:.4f} ({spot_source}) -- using the close")
        spot = close
    else:
        spot_source = "feed"
        log(logpath, f"{symbol}: no close on file for {session_date}, "
                     f"anchoring on the feed spot {feed_spot:.4f}")

    raw = df.copy()
    raw.insert(0, "symbol", symbol)
    raw.insert(1, "feed_ts", str(feed_ts))
    raw.insert(2, "capture_ts", capture_ts)
    raw.insert(3, "spot", spot)
    raw["expiry"] = raw["expiry"].dt.tz_convert("UTC").dt.tz_localize(None)
    raw = raw.drop(columns=["T"], errors="ignore")

    if not args.dry_run:
        path = write_raw(raw, os.path.join(args.outdir, "raw", session_date), symbol)
        log(logpath, f"{symbol}: {len(raw):,} contracts -> {os.path.basename(path)}")
    else:
        log(logpath, f"{symbol}: {len(raw):,} contracts (dry run, not written)")

    # ---- derived metrics, at the documented default parameters -------------
    chain = df[df["dte"] <= args.max_dte].copy()
    if chain.empty:
        log(logpath, f"{symbol}: no contracts within {args.max_dte} DTE, metrics skipped")
        return None

    glo, ghi = spot * (1 - args.grid_window), spot * (1 + args.grid_window)
    _, _, flip = gx.gamma_profile(chain, glo, ghi)

    plo, phi = spot * (1 - args.plot_window), spot * (1 + args.plot_window)
    windowed = chain[(chain["strike"] >= plo) & (chain["strike"] <= phi)]

    full_ps, _, _ = gx.gex_by_strike(chain, spot)
    _, calls, puts = gx.gex_by_strike(windowed, spot)
    call_wall, put_wall, fell_back = gx.find_walls(calls, puts, spot, args.wall_exclude)

    total_full = float(full_ps.sum())
    win_ps, _, _ = gx.gex_by_strike(windowed, spot)

    return {
        "feed_ts": str(feed_ts),
        "feed_date": feed_date,
        "session_date": session_date,
        "capture_ts": capture_ts,
        "symbol": symbol,
        "spot": round(spot, 4),
        "feed_spot": round(feed_spot, 4),
        "spot_source": spot_source,
        "contracts_total": len(df),
        "contracts_dte": len(chain),
        "contracts_window": len(windowed),
        "net_gex_full": round(total_full, 2),
        "net_gex_window": round(float(win_ps.sum()), 2),
        "flip": round(flip, 4) if flip is not None else "",
        "flip_pct_vs_spot": round(flip / spot - 1, 6) if flip is not None else "",
        "call_wall": call_wall if call_wall is not None else "",
        "put_wall": put_wall if put_wall is not None else "",
        "wall_fallback": int(fell_back),
        "regime": "positive" if total_full > 0 else "negative",
        "max_dte": args.max_dte,
        "wall_exclude": args.wall_exclude,
        "grid_window": args.grid_window,
        "plot_window": args.plot_window,
        "iv_rescaled": int(iv_rescaled),
        "schema_version": SCHEMA_VERSION,
    }


def append_metrics(metrics_path, rows):
    df = pd.DataFrame(rows, columns=METRIC_COLUMNS)
    header = not os.path.exists(metrics_path)
    df.to_csv(metrics_path, mode="a", header=header, index=False)


# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Capture daily GEX history.")
    p.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    p.add_argument("--outdir", default=None,
                   help="history directory (default: ./history next to this script)")
    p.add_argument("--max-dte", type=float, default=30,
                   help="DTE filter for the DERIVED metrics only. Raw chains "
                        "are always stored in full. (default 30)")
    p.add_argument("--wall-exclude", type=float, default=0.004)
    p.add_argument("--grid-window", type=float, default=0.15)
    p.add_argument("--plot-window", type=float, default=0.10)
    p.add_argument("--force", action="store_true",
                   help="capture even if this feed timestamp is already on file")
    p.add_argument("--ignore-calendar", action="store_true",
                   help="run even on a day the trading calendar would skip")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch and compute, write nothing")
    args = p.parse_args()

    if args.outdir is None:
        args.outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "history")
    os.makedirs(args.outdir, exist_ok=True)
    logpath = os.path.join(args.outdir, "capture.log")

    today = datetime.now(timezone.utc).date()
    if not cal.should_capture(today) and not args.ignore_calendar:
        log(logpath, f"=== capture skipped  {today:%Y-%m-%d %a}: "
                     f"{today - timedelta(days=1)} was not a trading session")
        return 0

    log(logpath, f"=== capture start  symbols={' '.join(args.symbols)}"
                 f"{'  (dry run)' if args.dry_run else ''}")

    rows, failures = [], []
    for sym in args.symbols:
        sym = sym.upper()
        try:
            row = capture_symbol(sym, args, logpath)
            if row:
                rows.append(row)
                log(logpath,
                    f"{sym}: spot {row['spot']:,.2f}  netGEX {row['net_gex_full']/1e9:,.2f}Bn  "
                    f"flip {row['flip']}  walls {row['call_wall']}/{row['put_wall']}  "
                    f"{row['regime']}")
        except SystemExit as e:
            # gamma_exposure exits on bad data; in a batch job that must not
            # take down the remaining symbols.
            failures.append(f"{sym}: {e}")
            log(logpath, f"{sym}: FAILED -- {e}")
        except Exception as e:
            failures.append(f"{sym}: {e}")
            log(logpath, f"{sym}: FAILED -- {e}\n{traceback.format_exc()}")

    if rows and not args.dry_run:
        append_metrics(os.path.join(args.outdir, "daily_metrics.csv"), rows)
        log(logpath, f"appended {len(rows)} metric row(s)")

    log(logpath, f"=== capture done  ok={len(rows)}  failed={len(failures)}")
    # Non-zero exit so Task Scheduler surfaces a bad run in its history.
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
