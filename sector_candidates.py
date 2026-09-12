#!/usr/bin/env python3
"""
Sector Agent -- candidate scoring (join fundamentals + technicals, rank, pair).

SCOPE
    The read layer the sector agent actually consults day to day. Joins the
    latest sector_fundamentals.csv row and the latest sector_technicals.csv
    row per name, applies Eugenio's stated quality gates, ranks within each
    sector, and writes one long/short candidate PAIR per sector to
    history/sector/sector_candidates.csv. Reads only the two files under
    --outdir -- never fetches anything itself, never touches the Trade
    Agent's files. Run this after sector_technicals.py in the same cycle.

QUALITY GATES (Eugenio's stated screening criteria, ways-of-working.md)
    A name trips a gate if ANY of:
      - illiquid chain       : atm_spread_pct > 0.10, or atm_oi < 100
      - negative free cash flow, OR compressing gross/operating margin
        (current period below its own prior-period value)
      - net debt / EBITDA above 3x
      - an ESTIMATED next filing date falls inside the next 14 days --
        a filing-date proxy for "earnings inside the trade window", not an
        official earnings calendar (see sector_fundamentals.py)

    Per Eugenio's stated rule, a name that trips a gate is NOT dropped --
    a failed quality gate makes it a candidate for the SHORT leg rather than
    an elimination. When quality and valuation disagree, quality wins;
    valuation/momentum here acts as a tiebreaker within each side, weighted
    accordingly in the composite score, not as an override of the quality read.

OUTPUT
    One row per sector: the long pick (best clean-quality name, or the least
    -bad name if nothing in the sector is fully clean) and the short pick
    (worst-scoring name among quality-gate failures, or the worst overall
    if nothing failed a gate) -- plus enough context (n_names_in_sector,
    pass/fail counts) to see how thin or thick the sector's screened set was.
"""
import argparse
import os
import sys
from datetime import datetime, timezone

import pandas as pd

CAND_COLUMNS = [
    "capture_ts", "sector_etf", "long_ticker", "long_score",
    "short_ticker", "short_score", "n_names_in_sector",
    "n_quality_passed", "n_quality_failed", "schema_version",
]
SCHEMA_VERSION = 1
QUALITY_GATE_DAYS_TO_FILING = 14


def log(path, msg):
    line = f"{datetime.now(timezone.utc).isoformat()}Z  {msg}"
    print(line)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def latest_rows(csv_path):
    if not os.path.exists(csv_path):
        return None
    df = pd.read_csv(csv_path)
    if df.empty:
        return None
    latest_ts = df["capture_ts"].max()
    return df[df["capture_ts"] == latest_ts].copy()


def quality_gate_failures(row, now=None):
    """Pure function: one merged row -> list of failed-gate reason strings.
    Empty list means the name is clean. now is injectable for testing."""
    now = now or datetime.now(timezone.utc)
    reasons = []

    spread = row.get("atm_spread_pct")
    oi = row.get("atm_oi")
    if pd.notna(spread) and spread > 0.10:
        reasons.append("wide_chain")
    if pd.notna(oi) and oi < 100:
        reasons.append("low_oi")

    fcf = row.get("fcf")
    if pd.notna(fcf) and fcf < 0:
        reasons.append("negative_fcf")

    gm, gmp = row.get("gross_margin"), row.get("gross_margin_prior")
    if pd.notna(gm) and pd.notna(gmp) and gm < gmp:
        reasons.append("compressing_gross_margin")

    om, omp = row.get("operating_margin"), row.get("operating_margin_prior")
    if pd.notna(om) and pd.notna(omp) and om < omp:
        reasons.append("compressing_operating_margin")

    ratio = row.get("net_debt_to_ebitda")
    if pd.notna(ratio) and ratio > 3.0:
        reasons.append("net_debt_over_3x_ebitda")

    nf = row.get("next_filing_est")
    if isinstance(nf, str) and nf:
        try:
            days_out = (datetime.strptime(nf, "%Y-%m-%d").date() - now.date()).days
            if 0 <= days_out <= QUALITY_GATE_DAYS_TO_FILING:
                reasons.append("filing_estimated_in_window")
        except ValueError:
            pass

    return reasons


def score_quality(row, failures):
    """Higher is better. Gate failures penalize rather than zero the score,
    since a failed-gate name still needs a rank among short candidates."""
    parts = []
    if pd.notna(row.get("revenue_growth_yoy")):
        parts.append(row["revenue_growth_yoy"])
    if pd.notna(row.get("operating_margin")):
        parts.append(row["operating_margin"])
    if pd.notna(row.get("net_debt_to_ebitda")):
        parts.append(-row["net_debt_to_ebitda"] / 10.0)   # lower leverage -> higher score
    base = sum(parts) if parts else 0.0
    return base - 0.5 * len(failures)


def score_momentum(row):
    parts = [v for v in (row.get("rel_strength_20d"), row.get("rel_strength_60d")) if pd.notna(v)]
    return sum(parts) / len(parts) if parts else 0.0


def composite_score(row, failures):
    """Quality weighted 2x momentum, per Eugenio's stated rule that quality
    wins when the two disagree and valuation/momentum serves as a check."""
    return 2.0 * score_quality(row, failures) + score_momentum(row)


def pick_pair(scored):
    """scored: list of (ticker, composite, failures). Returns (long, short)
    tuples of (ticker, score) or (None, None) if scored is empty."""
    if not scored:
        return None, None
    clean = [s for s in scored if not s[2]]
    flagged = [s for s in scored if s[2]]

    long_pool = clean if clean else scored
    long_pick = max(long_pool, key=lambda s: s[1])

    short_pool = flagged if flagged else scored
    short_pick = min(short_pool, key=lambda s: s[1])

    return (long_pick[0], long_pick[1]), (short_pick[0], short_pick[1])


def score_sector(grp, now=None):
    """One sector's merged rows -> list of (ticker, composite, failures)."""
    scored = []
    for _, row in grp.iterrows():
        failures = quality_gate_failures(row, now=now)
        scored.append((row["ticker"], composite_score(row, failures), failures))
    return scored


def build_candidates(merged, now=None):
    """Pure function: merged fundamentals+technicals dataframe -> output rows."""
    now = now or datetime.now(timezone.utc)
    out_rows = []
    for sector_etf, grp in merged.groupby("sector_etf"):
        scored = score_sector(grp, now=now)
        long_pick, short_pick = pick_pair(scored)
        if long_pick is None or short_pick is None:
            continue
        n_failed = sum(1 for s in scored if s[2])
        out_rows.append({
            "capture_ts": now.isoformat(), "sector_etf": sector_etf,
            "long_ticker": long_pick[0], "long_score": round(long_pick[1], 4),
            "short_ticker": short_pick[0], "short_score": round(short_pick[1], 4),
            "n_names_in_sector": len(scored),
            "n_quality_passed": len(scored) - n_failed, "n_quality_failed": n_failed,
            "schema_version": SCHEMA_VERSION,
        })
    return out_rows


def append_rows(out_path, rows):
    if not rows:
        return
    df = pd.DataFrame(rows, columns=CAND_COLUMNS)
    header = not os.path.exists(out_path)
    df.to_csv(out_path, mode="a", header=header, index=False)


def main():
    p = argparse.ArgumentParser(description="Sector agent -- score and pair long/short candidates.")
    p.add_argument("--outdir", default="history/sector")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    logpath = os.path.join(args.outdir, "candidates.log")
    fund = latest_rows(os.path.join(args.outdir, "sector_fundamentals.csv"))
    tech = latest_rows(os.path.join(args.outdir, "sector_technicals.csv"))

    if fund is None or tech is None:
        log(logpath, "missing fundamentals or technicals capture -- nothing to score")
        return 1

    merged = tech.merge(fund.drop(columns=["sector_etf"], errors="ignore"),
                         on="ticker", how="inner", suffixes=("", "_fund"))
    if merged.empty:
        log(logpath, "no overlap between fundamentals and technicals universes -- nothing to score")
        return 1

    out_rows = build_candidates(merged)
    log(logpath, f"=== candidates: {len(out_rows)} sector pairs written ===")

    if args.dry_run:
        print(pd.DataFrame(out_rows).to_string())
        return 0

    out_path = os.path.join(args.outdir, "sector_candidates.csv")
    append_rows(out_path, out_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
