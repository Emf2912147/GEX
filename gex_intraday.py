#!/usr/bin/env python3
"""
Intraday GEX / flow capture.

Companion to gex_capture.py, NOT a replacement. The two capture different
things because the feed carries different information on different clocks:

    gex_capture.py   once daily, after OCC settlement lands
                     -> POSITIONING. Settled open interest, full chain, raw.

    gex_intraday.py  every ~15 min during RTH
                     -> FLOW. Volume accumulating through the session, plus
                        the state vector (flip, walls, skew, ATM IV, term).

Open interest does not move until the next settlement, so re-capturing full
chains intraday adds nothing to positioning -- that is the reasoning already
in gex_capture.py's docstring, and it is why this script stores a SLIM,
WINDOWED snapshot rather than another copy of the chain.

Three things this gets right that a naive intraday run of gex_capture.py
does not:

 1. Snapshots do not overwrite each other. gex_capture.py writes to
    history/raw/<feed-date>/<SYM>.parquet -- a path derived from the DATE
    only -- so running it every 15 minutes leaves exactly one surviving
    snapshot per day and makes volume deltas impossible to reconstruct.
    This writes one file per feed timestamp.

 2. Unchanged bars are skipped, on TWO tests. The Cboe delayed feed does not
    refresh on our cadence. Capturing it again would create a phantom bar
    with zero volume delta and a real elapsed time, which reads as "flow
    stopped" when nothing happened at all. The obvious guard is the feed
    timestamp -- but the feed also republishes itself with a FRESH timestamp
    and identical content, so the timestamp guard alone is not enough. Spot
    and net GEX are compared against the last row as well; see same_values().

 3. Elapsed time is recorded per snapshot. GitHub Actions cron drifts and
    occasionally skips runs entirely, so bars are NOT evenly spaced. Any
    flow measure built on this must normalise by elapsed minutes rather
    than assume a 15-minute bar.

VIX CAPTURE
    Same 15-minute cycle also pulls VIX and VIX9D -- Cboe's own quotes
    endpoint, not the options-chain one, so it needs its own parser and its
    own file (history/vix_state.csv). Verified live 2026-09-10: VIX closed
    17.84 (+8.4% on the day, +21.9% over the trailing 20 sessions -- a fresh
    20-day high made that same day); VIX9D closed 17.70 (+13.5%).

    True VX futures term structure (front-two-month slope) is NOT available
    this way -- the same endpoint pattern for VX itself returns a flat 403.
    VIX9D vs VIX30 is used as the term-structure proxy instead: it answers
    the same shape question (short-dated vol richer or cheaper than 30-day)
    without needing futures settlements. It is a substitute, not the real
    thing, and is labelled as such in the output.

    The 20d change is computed fresh each run from Cboe's full VIX_History.csv
    (1990-present, ~470KB) rather than stored -- keeping 36 years of daily
    closes in a git repo just to read the last 20 rows would be exactly the
    kind of unbounded growth gex_intraday.py's slim-snapshot design exists to
    avoid. Only the derived scalar is written to disk.

Layout:
    history/
      intraday/<feed-date>/<SYMBOL>__<HHMMSS>.parquet   slim snapshots
      intraday_state.csv                                append-only state vector
      vix_state.csv                                     append-only VIX/VIX9D
      intraday.log

Usage:
    python gex_intraday.py                    # SPX SPY QQQ IWM + VIX/VIX9D
    python gex_intraday.py --symbols SPX QQQ
    python gex_intraday.py --no-vix            # skip the VIX capture
    python gex_intraday.py --dry-run
"""

import argparse
import os
import re
import sys
import traceback
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

import gamma_exposure as gx

# Bump when the slim schema or the state-vector definitions change.
SCHEMA_VERSION = 1

DEFAULT_SYMBOLS = ["SPX", "SPY", "QQQ", "IWM"]

# OCC symbol: root + YYMMDD + C/P + strike * 1000, zero padded to 8.
OPT_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")

# Slim snapshot schema. Deliberately keeps bid/ask, which parse_chain in
# gamma_exposure.py drops -- without a quote at snapshot time there is no
# way to ever improve on a pure volume proxy for trade side.
SNAPSHOT_COLUMNS = [
    "symbol", "feed_ts", "capture_ts", "elapsed_s", "spot",
    "expiry", "strike", "type", "volume", "oi",
    "bid", "ask", "iv", "delta", "gamma",
]

STATE_COLUMNS = [
    "feed_ts", "feed_date", "capture_ts", "elapsed_s", "symbol", "spot",
    "contracts_kept", "net_gex_window", "flip", "flip_pct_vs_spot",
    "call_wall", "put_wall", "regime",
    "atm_iv_30", "atm_iv_60", "term_slope", "rr25", "skew_state",
    "session_call_volume", "session_put_volume", "session_pc_volume",
    "dte_window", "strike_window", "schema_version",
]

# ---- VIX capture ----------------------------------------------------------
# Plain index quotes, not the options-chain endpoint -- underscore-prefixed
# symbol convention (_VIX, _VIX9D). History CSVs are read fresh each run and
# never stored; see the VIX CAPTURE docstring section above for why.
VIX_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{sym}.json"
VIX_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"

VIX_COLUMNS = [
    "feed_ts", "feed_date", "capture_ts", "elapsed_s",
    "vix", "vix_chg_1d_pct", "vix_20d_change_pct", "vix_20d_high",
    "vix9d", "vix9d_chg_1d_pct",
    "term_spread_9d_vs_30d", "term_state",
    "schema_version",
]


def log(path, msg):
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {msg}"
    print(line)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# --------------------------------------------------------------------------
# parsing


def parse_chain_full(payload):
    """Like gx.parse_chain but keeps bid/ask.

    Returns (df, spot, feed_ts). Raw quotes are preserved because the flow
    methodology is expected to change -- the repo's own principle is that
    derived numbers are a cache and raw inputs are not recoverable later.
    """
    data = payload.get("data", {})
    spot = data.get("current_price") or data.get("close")
    if not spot:
        sys.exit("No spot price in the Cboe response.")

    feed_ts = payload.get("timestamp", "unknown")
    rows = []
    for o in data.get("options", []):
        m = OPT_RE.match(o.get("option", "") or "")
        if not m:
            continue
        ymd = m.group("ymd")
        try:
            expiry = datetime(
                2000 + int(ymd[0:2]), int(ymd[2:4]), int(ymd[4:6]),
                20, 0, tzinfo=timezone.utc,          # 16:00 ET
            )
        except ValueError:
            continue
        rows.append({
            "expiry": expiry,
            "strike": int(m.group("strike")) / 1000.0,
            "type": m.group("cp"),
            "oi": o.get("open_interest") or 0,
            "volume": o.get("volume") or 0,
            "bid": o.get("bid") or 0.0,
            "ask": o.get("ask") or 0.0,
            "iv": o.get("iv") or 0.0,
            "gamma": o.get("gamma") or 0.0,
            "delta": o.get("delta") or 0.0,
        })

    if not rows:
        sys.exit("Parsed zero contracts. Cboe may have changed their schema.")

    df = pd.DataFrame(rows)
    now = datetime.now(timezone.utc)
    df["dte"] = (df["expiry"] - now).dt.total_seconds() / 86400.0
    df = df[df["dte"] > 0].copy()
    if df.empty:
        sys.exit("Every contract in the feed has already expired.")
    df["T"] = df["dte"] / 365.0
    return df, float(spot), feed_ts


# --------------------------------------------------------------------------
# dedupe / bar spacing


def last_snapshot(state_path, symbol):
    """The most recent row already on file for this symbol, or None.

    Returns feed_ts, capture_ts, spot and net_gex_window, because deduping on
    the timestamp alone is not enough -- see the content check in
    capture_symbol().
    """
    empty = {"feed_ts": None, "capture_ts": None, "spot": None, "net_gex": None}
    if not os.path.exists(state_path):
        return empty
    try:
        prior = pd.read_csv(
            state_path,
            usecols=["symbol", "feed_ts", "capture_ts", "spot", "net_gex_window"],
            dtype=str,
        )
    except Exception:
        return empty
    mine = prior[prior["symbol"] == symbol]
    if mine.empty:
        return empty
    row = mine.iloc[-1]
    return {
        "feed_ts": row["feed_ts"],
        "capture_ts": row["capture_ts"],
        "spot": row["spot"],
        "net_gex": row["net_gex_window"],
    }


def same_values(prev, spot, net_gex):
    """True when the feed has republished byte-identical numbers.

    Observed 2026-09-09: runs four minutes apart on a closed market returned
    DIFFERENT feed timestamps with IDENTICAL spot and net GEX. A timestamp-only
    guard lets those through and manufactures a bar with real elapsed time and
    zero flow -- the same phantom-bar failure the timestamp guard exists to
    prevent, arriving by a different door.

    Both fields must match. Spot alone would suppress a genuine repositioning
    at an unchanged print; net GEX alone would suppress a move that happens to
    leave the aggregate flat.
    """
    prior_spot, prior_gex = prev.get("spot"), prev.get("net_gex")
    if prior_spot in (None, "") or prior_gex in (None, ""):
        return False
    try:
        return (abs(float(prior_spot) - round(spot, 4)) < 1e-9 and
                abs(float(prior_gex) - round(net_gex, 2)) < 1e-6)
    except (TypeError, ValueError):
        return False


def elapsed_seconds(prev_capture_ts, now):
    if not prev_capture_ts:
        return ""
    try:
        prev = datetime.fromisoformat(prev_capture_ts)
    except ValueError:
        return ""
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=timezone.utc)
    return int((now - prev).total_seconds())


# --------------------------------------------------------------------------
# VIX / VIX9D


def fetch_vix_quote(symbol):
    """Cboe's plain index-quote endpoint -- not the options-chain one.

    Verified live 2026-09-10 (_VIX9D): current_price 17.70, prev_day_close
    15.59, last_trade_time "2026-09-10T16:15:02". Same field set for _VIX.
    """
    url = VIX_QUOTE_URL.format(sym=symbol)
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    payload = r.json()
    return payload.get("data", payload)


def fetch_20d_change(hist_url, today_price):
    """20-session change and fresh-20d-high flag, from the full history CSV.

    closes[-20] is the close from 20 sessions ago relative to today (the
    history file is settled EOD data and does not yet contain today's
    still-forming session). Returns (None, None) rather than guessing if the
    column layout does not match what was verified live -- a silently wrong
    number is worse than a blank one.
    """
    r = requests.get(hist_url, timeout=30)
    r.raise_for_status()
    from io import StringIO
    hist = pd.read_csv(StringIO(r.text))
    close_col = next((c for c in hist.columns if c.strip().upper() == "CLOSE"), None)
    if close_col is None:
        return None, None
    closes = hist[close_col].astype(float).tolist()
    if len(closes) < 20:
        return None, None
    look = closes[-20:]
    baseline = closes[-20]
    if not baseline:
        return None, None
    chg_20d = today_price / baseline - 1
    is_20d_high = today_price > max(look)
    return chg_20d, is_20d_high


def last_vix_row(state_path):
    """Mirrors last_snapshot() -- the most recent VIX/VIX9D row on file, or
    None-filled, so the same two-test dedupe (timestamp + content) applies."""
    empty = {"feed_ts": None, "capture_ts": None, "vix": None, "vix9d": None}
    if not os.path.exists(state_path):
        return empty
    try:
        prior = pd.read_csv(
            state_path, usecols=["feed_ts", "capture_ts", "vix", "vix9d"], dtype=str,
        )
    except Exception:
        return empty
    if prior.empty:
        return empty
    row = prior.iloc[-1]
    return {
        "feed_ts": row["feed_ts"], "capture_ts": row["capture_ts"],
        "vix": row["vix"], "vix9d": row["vix9d"],
    }


def same_vix_values(prev, vix, vix9d):
    """Mirrors same_values() -- content dedupe for the republished-feed case."""
    prior_vix, prior_vix9d = prev.get("vix"), prev.get("vix9d")
    if prior_vix in (None, "") or prior_vix9d in (None, ""):
        return False
    try:
        return (abs(float(prior_vix) - round(vix, 4)) < 1e-9 and
                abs(float(prior_vix9d) - round(vix9d, 4)) < 1e-9)
    except (TypeError, ValueError):
        return False


def capture_vix(args, logpath, now):
    vix_data = fetch_vix_quote("VIX")
    vix9d_data = fetch_vix_quote("VIX9D")

    vix = vix_data.get("current_price")
    vix9d = vix9d_data.get("current_price")
    if vix is None or vix9d is None:
        log(logpath, "VIX: current_price missing from one or both quotes, skipped")
        return None
    vix, vix9d = float(vix), float(vix9d)
    vix_prev_close = vix_data.get("prev_day_close")
    vix9d_prev_close = vix9d_data.get("prev_day_close")

    feed_ts = vix_data.get("last_trade_time") or vix_data.get("timestamp") or "unknown"
    state_path = os.path.join(args.outdir, "vix_state.csv")
    prev = last_vix_row(state_path)

    if prev["feed_ts"] == str(feed_ts) and not args.force:
        log(logpath, f"VIX: feed_ts {feed_ts} unchanged, skipping (phantom bar avoided)")
        return None

    if same_vix_values(prev, vix, vix9d) and not args.force:
        log(logpath, f"VIX: feed_ts advanced to {feed_ts} but VIX/VIX9D are "
                     f"unchanged from the last row -- republished feed, no-change "
                     f"bar avoided (nothing written)")
        return None

    elapsed = elapsed_seconds(prev["capture_ts"], now)
    capture_ts = now.isoformat(timespec="seconds")
    feed_date = str(feed_ts)[:10]

    vix_chg_1d = (vix / float(vix_prev_close) - 1) if vix_prev_close else None
    vix9d_chg_1d = (vix9d / float(vix9d_prev_close) - 1) if vix9d_prev_close else None

    chg_20d, is_20d_high = None, None
    try:
        chg_20d, is_20d_high = fetch_20d_change(VIX_HISTORY_URL, vix)
    except Exception as e:
        log(logpath, f"VIX: 20d-change lookup failed ({e}), leaving blank")

    term_spread = vix9d - vix
    term_state = "backwardation(short-rich)" if term_spread > 0 else "contango(short-cheap)"

    row = {
        "feed_ts": str(feed_ts),
        "feed_date": feed_date,
        "capture_ts": capture_ts,
        "elapsed_s": elapsed,
        "vix": round(vix, 4),
        "vix_chg_1d_pct": round(vix_chg_1d, 6) if vix_chg_1d is not None else "",
        "vix_20d_change_pct": round(chg_20d, 6) if chg_20d is not None else "",
        "vix_20d_high": bool(is_20d_high) if is_20d_high is not None else "",
        "vix9d": round(vix9d, 4),
        "vix9d_chg_1d_pct": round(vix9d_chg_1d, 6) if vix9d_chg_1d is not None else "",
        "term_spread_9d_vs_30d": round(term_spread, 4),
        "term_state": term_state,
        "schema_version": SCHEMA_VERSION,
    }

    if not args.dry_run:
        append_vix_state(state_path, [row])
        d1 = f"{vix_chg_1d*100:+.1f}%" if vix_chg_1d is not None else "n/a"
        d20 = f"{chg_20d*100:+.1f}%" if chg_20d is not None else "n/a"
        log(logpath, f"VIX: {vix:.2f} ({d1} 1d, {d20} 20d)  VIX9D: {vix9d:.2f}  "
                     f"term {term_state} -> vix_state.csv")
    else:
        log(logpath, f"VIX: {vix:.2f}  VIX9D: {vix9d:.2f} (dry run, not written)")

    return row


def append_vix_state(state_path, rows):
    df = pd.DataFrame(rows, columns=VIX_COLUMNS)
    header = not os.path.exists(state_path)
    df.to_csv(state_path, mode="a", header=header, index=False)


# --------------------------------------------------------------------------
# surface metrics


def _atm_iv(chain, spot, target_dte, tol=12.0):
    """ATM implied vol near a target DTE, averaged across the call and put."""
    near = chain[(chain["dte"] - target_dte).abs() <= tol]
    if near.empty:
        return np.nan
    exp = near.loc[(near["dte"] - target_dte).abs().idxmin(), "expiry"]
    slice_ = near[(near["expiry"] == exp) & (near["iv"] > 0)]
    if slice_.empty:
        return np.nan
    out = []
    for cp in ("C", "P"):
        side = slice_[slice_["type"] == cp]
        if side.empty:
            continue
        out.append(float(side.loc[(side["strike"] - spot).abs().idxmin(), "iv"]))
    return float(np.mean(out)) if out else np.nan


def _rr25(chain, target_dte=30.0, tol=12.0):
    """25-delta risk reversal: IV(25d call) - IV(25d put).

    Negative means the downside tail is bid, which is the normal index state.
    Magnitude is what the decision table's skew axis reads.
    """
    near = chain[(chain["dte"] - target_dte).abs() <= tol]
    if near.empty:
        return np.nan
    exp = near.loc[(near["dte"] - target_dte).abs().idxmin(), "expiry"]
    slice_ = near[(near["expiry"] == exp) & (near["iv"] > 0)]
    if slice_.empty:
        return np.nan

    def iv_at_25(cp):
        side = slice_[slice_["type"] == cp].copy()
        if side.empty:
            return np.nan
        side["absd"] = side["delta"].abs()
        side = side[(side["absd"] > 0.02) & (side["absd"] < 0.98)]
        if side.empty:
            return np.nan
        return float(side.loc[(side["absd"] - 0.25).abs().idxmin(), "iv"])

    c, p = iv_at_25("C"), iv_at_25("P")
    if np.isnan(c) or np.isnan(p):
        return np.nan
    return c - p


def _skew_state(rr, steep_threshold):
    """STEEP / FLAT for the decision table's skew axis.

    Threshold is a parameter, not a constant, because it has to be calibrated
    against actual observed distribution once history exists. Until then the
    label is provisional and should not be treated as locked.
    """
    if rr is None or (isinstance(rr, float) and np.isnan(rr)):
        return ""
    return "STEEP" if rr <= -steep_threshold else "FLAT"


# --------------------------------------------------------------------------
# capture


def capture_symbol(symbol, args, logpath, now):
    payload = gx.fetch_chain(symbol)
    df, spot, feed_ts = parse_chain_full(payload)
    df = gx.normalize_iv(df, quiet=True)

    state_path = os.path.join(args.outdir, "intraday_state.csv")
    prev = last_snapshot(state_path, symbol)

    if prev["feed_ts"] == str(feed_ts) and not args.force:
        log(logpath, f"{symbol}: feed_ts {feed_ts} unchanged, skipping "
                     "(phantom bar avoided)")
        return None, None

    elapsed = elapsed_seconds(prev["capture_ts"], now)
    capture_ts = now.isoformat(timespec="seconds")
    feed_date = str(feed_ts)[:10]

    # ---- window ---------------------------------------------------------
    # Everything the flip, walls, skew and near-term flow depend on lives
    # inside this window. Storing the full chain 27x a day would add roughly
    # 600MB/month to a git repo that keeps every version forever.
    lo, hi = spot * (1 - args.strike_window), spot * (1 + args.strike_window)
    kept = df[(df["dte"] <= args.dte_window) &
              (df["strike"] >= lo) & (df["strike"] <= hi)].copy()

    if kept.empty:
        log(logpath, f"{symbol}: nothing inside the capture window, skipped")
        return None, None

    # ---- state vector ---------------------------------------------------
    # Computed BEFORE anything is written, because net_gex_window is half the
    # content check below and a skipped bar must leave no parquet behind.
    metric_chain = df[df["dte"] <= args.max_dte].copy()

    glo, ghi = spot * (1 - args.grid_window), spot * (1 + args.grid_window)
    _, _, flip = gx.gamma_profile(metric_chain, glo, ghi)

    plo, phi = spot * (1 - args.plot_window), spot * (1 + args.plot_window)
    windowed = metric_chain[(metric_chain["strike"] >= plo) &
                            (metric_chain["strike"] <= phi)]
    win_ps, calls, puts = gx.gex_by_strike(windowed, spot)
    call_wall, put_wall, _ = gx.find_walls(calls, puts, spot, args.wall_exclude)

    net_gex_window = float(win_ps.sum())
    atm30 = _atm_iv(df, spot, 30.0)
    atm60 = _atm_iv(df, spot, 60.0)
    rr = _rr25(df)

    cvol = float(kept.loc[kept["type"] == "C", "volume"].sum())
    pvol = float(kept.loc[kept["type"] == "P", "volume"].sum())

    # ---- content check --------------------------------------------------
    if same_values(prev, spot, net_gex_window) and not args.force:
        log(logpath,
            f"{symbol}: feed_ts advanced to {feed_ts} but spot and net GEX are "
            f"unchanged from the last row -- republished feed, no-change bar "
            f"avoided (nothing written)")
        return None, None

    # ---- slim snapshot --------------------------------------------------
    snap = kept.copy()
    snap["symbol"] = symbol
    snap["feed_ts"] = str(feed_ts)
    snap["capture_ts"] = capture_ts
    snap["elapsed_s"] = elapsed
    snap["spot"] = spot
    snap["expiry"] = snap["expiry"].dt.tz_convert("UTC").dt.tz_localize(None)
    snap = snap[SNAPSHOT_COLUMNS]

    if not args.dry_run:
        outdir = os.path.join(args.outdir, "intraday", feed_date)
        os.makedirs(outdir, exist_ok=True)
        stamp = re.sub(r"[^0-9]", "", str(feed_ts))[-6:] or capture_ts[11:19].replace(":", "")
        path = os.path.join(outdir, f"{symbol}__{stamp}.parquet")
        try:
            snap.to_parquet(path, compression="zstd", index=False)
        except (ImportError, ValueError):
            path = path.replace(".parquet", ".csv.gz")
            snap.to_csv(path, index=False, compression="gzip")
        log(logpath, f"{symbol}: {len(snap):,} contracts -> {os.path.basename(path)}")
    else:
        log(logpath, f"{symbol}: {len(snap):,} contracts (dry run, not written)")

    state = {
        "feed_ts": str(feed_ts),
        "feed_date": feed_date,
        "capture_ts": capture_ts,
        "elapsed_s": elapsed,
        "symbol": symbol,
        "spot": round(spot, 4),
        "contracts_kept": len(kept),
        "net_gex_window": round(net_gex_window, 2),
        "flip": round(flip, 4) if flip is not None else "",
        "flip_pct_vs_spot": round(flip / spot - 1, 6) if flip is not None else "",
        "call_wall": call_wall if call_wall is not None else "",
        "put_wall": put_wall if put_wall is not None else "",
        "regime": "positive" if net_gex_window > 0 else "negative",
        "atm_iv_30": round(atm30, 6) if not np.isnan(atm30) else "",
        "atm_iv_60": round(atm60, 6) if not np.isnan(atm60) else "",
        "term_slope": round(atm60 - atm30, 6) if not (np.isnan(atm30) or np.isnan(atm60)) else "",
        "rr25": round(rr, 6) if not np.isnan(rr) else "",
        "skew_state": _skew_state(rr, args.skew_steep),
        "session_call_volume": cvol,
        "session_put_volume": pvol,
        "session_pc_volume": round(pvol / cvol, 4) if cvol else "",
        "dte_window": args.dte_window,
        "strike_window": args.strike_window,
        "schema_version": SCHEMA_VERSION,
    }
    return snap, state


def append_state(state_path, rows):
    df = pd.DataFrame(rows, columns=STATE_COLUMNS)
    header = not os.path.exists(state_path)
    df.to_csv(state_path, mode="a", header=header, index=False)


# --------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description="Capture intraday GEX / flow snapshots.")
    p.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    p.add_argument("--outdir", default=None)
    p.add_argument("--dte-window", type=float, default=45,
                   help="DTE ceiling for the stored slim snapshot (default 45)")
    p.add_argument("--strike-window", type=float, default=0.15,
                   help="strikes kept, as a fraction either side of spot (default 0.15)")
    p.add_argument("--max-dte", type=float, default=30,
                   help="DTE filter for the derived state vector, matching "
                        "gex_capture.py's default so the two series are comparable")
    p.add_argument("--wall-exclude", type=float, default=0.01)
    p.add_argument("--grid-window", type=float, default=0.15)
    p.add_argument("--plot-window", type=float, default=0.10)
    p.add_argument("--skew-steep", type=float, default=0.02,
                   help="provisional RR25 threshold for STEEP; calibrate once "
                        "history exists (default 0.02)")
    p.add_argument("--force", action="store_true",
                   help="capture even if the feed timestamp has not changed")
    p.add_argument("--no-vix", dest="no_vix", action="store_true",
                   help="skip the VIX/VIX9D capture")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.outdir is None:
        args.outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "history")
    os.makedirs(args.outdir, exist_ok=True)
    logpath = os.path.join(args.outdir, "intraday.log")
    now = datetime.now(timezone.utc)

    log(logpath, f"--- intraday capture  symbols={' '.join(args.symbols)}"
                 f"{'  (dry run)' if args.dry_run else ''}")

    states, failures, skipped = [], [], 0
    for sym in args.symbols:
        sym = sym.upper()
        try:
            _, state = capture_symbol(sym, args, logpath, now)
            if state is None:
                skipped += 1
                continue
            states.append(state)
            log(logpath,
                f"{sym}: spot {state['spot']:,.2f}  netGEX(win) "
                f"{state['net_gex_window']/1e9:,.2f}Bn  flip {state['flip']}  "
                f"walls {state['call_wall']}/{state['put_wall']}  "
                f"{state['regime']}  RR25 {state['rr25']} "
                f"({state['skew_state']})  P/C {state['session_pc_volume']}")
        except SystemExit as e:
            failures.append(f"{sym}: {e}")
            log(logpath, f"{sym}: FAILED -- {e}")
        except Exception as e:
            failures.append(f"{sym}: {e}")
            log(logpath, f"{sym}: FAILED -- {e}\n{traceback.format_exc()}")

    if states and not args.dry_run:
        append_state(os.path.join(args.outdir, "intraday_state.csv"), states)
        log(logpath, f"appended {len(states)} state row(s)")

    vix_row = None
    if not args.no_vix:
        try:
            vix_row = capture_vix(args, logpath, now)
        except Exception as e:
            failures.append(f"VIX: {e}")
            log(logpath, f"VIX: FAILED -- {e}\n{traceback.format_exc()}")

    log(logpath, f"--- intraday done  ok={len(states)}  skipped={skipped}  "
                 f"failed={len(failures)}"
                 f"{'  vix=written' if vix_row else ''}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
