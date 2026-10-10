#!/usr/bin/env python3
"""
Sector Agent -- technicals capture (Cboe delayed quotes + Stooq daily history).

SCOPE
    Feeds the sector long/short screening agent only. Writes exclusively to
    history/sector/sector_technicals.csv. Same isolation boundary as
    sector_fundamentals.py -- see claude/sector-agent.md; the Trade Agent /
    Agent007 pipeline's files are never opened by this script.

CADENCE
    Twice weekly, Tue and Wed evenings ET (see
    .github/workflows/sector_technicals.yml).

SCOPE (2026-10-10)
    Whatever the latest fundamentals capture holds: the top 30 holdings of
    XLF, XLV, XLK and XLP, 120 names. See sector_fundamentals.py.

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
from datetime import date, datetime, timedelta, timezone

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
    "rel_strength_20d", "rel_strength_60d", "adv_usd_20d",
    "atm_iv_30", "call_wall", "put_wall", "wall_fallback", "gamma_flip",
    "net_gex_musd", "schema_version",
]
SCHEMA_VERSION = 4   # 2: atm_oi/atm_spread_pct measured on one monthly expiry
                     # 3: adv_usd_20d added (underlying liquidity)
                     # 4: gamma/IV from the same chain -- atm_iv_30, call_wall,
                     #    put_wall, wall_fallback, gamma_flip, net_gex_musd
STOOQ_MIN_INTERVAL_S = 0.5
# Cboe rate-limits this endpoint. At 0.3s spacing with no retry, 39% of
# names on 2026-10-07 and 72% on 2026-10-08 came back "429 Too Many
# Requests" with no options data at all. 120 names at 1.2s is under three
# minutes, and a 429 is retried after a backoff instead of recorded as empty.
CBOE_MIN_INTERVAL_S = 1.2
CBOE_RETRY_WAITS_S = (5, 15, 45)
EXPECTED_UNIVERSE = 120
# Tradability is read off one expiry: the standard monthly nearest
# TARGET_DTE days out and at least MIN_DTE away, ATM +/- a few strikes.
TARGET_DTE = 30
MIN_DTE = 7
NEAR_CONTRACTS_N = 10   # five strikes, call and put


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
    if len(pairs) < EXPECTED_UNIVERSE * 2 // 3:
        log(logpath, f"WARNING universe is only {len(pairs)} names -- expected "
                     f"~{EXPECTED_UNIVERSE}. Check sector_fundamentals.csv.")
    return pairs


OPTION_SYMBOL_RE = re.compile(r"^(.+?)(\d{6})([CP])(\d{8})$")


def parse_option(opt):
    """Cboe option symbol -> (expiry date, strike), e.g.
    JPM261120C00335000 -> (2026-11-20, 335.0). None on anything unexpected."""
    try:
        m = OPTION_SYMBOL_RE.match(opt["option"])
        if not m:
            return None
        ymd = m.group(2)
        exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
        return exp, int(m.group(4)) / 1000.0
    except (KeyError, ValueError, TypeError):
        return None


def strike_of(opt):
    """Strike in dollars, or None. Kept for callers that only need the strike."""
    parsed = parse_option(opt)
    return parsed[1] if parsed else None


def is_standard_monthly(d):
    """Third-Friday expiry (the Thursday before when Friday is a holiday).
    Monthlies carry most of a single name's open interest; weeklies and
    LEAPS on the same strike are thin and wide."""
    if d.weekday() == 4:
        return 15 <= d.day <= 21
    if d.weekday() == 3:   # holiday-shifted: the Friday after is the third
        return 15 <= (d + timedelta(days=1)).day <= 21
    return False


def pick_expiry(expiries, today):
    """The standard monthly expiry nearest TARGET_DTE days out, at least
    MIN_DTE away. Falls back to any expiry in that window if the chain lists
    no monthly (rare for these names)."""
    live = [e for e in expiries if (e - today).days >= MIN_DTE]
    monthly = [e for e in live if is_standard_monthly(e)]
    pool = monthly or live
    if not pool:
        return None
    return min(pool, key=lambda e: (abs((e - today).days - TARGET_DTE), e))


def tradability_from_chain(spot, options, n_near=NEAR_CONTRACTS_N, today=None):
    """Pure function: (spot, options list) -> (atm_oi, atm_spread_pct).

    Measured on ONE expiry -- the standard monthly nearest 30 days out --
    using the n_near contracts (calls and puts) closest to spot. Until
    2026-10-10 this took the 20 contracts nearest spot across EVERY expiry,
    which mixed in yesterday's expired options (bid 0, ask 0.01), weeklies
    and LEAPS. JPM read 9.8% spread; its November monthly is 4.7%. Across
    the 90 names the old measure failed the liquidity gate on 71, this one
    on about 40.

    Spread is (ask - bid) / mid over two-sided quotes only; a contract with
    no bid has no market to measure. Unit-testable against fixtures."""
    if not options or spot is None:
        return None, None
    today = today or datetime.now(timezone.utc).date()
    parsed = [(o, parse_option(o)) for o in options]
    parsed = [(o, p) for o, p in parsed if p is not None]
    expiry = pick_expiry({p[0] for _, p in parsed}, today)
    if expiry is None:
        return None, None
    same = [(o, p[1]) for o, p in parsed if p[0] == expiry]
    near = [o for o, _ in sorted(same, key=lambda x: abs(x[1] - spot))[:n_near]]
    ois = [o.get("open_interest", 0) or 0 for o in near]
    widths = []
    for o in near:
        b, a = o.get("bid"), o.get("ask")
        if b is not None and a is not None and b > 0 and a > 0:
            widths.append((a - b) / ((a + b) / 2))
    atm_oi = int(np.median(ois)) if ois else None
    atm_spread_pct = float(np.median(widths)) if widths else None
    return atm_oi, atm_spread_pct


# Gamma and IV, computed from the chain this script already downloads, with
# gamma_exposure.py -- the GEX Monitor's own code and default parameters
# (gex_capture.py): contracts <= 30 DTE, walls from net per-strike dollar
# gamma within +/-10% of spot excluding +/-0.4%, flip searched +/-15%. Same
# method, so a wall here means what a wall on the GEX Monitor means.
GEX_MAX_DTE = 30
GEX_WALL_WINDOW = 0.10
GEX_WALL_EXCLUDE = 0.004
GEX_FLIP_WINDOW = 0.15


def gex_from_payload(payload, spot, options_today=None):
    """Cboe payload -> {atm_iv_30, call_wall, put_wall, wall_fallback,
    gamma_flip, net_gex_musd}. Any failure returns all-None rather than
    stopping the run: gamma is context, not a requirement for scoring."""
    empty = {k: None for k in ("atm_iv_30", "call_wall", "put_wall",
                               "wall_fallback", "gamma_flip", "net_gex_musd")}
    try:
        import gamma_exposure as gx
        df, _, _ = gx.parse_chain(payload)
        df = gx.normalize_iv(df, quiet=True)
    except SystemExit:
        return empty
    except Exception:
        return empty
    out = dict(empty)
    try:
        # ATM implied vol on the same monthly expiry the liquidity read uses
        exp_dates = {e.date() for e in df["expiry"]}
        today = options_today or datetime.now(timezone.utc).date()
        target = pick_expiry(exp_dates, today)
        if target is not None:
            m = df[(df["expiry"].dt.date == target) & (df["iv"] > 0)]
            m = m.iloc[(m["strike"] - spot).abs().argsort()[:4]]
            if len(m):
                out["atm_iv_30"] = float(m["iv"].median())

        chain = df[df["dte"] <= GEX_MAX_DTE]
        if chain.empty:
            return out
        lo, hi = spot * (1 - GEX_WALL_WINDOW), spot * (1 + GEX_WALL_WINDOW)
        win = chain[(chain["strike"] >= lo) & (chain["strike"] <= hi)]
        full_ps, _, _ = gx.gex_by_strike(chain, spot)
        out["net_gex_musd"] = float(full_ps.sum()) / 1e6
        if not win.empty:
            _, calls, puts = gx.gex_by_strike(win, spot)
            cw, pw, fb = gx.find_walls(calls, puts, spot, GEX_WALL_EXCLUDE)
            out["call_wall"] = float(cw) if cw is not None else None
            out["put_wall"] = float(pw) if pw is not None else None
            out["wall_fallback"] = int(fb)
        _, _, flip = gx.gamma_profile(chain, spot * (1 - GEX_FLIP_WINDOW),
                                      spot * (1 + GEX_FLIP_WINDOW), points=121)
        out["gamma_flip"] = flip
    except Exception:
        pass
    return out


def fetch_cboe_tradability(symbol, logpath):
    prefix = "_" if symbol.upper() in CASH_INDEX_SYMBOLS else ""
    url = CBOE_URL.format(sym=f"{prefix}{symbol.upper()}")
    payload = None
    for attempt, wait in enumerate((0,) + CBOE_RETRY_WAITS_S):
        time.sleep(CBOE_MIN_INTERVAL_S + wait)
        try:
            resp = requests.get(url, timeout=20)
            if resp.status_code == 429 and attempt < len(CBOE_RETRY_WAITS_S):
                log(logpath, f"{symbol}: Cboe 429, retrying in "
                             f"{CBOE_RETRY_WAITS_S[attempt]}s")
                continue
            resp.raise_for_status()
            payload = resp.json()
            break
        except Exception as e:
            log(logpath, f"{symbol}: Cboe fetch failed -- {e}")
            return None, None, None, 0, {}
    if payload is None:
        log(logpath, f"{symbol}: Cboe still rate-limited after "
                     f"{len(CBOE_RETRY_WAITS_S)} retries -- options data left empty")
        return None, None, None, 0, {}

    data = payload.get("data", {})
    spot = data.get("current_price")
    options = data.get("options", [])
    if not options or spot is None:
        log(logpath, f"{symbol}: Cboe payload empty -- refused")
        return None, None, None, 0, {}

    atm_oi, atm_spread_pct = tradability_from_chain(spot, options)
    return spot, atm_oi, atm_spread_pct, len(options), gex_from_payload(payload, spot)


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


def fetch_history_yf(symbols, logpath, chunk=100, period="1y", adv_out=None):
    """Daily closes for many symbols at once. Returns {symbol: [closes]}.

    If adv_out is a dict it is filled with {symbol: median daily dollar
    volume over the last 20 sessions} -- the underlying-liquidity reading
    sector_candidates.py uses for its stock-only table.

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
            if adv_out is not None:
                try:
                    sub = df[ysym] if len(mapped) > 1 else df
                    dv = (sub["Close"] * sub["Volume"]).dropna().tail(20)
                    if len(dv) >= 10:
                        adv_out[orig] = float(dv.median())
                except Exception:
                    pass
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


def capture_one(ticker, sector_etf, sector_mom, logpath, closes=None, run_ts=None,
                adv_usd=None):
    spot, atm_oi, atm_spread_pct, n_contracts, gexd = fetch_cboe_tradability(ticker, logpath)
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
        # One timestamp per run, as in sector_fundamentals.py -- per-row
        # stamps left sector_candidates.py matching a single row.
        "capture_ts": run_ts or datetime.now(timezone.utc).isoformat(),
        "sector_etf": sector_etf, "ticker": ticker, "spot": spot,
        "atm_oi": atm_oi, "atm_spread_pct": atm_spread_pct, "chain_contracts": n_contracts,
        "mom_20d": mom20, "mom_60d": mom60,
        "sector_mom_20d": smom20, "sector_mom_60d": smom60,
        "rel_strength_20d": rel20, "rel_strength_60d": rel60,
        "adv_usd_20d": adv_usd,
        "atm_iv_30": gexd.get("atm_iv_30"), "call_wall": gexd.get("call_wall"),
        "put_wall": gexd.get("put_wall"), "wall_fallback": gexd.get("wall_fallback"),
        "gamma_flip": gexd.get("gamma_flip"), "net_gex_musd": gexd.get("net_gex_musd"),
        "schema_version": SCHEMA_VERSION,
    }


def append_rows(out_path, rows):
    if not rows:
        return
    df = pd.DataFrame(rows, columns=TECH_COLUMNS)
    if os.path.exists(out_path):
        old = pd.read_csv(out_path, dtype=str, keep_default_na=False)
        if list(old.columns) != TECH_COLUMNS:
            # Column added (schema bump): rewrite once with the new header;
            # older rows keep blanks in the new column.
            pd.concat([old, df.astype(str).replace({"None": "", "nan": ""})],
                      ignore_index=True) \
              .reindex(columns=TECH_COLUMNS).to_csv(out_path, index=False)
            return
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
        adv = {}
        hist = fetch_history_yf([t for t, _ in pairs] + etfs, logpath, adv_out=adv)
    else:
        hist, adv = {}, {}

    sector_moms = {}
    for e in etfs:
        ec = hist.get(e) if hist else fetch_stooq_history(e, logpath)
        sector_moms[e] = (momentum(ec, 20), momentum(ec, 60))
        if ec is None:
            log(logpath, f"{e}: no sector history -- relative momentum will be null")

    rows = []
    run_ts = datetime.now(timezone.utc).isoformat()
    for ticker, sector_etf in pairs:
        # [] not None when yfinance is the source: None means "go fetch it
        # yourself", which would send every name Yahoo missed straight back
        # to the dead Stooq path. [] means "no history", and momentum()
        # returns None for it, which is the honest answer.
        row = capture_one(ticker, sector_etf, sector_moms[sector_etf], logpath,
                          closes=hist.get(ticker, []) if args.source == "yfinance"
                          else None, run_ts=run_ts,
                          adv_usd=adv.get(ticker) if args.source == "yfinance" else None)
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
