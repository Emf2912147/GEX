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

 4. A chain with no Greeks in it is REFUSED, not stored. Cboe occasionally
    serves a structurally valid payload with every gamma and every IV
    zeroed; every metric downstream then degrades silently into a
    normal-looking row (net GEX exactly 0, flip pinned to the search
    grid's floor, both walls on one strike). See check_chain_integrity().

WHAT THIS SCRIPT REFUSES TO DO
    Three guards, all the same principle -- write nothing rather than write
    something wrong, because a wrong row is indistinguishable from a right one
    once it is on disk and the agent reading it downstream has no way to tell:

      unchanged feed        -> skip  (nothing happened)
      unchanged content     -> skip  (nothing happened, feed republished)
      Greeks missing        -> FAIL  (something happened, and it was bad)

    The third is deliberately not a skip. A skip is a normal, silent, expected
    outcome that reads as zero in the summary; a refusal has to be loud or it
    is not a guard at all.

VIX CAPTURE
    Same 15-minute cycle also pulls VIX and VIX9D -- Cboe's own quotes
    endpoint, not the options-chain one, so it needs its own parser and its
    own file (history/vix_state.csv). Verified live 2026-09-10: VIX closed
    17.84 (+8.4% on the day, +21.9% over the trailing 20 sessions -- a fresh
    20-day high made that same day); VIX9D closed 17.70 (+13.5%).

    DEDUPE HERE IS CONTENT-ONLY. That is a simplification rather than a fix --
    an earlier note in this file claimed the quotes endpoint's last_trade_time
    never advances intraday, and that was simply wrong; see the corrected
    history in capture_vix().

    The two legs DO refresh independently, and that one is real: on 2026-09-11
    the 13:12 UTC row had VIX down 10.1% with vix9d_chg_1d_pct exactly 0.0,
    which read as backwardation, spread +1.67. An hour later, with both legs
    live, the same pair read contango, spread -1.43 -- a full sign flip caused
    by nothing but which file had refreshed. term_state is therefore suppressed
    to "unreliable(vix9d-stale)" in that situation; see term_structure_state().

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

REALIZED VOL CAPTURE
    WHY THIS EXISTS
        The state vector carries ATM IV but had no realized vol to price it
        against, so every "is vol rich or cheap" question -- the vol axis of
        the decision table, and the whole edge/mispricing scan -- had to stop
        and ask a human for an HV20 number off a broker screen. That works,
        but it is manual, it is unrepeatable, and it cannot be backtested.

        The blocker was never the data. Cboe publishes free daily closes; the
        earlier attempts failed because they went through a fetch-and-
        summarise path that silently truncated a multi-decade file and
        reported 1984 rows as "the last 45 sessions". Read the same file with
        an ordinary HTTP client and parse it deterministically and the
        problem disappears entirely -- which is exactly how the VIX 20d change
        above already works.

    TWO SCHEMAS, NOT ONE -- verified live 2026-09-11
        VIX / VXN / XSP   ->  DATE,OPEN,HIGH,LOW,CLOSE
        SPX / RUT         ->  DATE,SPX       (date and close only, the price
                                              column named for the symbol)
        A CLOSE-only reader silently returns nothing on the second family, so
        fetch_history_closes() handles both and refuses anything it cannot
        identify rather than guessing at a column.

    WHAT IS AND IS NOT AVAILABLE -- verified live 2026-09-11
        SPX, RUT, XSP, VIX, VXN   200
        SPY, NDX, _NDX            403
        Cboe publishes S&P and Russell series and its own volatility indices
        for free; Nasdaq-owned series are not theirs to republish. So:
          SPX -> its own series
          SPY -> SPX      (SPY tracks the S&P 500; a constant scale factor
                           cancels out of log returns, so realized vol is the
                           same series)
          IWM -> RUT      (same reasoning, Russell 2000)
          QQQ -> nothing. No free Nasdaq-100 price history exists here, and
                 VXN is an IMPLIED vol index, not prices, so it cannot stand
                 in. QQQ simply gets no row and the agent still asks. Three of
                 four automated honestly beats four of four with one invented.
        Every proxied read is written with rv_proxy=True and rv_source naming
        the series actually used, so a substitute can never be mistaken for a
        direct reading -- the same discipline the VIX9D term-structure proxy
        already follows.

    CADENCE
        Realized vol changes once a day, at the close; the capture runs every
        15 minutes. So this fetches at most ONCE PER SYMBOL PER UTC DAY, on
        the first cycle of the day, and skips without touching the network
        afterwards. A row is written on every such first cycle even when
        rv_asof_date has not advanced (a holiday, or Cboe not having posted
        yet): the alternative -- skip and retry -- re-fetches a multi-MB file
        every 15 minutes all day on exactly the days the data is not there.
        rv_asof_date states what the numbers are actually through, so a
        repeated as-of date is visible rather than hidden.

    NOTE ON HV: annualised with 252 trading days, sample stdev of log returns.
    Broker platforms differ slightly in lookback and annualisation convention,
    so expect the same neighbourhood as a ThinkOrSwim "Hist. Vol." reading,
    not an identical decimal.

Layout:
    history/
      intraday/<feed-date>/<SYMBOL>__<HHMMSS>.parquet   slim snapshots
      intraday_state.csv                                append-only state vector
      vix_state.csv                                     append-only VIX/VIX9D
      realized_vol.csv                                  append-only HV + skew
      intraday.log

Usage:
    python gex_intraday.py                    # SPX SPY QQQ IWM + VIX + realized vol
    python gex_intraday.py --symbols SPX QQQ
    python gex_intraday.py --no-vix            # skip the VIX capture
    python gex_intraday.py --no-rv             # skip the realized-vol capture
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


class ChainIntegrityError(Exception):
    """The payload parsed cleanly but carries no usable Greeks.

    Raised instead of returning a skip, because this is a FAILURE and the run
    summary must say so. A skip means "nothing changed"; this means "the feed
    lied and we refused it" -- collapsing the two would hide the fault in a
    number that normally reads zero.
    """

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

# ---- realized vol ---------------------------------------------------------
HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{sym}_History.csv"

# Trading days per year, for annualising. 252 is the convention the decision
# table's RICH/CHEAP axis assumes when it compares ATM IV against HV.
TRADING_DAYS = 252

# symbol -> (history series to read, is_proxy). EVERY ENTRY HERE WAS FETCHED
# LIVE 2026-09-11 and returned 200 with a parseable schema. Do not add a symbol
# without actually fetching it first -- SPY and NDX both look like they belong
# and both return 403. See the REALIZED VOL CAPTURE docstring section.
RV_HISTORY = {
    "SPX": ("SPX", False),
    "SPY": ("SPX", True),      # SPY tracks the S&P 500
    "IWM": ("RUT", True),      # IWM tracks the Russell 2000
    "RUT": ("RUT", False),
    "XSP": ("XSP", False),
    "VIX": ("VIX", False),
    # QQQ deliberately absent -- no free Nasdaq-100 price history on this
    # endpoint. Leaving it out makes the gap explicit in the data instead of
    # filling it with a series that is not the Nasdaq-100.
}

# Lookback for the asymmetry block. 60 sessions is long enough to hold a usable
# number of down days while still describing the current regime rather than
# last year's.
RV_ASYM_LOOKBACK = 60
RV_MIN_PER_SIDE = 5

RV_COLUMNS = [
    "symbol", "capture_ts", "rv_asof_date", "rv_source", "rv_proxy",
    "hv10", "hv20", "hv60",
    "rv_down_up_ratio_60", "rv_tail_ratio_60", "rv_skew_60",
    "n_closes", "schema_version",
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


def greek_fill(chain):
    """(gamma fill, iv fill) -- share of contracts carrying a usable Greek."""
    if chain.empty:
        return 0.0, 0.0
    return float((chain["gamma"] != 0).mean()), float((chain["iv"] > 0).mean())


def check_chain_integrity(symbol, chain, min_fill):
    """Refuse a chain whose Greeks came back empty. Raises ChainIntegrityError.

    WHY THIS EXISTS
        2026-09-10 13:45:29: Cboe served SPX and QQQ chains with every gamma and
        every IV zeroed. SPY and IWM were fine in the same cycle, so this is
        per-symbol and not a whole-feed outage. Nothing downstream noticed:

          gex_by_strike()  summed to exactly 0.0
          gamma_profile()  found no zero crossing and returned the FLOOR of its
                           search grid, so flip came out at exactly 0.85 x spot
          find_walls()     collapsed both walls onto the same lowest strike
          _atm_iv()        filters on iv > 0, found none, returned NaN
          regime           "negative", because 0 > 0 is False

        Every one of those is a silent degradation that produces a
        normal-LOOKING row: real spot, real contract count, a confident regime
        label, a flip and two walls. The run logged "ok=4 skipped=0 failed=0".
        Two corrupt state rows and two corrupt parquet snapshots were written
        and pushed. The Trade Agent reads the most recent row per symbol, so a
        row like that is one unlucky timing away from being the basis of a
        trade.

    WHAT IS CHECKED
        The primary test has no threshold to tune and cannot false-positive:
        if NOT ONE contract in the window carries a non-zero gamma, or not one
        carries a positive IV, the payload is empty of the only thing it is
        being read for. A real chain of thousands of contracts cannot look like
        that.

        `min_fill` is the secondary, tunable guard, for PARTIAL zeroing -- a
        failure mode not yet observed. It is deliberately LOW. The honest
        position is that the true fill rate of a healthy chain has not been
        measured (reading the stored parquet snapshots needs pyarrow, and this
        session could reach neither PyPI nor the raw files), so a high floor
        would be a guess that rejects good data. Instead every run now LOGS its
        observed fill, which turns the log into the measuring instrument: after
        a week of sessions, set this from the observed distribution rather than
        from anyone's intuition.
    """
    g_fill, iv_fill = greek_fill(chain)
    if g_fill == 0.0 or iv_fill == 0.0:
        raise ChainIntegrityError(
            f"{symbol}: chain carries no usable Greeks "
            f"(gamma fill {g_fill:.1%}, IV fill {iv_fill:.1%} over "
            f"{len(chain):,} contracts) -- refusing to write"
        )
    if g_fill < min_fill:
        raise ChainIntegrityError(
            f"{symbol}: gamma fill {g_fill:.1%} is below the {min_fill:.0%} "
            f"floor over {len(chain):,} contracts -- refusing to write"
        )
    return g_fill, iv_fill


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


def fetch_history_closes(hist_url):
    """(dates, closes) from a Cboe daily-price history CSV, oldest first.

    TWO SCHEMAS are in use on this endpoint and they are not interchangeable
    -- both verified live 2026-09-11:

        VIX / VXN / XSP   DATE,OPEN,HIGH,LOW,CLOSE
        SPX / RUT         DATE,SPX          <- date and close only, price
                                               column named for the symbol

    A reader that only knows the first family returns nothing at all on the
    second, silently, which is how a missing HV20 looks identical to a broken
    one. Resolution order:

        1. a column literally named CLOSE                -> use it
        2. exactly two columns, the first of them DATE   -> use the second
        3. anything else                                 -> (None, None)

    Rule 3 is the important one. Grabbing "whatever column looks numeric" out
    of a five-column file whose headers changed would produce a number that is
    wrong rather than absent, and a wrong realized vol flows straight into the
    RICH/CHEAP axis of the decision table.

    Dates come back as ISO strings. Cboe writes them MM/DD/YYYY; anything that
    will not parse is passed through verbatim rather than dropped, so a format
    change shows up in rv_asof_date instead of silently emptying the series.
    """
    r = requests.get(hist_url, timeout=30)
    r.raise_for_status()
    from io import StringIO
    hist = pd.read_csv(StringIO(r.text))
    if hist.empty or len(hist.columns) < 2:
        return None, None

    cols = [str(c).strip() for c in hist.columns]
    close_col = next((c for c, name in zip(hist.columns, cols)
                      if name.upper() == "CLOSE"), None)
    if close_col is None:
        if len(hist.columns) == 2 and cols[0].upper() == "DATE":
            close_col = hist.columns[1]
        else:
            return None, None

    closes = pd.to_numeric(hist[close_col], errors="coerce")
    date_col = hist.columns[0]
    parsed = pd.to_datetime(hist[date_col], format="%m/%d/%Y", errors="coerce")
    if parsed.isna().all():
        parsed = pd.to_datetime(hist[date_col], errors="coerce")

    dates = [
        p.strftime("%Y-%m-%d") if pd.notna(p) else str(raw)
        for p, raw in zip(parsed, hist[date_col])
    ]

    keep = closes.notna() & (closes > 0)
    if not keep.any():
        return None, None
    closes = closes[keep].astype(float).tolist()
    dates = [d for d, k in zip(dates, keep) if k]
    return dates, closes


def fetch_20d_change(hist_url, today_price):
    """20-session change and fresh-20d-high flag, from the full history CSV.

    closes[-20] is the close from 20 sessions ago relative to today (the
    history file is settled EOD data and does not yet contain today's
    still-forming session). Returns (None, None) rather than guessing if the
    column layout does not match what was verified live -- a silently wrong
    number is worse than a blank one.
    """
    _, closes = fetch_history_closes(hist_url)
    if not closes or len(closes) < 20:
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


# A VIX move this large with VIX9D exactly unchanged means the two legs are not
# from the same moment. 0.5% is well outside quote noise and well inside a real
# session move.
VIX_STALE_LEG_MOVE = 0.005


def term_structure_state(vix_chg_1d, vix9d_chg_1d, term_spread):
    """contango / backwardation -- or a refusal to call it.

    The two legs come from two separate files on a CDN and do not update in
    lockstep. Observed 2026-09-11: VIX had moved -10.1% on the day while
    vix9d_chg_1d was EXACTLY 0.0 -- VIX9D had not begun ticking. Comparing a
    live leg against a stale one flipped the reading from contango (yesterday)
    to backwardation, purely as an artifact of which file had refreshed.

    Backwardation is a meaningful signal -- short-dated vol bid over 30-day,
    the shape that argues against calendars and diagonals (gate G4). Publishing
    a fake one is worse than publishing nothing, so this returns an explicit
    unreliable marker rather than a regime label. Downstream readers looking
    for "contango"/"backwardation" will not match it, which is the point: an
    unusable reading should fail to match, not quietly pass as a regime.
    """
    legs_mismatched = (
        vix_chg_1d is not None and vix9d_chg_1d is not None
        and abs(vix_chg_1d) > VIX_STALE_LEG_MOVE and vix9d_chg_1d == 0.0
    )
    if legs_mismatched:
        return "unreliable(vix9d-stale)"
    return "backwardation(short-rich)" if term_spread > 0 else "contango(short-cheap)"


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

    # ---- dedupe: CONTENT ONLY --------------------------------------------
    # A simplification, NOT a bug fix. Read the history before changing it
    # back, because the reasoning here was wrong once already.
    #
    # 2026-09-11 this was believed to be fixing a defect: vix_state.csv held
    # exactly one row for the day, and last_trade_time was observed still
    # reading the prior close ("2026-09-10T16:15:01") well after the open. The
    # conclusion drawn -- that last_trade_time never advances intraday, so a
    # feed_ts gate freezes the series after the first row -- was WRONG on both
    # counts. Later the same session the file held four rows with feed_ts
    # advancing normally (09:57, 10:15, 10:39 ET, each about 15 minutes behind
    # the capture, which is just the delayed feed). The single stuck value at
    # 13:12 UTC was correct: that cycle ran PRE-OPEN, before any print existed.
    # The corroborating "live" fetch had hit a stale CDN node serving the
    # previous day's file.
    #
    # Content-only is kept anyway because it is the honest test -- if neither
    # leg has moved there is nothing to record, whatever any timestamp says --
    # and it subsumes what the feed_ts gate did. But it buys little, and the
    # real lesson is the one above: one observation plus one ad-hoc fetch of a
    # CDN-served file is not evidence about how a field behaves over a session.
    #
    # feed_ts is still written to every row for provenance.
    if same_vix_values(prev, vix, vix9d) and not args.force:
        log(logpath, f"VIX: {vix:.2f}/{vix9d:.2f} unchanged from the last row, "
                     f"nothing written (feed_ts {feed_ts})")
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
    term_state = term_structure_state(vix_chg_1d, vix9d_chg_1d, term_spread)

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
# realized vol


def log_returns(closes):
    """Close-to-close log returns. n closes -> n-1 returns."""
    arr = np.asarray(closes, dtype=float)
    if arr.size < 2:
        return np.array([])
    return np.diff(np.log(arr))


def hv(returns, n, trading_days=TRADING_DAYS):
    """Annualised realized vol over the last n returns, as a DECIMAL.

    Decimal, not percent, to match atm_iv_30/atm_iv_60 in the state vector --
    the two get divided by each other constantly and a unit mismatch there
    would be invisible and wrong rather than obvious and wrong.

    Sample stdev (ddof=1). Needs n returns, i.e. n+1 closes; returns None
    rather than a vol computed from a shorter window than advertised.
    """
    if len(returns) < n or n < 2:
        return None
    sd = float(pd.Series(returns[-n:]).std(ddof=1))
    if not np.isfinite(sd):
        return None
    return sd * float(np.sqrt(trading_days))


def move_asymmetry(returns, n=RV_ASYM_LOOKBACK, min_side=RV_MIN_PER_SIDE):
    """How lopsided the realized moves have actually been, over n sessions.

    This is the missing half of the edge scan. RR25 says what the surface is
    CHARGING for downside relative to upside; these say what the underlying has
    actually DONE. Skew steeper than the realized asymmetry justifies means the
    put wing is expensive; flatter means downside convexity is underpriced.
    Neither reading means anything without the other, which is why the state
    vector carrying RR25 but no realized counterpart left the comparison
    permanently unanswerable.

    Three views, because one number would hide too much:
      down_up   mean down-day size / mean up-day size. The everyday asymmetry.
      tail      deep down move / deep up move. The wing comparison, and the
                closest analogue to what a 25-delta risk reversal prices.
      skew      sample skewness of the return distribution. Negative = a long
                left tail, the shape that justifies put skew existing at all.

    The tail ratio is computed on EACH SIDE'S OWN distribution -- the 10th
    percentile of the down days against the 90th percentile of the up days --
    not as two percentiles of the pooled series. The pooled version has a
    failure mode that a unit test caught and that would have been very hard to
    spot in production: when down days are rarer than 10% of the window, the
    10th percentile of all returns sits ABOVE every negative return, so a
    series of 55 small gains and 5 crashes reports a tail ratio of exactly
    1.00 -- "perfectly symmetric, no edge" -- about as wrong as an answer can
    be while still looking like a reasonable number. Conditioning on each side
    is well defined however lopsided the day count is. A put wing and a call
    wing are each priced off their own side too, so this is also the closer
    analogue.

    All three are None when the window is too short or too one-sided to mean
    anything -- a "ratio" computed from two down days is noise wearing a
    number's clothing.
    """
    if len(returns) < n:
        return None, None, None
    w = pd.Series(returns[-n:])

    ups, downs = w[w > 0], w[w < 0]
    enough = len(ups) >= min_side and len(downs) >= min_side

    down_up = None
    if enough:
        up_mean = float(ups.mean())
        if up_mean > 0:
            down_up = float(downs.abs().mean()) / up_mean

    tail = None
    if enough:
        down_tail = abs(float(downs.quantile(0.10)))   # deep in the down tail
        up_tail = float(ups.quantile(0.90))            # deep in the up tail
        if up_tail > 0:
            tail = down_tail / up_tail

    skew = float(w.skew())
    if not np.isfinite(skew):
        skew = None

    return down_up, tail, skew


def captured_today(rv_path, symbol, now):
    """True when realized vol for this symbol was already written today (UTC).

    The gate that keeps a once-a-day number from pulling a multi-megabyte file
    32 times a day. Deliberately keyed on capture_ts rather than rv_asof_date:
    asking "has the as-of date advanced" cannot be answered without doing the
    fetch first, which is the cost this exists to avoid.
    """
    if not os.path.exists(rv_path):
        return False
    try:
        prior = pd.read_csv(rv_path, usecols=["symbol", "capture_ts"], dtype=str)
    except Exception:
        return False
    mine = prior[prior["symbol"] == symbol]
    if mine.empty:
        return False
    last_ts = str(mine.iloc[-1]["capture_ts"])
    return last_ts[:10] == now.date().isoformat()


def capture_realized_vol(args, logpath, now):
    """One realized-vol row per mapped symbol per day. Never raises."""
    rv_path = os.path.join(args.outdir, "realized_vol.csv")
    rows = []

    for sym in args.symbols:
        sym = sym.upper()
        mapping = RV_HISTORY.get(sym)
        if mapping is None:
            log(logpath, f"RV {sym}: no free Cboe daily-close series for this "
                         f"symbol, skipped (see RV_HISTORY)")
            continue
        if captured_today(rv_path, sym, now) and not args.force:
            continue

        source, is_proxy = mapping
        try:
            dates, closes = fetch_history_closes(HISTORY_URL.format(sym=source))
        except Exception as e:
            log(logpath, f"RV {sym}: history fetch failed ({e}), skipped")
            continue

        if not closes:
            log(logpath, f"RV {sym}: {source}_History.csv returned no usable "
                         f"closes (schema change?), skipped")
            continue

        rets = log_returns(closes)
        hv10, hv20, hv60 = hv(rets, 10), hv(rets, 20), hv(rets, 60)
        down_up, tail, skew = move_asymmetry(rets)

        rows.append({
            "symbol": sym,
            "capture_ts": now.isoformat(timespec="seconds"),
            "rv_asof_date": dates[-1] if dates else "",
            "rv_source": source,
            "rv_proxy": bool(is_proxy),
            "hv10": round(hv10, 6) if hv10 is not None else "",
            "hv20": round(hv20, 6) if hv20 is not None else "",
            "hv60": round(hv60, 6) if hv60 is not None else "",
            "rv_down_up_ratio_60": round(down_up, 4) if down_up is not None else "",
            "rv_tail_ratio_60": round(tail, 4) if tail is not None else "",
            "rv_skew_60": round(skew, 4) if skew is not None else "",
            "n_closes": len(closes),
            "schema_version": SCHEMA_VERSION,
        })

        proxy_note = f" (via {source})" if is_proxy else ""
        hv20_txt = f"{hv20 * 100:.2f}%" if hv20 is not None else "n/a"
        asym_txt = f"{down_up:.2f}x" if down_up is not None else "n/a"
        log(logpath,
            f"RV {sym}{proxy_note}: HV20 {hv20_txt}  down/up {asym_txt}  "
            f"through {dates[-1] if dates else '?'}  ({len(closes):,} closes)")

    if rows and not args.dry_run:
        append_rv_state(rv_path, rows)
        log(logpath, f"appended {len(rows)} realized-vol row(s) -> realized_vol.csv")
    elif rows:
        log(logpath, f"{len(rows)} realized-vol row(s) (dry run, not written)")

    return rows


def append_rv_state(rv_path, rows):
    df = pd.DataFrame(rows, columns=RV_COLUMNS)
    header = not os.path.exists(rv_path)
    df.to_csv(rv_path, mode="a", header=header, index=False)


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

    # Integrity gate. Deliberately placed BEFORE the first derived number, so a
    # payload with no Greeks in it never reaches gamma_profile()/find_walls()
    # and never leaves a parquet behind. Raises rather than returning a skip --
    # see check_chain_integrity() for the 2026-09-10 incident this exists for.
    g_fill, iv_fill = check_chain_integrity(symbol, metric_chain, args.min_greek_fill)
    log(logpath, f"{symbol}: greek fill gamma {g_fill:.1%} / iv {iv_fill:.1%} "
                 f"over {len(metric_chain):,} in-window contracts")

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
    p.add_argument("--min-greek-fill", type=float, default=0.10,
                   help="minimum share of in-window contracts carrying a "
                        "non-zero gamma before a chain is accepted (default "
                        "0.10; deliberately low -- recalibrate from the logged "
                        "fill rates once a few weeks exist)")
    p.add_argument("--skew-steep", type=float, default=0.02,
                   help="provisional RR25 threshold for STEEP; calibrate once "
                        "history exists (default 0.02)")
    p.add_argument("--force", action="store_true",
                   help="capture even if the feed timestamp has not changed")
    p.add_argument("--no-vix", dest="no_vix", action="store_true",
                   help="skip the VIX/VIX9D capture")
    p.add_argument("--no-rv", dest="no_rv", action="store_true",
                   help="skip the realized-vol capture")
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

    states, failures, skipped, refused = [], [], 0, 0
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
        except ChainIntegrityError as e:
            # A refusal, not a crash: the payload was structurally sound and
            # empty of Greeks. Counted as a failure so the run is non-zero and
            # the summary cannot read like a clean cycle.
            refused += 1
            failures.append(str(e))
            log(logpath, f"REFUSED -- {e}")
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

    # Independent of the chain capture on purpose: realized vol comes from the
    # daily-close history, so a symbol whose chain fetch failed above still
    # gets its HV, and a failure here can never cost us the flow snapshot.
    rv_rows = []
    if not args.no_rv:
        try:
            rv_rows = capture_realized_vol(args, logpath, now)
        except Exception as e:
            failures.append(f"RV: {e}")
            log(logpath, f"RV: FAILED -- {e}\n{traceback.format_exc()}")

    log(logpath, f"--- intraday done  ok={len(states)}  skipped={skipped}  "
                 f"failed={len(failures)}"
                 f"{f'  REFUSED={refused}' if refused else ''}"
                 f"{'  vix=written' if vix_row else ''}"
                 f"{f'  rv={len(rv_rows)}' if rv_rows else ''}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
