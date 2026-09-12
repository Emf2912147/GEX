#!/usr/bin/env python3
"""
Sector Agent -- fundamentals capture (Yahoo Finance + SSGA holdings).

SCOPE
    Feeds the sector long/short screening agent only. Writes exclusively
    under history/sector/. Never reads or writes intraday_state.csv,
    vix_state.csv, daily_metrics.csv, or rader_daily.csv -- see the
    isolation note in claude/sector-agent.md. Enforced two ways: this
    script has no code path that opens a file outside --outdir, and the
    GitHub Actions workflow that runs it stages its commit with
    `git add history/sector` specifically, never `git add -A`.

CADENCE
    Twice weekly (Tue/Fri by default -- see
    .github/workflows/sector_fundamentals.yml). Fundamentals move slowly;
    there is no value refreshing them daily.

SOURCE CHANGE, 2026-09-12: SEC EDGAR -> Yahoo Finance
    The first live run against SEC EDGAR (data.sec.gov / www.sec.gov) was
    rejected with a 403 on the very first request -- before any rate limit
    could have been hit, and after the User-Agent was already fixed to
    match SEC's own documented format. That combination points to an
    IP-range block on GitHub-hosted runners rather than anything fixable
    client-side (SEC has a documented history of blocking cloud-provider IP
    ranges wholesale). Eugenio chose to switch to Yahoo Finance instead,
    explicitly as personal use with no redistribution -- Yahoo's blocking is
    rate/pattern-based rather than a blanket IP ban, so twice-weekly volume
    should not trigger it, but note this is a real trade-off from EDGAR's
    clean public-domain status: Yahoo's terms are more restrictive about
    retaining/republishing their data, accepted here knowingly.

    yfinance (the library used here) scrapes Yahoo's own web endpoints --
    it is not an official, versioned API, and its exact field/row names have
    shifted before when Yahoo changed their site internally. Every field
    below is read defensively: multiple plausible labels are tried in order,
    and a field that matches nothing is left blank (logged, not guessed) --
    the same discipline used for SEC's XBRL tag-name drift in the prior
    version of this script. This version has NOT been run against live data
    (PyPI and Yahoo are both unreachable from the environment this was
    built in) -- the first real Actions run is the actual test. Watch
    fundamentals_capture.log for how many fields come back blank.

SOURCES (both free, no API key)
    Universe    : State Street's daily holdings file per SPDR sector ETF --
                  https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-<ticker>.xlsx
    Financials  : Yahoo Finance via the yfinance library (Ticker.info,
                  .financials, .balance_sheet, .cashflow, .get_earnings_dates).

WHAT IT COMPUTES
    Per constituent: revenue growth (YoY, from annual figures), gross/
    operating margin (current AND prior year, so "compressing" is a
    checkable comparison), free cash flow (prefers Yahoo's own trailing
    figure; falls back to operating cash flow - capex from the annual
    cash-flow statement), net debt / EBITDA, and the next estimated
    earnings date from Yahoo's own earnings calendar -- a real calendar
    now, an improvement over the EDGAR version's filing-cadence estimate,
    though still Yahoo's own estimate and not guaranteed.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:
    yf = None  # import failure surfaces clearly at first use, not at module load

SECTOR_ETFS = {
    "XLE": "Energy", "XLF": "Financials", "XLK": "Technology",
    "XLV": "Health Care", "XLY": "Consumer Discretionary", "XLP": "Consumer Staples",
    "XLI": "Industrials", "XLB": "Materials", "XLU": "Utilities",
    "XLRE": "Real Estate", "XLC": "Communication Services",
}

SSGA_HOLDINGS_URL = "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{sym}.xlsx"

FUND_COLUMNS = [
    "capture_ts", "sector_etf", "ticker",
    "revenue", "revenue_prior", "revenue_growth_yoy",
    "gross_margin", "gross_margin_prior",
    "operating_margin", "operating_margin_prior",
    "fcf", "fcf_prior",
    "total_debt", "cash_and_equiv", "net_debt", "ebitda_approx", "net_debt_to_ebitda",
    "shares_outstanding",
    "next_filing_est", "schema_version",
]
# Bumped from 1 -> 2: source changed EDGAR -> Yahoo Finance and the `cik`
# column was dropped (yfinance needs no CIK lookup). A reader that assumes
# the old column set should see this change, not silently misalign columns
# -- the exact failure mode a schema_version bump exists to prevent.
SCHEMA_VERSION = 2

YF_MIN_INTERVAL_S = 0.5   # spacing between tickers -- twice-weekly volume, no rush

# Candidate row labels per concept, tried in order, on yfinance's annual
# statement DataFrames (columns = fiscal year end, most recent first).
# yfinance has renamed these before between versions -- this list is a
# defense against drift, not a guarantee every label here is current.
ROW_LABELS = {
    "revenue": ["Total Revenue", "TotalRevenue", "Operating Revenue"],
    "gross_profit": ["Gross Profit", "GrossProfit"],
    "operating_income": ["Operating Income", "OperatingIncome", "EBIT"],
    "op_cash_flow": ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities",
                      "Total Cash From Operating Activities"],
    "capex": ["Capital Expenditure", "CapitalExpenditures", "Purchase Of PP&E"],
    "dep_amort": ["Depreciation And Amortization", "Depreciation Amortization Depletion",
                  "Depreciation"],
    "cash": ["Cash And Cash Equivalents", "CashAndCashEquivalents",
             "Cash Cash Equivalents And Short Term Investments"],
    "total_debt": ["Total Debt", "TotalDebt"],
    "long_term_debt": ["Long Term Debt", "LongTermDebt"],
    "current_debt": ["Current Debt", "CurrentDebt", "Short Long Term Debt"],
}


def log(path, msg):
    line = f"{datetime.now(timezone.utc).isoformat()}Z  {msg}"
    print(line)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def fetch_sector_holdings(sym, logpath):
    """Return the list of constituent tickers for one SPDR sector ETF."""
    url = SSGA_HOLDINGS_URL.format(sym=sym.lower())
    try:
        resp = requests.get(url, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0 (SectorAgent/1.0)"})
        resp.raise_for_status()
    except Exception as e:
        log(logpath, f"{sym}: holdings fetch failed -- {e}")
        return []
    try:
        import io
        df = pd.read_excel(io.BytesIO(resp.content), skiprows=4)
    except Exception as e:
        log(logpath, f"{sym}: holdings parse failed -- {e}")
        return []
    ticker_col = next((c for c in df.columns if str(c).strip().lower() in
                        ("ticker", "identifier")), None)
    if ticker_col is None:
        log(logpath, f"{sym}: holdings schema changed -- no ticker column found "
                      f"among {list(df.columns)}")
        return []
    tickers = df[ticker_col].dropna().astype(str).str.strip().str.upper().tolist()
    tickers = [t for t in tickers if t and t.isascii() and t not in ("CASH", "CASH_USD", "NET CASH", "USD")]
    log(logpath, f"{sym}: {len(tickers)} constituents")
    return tickers


def annual_latest_and_prior(df, concept):
    """df: a yfinance annual statement DataFrame (rows=line items, columns=
    fiscal-year-end dates, most recent first). Tries each candidate label
    for `concept` until one has data. Returns (latest, prior) -- prior is
    None if there's only one period. Never raises on a missing/renamed row."""
    if df is None or not hasattr(df, "empty") or df.empty:
        return None, None
    for label in ROW_LABELS[concept]:
        if label in df.index:
            row = df.loc[label].dropna()
            if len(row) == 0:
                continue
            vals = row.tolist()
            return vals[0], (vals[1] if len(vals) > 1 else None)
    return None, None


def estimate_next_earnings(ticker_obj, logpath, ticker):
    """Yahoo's own earnings calendar -- a real calendar, not a proxy, but
    still Yahoo's own estimate and one of yfinance's more fragile calls
    historically. Returns an ISO date string or None; never raises."""
    try:
        dates = ticker_obj.get_earnings_dates(limit=8)
        if dates is None or dates.empty:
            return None
        now = pd.Timestamp.now(tz=dates.index.tz) if dates.index.tz else pd.Timestamp.now()
        future = dates.index[dates.index >= now]
        if len(future) == 0:
            return None
        return future.min().date().isoformat()
    except Exception as e:
        log(logpath, f"{ticker}: earnings-date lookup failed -- {e}")
        return None


def build_row(ticker, sector_etf, info, financials, balance_sheet, cashflow, next_filing):
    """Pure function: yfinance data -> output row. Separated from the
    network calls so it's unit-testable against fixtures."""
    info = info or {}

    revenue, revenue_prior = annual_latest_and_prior(financials, "revenue")
    gp, gp_prior = annual_latest_and_prior(financials, "gross_profit")
    oi, oi_prior = annual_latest_and_prior(financials, "operating_income")
    ocf, ocf_prior = annual_latest_and_prior(cashflow, "op_cash_flow")
    capex, capex_prior = annual_latest_and_prior(cashflow, "capex")
    dep_amort, _ = annual_latest_and_prior(cashflow, "dep_amort")
    cash, _ = annual_latest_and_prior(balance_sheet, "cash")
    total_debt, _ = annual_latest_and_prior(balance_sheet, "total_debt")
    if total_debt is None:
        lt_debt, _ = annual_latest_and_prior(balance_sheet, "long_term_debt")
        cur_debt, _ = annual_latest_and_prior(balance_sheet, "current_debt")
        if lt_debt is not None or cur_debt is not None:
            total_debt = (lt_debt or 0) + (cur_debt or 0)

    # Prefer info's own values where the statement-derived one is missing
    # or where Yahoo's precomputed TTM figure is simply the better number
    # (freeCashflow, ebitda are both TTM in .info, not fiscal-year).
    fcf = info.get("freeCashflow")
    fcf_prior = (ocf_prior - capex_prior) if ocf_prior is not None and capex_prior is not None else None
    if fcf is None and ocf is not None and capex is not None:
        fcf = ocf - capex

    cash = cash if cash is not None else info.get("totalCash")
    total_debt = total_debt if total_debt is not None else info.get("totalDebt")
    shares = info.get("sharesOutstanding")

    ebitda = info.get("ebitda")
    if ebitda is None and oi is not None and dep_amort is not None:
        ebitda = oi + dep_amort

    gross_margin = (gp / revenue) if gp is not None and revenue else info.get("grossMargins")
    gross_margin_prior = (gp_prior / revenue_prior) if gp_prior is not None and revenue_prior else None
    operating_margin = (oi / revenue) if oi is not None and revenue else info.get("operatingMargins")
    operating_margin_prior = (oi_prior / revenue_prior) if oi_prior is not None and revenue_prior else None

    net_debt = (total_debt - cash) if total_debt is not None and cash is not None else None
    net_debt_to_ebitda = (net_debt / ebitda) if net_debt is not None and ebitda not in (None, 0) else None
    revenue_growth = (revenue / revenue_prior - 1) if revenue is not None and revenue_prior else info.get("revenueGrowth")

    return {
        "capture_ts": datetime.now(timezone.utc).isoformat(),
        "sector_etf": sector_etf, "ticker": ticker,
        "revenue": revenue, "revenue_prior": revenue_prior, "revenue_growth_yoy": revenue_growth,
        "gross_margin": gross_margin, "gross_margin_prior": gross_margin_prior,
        "operating_margin": operating_margin, "operating_margin_prior": operating_margin_prior,
        "fcf": fcf, "fcf_prior": fcf_prior,
        "total_debt": total_debt, "cash_and_equiv": cash, "net_debt": net_debt,
        "ebitda_approx": ebitda, "net_debt_to_ebitda": net_debt_to_ebitda,
        "shares_outstanding": shares,
        "next_filing_est": next_filing, "schema_version": SCHEMA_VERSION,
    }


def capture_one(ticker, sector_etf, logpath):
    if yf is None:
        log(logpath, f"{ticker}: yfinance not installed -- skipped")
        return None
    time.sleep(YF_MIN_INTERVAL_S)
    try:
        t = yf.Ticker(ticker)
        info = t.info
        financials = t.financials
        balance_sheet = t.balance_sheet
        cashflow = t.cashflow
    except Exception as e:
        log(logpath, f"{ticker}: yfinance fetch failed -- {e}")
        return None
    next_filing = estimate_next_earnings(t, logpath, ticker)
    return build_row(ticker, sector_etf, info, financials, balance_sheet, cashflow, next_filing)


def append_rows(out_path, rows):
    if not rows:
        return
    df = pd.DataFrame(rows, columns=FUND_COLUMNS)
    header = not os.path.exists(out_path)
    df.to_csv(out_path, mode="a", header=header, index=False)


def main():
    p = argparse.ArgumentParser(description="Sector agent -- fundamentals capture (Yahoo Finance + SSGA).")
    p.add_argument("--outdir", default="history/sector")
    p.add_argument("--sectors", nargs="*", default=list(SECTOR_ETFS.keys()))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    logpath = os.path.join(args.outdir, "fundamentals_capture.log")
    out_path = os.path.join(args.outdir, "sector_fundamentals.csv")

    log(logpath, f"=== fundamentals capture start ({len(args.sectors)} sectors) ===")

    all_rows = []
    seen = set()
    for sym in args.sectors:
        tickers = fetch_sector_holdings(sym, logpath)
        for t in tickers:
            if t in seen:
                continue  # a name can sit in more than one sector fund -- capture it once
            seen.add(t)
            row = capture_one(t, sym, logpath)
            if row:
                all_rows.append(row)

    log(logpath, f"=== fundamentals capture done: {len(all_rows)}/{len(seen)} names captured ===")

    if args.dry_run:
        print(pd.DataFrame(all_rows).head(20).to_string())
        return 0

    append_rows(out_path, all_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
