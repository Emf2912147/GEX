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

OPTIONS LIQUIDITY -- an eligibility screen, not a gate (2026-10-10)
    A name whose options are illiquid -- atm_spread_pct > 0.10 or
    atm_oi < 100, measured on the monthly expiry nearest 30 days (see
    sector_technicals.py) -- is removed before scoring and can be neither
    the long nor the short. Until 2026-10-10 it was a quality gate, which
    made illiquid names SHORT candidates: MRSH was the XLF short with 20
    contracts of open interest and a 16% spread. A name with no liquidity
    reading at all is removed too.

QUALITY GATES (Eugenio's stated screening criteria, ways-of-working.md)
    Applied to the liquid names only. A name trips a gate if ANY of:
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

UNDERLYING LIQUIDITY (2026-10-10)
    A name whose OPTIONS are illiquid can still be traded in the stock. It
    goes to the stock-only table if its median daily dollar volume over the
    last 20 sessions (adv_usd_20d, from sector_technicals.py) is at least
    MIN_ADV_USD. Below that, or with no volume reading, it is in neither table.

OUTPUT -- two ranked tables plus the original pair file
    sector_candidates_options.csv : names with liquid OPTIONS.
    sector_candidates_stock.csv   : names with illiquid options but a liquid
                                    underlying -- trade the shares.
    Every row carries a trade quality score (0-100, graded A-D) -- see the
    "Trade quality score" block above trade_quality() for how it is built.
    Each holds, per sector, up to TOP_K longs (highest composite score among
    names passing every quality gate) and up to TOP_K shorts (lowest score
    among names failing a gate; the lowest overall if none fails), one row
    per name with its rank, score, failed gates and liquidity readings.
    The two tables never share a name.

    sector_candidates.csv keeps its one-pair-per-sector format -- it is the
    #1 long and #1 short of the options table -- so anything already
    reading it is unaffected. Its detail:
    One row per sector, from the liquid names only: the long pick (highest
    composite score among names that pass every quality gate; blank if none
    does) and the short pick (lowest composite score among names that fail
    a gate, or the lowest overall if every liquid name passes; blank only if
    no name in the sector is liquid) -- plus counts (n_names_in_sector,
    n_illiquid, n_scorable, pass/fail) showing how thin the screened set was.
"""
import argparse
import os
import sys
from datetime import datetime, timezone

import pandas as pd

RANKED_COLUMNS = [
    "capture_ts", "table", "sector_etf", "side", "rank", "ticker", "score",
    "failed_gates", "spot", "atm_spread_pct", "atm_oi", "adv_usd_20d",
    "rel_strength_20d", "rel_strength_60d",
    "n_names_in_sector", "n_in_table", "n_quality_passed",
    "trade_quality", "grade", "tq_conviction", "tq_momentum", "tq_execution",
    "tq_event_carry", "dividend_yield", "next_filing_est", "schema_version",
]
# 2: trade quality score and its four components; dividend_yield and
#    next_filing_est carried through for display.
RANKED_SCHEMA_VERSION = 2
TOP_K = 3
MIN_ADV_USD = 50_000_000   # median daily dollar volume, last 20 sessions

CAND_COLUMNS = [
    "capture_ts", "sector_etf", "long_ticker", "long_score",
    "short_ticker", "short_score", "n_names_in_sector", "n_scorable",
    "n_quality_passed", "n_quality_failed", "n_illiquid", "schema_version",
]
# 2: n_scorable column added; long_ticker may be blank (no qualifying long).
# 3: illiquid options exclude a name from both legs; n_illiquid added.
#    n_scorable and the pass/fail counts are over the liquid names only.
SCHEMA_VERSION = 3
MAX_ATM_SPREAD_PCT = 0.10
MIN_ATM_OI = 100
QUALITY_GATE_DAYS_TO_FILING = 14


def log(path, msg):
    line = f"{datetime.now(timezone.utc).isoformat()}Z  {msg}"
    print(line)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def latest_rows(csv_path):
    """Every row of the most recent capture DAY, one per ticker.

    Not an exact match on max(capture_ts). Technicals stamped each row
    separately until 2026-10-10, so an exact match returned one row, the
    merge found no overlap, and this script never produced a pair. Going by
    day works for both stamping styles; if a day holds two runs, the later
    row per ticker wins.
    """
    if not os.path.exists(csv_path):
        return None
    df = pd.read_csv(csv_path)
    if df.empty:
        return None
    day = pd.to_datetime(df["capture_ts"], format="mixed", utc=True).dt.date
    latest = df[day == day.max()].sort_values("capture_ts")
    return latest.drop_duplicates("ticker", keep="last").copy()


def unscorable(row):
    """Why a name cannot be ranked at all, or None.

    Missing data must never win a sector. Before this, a name with no price
    history (VYLR, 2026-10-07 dry run) scored 0 on momentum and an empty
    options chain tripped no liquidity gate -- both read as clean, and the
    emptiest name in the sector came out as the long pick.
    """
    if pd.isna(row.get("mom_20d")) and pd.isna(row.get("mom_60d")):
        return "no_price_history"
    # Missing OPTIONS data no longer makes a name unscorable: is_liquid()
    # treats it as illiquid options, and the name can still qualify for the
    # stock-only table on its underlying volume.
    return None


def quality_gate_failures(row, now=None):
    """Pure function: one merged row -> list of failed-gate reason strings.
    Empty list means the name is clean. now is injectable for testing."""
    now = now or datetime.now(timezone.utc)
    reasons = []

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


def is_liquid(row):
    """True only when both readings exist and pass. Missing data is not
    liquidity -- a name we could not measure is not one to trade."""
    spread, oi = row.get("atm_spread_pct"), row.get("atm_oi")
    return (pd.notna(spread) and pd.notna(oi)
            and spread <= MAX_ATM_SPREAD_PCT and oi >= MIN_ATM_OI)


def is_stock_liquid(row):
    adv = pd.to_numeric(row.get("adv_usd_20d"), errors="coerce")
    return pd.notna(adv) and adv >= MIN_ADV_USD


def table_of(row):
    """'options', 'stock', or None -- which table a name belongs to."""
    if unscorable(row):
        return None
    if is_liquid(row):
        return "options"
    if is_stock_liquid(row):
        return "stock"
    return None


def rank_sides(scored, k=TOP_K):
    """scored: list of (ticker, composite, failures, row). Returns
    (longs, shorts), each a list of up to k entries, best first. Longs must
    pass every gate. Shorts come from gate failures, weakest first; if none
    failed, from the weakest names that are not already longs."""
    clean = sorted((s for s in scored if not s[2]), key=lambda s: -s[1])
    longs = clean[:k]
    flagged = sorted((s for s in scored if s[2]), key=lambda s: s[1])
    if not flagged:
        taken = {s[0] for s in longs}
        flagged = sorted((s for s in scored if s[0] not in taken), key=lambda s: s[1])
    return longs, flagged[:k]


def _num(v, nd=4):
    v = pd.to_numeric(v, errors="coerce")
    return "" if pd.isna(v) else round(float(v), nd)


# --------------------------------------------------------------------------
# Trade quality score (TQS), 0-100
#
# The composite score decides WHICH names are candidates and in what order.
# TQS grades how good each one is AS A TRADE, on four parts:
#   conviction  30  how FAR the composite stands from the sector average, in
#                   standard deviations over every scorable name in the sector
#                   (both tables): +2 sd scores full marks for a long, -2 sd
#                   for a short, the average scores 0. Not a rank -- every
#                   listed name is near the top or bottom by construction, so
#                   a rank would grade them all alike.
#   momentum    25  does price confirm the side? Average 20d/60d return vs the
#                   sector ETF: +10% scores full marks for a long, -10% for a
#                   short, 0% scores half
#   execution   20  how cleanly it can be traded. Options table: ATM spread
#                   (10% -> 0, 2% -> full) and open interest (100 -> 0,
#                   5,000 -> full). Shares table: daily dollar volume
#                   ($50M -> 0, $2B -> full)
#   event/carry 25  starts full; an estimated filing within 14 days costs
#                   half (a binary event inside the trade window), and a
#                   short loses up to half for the dividend it must pay
#                   (8%+ yield costs the full half)
# Grades: A >= 75, B >= 60, C >= 45, D below.
# --------------------------------------------------------------------------
TQS_WEIGHTS = {"conviction": 30, "momentum": 25, "execution": 20, "event_carry": 25}
CONVICTION_FULL_Z = 2.0
GRADES = ((75, "A"), (60, "B"), (45, "C"), (0, "D"))
EVENT_WINDOW_DAYS = 14


def _clamp(x):
    return max(0.0, min(1.0, x))


def days_to_filing(row, now):
    nf = row.get("next_filing_est")
    if not isinstance(nf, str) or not nf:
        return None
    try:
        return (datetime.strptime(nf, "%Y-%m-%d").date() - now.date()).days
    except ValueError:
        return None


def trade_quality(row, side, table, z, now):
    """z: the name's composite in sector standard deviations from the
    sector mean. Returns (total 0-100, grade, {component: 0-1})."""
    import math
    parts = {}
    parts["conviction"] = _clamp((z if side == "long" else -z) / CONVICTION_FULL_Z)

    rels = [v for v in (row.get("rel_strength_20d"), row.get("rel_strength_60d"))
            if pd.notna(v)]
    if rels:
        m = sum(rels) / len(rels)
        parts["momentum"] = _clamp(0.5 + (m if side == "long" else -m) / 0.20)
    else:
        parts["momentum"] = 0.5

    if table == "options":
        spread = pd.to_numeric(row.get("atm_spread_pct"), errors="coerce")
        oi = pd.to_numeric(row.get("atm_oi"), errors="coerce")
        s_part = _clamp((MAX_ATM_SPREAD_PCT - spread) / 0.08) if pd.notna(spread) else 0.0
        o_part = (_clamp(math.log10(oi / MIN_ATM_OI) / math.log10(50))
                  if pd.notna(oi) and oi > 0 else 0.0)
        parts["execution"] = 0.6 * s_part + 0.4 * o_part
    else:
        adv = pd.to_numeric(row.get("adv_usd_20d"), errors="coerce")
        parts["execution"] = (_clamp(math.log10(adv / MIN_ADV_USD) / math.log10(40))
                              if pd.notna(adv) and adv > 0 else 0.0)

    ev = 1.0
    d = days_to_filing(row, now)
    if d is not None and 0 <= d <= EVENT_WINDOW_DAYS:
        ev -= 0.5
    if side == "short":
        dy = pd.to_numeric(row.get("dividend_yield"), errors="coerce")
        if pd.notna(dy) and dy > 0:
            ev -= 0.5 * _clamp(dy / 0.08)
    parts["event_carry"] = _clamp(ev)

    total = sum(TQS_WEIGHTS[k] * v for k, v in parts.items())
    grade = next(g for cut, g in GRADES if total >= cut)
    return round(total, 1), grade, parts


def sector_zscores(grp, now):
    """{ticker: composite z-score} over every scorable name in the sector,
    whichever table it lands in."""
    sc_ = {}
    for _, r in grp.iterrows():
        if unscorable(r):
            continue
        sc_[r["ticker"]] = composite_score(r, quality_gate_failures(r, now=now))
    vals = pd.Series(sc_, dtype=float)
    sd = vals.std(ddof=0) if len(vals) > 1 else 0.0
    if not sd:
        return {t: 0.0 for t in sc_}
    return ((vals - vals.mean()) / sd).to_dict()


def build_ranked(merged, now=None, k=TOP_K):
    """Pure function: merged dataframe -> rows for the two ranked tables."""
    now = now or datetime.now(timezone.utc)
    rows = {"options": [], "stock": []}
    for sector_etf, grp in merged.groupby("sector_etf"):
        zs = sector_zscores(grp, now)
        for table in ("options", "stock"):
            scored = []
            for _, r in grp.iterrows():
                if table_of(r) != table:
                    continue
                fl = quality_gate_failures(r, now=now)
                scored.append((r["ticker"], composite_score(r, fl), fl, r))
            longs, shorts = rank_sides(scored, k)
            n_pass = sum(1 for s in scored if not s[2])
            for side, picks in (("long", longs), ("short", shorts)):
                for i, (t, sc_, fl, r) in enumerate(picks, 1):
                    tq, grade, parts = trade_quality(r, side, table,
                                                     zs.get(t, 0.0), now)
                    rows[table].append({
                        "capture_ts": now.isoformat(), "table": table,
                        "sector_etf": sector_etf, "side": side, "rank": i,
                        "ticker": t, "score": round(sc_, 4),
                        "failed_gates": ";".join(fl),
                        "spot": _num(r.get("spot"), 2),
                        "atm_spread_pct": _num(r.get("atm_spread_pct")),
                        "atm_oi": _num(r.get("atm_oi"), 0),
                        "adv_usd_20d": _num(r.get("adv_usd_20d"), 0),
                        "rel_strength_20d": _num(r.get("rel_strength_20d")),
                        "rel_strength_60d": _num(r.get("rel_strength_60d")),
                        "n_names_in_sector": len(grp), "n_in_table": len(scored),
                        "n_quality_passed": n_pass,
                        "trade_quality": tq, "grade": grade,
                        "tq_conviction": round(parts["conviction"], 3),
                        "tq_momentum": round(parts["momentum"], 3),
                        "tq_execution": round(parts["execution"], 3),
                        "tq_event_carry": round(parts["event_carry"], 3),
                        "dividend_yield": _num(r.get("dividend_yield")),
                        "next_filing_est": r.get("next_filing_est") or "",
                        "schema_version": RANKED_SCHEMA_VERSION,
                    })
    return rows


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
    tuples of (ticker, score); either is None when nothing qualifies.

    The long leg must pass every quality gate. If no name in the sector does,
    there is NO long -- the old fallback to "the least-bad name" put longs on
    sectors where nothing passed (XLE 0/21, XLF 0/76 on 2026-10-07), two of
    them with negative scores. The short leg still prefers gate failures and
    falls back to the weakest name overall.
    """
    if not scored:
        return None, None
    clean = [s for s in scored if not s[2]]
    flagged = [s for s in scored if s[2]]

    long_pick = max(clean, key=lambda s: s[1]) if clean else None

    short_pool = flagged if flagged else scored
    short_pick = min(short_pool, key=lambda s: s[1])

    return ((long_pick[0], long_pick[1]) if long_pick else None,
            (short_pick[0], short_pick[1]))


def score_sector(grp, now=None):
    """One sector's merged rows -> list of (ticker, composite, failures)."""
    scored = []
    for _, row in grp.iterrows():
        if unscorable(row) or not is_liquid(row):
            continue
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
        n_failed = sum(1 for s in scored if s[2])
        n_illiquid = sum(1 for _, r in grp.iterrows()
                         if not unscorable(r) and not is_liquid(r))
        out_rows.append({
            "capture_ts": now.isoformat(), "sector_etf": sector_etf,
            "long_ticker": long_pick[0] if long_pick else "",
            "long_score": round(long_pick[1], 4) if long_pick else "",
            "short_ticker": short_pick[0] if short_pick else "",
            "short_score": round(short_pick[1], 4) if short_pick else "",
            "n_names_in_sector": len(grp), "n_scorable": len(scored),
            "n_quality_passed": len(scored) - n_failed, "n_quality_failed": n_failed,
            "n_illiquid": n_illiquid,
            "schema_version": SCHEMA_VERSION,
        })
    return out_rows


def append_rows(out_path, rows, columns=CAND_COLUMNS):
    if not rows:
        return
    df = pd.DataFrame(rows, columns=columns)
    if os.path.exists(out_path):
        old = pd.read_csv(out_path, dtype=str, keep_default_na=False)
        if list(old.columns) != columns:
            # Column set changed (a schema bump). Rewrite once with the new
            # header; older rows keep blanks in the new columns.
            pd.concat([old, df.astype(str)], ignore_index=True) \
              .reindex(columns=columns).to_csv(out_path, index=False)
            return
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
    for r in out_rows:
        log(logpath, f"{r['sector_etf']}: long {r['long_ticker'] or '(none qualifies)'}  "
                     f"short {r['short_ticker'] or '(none liquid)'}  -- {r['n_illiquid']} illiquid excluded, "
                     f"{r['n_scorable']} liquid and scorable, "
                     f"{r['n_quality_passed']} passed the gates")
    ranked = build_ranked(merged)
    for table, rows in ranked.items():
        for sector in sorted({r["sector_etf"] for r in merged.to_dict("records")}):
            sel = [r for r in rows if r["sector_etf"] == sector]
            lg = ",".join(f"{r['ticker']}({r['grade']})" for r in sel
                          if r["side"] == "long") or "-"
            sh = ",".join(f"{r['ticker']}({r['grade']})" for r in sel
                          if r["side"] == "short") or "-"
            n = sel[0]["n_in_table"] if sel else 0
            log(logpath, f"[{table}] {sector}: {n} names  long {lg}  short {sh}")
    log(logpath, f"=== candidates: {len(out_rows)} sector pairs, "
                 f"{len(ranked['options'])} options-table rows, "
                 f"{len(ranked['stock'])} stock-table rows ===")

    if args.dry_run:
        print(pd.DataFrame(out_rows).to_string())
        for table, rows in ranked.items():
            print(f"\n[{table}]")
            print(pd.DataFrame(rows, columns=RANKED_COLUMNS)
                  [["sector_etf", "side", "rank", "ticker", "score", "trade_quality",
                    "grade", "tq_conviction", "tq_momentum", "tq_execution",
                    "tq_event_carry"]].to_string())
        return 0

    out_path = os.path.join(args.outdir, "sector_candidates.csv")
    append_rows(out_path, out_rows)
    for table, rows in ranked.items():
        append_rows(os.path.join(args.outdir, f"sector_candidates_{table}.csv"),
                    rows, RANKED_COLUMNS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
