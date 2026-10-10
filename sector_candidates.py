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

VALUATION (2026-10-10)
    Two uses, both sector-relative except the floor:
    1. BELOW-BOOK FLOOR -- Eugenio's rule: a stock trading below book value
       (price_to_book < SHORT_MIN_PB = 1.0) is NOT a short candidate. Deep
       value tends to range rather than fall further, so the downside left to
       capture is small. It can still be a long if it passes every gate.
       Names with no P/B reading (negative book equity, missing data) are
       not excluded.
    2. VALUE SCORE -- value_z, how cheap a name is against its own sector,
       in standard deviations (positive = cheaper), clipped to +/-2:
         XLF            : price to book (EV/EBITDA does not apply to banks)
         other sectors  : average of EV/EBITDA (lower = cheaper) and free
                          cash flow yield (higher = cheaper)
       It enters the composite at VALUE_WEIGHT (0.25 per sd), below quality
       (2x) per the "quality wins" rule: a cheap name whose fundamentals are
       deteriorating still ranks low, and an expensive one is pushed down.
    3. DEEP VALUE, IMPROVING -- Eugenio's rule: a deep-value stock that is
       improving should score higher as a long. A name is deep value if it
       trades below book or value_z >= DEEP_VALUE_Z (1 sd cheaper than its
       sector), and improving if at least IMPROVING_MIN of these hold:
       operating margin above the prior year, gross margin above the prior
       year, revenue growing, price beating the sector ETF (avg 20d/60d).
       Such a name gets VALUE_TURN_BONUS added to its composite and, like a
       below-book name, can never be a short. A cheap
       name that is NOT improving gets nothing extra -- cheapness alone is a
       value trap risk, not a long signal.

UNDERLYING LIQUIDITY (2026-10-10)
    A name whose OPTIONS are illiquid can still be traded in the stock. It
    goes to the stock-only table if its median daily dollar volume over the
    last 20 sessions (adv_usd_20d, from sector_technicals.py) is at least
    MIN_ADV_USD. Below that, or with no volume reading, it is in neither table.

WATCHLIST (2026-10-10)
    Names Eugenio always wants scored, whatever their liquidity: CPB (he
    works for Campbell's). Each is written every run to
    sector_candidates_watch.csv with its full score, grade, failed gates,
    valuation and its rank among every scorable name in its sector. The
    liquidity screens are NOT bypassed for the ranked tables -- a watch name
    appears there only if it qualifies on its own; the watch file is where
    it is always visible. Its "table" column reads watch-options,
    watch-stock or watch-illiquid, so the liquidity status is never hidden.
    Must also be captured: see EXTRA_TICKERS in sector_fundamentals.py.

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
    "tq_event_carry", "dividend_yield", "next_filing_est",
    "price_to_book", "ev_to_ebitda", "fcf_yield", "forward_pe", "value_z",
    "value_turn",
    "atm_iv_30", "call_wall", "put_wall", "wall_fallback", "gamma_flip",
    "gamma_regime", "to_put_wall", "to_call_wall", "entry", "fund_basis",
    "schema_version",
]
# 2: trade quality score and its four components; dividend_yield and
#    next_filing_est carried through for display.
# 3: valuation columns; composite includes value_z; below-book names are
#    never shorts.
# 4: gamma context and entry read from call/put walls; fund_basis.
RANKED_SCHEMA_VERSION = 4
# Within this distance of a wall, price is "at" it.
WALL_NEAR = 0.02
WATCHLIST = {"CPB"}
SHORT_MIN_PB = 1.0
# A P/B below this is a data error, not a valuation. Yahoo reports BRK.B at
# ~0.0007x (book per share is in class-A units), which put Berkshire on the
# below-book list on 2026-10-10. Such readings are treated as missing.
MIN_VALID_PB = 0.2
VALUE_WEIGHT = 0.25
VALUE_Z_CLIP = 2.0
BOOK_VALUE_SECTORS = {"XLF"}
DEEP_VALUE_Z = 1.0
IMPROVING_MIN = 2
VALUE_TURN_BONUS = 0.5    # composite points; one failed gate costs 1.0
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


def shortable(row):
    """False for a stock trading below book value, or for a deep-value name
    that is improving (see VALUATION above) -- both are long-side setups."""
    pb = pd.to_numeric(row.get("price_to_book"), errors="coerce")
    if pd.notna(pb) and 0 < pb < SHORT_MIN_PB:
        return False
    return not value_turn(row)


def _zcol(s):
    s = pd.to_numeric(s, errors="coerce")
    sd = s.std(ddof=0)
    if s.notna().sum() < 3 or not sd:
        return pd.Series(float("nan"), index=s.index)
    return (s - s.mean()) / sd


def add_value_z(merged):
    """Adds value_z per sector: positive = cheaper than the sector. Uses
    log(P/B) and log(EV/EBITDA) so one extreme multiple cannot dominate."""
    import numpy as np
    out = merged.copy()
    out["value_z"] = 0.0
    for sector, idx in out.groupby("sector_etf").groups.items():
        g = out.loc[idx]
        if sector in BOOK_VALUE_SECTORS:
            pb = pd.to_numeric(g.get("price_to_book"), errors="coerce")
            parts = [-_zcol(np.log(pb.where(pb > 0)))]
        else:
            ev = pd.to_numeric(g.get("ev_to_ebitda"), errors="coerce")
            parts = [-_zcol(np.log(ev.where(ev > 0))),
                     _zcol(g.get("fcf_yield"))]
        z = pd.concat(parts, axis=1).mean(axis=1, skipna=True)
        out.loc[idx, "value_z"] = z.fillna(0.0).clip(-VALUE_Z_CLIP, VALUE_Z_CLIP)
    return out


def improving_signals(row):
    """List of the improvement signals a name shows (see VALUATION 3)."""
    out = []
    def up(a, b):
        a = pd.to_numeric(row.get(a), errors="coerce")
        b = pd.to_numeric(row.get(b), errors="coerce")
        return pd.notna(a) and pd.notna(b) and a > b
    if up("operating_margin", "operating_margin_prior"):
        out.append("op margin up")
    if up("gross_margin", "gross_margin_prior"):
        out.append("gross margin up")
    g = pd.to_numeric(row.get("revenue_growth_yoy"), errors="coerce")
    if pd.notna(g) and g > 0:
        out.append("revenue growing")
    if score_momentum(row) > 0:
        out.append("beating sector")
    return out


def is_deep_value(row):
    pb = pd.to_numeric(row.get("price_to_book"), errors="coerce")
    vz = pd.to_numeric(row.get("value_z"), errors="coerce")
    return bool((pd.notna(pb) and 0 < pb < SHORT_MIN_PB)
                or (pd.notna(vz) and vz >= DEEP_VALUE_Z))


def value_turn(row):
    """True for a deep-value name that is improving."""
    return is_deep_value(row) and len(improving_signals(row)) >= IMPROVING_MIN


def rank_sides(scored, k=TOP_K):
    """scored: list of (ticker, composite, failures, row). Returns
    (longs, shorts), each a list of up to k entries, best first. Longs must
    pass every gate. Shorts come from gate failures, weakest first; if none
    failed, from the weakest names that are not already longs."""
    clean = sorted((s for s in scored if not s[2]), key=lambda s: -s[1])
    longs = clean[:k]
    can_short = [s for s in scored if shortable(s[3])]
    flagged = sorted((s for s in can_short if s[2]), key=lambda s: s[1])
    if not flagged:
        taken = {s[0] for s in longs}
        flagged = sorted((s for s in can_short if s[0] not in taken), key=lambda s: s[1])
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
                        "price_to_book": _num(r.get("price_to_book"), 2),
                        "ev_to_ebitda": _num(r.get("ev_to_ebitda"), 2),
                        "fcf_yield": _num(r.get("fcf_yield")),
                        "forward_pe": _num(r.get("forward_pe"), 2),
                        "value_z": _num(r.get("value_z"), 3),
                        "value_turn": ";".join(improving_signals(r)) if value_turn(r) else "",
                        **gamma_fields(r, side),
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
    """Quality weighted 2x, plus momentum, plus valuation at VALUE_WEIGHT per
    sector sd -- per Eugenio's stated rule that quality wins when they
    disagree and valuation/momentum serve as a check. value_z is set by
    add_value_z(); a row without it contributes 0."""
    vz = pd.to_numeric(row.get("value_z"), errors="coerce")
    vz = 0.0 if pd.isna(vz) else float(vz)
    bonus = VALUE_TURN_BONUS if value_turn(row) else 0.0
    return (2.0 * score_quality(row, failures) + score_momentum(row)
            + VALUE_WEIGHT * vz + bonus)


def pick_pair(scored, no_short=frozenset()):
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

    can_short = [s for s in scored if s[0] not in no_short]
    flagged_ok = [s for s in can_short if s[2]]
    short_pool = flagged_ok if flagged_ok else can_short
    short_pick = min(short_pool, key=lambda s: s[1]) if short_pool else None

    return ((long_pick[0], long_pick[1]) if long_pick else None,
            (short_pick[0], short_pick[1]) if short_pick else None)


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
        no_short = {r["ticker"] for _, r in grp.iterrows() if not shortable(r)}
        long_pick, short_pick = pick_pair(scored, no_short)
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


def entry_read(row, side):
    """Where price sits between its gamma walls, from the trade's side.
    Walls come from sector_technicals.py (the GEX Monitor's method). The
    put wall is where dealer hedging tends to support price, the call wall
    where it tends to cap it. Returns (to_put_wall, to_call_wall, entry,
    regime): distances as fractions of spot (put wall below -> negative),
    entry one of:
      long  -- 'at support'   within WALL_NEAR above the put wall: entry zone
               'near cap'     within WALL_NEAR below the call wall: little room,
                              wait for a pullback
               'mid-range'    otherwise
      short -- 'at resistance' within WALL_NEAR below the call wall
               'near support'  within WALL_NEAR above the put wall: wait for
                               a bounce
               'mid-range'
    '' when the walls are missing (no liquid chain)."""
    spot = pd.to_numeric(row.get("spot"), errors="coerce")
    cw = pd.to_numeric(row.get("call_wall"), errors="coerce")
    pw = pd.to_numeric(row.get("put_wall"), errors="coerce")
    flip = pd.to_numeric(row.get("gamma_flip"), errors="coerce")
    regime = ""
    if pd.notna(spot) and pd.notna(flip):
        regime = "positive" if spot >= flip else "negative"
    if pd.isna(spot) or spot <= 0 or (pd.isna(cw) and pd.isna(pw)):
        return None, None, "", regime
    dp = (pw / spot - 1) if pd.notna(pw) else None
    dc = (cw / spot - 1) if pd.notna(cw) else None
    if side == "long":
        if dp is not None and -WALL_NEAR <= dp <= 0:
            e = "at support"
        elif dc is not None and 0 <= dc <= WALL_NEAR:
            e = "near cap"
        else:
            e = "mid-range"
    else:
        if dc is not None and 0 <= dc <= WALL_NEAR:
            e = "at resistance"
        elif dp is not None and -WALL_NEAR <= dp <= 0:
            e = "near support"
        else:
            e = "mid-range"
    return dp, dc, e, regime


def gamma_fields(r, side):
    dp, dc, e, regime = entry_read(r, side)
    return {
        "atm_iv_30": _num(r.get("atm_iv_30")),
        "call_wall": _num(r.get("call_wall"), 2),
        "put_wall": _num(r.get("put_wall"), 2),
        "wall_fallback": _num(r.get("wall_fallback"), 0),
        "gamma_flip": _num(r.get("gamma_flip"), 2),
        "gamma_regime": regime,
        "to_put_wall": "" if dp is None else round(dp, 4),
        "to_call_wall": "" if dc is None else round(dc, 4),
        "entry": e,
        "fund_basis": r.get("fund_basis") or "",
    }


def build_watch(merged, now=None):
    """One row per WATCHLIST name present in the data, always -- liquidity
    exempt. side is the side its scores point to: 'long' if it passes every
    gate, 'short' if it fails one and may be shorted, 'none' if it fails one
    but is protected from the short side (below book, or deep value and
    improving). rank = position among every scorable name in its sector."""
    now = now or datetime.now(timezone.utc)
    out = []
    for sector_etf, grp in merged.groupby("sector_etf"):
        watch = grp[grp["ticker"].isin(WATCHLIST)]
        if watch.empty:
            continue
        zs = sector_zscores(grp, now)
        comps, fails = {}, {}
        for _, r in grp.iterrows():
            if not unscorable(r):
                fails[r["ticker"]] = quality_gate_failures(r, now=now)
                comps[r["ticker"]] = composite_score(r, fails[r["ticker"]])
        order = sorted(comps, key=lambda t: -comps[t])
        for _, r in watch.iterrows():
            t = r["ticker"]
            if t not in comps:
                continue
            fl = fails[t]
            natural = table_of(r) or "illiquid"
            side = "long" if not fl else ("short" if shortable(r) else "none")
            tq, grade, parts = trade_quality(
                r, side if side != "none" else "long",
                natural if natural != "illiquid" else "stock", zs.get(t, 0.0), now)
            if side == "none":
                tq, grade = "", ""
            out.append({
                "capture_ts": now.isoformat(), "table": f"watch-{natural}",
                "sector_etf": sector_etf, "side": side,
                "rank": order.index(t) + 1, "ticker": t,
                "score": round(comps[t], 4), "failed_gates": ";".join(fl),
                "spot": _num(r.get("spot"), 2),
                "atm_spread_pct": _num(r.get("atm_spread_pct")),
                "atm_oi": _num(r.get("atm_oi"), 0),
                "adv_usd_20d": _num(r.get("adv_usd_20d"), 0),
                "rel_strength_20d": _num(r.get("rel_strength_20d")),
                "rel_strength_60d": _num(r.get("rel_strength_60d")),
                "n_names_in_sector": len(grp), "n_in_table": len(order),
                "n_quality_passed": sum(1 for x in order if not fails[x]),
                "trade_quality": tq, "grade": grade,
                "tq_conviction": round(parts["conviction"], 3),
                "tq_momentum": round(parts["momentum"], 3),
                "tq_execution": round(parts["execution"], 3),
                "tq_event_carry": round(parts["event_carry"], 3),
                "dividend_yield": _num(r.get("dividend_yield")),
                "next_filing_est": r.get("next_filing_est") or "",
                "price_to_book": _num(r.get("price_to_book"), 2),
                "ev_to_ebitda": _num(r.get("ev_to_ebitda"), 2),
                "fcf_yield": _num(r.get("fcf_yield")),
                "forward_pe": _num(r.get("forward_pe"), 2),
                "value_z": _num(r.get("value_z"), 3),
                "value_turn": ";".join(improving_signals(r)) if value_turn(r) else "",
                **gamma_fields(r, side if side in ("long", "short") else "long"),
                "schema_version": RANKED_SCHEMA_VERSION,
            })
    return out


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

    pb_all = pd.to_numeric(merged.get("price_to_book"), errors="coerce")
    bad_pb = merged[(pb_all > 0) & (pb_all < MIN_VALID_PB)]
    if not bad_pb.empty:
        log(logpath, "P/B ignored as a data error (< %.1fx): %s" % (
            MIN_VALID_PB, ", ".join(f"{t} {v:.4f}x" for t, v in
                                    zip(bad_pb["ticker"], pb_all[bad_pb.index]))))
        merged.loc[bad_pb.index, "price_to_book"] = float("nan")
    merged = add_value_z(merged)
    pbs = pd.to_numeric(merged["price_to_book"], errors="coerce")
    below_book = merged[(pbs > 0) & (pbs < SHORT_MIN_PB)]
    for sector, g in below_book.groupby("sector_etf"):
        log(logpath, f"{sector}: below book, excluded from shorts -- " +
            ", ".join(f"{t} {pb:.2f}x" for t, pb in
                      zip(g["ticker"], pd.to_numeric(g["price_to_book"]))))
    turns = merged[merged.apply(value_turn, axis=1)]
    for sector, g in turns.groupby("sector_etf"):
        log(logpath, f"{sector}: deep value + improving (+{VALUE_TURN_BONUS}, no short) -- "
            + ", ".join(g["ticker"]))
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
    watch_rows = build_watch(merged)
    for w in watch_rows:
        log(logpath, f"[watch] {w['ticker']} ({w['sector_etf']}, {w['table'][6:]}): "
                     f"{w['side']}  grade {w['grade'] or '-'}  score {w['score']:+.2f}  "
                     f"rank {w['rank']}/{w['n_in_table']}  "
                     f"gates: {w['failed_gates'] or 'all pass'}")
    missing = WATCHLIST - {w["ticker"] for w in watch_rows}
    if missing:
        log(logpath, f"[watch] not in this capture: {', '.join(sorted(missing))}")
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
    append_rows(os.path.join(args.outdir, "sector_candidates_watch.csv"),
                watch_rows, RANKED_COLUMNS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
