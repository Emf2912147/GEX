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

 2. Duplicate feed timestamps are skipped. The Cboe delayed feed does not
    refresh on our cadence. Capturing an unchanged feed_ts again would
    create a phantom bar with zero volume delta and a real elapsed time,
    which reads as "flow stopped" when nothing happened at all.

 3. Elapsed time is recorded per snapshot. GitHub Actions cron drifts and
    occasionally skips runs entirely, so bars are NOT evenly spaced. Any
    flow measure built on this must normalise by elapsed minutes rather
    than assume a 15-minute bar.

Layout:
    history/
      intraday/<feed-date>/<SYMBOL>__<HHMMSS>.parquet   slim snapshots
      intraday_state.csv                                append-only state vector
      intraday.log

Usage:
    python gex_intraday.py                    # SPX SPY QQQ IWM
    python gex_intraday.py --symbols SPX QQQ
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
    """Most recent (feed_ts, capture_ts) already on file for this symbol."""
    if not os.path.exists(state_path):
        return None, None
    try:
        prior = pd.read_csv(state_path, usecols=["symbol", "feed_ts", "capture_ts"],
                            dtype=str)
    except Exception:
        return None, None
    mine = prior[prior["symbol"] == symbol]
    if mine.empty:
        return None, None
    row = mine.iloc[-1]
    return row["feed_ts"], row["capture_ts"]


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
    prev_feed_ts, prev_capture_ts = last_snapshot(state_path, symbol)

    if prev_feed_ts == str(feed_ts) and not args.force:
        log(logpath, f"{symbol}: feed_ts {feed_ts} unchanged, skipping "
                     "(phantom bar avoided)")
        return None, None

    elapsed = elapsed_seconds(prev_capture_ts, now)
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

    # ---- state vector ---------------------------------------------------
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

    log(logpath, f"--- intraday done  ok={len(states)}  skipped={skipped}  "
                 f"failed={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
