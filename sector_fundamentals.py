#!/usr/bin/env python3
"""
Sector Agent -- fundamentals capture (SEC EDGAR + SSGA holdings).

SCOPE
    Feeds the sector long/short screening agent only. Writes exclusively
    under history/sector/. Never reads or writes intraday_state.csv,
    vix_state.csv, daily_metrics.csv, rader_daily.csv, or anything the
    Trade Agent / Agent007 pipeline touches -- see the isolation note in
    claude/sector-agent.md. That boundary is enforced two ways: this script
    literally has no code path that opens any file outside --outdir, and the
    GitHub Actions workflow that runs it stages its commit with
    `git add history/sector` specifically, never `git add -A`.

CADENCE
    Twice weekly (Tue/Fri by default -- see
    .github/workflows/sector_fundamentals.yml). Fundamentals move slowly;
    there is no value refreshing them daily, unlike the technicals capture.

SOURCES (both free, both keyless)
    Universe  : State Street's daily holdings file per SPDR sector ETF --
                https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-<ticker>.xlsx
    Financials: SEC EDGAR companyfacts API (data.sec.gov). U.S. government
                work product -- public domain, free, no redistribution
                restriction. The only conditions are on the client: identify
                yourself via User-Agent and stay under the SEC's 10 req/s
                rate limit (enforced here client-side via SEC_MIN_INTERVAL_S).

WHAT IT COMPUTES
    Per constituent: revenue growth (YoY), gross/operating margin (current
    AND prior period, so "compressing" is a checkable comparison, not an
    assertion), free cash flow (operating cash flow - capex), net debt /
    EBITDA (EBITDA approximated as operating income + D&A), and an
    ESTIMATED next filing date extrapolated from EDGAR's own filing
    cadence. That last field is NOT an earnings calendar -- filings lag
    earnings announcements by days to weeks -- and is labelled an estimate
    everywhere it's consumed (see sector_candidates.py).

KNOWN LIMITS
    XBRL tag names are not fully standardized across filers -- financials
    in particular tag things differently than industrials or REITs. This
    reader tries a short list of common alternate tags per concept (TAGS
    below) and leaves a field blank -- logged, not guessed -- when nothing
    matches. A blank field is an honest answer; a guessed number is not.
    This is a first pass tuned for large-cap SPDR-sector constituents, which
    mostly file standard GAAP tags; smaller or unusual filers may come back
    thin. Check fundamentals_capture.log after the first real run.
"""
import argparse
import io
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import requests

SECTOR_ETFS = {
    "XLE": "Energy", "XLF": "Financials", "XLK": "Technology",
    "XLV": "Health Care", "XLY": "Consumer Discretionary", "XLP": "Consumer Staples",
    "XLI": "Industrials", "XLB": "Materials", "XLU": "Utilities",
    "XLRE": "Real Estate", "XLC": "Communication Services",
}

SSGA_HOLDINGS_URL = "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{sym}.xlsx"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# SEC's fair-access policy requires an identifying User-Agent. Points at the
# repo rather than a personal address -- this file is public.
USER_AGENT = "SectorAgent/1.0 (github.com/emf2912147/GEX)"
SEC_MIN_INTERVAL_S = 0.15   # ~6.6 req/s, safely under the SEC's 10 req/s cap

FUND_COLUMNS = [
    "capture_ts", "sector_etf", "ticker", "cik", "fiscal_year_end",
    "revenue", "revenue_prior", "revenue_growth_yoy",
    "gross_margin", "gross_margin_prior",
    "operating_margin", "operating_margin_prior",
    "fcf", "fcf_prior",
    "total_debt", "cash_and_equiv", "net_debt", "ebitda_approx", "net_debt_to_ebitda",
    "shares_outstanding",
    "next_filing_est", "schema_version",
]
SCHEMA_VERSION = 1

# Common alternate US-GAAP tags per concept, tried in order until one has data.
TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "op_cash_flow": ["NetCashProvidedByUsedInOperatingActivities",
                      "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "total_debt_lt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "total_debt_st": ["LongTermDebtCurrent", "ShortTermBorrowings", "DebtCurrent"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations"],
    "dep_amort": ["DepreciationDepletionAndAmortization",
                  "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortization"],
    "shares": ["CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"],
}


def log(path, msg):
    line = f"{datetime.now(timezone.utc).isoformat()}Z  {msg}"
    print(line)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def sec_get(url, logpath, params=None):
    """Rate-limited GET against an SEC endpoint, with the required User-Agent."""
    time.sleep(SEC_MIN_INTERVAL_S)
    resp = requests.get(url, headers={"User-Agent": USER_AGENT,
                                       "Accept-Encoding": "gzip, deflate"},
                         params=params, timeout=30)
    resp.raise_for_status()
    return resp


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
        # SSGA's daily holdings xlsx carries a few descriptive rows before the
        # real table -- observed at 4 header rows; if the file layout changes
        # this raises and is caught below rather than silently misreading.
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


def load_cik_map(logpath):
    resp = sec_get(SEC_TICKERS_URL, logpath)
    data = resp.json()
    return {row["ticker"].upper(): row["cik_str"] for row in data.values()}


def latest_and_prior(facts_by_tag, unit="USD", form_pref=("10-K", "10-Q")):
    """From one XBRL us-gaap fact block, return (latest_value, prior_value)
    for the most recent two periods of the SAME form/period-length, so a
    YoY comparison never pits a quarter against a full year."""
    if not facts_by_tag or unit not in facts_by_tag.get("units", {}):
        return None, None
    rows = [r for r in facts_by_tag["units"][unit]
            if r.get("form") in form_pref and r.get("val") is not None and r.get("end")]
    if not rows:
        return None, None
    rows.sort(key=lambda r: r["end"])
    latest_form = rows[-1]["form"]
    same_form = [r for r in rows if r["form"] == latest_form]
    if len(same_form) < 2:
        return (same_form[-1]["val"] if same_form else None), None
    return same_form[-1]["val"], same_form[-2]["val"]


def first_available(facts, concept):
    """Try each alternate tag for a concept until one has usable data."""
    for tag in TAGS[concept]:
        block = facts.get("facts", {}).get("us-gaap", {}).get(tag)
        if block:
            latest, prior = latest_and_prior(block)
            if latest is not None:
                return latest, prior
    return None, None


def estimate_next_filing(cik, logpath):
    """Extrapolate the next likely 10-Q/10-K filing date from filing cadence.
    NOT an earnings calendar -- filings lag earnings by days to weeks. Every
    consumer of this field must treat it as an estimate, not a fact."""
    try:
        resp = sec_get(SEC_SUBMISSIONS_URL.format(cik=cik), logpath)
        recent = resp.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        qtrly = sorted(d for f, d in zip(forms, dates) if f in ("10-Q", "10-K"))
        if len(qtrly) < 2:
            return None
        last = datetime.strptime(qtrly[-1], "%Y-%m-%d")
        prev = datetime.strptime(qtrly[-2], "%Y-%m-%d")
        cadence_days = (last - prev).days
        if cadence_days <= 0:
            return None
        est = last + pd.Timedelta(days=cadence_days)
        return est.date().isoformat()
    except Exception as e:
        log(logpath, f"filing-date estimate failed for CIK {cik} -- {e}")
        return None


def capture_one(ticker, cik, sector_etf, logpath):
    try:
        resp = sec_get(SEC_FACTS_URL.format(cik=cik), logpath)
        facts = resp.json()
    except Exception as e:
        log(logpath, f"{ticker}: companyfacts fetch failed -- {e}")
        return None
    return build_row(ticker, cik, sector_etf, facts, estimate_next_filing(cik, logpath))


def build_row(ticker, cik, sector_etf, facts, next_filing):
    """Pure function: facts JSON -> output row. Separated from capture_one
    so it can be unit tested against fixtures with no network involved."""
    revenue, revenue_prior = first_available(facts, "revenue")
    gp, gp_prior = first_available(facts, "gross_profit")
    oi, oi_prior = first_available(facts, "operating_income")
    ocf, ocf_prior = first_available(facts, "op_cash_flow")
    capex, capex_prior = first_available(facts, "capex")
    debt_lt, _ = first_available(facts, "total_debt_lt")
    debt_st, _ = first_available(facts, "total_debt_st")
    cash, _ = first_available(facts, "cash")
    dep_amort, _ = first_available(facts, "dep_amort")
    shares, _ = first_available(facts, "shares")

    gross_margin = (gp / revenue) if gp is not None and revenue else None
    gross_margin_prior = (gp_prior / revenue_prior) if gp_prior is not None and revenue_prior else None
    op_margin = (oi / revenue) if oi is not None and revenue else None
    op_margin_prior = (oi_prior / revenue_prior) if oi_prior is not None and revenue_prior else None
    fcf = (ocf - capex) if ocf is not None and capex is not None else None
    fcf_prior = (ocf_prior - capex_prior) if ocf_prior is not None and capex_prior is not None else None
    total_debt = None
    if debt_lt is not None or debt_st is not None:
        total_debt = (debt_lt or 0) + (debt_st or 0)
    net_debt = (total_debt - cash) if total_debt is not None and cash is not None else None
    ebitda = (oi + dep_amort) if oi is not None and dep_amort is not None else None
    net_debt_to_ebitda = (net_debt / ebitda) if net_debt is not None and ebitda not in (None, 0) else None
    revenue_growth = (revenue / revenue_prior - 1) if revenue is not None and revenue_prior else None

    return {
        "capture_ts": datetime.now(timezone.utc).isoformat(),
        "sector_etf": sector_etf, "ticker": ticker, "cik": cik,
        "fiscal_year_end": facts.get("fiscalYearEnd", ""),
        "revenue": revenue, "revenue_prior": revenue_prior, "revenue_growth_yoy": revenue_growth,
        "gross_margin": gross_margin, "gross_margin_prior": gross_margin_prior,
        "operating_margin": op_margin, "operating_margin_prior": op_margin_prior,
        "fcf": fcf, "fcf_prior": fcf_prior,
        "total_debt": total_debt, "cash_and_equiv": cash, "net_debt": net_debt,
        "ebitda_approx": ebitda, "net_debt_to_ebitda": net_debt_to_ebitda,
        "shares_outstanding": shares,
        "next_filing_est": next_filing, "schema_version": SCHEMA_VERSION,
    }


def append_rows(out_path, rows):
    if not rows:
        return
    df = pd.DataFrame(rows, columns=FUND_COLUMNS)
    header = not os.path.exists(out_path)
    df.to_csv(out_path, mode="a", header=header, index=False)


def main():
    p = argparse.ArgumentParser(description="Sector agent -- fundamentals capture (SEC EDGAR + SSGA).")
    p.add_argument("--outdir", default="history/sector")
    p.add_argument("--sectors", nargs="*", default=list(SECTOR_ETFS.keys()))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    logpath = os.path.join(args.outdir, "fundamentals_capture.log")
    out_path = os.path.join(args.outdir, "sector_fundamentals.csv")

    log(logpath, f"=== fundamentals capture start ({len(args.sectors)} sectors) ===")

    cik_map = load_cik_map(logpath)
    all_rows = []
    seen = set()
    for sym in args.sectors:
        tickers = fetch_sector_holdings(sym, logpath)
        for t in tickers:
            if t in seen:
                continue  # a name can sit in more than one sector fund -- capture it once
            seen.add(t)
            cik = cik_map.get(t)
            if cik is None:
                log(logpath, f"{t}: no CIK match in SEC ticker map -- skipped")
                continue
            row = capture_one(t, cik, sym, logpath)
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
