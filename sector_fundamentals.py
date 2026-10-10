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

SCOPE NARROWED 2026-10-10
    The top 30 holdings by fund weight of four SPDR sector ETFs only --
    XLF, XLV, XLK and XLP (XLP added 2026-10-10), 120 names. Membership is re-read from State Street's
    holdings file on every run, so a name that moves into or out of a
    fund's top 30 is picked up automatically. The 11-sector, ~517-name
    history collected before this date was deleted: the fundamentals run
    had been crashing since 2026-10-06, technicals had lost most of its
    options data to Cboe rate limits, and candidates had never produced a
    single pair.

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
import re
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
    "XLF": "Financials", "XLV": "Health Care", "XLK": "Technology",
    "XLP": "Consumer Staples",
}
# Constituents kept per fund, largest weight first.
TOP_N = 30

# Names captured every run whatever their fund weight, with the sector they
# are scored in. CPB: Eugenio's request (2026-10-10) -- he works for
# Campbell's and wants it scored alongside staples. It sits below XLP's top
# 30, so without this it is never captured. sector_candidates.py also
# exempts these names from the liquidity screens (WATCHLIST there).
EXTRA_TICKERS = {"CPB": "XLP"}

SSGA_HOLDINGS_URL = "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{sym}.xlsx"

FUND_COLUMNS = [
    "capture_ts", "sector_etf", "ticker",
    "revenue", "revenue_prior", "revenue_growth_yoy",
    "gross_margin", "gross_margin_prior",
    "operating_margin", "operating_margin_prior",
    "fcf", "fcf_prior",
    "total_debt", "cash_and_equiv", "net_debt", "ebitda_approx", "net_debt_to_ebitda",
    "shares_outstanding", "dividend_yield",
    "market_cap", "price_to_book", "ev_to_ebitda", "forward_pe", "fcf_yield",
    "fund_basis", "period_end",
    "next_filing_est", "schema_version",
]
# Bumped from 1 -> 2: source changed EDGAR -> Yahoo Finance and the `cik`
# column was dropped (yfinance needs no CIK lookup). A reader that assumes
# the old column set should see this change, not silently misalign columns
# -- the exact failure mode a schema_version bump exists to prevent.
SCHEMA_VERSION = 5
# 5 (2026-10-10): QUARTERLY basis. Revenue growth and the margin gates now
#   come from quarterly statements, not annual ones. Annual figures lagged up
#   to a year: CPB scored "revenue growing" off fiscal 2025 (+6.4%, the Sovos
#   year) after fiscal 2026 had reported -5%. fund_basis records which basis
#   each row used, period_end the latest period it reflects:
#     ttm    -- 8+ quarters: last 4 quarters vs the 4 before (TTM vs TTM)
#     q_yoy  -- 5-7 quarters: latest quarter vs the same quarter a year ago
#     annual -- fewer than 5 usable quarters: the old annual comparison
#   Debt and cash come from the latest quarterly balance sheet.
# 4 (2026-10-10): valuation -- market_cap, price_to_book, ev_to_ebitda,
#   forward_pe, fcf_yield (TTM free cash flow / market cap). Used by
#   sector_candidates.py for the valuation score and the below-book rule.
# 3 (2026-10-10): dividend_yield added -- forward annual dividend / price, as
#   a fraction. Feeds the short-carry part of the trade quality score: a
#   short pays the dividend.

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


def yahoo_symbol(t):
    """Yahoo writes share classes with a dash: BRK.B -> BRK-B, BF.B -> BF-B.
    Without this, BRK.B -- XLF's largest holding -- came back empty on every
    capture. sector_technicals.py has always done the same mapping."""
    return str(t).replace(".", "-")


def top_by_weight(df, ticker_col, n, sym, logpath):
    """The n largest holdings by fund weight, as an ordered list of tickers.

    State Street's file has a Weight column; when it is present the list is
    sorted on it explicitly. If a schema change ever drops it, fall back to
    file order -- the file itself lists holdings largest first -- and say so
    in the log rather than guessing quietly.
    """
    weight_col = next((c for c in df.columns
                       if str(c).strip().lower().startswith("weight")), None)
    df = df.copy()
    df["_t"] = df[ticker_col].astype(str).str.strip().str.upper()
    if weight_col is not None:
        df["_w"] = pd.to_numeric(df[weight_col], errors="coerce")
        df = df.sort_values("_w", ascending=False, kind="stable")
    else:
        log(logpath, f"{sym}: no Weight column in holdings -- taking the first "
                     f"{n} rows in file order")
    return df["_t"].tolist()


def fetch_sector_holdings(sym, logpath, top_n=TOP_N):
    """Return the top_n constituent tickers, by weight, for one SPDR sector ETF."""
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
    df = df[df[ticker_col].notna()]
    tickers = top_by_weight(df, ticker_col, top_n, sym, logpath)
    tickers = [t for t in tickers if t and t.isascii()
               and t not in ("CASH", "CASH_USD", "NET CASH", "USD")]
    # SSGA holdings files carry index-option roots and internal placeholder
    # rows alongside the real constituents -- XASZ6, IXPU6, XARU6, 2682320D
    # and similar, 24 of 532 names. They are not equities, fail every price
    # fetch, and one of them (XASZ6) is the junk row that happened to land
    # last and become the entire technicals universe. A US equity ticker is
    # 1-5 letters, optionally with a class suffix after a dot or dash.
    bad = [t for t in tickers if not re.fullmatch(r"[A-Z]{1,5}([.\-][A-Z]{1,2})?", t)]
    if bad:
        log(logpath, f"{sym}: dropped {len(bad)} non-equity rows -- {bad[:6]}")
    tickers = [t for t in tickers if t not in set(bad)]
    total = len(tickers)
    tickers = tickers[:top_n]
    log(logpath, f"{sym}: top {len(tickers)} of {total} constituents by weight")
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


def quarterly_series(df, concept):
    """yfinance quarterly statement -> pd.Series of one line item, newest
    first, NaNs KEPT so position i is always the i-th most recent quarter.
    None if no candidate label has data."""
    if df is None or not hasattr(df, "empty") or df.empty:
        return None
    for label in ROW_LABELS[concept]:
        if label in df.index:
            row = pd.to_numeric(df.loc[label], errors="coerce")
            if row.notna().any():
                try:
                    row = row.sort_index(ascending=False)
                except TypeError:
                    pass
                return row
    return None


def _qsum(series, start, n=4):
    """Sum of n consecutive quarters from position start; None unless every
    one of them is reported."""
    if series is None or len(series) < start + n:
        return None
    chunk = series.iloc[start:start + n]
    return float(chunk.sum()) if chunk.notna().all() else None


def _qval(series, i):
    if series is None or len(series) <= i:
        return None
    v = series.iloc[i]
    return float(v) if pd.notna(v) else None


def _year_apart(series, i, j):
    """True when quarters i and j are ~one year apart (guards against a
    missing quarter shifting the comparison)."""
    try:
        d = abs((pd.Timestamp(series.index[i]) - pd.Timestamp(series.index[j])).days)
        return 330 <= d <= 400
    except Exception:
        return True


def quarterly_fundamentals(q_fin, q_cf, q_bs):
    """Quarterly-basis revenue, margins, FCF prior, debt and cash.
    Returns a dict (keys as build_row uses them) or None when there are
    fewer than 5 usable revenue quarters."""
    rev = quarterly_series(q_fin, "revenue")
    if rev is None or rev.notna().sum() < 5:
        return None
    gp = quarterly_series(q_fin, "gross_profit")
    oi = quarterly_series(q_fin, "operating_income")
    out = {"period_end": str(pd.Timestamp(rev.index[0]).date())
           if len(rev) else None}

    def ratio(a, b):
        return (a / b) if a is not None and b else None

    r_now, r_prev = _qsum(rev, 0), _qsum(rev, 4)
    if r_now is not None and r_prev is not None and _year_apart(rev, 0, 4):
        out["basis"] = "ttm"
        out["revenue"], out["revenue_prior"] = r_now, r_prev
        out["gross_margin"] = ratio(_qsum(gp, 0), r_now)
        out["gross_margin_prior"] = ratio(_qsum(gp, 4), r_prev)
        out["operating_margin"] = ratio(_qsum(oi, 0), r_now)
        out["operating_margin_prior"] = ratio(_qsum(oi, 4), r_prev)
    else:
        q0, q4 = _qval(rev, 0), _qval(rev, 4)
        if q0 is None or q4 is None or not _year_apart(rev, 0, 4):
            return None
        out["basis"] = "q_yoy"
        out["revenue"], out["revenue_prior"] = q0, q4
        out["gross_margin"] = ratio(_qval(gp, 0), q0)
        out["gross_margin_prior"] = ratio(_qval(gp, 4), q4)
        out["operating_margin"] = ratio(_qval(oi, 0), q0)
        out["operating_margin_prior"] = ratio(_qval(oi, 4), q4)

    ocf = quarterly_series(q_cf, "op_cash_flow")
    capex = quarterly_series(q_cf, "capex")
    o_prev, c_prev = _qsum(ocf, 4), _qsum(capex, 4)
    out["fcf_prior"] = (o_prev + c_prev if c_prev is not None and c_prev < 0
                        else (o_prev - c_prev if o_prev is not None and c_prev is not None
                              else None))
    debt = _qval(quarterly_series(q_bs, "total_debt"), 0)
    if debt is None:
        lt = _qval(quarterly_series(q_bs, "long_term_debt"), 0)
        cur = _qval(quarterly_series(q_bs, "current_debt"), 0)
        debt = (lt or 0) + (cur or 0) if (lt is not None or cur is not None) else None
    out["total_debt"] = debt
    out["cash"] = _qval(quarterly_series(q_bs, "cash"), 0)
    return out


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


def _pos(v):
    """Float if a usable positive number, else None. A negative P/B (negative
    book equity) or EV/EBITDA (negative EBITDA) is not a valuation."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 and f == f else None


def valuation(info, fcf):
    """Valuation fields from Yahoo's .info. Missing or negative values are
    left None rather than guessed -- the scorer treats None as 'no view'."""
    info = info or {}
    mcap = _pos(info.get("marketCap"))
    fcf_yield = None
    if mcap and fcf is not None:
        try:
            fcf_yield = float(fcf) / mcap
        except (TypeError, ValueError):
            fcf_yield = None
    return {
        "market_cap": mcap,
        # < 0.2x is a Yahoo units error (BRK.B reads ~0.0007x), not a value
        "price_to_book": (lambda v: v if v is None or v >= 0.2 else None)(
            _pos(info.get("priceToBook"))),
        "ev_to_ebitda": _pos(info.get("enterpriseToEbitda")),
        "forward_pe": _pos(info.get("forwardPE")),
        "fcf_yield": fcf_yield,
    }


def forward_dividend_yield(info):
    """Forward annual dividend / price, as a fraction (0.075 = 7.5%).

    Computed from dividendRate ($/share/yr) and price rather than read from
    Yahoo's dividendYield, whose units changed between yfinance versions
    (fraction in some, percent in others). Falls back to the trailing
    yield, which Yahoo reports as a fraction. 0.0 for a non-payer whose
    price is known; None when nothing usable is there."""
    info = info or {}
    price = next((info.get(k) for k in ("currentPrice", "regularMarketPrice",
                                       "previousClose") if info.get(k)), None)
    rate = info.get("dividendRate")
    if rate is not None and price:
        try:
            return float(rate) / float(price)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    trailing = info.get("trailingAnnualDividendYield")
    if trailing is not None:
        try:
            t = float(trailing)
            return t if t < 1 else None    # a percent slipped through -- refuse
        except (TypeError, ValueError):
            pass
    return 0.0 if price else None


def _fcf(ocf, capex):
    """Operating cash flow less capital spending. Yahoo reports capex as a
    NEGATIVE number; the old `ocf - capex` added it back and overstated FCF."""
    if ocf is None or capex is None:
        return None
    return ocf + capex if capex < 0 else ocf - capex


def build_row(ticker, sector_etf, info, financials, balance_sheet, cashflow, next_filing,
              run_ts, q_fin=None, q_cf=None, q_bs=None):
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
    fcf_prior = _fcf(ocf_prior, capex_prior)
    if fcf is None:
        fcf = _fcf(ocf, capex)

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

    # Quarterly basis overrides the annual figures wherever it has data --
    # see SCHEMA_VERSION 5.
    basis, period_end = "annual", None
    try:
        dates = list(financials.columns) if financials is not None and not financials.empty else []
        period_end = str(pd.Timestamp(max(dates)).date()) if dates else None
    except Exception:
        period_end = None
    q = quarterly_fundamentals(q_fin, q_cf, q_bs)
    if q:
        basis, period_end = q["basis"], q["period_end"]
        revenue, revenue_prior = q["revenue"], q["revenue_prior"]
        # Current and prior margins are replaced as a PAIR, so a gate never
        # compares a quarterly margin against an annual one. A side the
        # quarters do not report (gross profit is often missing for banks)
        # becomes None, which trips no gate.
        gross_margin, gross_margin_prior = q["gross_margin"], q["gross_margin_prior"]
        operating_margin, operating_margin_prior = (q["operating_margin"],
                                                    q["operating_margin_prior"])
        if q.get("fcf_prior") is not None:
            fcf_prior = q["fcf_prior"]
        if q.get("total_debt") is not None:
            total_debt = q["total_debt"]
        if q.get("cash") is not None:
            cash = q["cash"]

    net_debt = (total_debt - cash) if total_debt is not None and cash is not None else None
    net_debt_to_ebitda = (net_debt / ebitda) if net_debt is not None and ebitda not in (None, 0) else None
    dividend_yield = forward_dividend_yield(info)
    val = valuation(info, fcf)
    revenue_growth = (revenue / revenue_prior - 1) if revenue is not None and revenue_prior else info.get("revenueGrowth")

    return {
        # One timestamp for the WHOLE run, passed in -- not datetime.now()
        # per row. Stamping each row separately gave all 517 rows distinct
        # timestamps ~1.5s apart, so sector_technicals.load_universe(), which
        # selected capture_ts == max(), matched exactly ONE row. That is why
        # every technicals run since 2026-09-12 logged "universe: 1 names"
        # and why candidates has never had anything to score.
        "capture_ts": run_ts,
        "sector_etf": sector_etf, "ticker": ticker,
        "revenue": revenue, "revenue_prior": revenue_prior, "revenue_growth_yoy": revenue_growth,
        "gross_margin": gross_margin, "gross_margin_prior": gross_margin_prior,
        "operating_margin": operating_margin, "operating_margin_prior": operating_margin_prior,
        "fcf": fcf, "fcf_prior": fcf_prior,
        "total_debt": total_debt, "cash_and_equiv": cash, "net_debt": net_debt,
        "ebitda_approx": ebitda, "net_debt_to_ebitda": net_debt_to_ebitda,
        "shares_outstanding": shares, "dividend_yield": dividend_yield,
        **val, "fund_basis": basis, "period_end": period_end,
        "next_filing_est": next_filing, "schema_version": SCHEMA_VERSION,
    }


def capture_one(ticker, sector_etf, logpath, run_ts):
    if yf is None:
        log(logpath, f"{ticker}: yfinance not installed -- skipped")
        return None
    time.sleep(YF_MIN_INTERVAL_S)
    try:
        t = yf.Ticker(yahoo_symbol(ticker))
        info = t.info
        financials = t.financials
        balance_sheet = t.balance_sheet
        cashflow = t.cashflow
    except Exception as e:
        log(logpath, f"{ticker}: yfinance fetch failed -- {e}")
        return None
    try:
        q_fin = t.quarterly_financials
        q_cf = t.quarterly_cashflow
        q_bs = t.quarterly_balance_sheet
    except Exception as e:
        log(logpath, f"{ticker}: quarterly statements unavailable, annual basis -- {e}")
        q_fin = q_cf = q_bs = None
    next_filing = estimate_next_earnings(t, logpath, ticker)
    # run_ts must reach build_row. The 2026-10-06 change passed it as far as
    # here and stopped, so every run since crashed on its first ticker with
    # NameError: name 'run_ts' is not defined.
    return build_row(ticker, sector_etf, info, financials, balance_sheet, cashflow,
                     next_filing, run_ts, q_fin, q_cf, q_bs)


def append_rows(out_path, rows):
    if not rows:
        return
    df = pd.DataFrame(rows, columns=FUND_COLUMNS)
    if os.path.exists(out_path):
        old = pd.read_csv(out_path, dtype=str, keep_default_na=False)
        if list(old.columns) != FUND_COLUMNS:
            # Column added (schema bump): rewrite once with the new header;
            # older rows keep blanks in the new column.
            pd.concat([old, df.astype(str).replace({"None": "", "nan": ""})],
                      ignore_index=True) \
              .reindex(columns=FUND_COLUMNS).to_csv(out_path, index=False)
            return
    header = not os.path.exists(out_path)
    df.to_csv(out_path, mode="a", header=header, index=False)


def main():
    p = argparse.ArgumentParser(description="Sector agent -- fundamentals capture (Yahoo Finance + SSGA).")
    p.add_argument("--outdir", default="history/sector")
    p.add_argument("--sectors", nargs="*", default=list(SECTOR_ETFS.keys()))
    p.add_argument("--top-n", type=int, default=TOP_N,
                   help=f"holdings kept per fund, by weight (default {TOP_N})")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    logpath = os.path.join(args.outdir, "fundamentals_capture.log")
    out_path = os.path.join(args.outdir, "sector_fundamentals.csv")

    log(logpath, f"=== fundamentals capture start ({len(args.sectors)} sectors) ===")

    run_ts = datetime.now(timezone.utc).isoformat()
    all_rows = []
    seen = set()
    for sym in args.sectors:
        tickers = fetch_sector_holdings(sym, logpath, args.top_n)
        for t in tickers:
            if t in seen:
                continue  # a name can sit in more than one sector fund -- capture it once
            seen.add(t)
            row = capture_one(t, sym, logpath, run_ts)
            if row:
                all_rows.append(row)

    for t, sym in EXTRA_TICKERS.items():
        if t in seen or sym not in args.sectors:
            continue   # already a top-N holding this run, or sector not run
        seen.add(t)
        log(logpath, f"{sym}: watchlist name {t} added outside the top {args.top_n}")
        row = capture_one(t, sym, logpath, run_ts)
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
