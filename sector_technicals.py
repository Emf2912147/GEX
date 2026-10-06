#!/usr/bin/env python3
"""
Sector Agent -- technicals capture (Cboe delayed quotes + Stooq daily history).

SCOPE
    Feeds the sector long/short screening agent only. Writes exclusively to
    history/sector/sector_technicals.csv. Same isolation boundary as
    sector_fundamentals.py -- see claude/sector-agent.md; the Trade Agent /
    Agent007 pipeline's files are never opened by this script.

CADENCE
    Twice daily on trading days, ~11:00 and ~15:15 ET (see
    .github/workflows/sector_technicals.yml). NOTE: that workflow's cron is
    UTC and does not auto-adjust for US DST -- both cron lines need shifting
    by one hour at the November/March clock changes, or the real capture
    time drifts an hour against ET. Flagged here so it isn't forgotten.

SOURCES (both free, both keyless)
    Options tradability : Cboe's delayed-quotes endpoint, per symbol -- the
                           same endpoint Agent007's off-pipeline read already
                           uses successfully for arbitrary symbols. Different
                           from the indices-only daily-price-history endpoint
                           that 403s on SPY/individual names, so no new risk
                           carried over from that earlier finding.
    Price / momentum     : Stooq's per-symbol daily CSV feed. Free and
                           keyless, but its usage terms are not as clearly
                           published as EDGAR's -- treat it as provisional
                           until it has run clean for a few weeks, the same
                           posture this project took with Cboe's own history
                           schema before it was proven out.

UNIVERSE
    Reads (ticker, sector_etf) pairs from the most recent
    sector_fundamentals.csv capture. Refuses (not guesses) if that file
    doesn't exist yet -- run sector_fundamentals.py at least once first.
"""
import argparse
import io
import re
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"
CASH_INDEX_SYMBOLS = {"SPX", "NDX", "RUT", "VIX", "XSP", "DJX"}

TECH_COLUMNS = [
    "capture_ts", "sector_etf", "ticker", "spot",
    "atm_oi", "atm_spread_pct", "chain_contracts",
    "mom_20d", "mom_60d", "sector_mom_20d", "sector_mom_60d",
    "rel_strength_20d", "rel_strength_60d", "schema_version",
]
SCHEMA_VERSION = 1
STOOQ_MIN_INTERVAL_S = 0.5
CBOE_MIN_INTERVAL_S = 0.3
NEAR_STRIKES_N = 20


def log(path, msg):
    line = f"{datetime.now(timezone.utc).isoformat()}Z  {msg}"
    print(line)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def load_universe(fund_csv, logpath):
    """(ticker, sector_etf) pairs from the latest fundamentals capture."""
    if not os.path.exists(fund_csv):
        log(logpath, "no sector_fundamentals.csv yet -- technicals has no universe to read")
        return []
    df = pd.read_csv(fund_csv)
    if df.empty:
        return []
    # Select by capture DAY, not by an exact capture_ts. Rows written before
    # the run-timestamp fix carry a distinct timestamp each, so an equality
    # match on max() returns a single row -- which is exactly what happened
    # on every run from 2026-09-12 onward. Going by day is correct for both
    # the old per-row stamps and the new single run stamp, so this works
    # against the existing file without needing a migration.
    day = pd.to_datetime(df["capture_ts"], format="mixed", utc=True).dt.date
    latest_day = day.max()
    latest = df[day == latest_day]
    pairs = list(latest[["ticker", "sector_etf"]].drop_duplicates()
                 .itertuples(index=False, name=None))
    # Rows captured before the holdings filter existed still carry index-option
    # roots and placeholders. Filter on the read side too, so the existing file
    # works without a migration.
    keep = [p for p in pairs
            if re.fullmatch(r"[A-Z]{1,5}([.\-][A-Z]{1,2})?", str(p[0]))]
    if len(keep) < len(pairs):
        dropped = [p[0] for p in pairs if p not in keep]
        log(logpath, f"dropped {len(dropped)} non-equity names -- {dropped[:6]}")
    pairs = keep
    log(logpath, f"universe: {len(pairs)} names from fundamentals {latest_day}")
    if len(pairs) < 50:
        log(logpath, f"WARNING universe is only {len(pairs)} names -- expected "
                     f"~500. Check sector_fundamentals.csv.")
    return pairs


def strike_of(opt):
    """Cboe option symbols encode the strike in thousandths in the trailing
    8 digits, e.g. ...C00745000 -> 745.00. Returns None rather than raising
    on anything unexpected."""
    try:
        return int(opt["option"][-8:]) / 1000.0
    except (KeyError, ValueError, TypeError):
        return None


def tradability_from_chain(spot, options, n_near=NEAR_STRIKES_N):
    """Pure function: (spot, options list) -> (atm_oi, atm_spread_pct).
    Separated from the network call so it's unit-testable against fixtures."""
    if not options or spot is None:
        return None, None
    near = sorted(options, key=lambda o: abs((strike_of(o) or 1e12) - spot))[:n_near]
    ois = [o.get("open_interest", 0) or 0 for o in near]
    widths = []
    for o in near:
        b, a = o.get("bid"), o.get("ask")
        if b is not None and a is not None and (b + a) > 0:
            widths.append((a - b) / ((a + b) / 2))
    atm_oi = int(np.median(ois)) if ois else None
    atm_spread_pct = float(np.median(widths)) if widths else None
    return atm_oi, atm_spread_pct


def fetch_cboe_tradability(symbol, logpath):
    prefix = "_" if symbol.upper() in CASH_INDEX_SYMBOLS else ""
    url = CBOE_URL.format(sym=f"{prefix}{symbol.upper()}")
    time.sleep(CBOE_MIN_INTERVAL_S)
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        log(logpath, f"{symbol}: Cboe fetch failed -- {e}")
        return None, None, None, 0

    data = payload.get("data", {})
    spot = data.get("current_price")
    options = data.get("options", [])
    if not options or spot is None:
        log(logpath, f"{symbol}: Cboe payload empty -- refused")
        return None, None, None, 0

    atm_oi, atm_spread_pct = tradability_from_chain(spot, options)
    return spot, atm_oi, atm_spread_pct, len(options)


def parse_stooq_csv(text):
    """Pure function: raw Stooq response text -> list of closes, or None.
    Unit-testable without a network call."""
    if not text or text.strip().lower().startswith("no data") or "<html" in text.lower():
        return None
    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception:
        return None
    if "Close" not in df.columns or len(df) < 61:
        return None
    return df["Close"].astype(float).tolist()


def yahoo_symbol(t):
    """Yahoo writes share classes with a dash: BRK.B -> BRK-B, BF.B -> BF-B."""
    return str(t).replace(".", "-")


def fetch_history_yf(symbols, logpath, chunk=100, period="1y"):
    """Daily closes for many symbols at once. Returns {symbol: [closes]}.

    REPLACES STOOQ. On 2026-10-06 Stooq returned 404 for every symbol in the
    universe -- xom.us, jpm.us, xle.us, all of them, which are valid paths --
    and then began refusing connections outright. That is blocking or rate
    limiting, not a symbol problem. Stooq was always the single point of
    failure here and the module docstring flagged it as provisional; it never
    ran clean for even one capture.

    yfinance is already a dependency of this workflow and is already proven
    against the runner's IP: sector_fundamentals.py has pulled Yahoo financials
    successfully on every capture since 2026-09-12.

    Batched, not per-symbol. The old path slept 2s between 504 sequential
    requests -- 17 minutes of wall clock before any failure was even visible.
    """
    import yfinance as yf

    out = {}
    syms = list(dict.fromkeys(symbols))
    for i in range(0, len(syms), chunk):
        batch = syms[i:i + chunk]
        mapped = {yahoo_symbol(s): s for s in batch}
        try:
            df = yf.download(list(mapped), period=period, interval="1d",
                             auto_adjust=False, progress=False,
                             group_by="ticker", threads=True)
        except Exception as e:
            log(logpath, f"yfinance batch {i // chunk + 1} failed -- {e}")
            continue
        for ysym, orig in mapped.items():
            try:
                col = df[ysym]["Close"] if len(mapped) > 1 else df["Close"]
                closes = [float(x) for x in col.dropna().tolist()]
            except Exception:
                closes = []
            if len(closes) >= 61:
                out[orig] = closes
        log(logpath, f"yfinance batch {i // chunk + 1}: "
                     f"{sum(1 for s in batch if s in out)}/{len(batch)} with history")

    missing = [s for s in syms if s not in out]
    if missing:
        log(logpath, f"no usable history for {len(missing)} names -- {missing[:8]}")
    return out


def fetch_stooq_history(symbol, logpath):
    time.sleep(STOOQ_MIN_INTERVAL_S)
    try:
        resp = requests.get(STOOQ_URL.format(sym=symbol.lower()), timeout=20)
        resp.raise_for_status()
    except Exception as e:
        log(logpath, f"{symbol}: Stooq fetch failed -- {e}")
        return None
    closes = parse_stooq_csv(resp.text)
    if closes is None:
        log(logpath, f"{symbol}: Stooq returned no usable history")
    return closes


def momentum(closes, n):
    """Trailing n-session return. None if there isn't enough history --
    never approximated from a shorter window."""
    if closes is None or len(closes) < n + 1:
        return None
    return closes[-1] / closes[-(n + 1)] - 1


def capture_one(ticker, sector_etf, sector_mom, logpath, closes=None):
    spot, atm_oi, atm_spread_pct, n_contracts = fetch_cboe_tradability(ticker, logpath)
    if closes is None:
        closes = fetch_stooq_history(ticker, logpath)
    mom20 = momentum(closes, 20)
    mom60 = momentum(closes, 60)
    smom20, smom60 = sector_mom
    rel20 = (mom20 - smom20) if mom20 is not None and smom20 is not None else None
    rel60 = (mom60 - smom60) if mom60 is not None and smom60 is not None else None

    if spot is None and closes is None:
        log(logpath, f"{ticker}: no usable data from either source -- skipped entirely")
        return None

    return {
        "capture_ts": datetime.now(timezone.utc).isoformat(),
        "sector_etf": sector_etf, "ticker": ticker, "spot": spot,
        "atm_oi": atm_oi, "atm_spread_pct": atm_spread_pct, "chain_contracts": n_contracts,
        "mom_20d": mom20, "mom_60d": mom60,
        "sector_mom_20d": smom20, "sector_mom_60d": smom60,
        "rel_strength_20d": rel20, "rel_strength_60d": rel60,
        "schema_version": SCHEMA_VERSION,
    }


def append_rows(out_path, rows):
    if not rows:
        return
    df = pd.DataFrame(rows, columns=TECH_COLUMNS)
    header = not os.path.exists(out_path)
    df.to_csv(out_path, mode="a", header=header, index=False)


def main():
    p = argparse.ArgumentParser(description="Sector agent -- technicals capture (Cboe + Stooq).")
    p.add_argument("--outdir", default="history/sector")
    p.add_argument("--source", choices=["yfinance", "stooq"], default="yfinance",
                   help="price history source; stooq is the old path, kept "
                        "only as a manual fallback (it was 404ing as of 2026-10-06)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    logpath = os.path.join(args.outdir, "technicals_capture.log")
    fund_csv = os.path.join(args.outdir, "sector_fundamentals.csv")
    out_path = os.path.join(args.outdir, "sector_technicals.csv")

    pairs = load_universe(fund_csv, logpath)
    if not pairs:
        log(logpath, "nothing to capture -- run sector_fundamentals.py at least once first")
        return 1

    log(logpath, f"=== technicals capture start ({len(pairs)} names) ===")

    etfs = sorted({e for _, e in pairs})
    if args.source == "yfinance":
        hist = fetch_history_yf([t for t, _ in pairs] + etfs, logpath)
    else:
        hist = {}

    sector_moms = {}
    for e in etfs:
        ec = hist.get(e) if hist else fetch_stooq_history(e, logpath)
        sector_moms[e] = (momentum(ec, 20), momentum(ec, 60))
        if ec is None:
            log(logpath, f"{e}: no sector history -- relative momentum will be null")

    rows = []
    for ticker, sector_etf in pairs:
        # [] not None when yfinance is the source: None means "go fetch it
        # yourself", which would send every name Yahoo missed straight back
        # to the dead Stooq path. [] means "no history", and momentum()
        # returns None for it, which is the honest answer.
        row = capture_one(ticker, sector_etf, sector_moms[sector_etf], logpath,
                          closes=hist.get(ticker, []) if args.source == "yfinance"
                          else None)
        if row:
            rows.append(row)

    log(logpath, f"=== technicals capture done: {len(rows)}/{len(pairs)} names captured ===")

    if args.dry_run:
        print(pd.DataFrame(rows).head(20).to_string())
        return 0

    append_rows(out_path, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
