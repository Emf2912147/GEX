#!/usr/bin/env python3
"""
Sector support agent -- the arithmetic behind the scheduled trade alert.

Works on any sector the sector pipeline covers (XLK, XLF, XLV, XLP). Reads
only what the repo already stores and writes nothing to it: every mode
prints JSON. Live inputs (daily bars, quotes, option chains, short interest)
are gathered by the Claude session that runs it, through the Robinhood
connector, and passed in as JSON files.

    python sector_support_agent.py etfs   --sector XLK
    python sector_support_agent.py indices --bars-dir bars/
    python sector_support_agent.py rank   --sector XLK --bars-dir bars/ [--short si.json]
    python sector_support_agent.py check  --sector XLK --ticker T --bars b.json --price P
    python sector_support_agent.py score  --sector XLK --ticker T --chain chain.json
    python sector_support_agent.py bear-indices --bars-dir bars/
    python sector_support_agent.py bear-check --ticker T --bars b.json --price P --resistance R --target X
    python sector_support_agent.py bear-score --ticker T --chain puts.json --resistance R --target X
    python sector_support_agent.py quality --side bear|bull --ticker T --daily d.json --hourly h.json
                                   --level L --target X [--index-daily i.json --index-hourly ih.json]

`etfs` lists the ETFs whose daily bars the run must supply in --bars-dir as
<ETF>.json (a list of Robinhood daily bars, the live price appended as the
last bar). Every gate is re-evaluated on every run: a block at one check
says nothing about the next.

REGIME (gate 1 -- judged per name, by the name's own industry)
    Each constituent maps to an industry ETF (INDUSTRY below): XLK semis to
    SMH, software to IGV; XLF banks to KBE, brokers and asset managers to
    IAI, insurers to KIE; XLV biotech to IBB, devices to IHI, managed care
    and providers to IHF, pharma to PPH. Anything unmapped uses the sector
    ETF itself.
    rising     that ETF's last price above its 20-day average AND its 20-day
               change > 0
    stable     dealer gamma positive with spot >= 0.5% above the flip, read
               from the GEX Monitor's latest intraday row -- the industry ETF
               when the monitor captures it (SMH, XLF), else the sector's
               broad proxy (QQQ for XLK, SPY otherwise); VIX term not in
               backwardation; VIX not at a 20-day high. An ETF with fewer
               than MIN_GEX_ROWS captures falls back to the broad proxy.
    A name whose industry fails cannot be the watch name. Other names in the
    sector whose industry passes still can.

RANK (gate 2 -- which name)
    Every constituent with liquid options (ATM spread <= 10%, ATM OI >= 100)
    is scored 0-100 on how likely it is to rise from here:
      technical location 35  closeness to the put wall (best inside 3%),
                             wall strength (sector_candidates.wall_stability),
                             spot above the gamma flip, relative strength
      valuation          25  value_z vs the sector, improvement signals,
                             deep-value-and-improving
      volume / liquidity 15  20d dollar volume, ATM OI, ATM spread
      short interest     15  % of float short -- squeeze fuel at support.
                             Neutral (0.5) when not supplied; flagged.
      event              10  full marks unless earnings fall within 14 days
    Also excluded: a put wall that MOVED last session or is NEW (untested),
    and NO ROOM TO RUN -- the call wall less than MIN_ROOM (2%) above the
    price. CPB is never a trade candidate (employer).

CHECK (gate 3 -- price at support, room still there)
    Fires when price is within BAND (0.25%) of the put wall: any 5-minute
    bar since the last check with low <= wall x 1.0025 and the latest price
    still >= wall x 0.9975 -- and the call wall still >= 2% above the live
    price. A close below the band is a broken level, never traded.

INDICES (the index scan -- runs beside the sector scan)
    Universe: INDEX_UNIVERSE, the GEX Monitor's ETFs. For each, the setup is
    built from the monitor's own graded levels (support_resistance.py):
      support    the nearest A/B-graded support inside +/-1 sigma
      target     the nearest A/B-graded resistance above that support; it
                 must be >= MIN_ROOM (2%) above the support, or "no room"
      regime     its own trend (above the 20-day average, 20d change > 0)
                 and its own dealer gamma (positive, >= 0.5% above the flip;
                 fewer than MIN_GEX_ROWS captures falls back to QQQ for
                 tech ETFs, SPY otherwise), plus the VIX checks
    Score 0-100: support quality 35 (the level's own S/R grade score),
    trend 25, room to target 15, closeness to support 15 (best inside 1.5%),
    volatility 10 (ATM IV30 below its realized-vol-free ceiling of 35%).
    A setup is eligible when regime is favorable and room >= 2%. The same
    check and score modes then run with --put-wall = support and
    --call-wall = target.

BEAR MODE (index scan, negative gamma -- sell the rip)
    When an index ETF is in a BEARISH regime the agent looks for a bear put
    vertical at RESISTANCE instead of a bull call vertical at support.
      regime     falling (last price below its 20-day average AND 20d change
                 < 0) and dealer gamma negative or spot below the flip (same
                 proxy fallback as the bullish scan). VIX is not a gate here:
                 backwardation is consistent with the bear case.
      resistance the nearer of the nearest A/B-graded resistance above the
                 price inside +/-1 sigma and the gamma flip when it sits above
                 the price
      target     the nearest A/B-graded support below the resistance; it
                 must be >= MIN_ROOM (2%) below the resistance, or "no room"
    Score 0-100: resistance quality 35 (S/R grade score; the flip counts as
    60), downtrend 25, room to target 15, closeness to resistance 15 (best
    inside 1.5%), volatility 10. bear-check fires "at_resistance" when a
    5-minute bar's high reached the band (0.25%) and the price is still at
    or below resistance + band; a price above the band is "broken" (the rally
    took the level -- never traded). bear-score ranks bear put verticals:
    long put from ~ATM up to the resistance, short put at or above the
    target, 14-60 DTE, same weights as the bull score.

QUALITY (the phase of the move -- setup_quality.py)
    Run on every setup that reaches at_support / at_resistance, before any
    recommendation. Scores trend (swing structure), extension WITH hourly RSI
    divergence, room in ATRs, valuation against peers (from this repo's
    sector_fundamentals) and the index's condition, 0-100. Two vetoes: never
    a bear setup when oversold (never a bull setup when overbought), and never
    trade an overextended run that has no divergence. RSI levels are each
    symbol's own (10th/90th percentile of its daily RSI history). See
    setup_quality.py for the full rules.

SCORE (the trade)
    Bull call verticals, long strike from the wall up to ~ATM, short strike
    at or below the call wall, expirations 14-60 DTE, scored on
    risk/reward 30, price edge vs lognormal fair value at the long leg's IV
    30, liquidity 25, DTE 15 (best 21-45). Under MIN_SPREAD_SCORE (55) it
    reports "at support, no edge". Nothing here places an order.
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone, date

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")
SECT = os.path.join(HIST, "sector")
sys.path.insert(0, HERE)
import sector_candidates as sc  # noqa: E402

SECTORS = ("XLK", "XLF", "XLV", "XLP")
BROAD_PROXY = {"XLK": "QQQ", "XLF": "SPY", "XLV": "SPY", "XLP": "SPY"}
# Industry ETF per constituent. Unlisted names use the sector ETF.
INDUSTRY = {
    "XLK": {
        "SMH": ["NVDA", "AMD", "AVGO", "MU", "INTC", "AMAT", "LRCX", "TXN",
                "KLAC", "MRVL", "ADI", "QCOM"],
        "IGV": ["MSFT", "ORCL", "CRM", "NOW", "PANW", "CRWD", "PLTR", "ADBE",
                "INTU"],
    },
    "XLF": {
        "KBE": ["JPM", "BAC", "WFC", "C", "USB", "PNC"],
        "IAI": ["GS", "MS", "SCHW", "BLK", "BX", "KKR", "HOOD", "BNY"],
        "KIE": ["ALL", "AON", "AJG", "CB", "PGR", "TRV", "MRSH"],
    },
    "XLV": {
        "IBB": ["AMGN", "GILD", "REGN", "VRTX", "MRNA"],
        "IHI": ["ABT", "BSX", "BDX", "EW", "ISRG", "MDT", "SYK"],
        "IHF": ["UNH", "ELV", "CI", "HUM", "CVS", "HCA"],
        "PPH": ["LLY", "JNJ", "MRK", "PFE", "ABBV", "BMY"],
    },
    "XLP": {},
}
INDEX_UNIVERSE = ["SPY", "QQQ", "IWM", "XLK", "SMH", "XLF", "GLD", "TLT"]
INDEX_PROXY = {"QQQ": "QQQ", "XLK": "QQQ", "SMH": "QQQ"}     # else SPY
INDEX_NEAR = 0.015
NEVER_TRADE = {"CPB"}       # employer -- watched in the monitor, never traded
BAND = 0.0025
MIN_ROOM = 0.02
FLIP_BUFFER = 0.005
MIN_GEX_ROWS = 60           # ~2 sessions of 15-minute captures
NEAR_WALL = 0.03
MAX_ATM_SPREAD = 0.10
MIN_ATM_OI = 100
EVENT_DAYS = 14
DTE_MIN, DTE_MAX = 14, 60
LEG_MIN_OI = 100
MIN_SPREAD_SCORE = 55
GRADE_CUTS = ((75, "A"), (60, "B"), (45, "C"), (0, "D"))
WEIGHTS = {"location": 35, "valuation": 25, "liquidity": 15,
           "short_interest": 15, "event": 10}


def clamp(x):
    return max(0.0, min(1.0, float(x)))


def num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def out(obj):
    print(json.dumps(obj, indent=1, default=str))


def industry_of(sector, ticker):
    for etf, names in INDUSTRY.get(sector, {}).items():
        if ticker in names:
            return etf
    return sector


def industry_sector(ticker):
    """The sector whose INDUSTRY map lists this constituent, else None."""
    for sec, m in INDUSTRY.items():
        if any(ticker in names for names in m.values()):
            return sec
    return None


def needed_etfs(sector):
    return [sector] + list(INDUSTRY.get(sector, {}))


# --------------------------------------------------------------------------
# regime
# --------------------------------------------------------------------------

def trend(etf, bars_dir):
    """(ok, detail) -- last price above the 20-day average, 20d change > 0."""
    path = os.path.join(bars_dir or "", f"{etf}.json")
    if not bars_dir or not os.path.exists(path):
        return False, f"no {etf} bars supplied"
    with open(path, encoding="utf-8") as fh:
        bars = json.load(fh)
    closes = [float(b["close_price"]) for b in bars if not b.get("interpolated")]
    if len(closes) < 21:
        return False, f"only {len(closes)} {etf} closes"
    sma = sum(closes[-20:]) / 20
    chg = closes[-1] / closes[-21] - 1
    ok = closes[-1] > sma and chg > 0
    return ok, f"{etf} {closes[-1]:.2f} vs 20d avg {sma:.2f}, 20d {chg:+.2%}"


def gamma_state(symbol, s):
    """(ok, detail, used) from the GEX Monitor's latest row for symbol."""
    rows = s[s["symbol"] == symbol]
    if len(rows) < MIN_GEX_ROWS:
        return None, f"{symbol}: only {len(rows)} captures", symbol
    r = rows.iloc[-1]
    spot, flip = float(r["spot"]), float(r["flip"])
    above = spot / flip - 1
    ok = r["regime"] == "positive" and above >= FLIP_BUFFER
    return ok, (f"{symbol} {r['regime']} gamma, spot {spot:.2f} vs flip "
                f"{flip:.2f} ({above:+.2%}) at {r['capture_ts']}"), symbol


def regimes(sector, bars_dir):
    """{etf: {favorable, checks, blocks}} for the sector ETF and each
    industry ETF it uses."""
    s = pd.read_csv(os.path.join(HIST, "intraday_state.csv")).sort_values("capture_ts")
    v = pd.read_csv(os.path.join(HIST, "vix_state.csv")).tail(1)
    vix_checks = []
    if len(v):
        r = v.iloc[0]
        term = str(r["term_state"])
        vix_checks.append(("VIX term not in backwardation",
                           not term.startswith("backwardation"), term))
        hi = str(r.get("vix_20d_high")).lower() == "true"
        vix_checks.append(("VIX not at a 20-day high", not hi,
                           f"VIX {r['vix']} ({'20d high' if hi else 'below 20d high'})"))
    else:
        vix_checks.append(("VIX data", False, "no VIX rows"))

    res = {}
    for etf in needed_etfs(sector):
        checks = []
        ok, d = trend(etf, bars_dir)
        checks.append({"check": f"{etf} rising", "ok": ok, "detail": d})
        g_ok, g_d, used = gamma_state(etf, s)
        if g_ok is None:                       # not captured: broad proxy
            g_ok, g_d, used = gamma_state(BROAD_PROXY[sector], s)
            if g_ok is None:
                g_ok = False
        checks.append({"check": f"{used} gamma stable (positive, >= 0.5% above flip)",
                       "ok": bool(g_ok), "detail": g_d})
        for name, ok2, d2 in vix_checks:
            checks.append({"check": name, "ok": bool(ok2), "detail": d2})
        blocks = [f"{c['check']}: {c['detail']}" for c in checks if not c["ok"]]
        res[etf] = {"favorable": not blocks, "checks": checks, "blocks": blocks}
    return res


# --------------------------------------------------------------------------
# indices
# --------------------------------------------------------------------------

def index_setups(bars_dir):
    import build_dashboard as bd
    import support_resistance as sr
    metrics = bd.load_metrics(HIST)
    s = pd.read_csv(os.path.join(HIST, "intraday_state.csv")).sort_values("capture_ts")
    v = pd.read_csv(os.path.join(HIST, "vix_state.csv")).tail(1)
    vr = v.iloc[0] if len(v) else None
    out_rows = []
    for sym in INDEX_UNIVERSE:
        sub = metrics[metrics["symbol"] == sym]
        row = {"symbol": sym, "eligible": False, "excluded_because": []}
        if sub.empty:
            row["excluded_because"].append("not captured by the GEX Monitor yet")
            out_rows.append(row)
            continue
        try:
            L = sr.levels(HIST, metrics, sym, bd.BIN_SIZE.get(sym, bd.DEFAULT_BIN),
                          sub.iloc[-1])
        except Exception as e:
            L = None
            row["excluded_because"].append(f"levels failed: {e}")
        if not L:
            row["excluded_because"].append("no graded levels inside 1 sigma")
            out_rows.append(row)
            continue
        # live price: the latest intraday capture
        live = s[s["symbol"] == sym]
        price = float(live.iloc[-1]["spot"]) if len(live) else float(L["spot"])
        sup = [x for x in L["support"] if x["grade"] in ("A", "B")]
        sup = max(sup, key=lambda x: x["strike"]) if sup else None
        res = None
        if sup:
            above = [x for x in L["resistance"] if x["grade"] in ("A", "B")
                     and x["strike"] > sup["strike"]]
            res = min(above, key=lambda x: x["strike"]) if above else None

        checks = []
        ok, d = trend(sym, bars_dir)
        checks.append({"check": f"{sym} rising", "ok": ok, "detail": d})
        g_ok, g_d, used = gamma_state(sym, s)
        if g_ok is None:
            g_ok, g_d, used = gamma_state(INDEX_PROXY.get(sym, "SPY"), s)
            g_ok = bool(g_ok)
        checks.append({"check": f"{used} gamma stable", "ok": bool(g_ok), "detail": g_d})
        if vr is not None:
            term = str(vr["term_state"])
            checks.append({"check": "VIX term not in backwardation",
                           "ok": not term.startswith("backwardation"), "detail": term})
            hi = str(vr.get("vix_20d_high")).lower() == "true"
            checks.append({"check": "VIX not at a 20-day high", "ok": not hi,
                           "detail": f"VIX {vr['vix']}"})
        favorable = all(c["ok"] for c in checks)

        reasons = []
        if not sup:
            reasons.append("no A/B-graded support inside 1 sigma")
        room = (res["strike"] / sup["strike"] - 1) if (sup and res) else None
        if sup and (room is None or room < MIN_ROOM):
            reasons.append(f"no room to run: next strong resistance {room:+.1%} above support"
                           if room is not None else "no strong resistance target above support")
        if not favorable:
            reasons.append("regime not favorable")

        dist = (price / sup["strike"] - 1) if sup else None
        chg = None
        try:
            chg = float(d.split("20d ")[1].rstrip("%")) / 100
        except (IndexError, ValueError):
            pass
        p_sup = (sup["score"] / 100) if sup else 0.0
        p_trend = (0.5 * (1.0 if ok else 0.0) + 0.5 * clamp(0.5 + (chg or 0) / 0.10))
        p_room = clamp((room or 0) / 0.05)
        p_near = clamp(1 - dist / INDEX_NEAR) if (dist is not None and dist >= 0) else 0.0
        iv = L.get("iv")
        p_vol = clamp(1 - (iv or 0.35) / 0.35) if iv else 0.5
        total = round(35 * p_sup + 25 * p_trend + 15 * p_room + 15 * p_near + 10 * p_vol, 1)
        row.update({
            "score": total, "grade": next(g for c, g in GRADE_CUTS if total >= c),
            "price": round(price, 2), "regime_favorable": favorable, "checks": checks,
            "support": sup["strike"] if sup else None,
            "support_grade": f"{sup['grade']} {sup['label']}" if sup else None,
            "support_strength": sup["strength"] if sup else None,
            "target": res["strike"] if res else None,
            "target_grade": f"{res['grade']} {res['label']}" if res else None,
            "room_from_support": round(room, 4) if room is not None else None,
            "to_support": round(dist, 4) if dist is not None else None,
            "band": [round(sup["strike"] * (1 - BAND), 2), round(sup["strike"] * (1 + BAND), 2)] if sup else None,
            "iv30": round(iv, 4) if iv else None,
            "eligible": not reasons, "excluded_because": reasons,
        })
        out_rows.append(row)
    out_rows.sort(key=lambda x: (not x["eligible"], -(x.get("score") or 0)))
    best = next((x for x in out_rows if x["eligible"]), None)
    return {"best": best, "setups": out_rows}


# --------------------------------------------------------------------------
# rank
# --------------------------------------------------------------------------

def rank(sector, bars_dir=None, short=None):
    tech = sc.latest_rows(os.path.join(SECT, "sector_technicals.csv"))
    fund = sc.latest_rows(os.path.join(SECT, "sector_fundamentals.csv"))
    if tech is None:
        return {"error": "no sector technicals"}
    tech = tech[tech["sector_etf"] == sector]
    if tech.empty:
        return {"error": f"no {sector} rows -- the sector pipeline covers {SECTORS}"}
    m = (tech.merge(fund.drop(columns=["capture_ts", "sector_etf"]), on="ticker",
                    how="left", suffixes=("", "_f")) if fund is not None else tech)
    m = sc.add_value_z(m)
    sc.WALL_HIST.update(sc.wall_history(os.path.join(SECT, "sector_technicals.csv")))
    reg = regimes(sector, bars_dir)
    short = short or {}
    now = datetime.now(timezone.utc)

    rows = []
    for _, r in m.iterrows():
        t = r["ticker"]
        ind = industry_of(sector, t)
        spot, pw, cw = num(r["spot"]), num(r["put_wall"]), num(r["call_wall"])
        spread, oi = num(r.get("atm_spread_pct")), num(r.get("atm_oi"))
        reasons = []
        if t in NEVER_TRADE:
            reasons.append("never traded (employer)")
        if spread is None or spread > MAX_ATM_SPREAD or oi is None or oi < MIN_ATM_OI:
            reasons.append("options not liquid enough for a vertical")
        if not (spot and pw and pw < spot):
            reasons.append("no put wall below spot")
        st = sc.stability_fields(t)
        strength = st.get("put_wall_strength") or ""
        if strength in ("moved", "new", ""):
            reasons.append(f"put wall {strength or 'unknown'} -- untested")
        # Room to run is judged from support -- where the trade is entered.
        room = (cw / pw - 1) if (cw and pw) else None
        if room is None or room < MIN_ROOM:
            reasons.append(f"no room to run: call wall {room:+.1%} above support"
                           if room is not None else "no call wall")
        if not reg[ind]["favorable"]:
            reasons.append(f"{ind} regime not favorable")

        dist = (spot / pw - 1) if (spot and pw) else None
        prox = clamp(1 - dist / NEAR_WALL) if dist is not None and dist >= 0 else 0.0
        wall_q = {"strong": 1.0, "steady": 0.7, "shifting": 0.3}.get(strength, 0.0)
        flip = num(r.get("gamma_flip"))
        above_flip = 1.0 if (flip and spot and spot >= flip) else 0.0
        rs = [v for v in (num(r.get("rel_strength_20d")), num(r.get("rel_strength_60d"))) if v is not None]
        mom = clamp(0.5 + (sum(rs) / len(rs)) / 0.20) if rs else 0.5
        location = 0.40 * prox + 0.30 * wall_q + 0.15 * above_flip + 0.15 * mom

        vz = num(r.get("value_z")) or 0.0
        imp = sc.improving_signals(r)
        turn = sc.value_turn(r)
        valuation = clamp(0.5 + vz / 4) * 0.6 + clamp(len(imp) / 4) * 0.3 + (0.1 if turn else 0.0)

        adv = num(r.get("adv_usd_20d"))
        p_adv = clamp(math.log10(adv / 5e7) / math.log10(40)) if adv and adv > 0 else 0.0
        p_oi = clamp(math.log10(oi / MIN_ATM_OI) / math.log10(50)) if oi and oi > 0 else 0.0
        p_sp = clamp((MAX_ATM_SPREAD - spread) / 0.08) if spread is not None else 0.0
        liquidity = 0.4 * p_adv + 0.3 * p_oi + 0.3 * p_sp

        si = num(short.get(t))
        si_part = clamp(si / 0.10) if si is not None else 0.5

        d = sc.days_to_filing(r, now)
        event = 0.0 if (d is not None and 0 <= d <= EVENT_DAYS) else 1.0

        parts = {"location": location, "valuation": valuation, "liquidity": liquidity,
                 "short_interest": si_part, "event": event}
        total = round(sum(WEIGHTS[k] * v for k, v in parts.items()), 1)
        rows.append({
            "ticker": t, "industry_etf": ind, "score": total,
            "grade": next(g for c, g in GRADE_CUTS if total >= c),
            "eligible": not reasons, "excluded_because": reasons,
            "spot": spot, "put_wall": pw, "call_wall": cw, "gamma_flip": flip,
            "to_put_wall": round(dist, 4) if dist is not None else None,
            "room_from_support": round(room, 4) if room is not None else None,
            "band": [round(pw * (1 - BAND), 2), round(pw * (1 + BAND), 2)] if pw else None,
            "put_wall_strength": strength,
            "put_wall_held": f"{st.get('put_wall_held')}/{st.get('put_wall_days')}",
            "atm_iv_30": num(r.get("atm_iv_30")),
            "short_float": si, "short_interest_supplied": si is not None,
            "improving": imp, "value_z": round(vz, 2),
            "earnings_in_days": d,
            "parts": {k: round(v, 3) for k, v in parts.items()},
        })
    rows.sort(key=lambda x: (not x["eligible"], -x["score"]))
    watch = next((x for x in rows if x["eligible"]), None)
    return {"sector": sector, "as_of": str(tech["capture_ts"].max()),
            "regimes": reg, "watch": watch, "ranked": rows}


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------

def check(ticker, bars, put_wall, call_wall=None, price=None):
    lo_band, hi_band = put_wall * (1 - BAND), put_wall * (1 + BAND)
    real = [b for b in bars if not b.get("interpolated")]
    if not real:
        return {"ticker": ticker, "state": "no_data"}
    low = min(float(b["low_price"]) for b in real)
    last = float(price) if price else float(real[-1]["close_price"])
    touched = low <= hi_band
    if touched and last < lo_band:
        state = "broken"          # went through support -- never trade
    elif touched:
        state = "at_support"
    else:
        state = "waiting"
    room = (call_wall / last - 1) if call_wall else None
    if state == "at_support" and (room is None or room < MIN_ROOM):
        state = "no_room"         # call wall under 2% overhead -- skip
    return {"ticker": ticker, "state": state, "put_wall": put_wall,
            "call_wall": call_wall,
            "room_to_call_wall": round(room, 4) if room is not None else None,
            "band": [round(lo_band, 2), round(hi_band, 2)],
            "low_since_check": low, "last": last,
            "distance_now": round(last / put_wall - 1, 4)}


# --------------------------------------------------------------------------
# bear mode (index scan): bear put verticals at resistance in negative gamma
# --------------------------------------------------------------------------

def downtrend(etf, bars_dir):
    """(ok, detail, chg) -- last price below the 20-day average, 20d change < 0."""
    path = os.path.join(bars_dir or "", f"{etf}.json")
    if not bars_dir or not os.path.exists(path):
        return False, f"no {etf} bars supplied", None
    with open(path, encoding="utf-8") as fh:
        bars = json.load(fh)
    closes = [float(b["close_price"]) for b in bars if not b.get("interpolated")]
    if len(closes) < 21:
        return False, f"only {len(closes)} {etf} closes", None
    sma = sum(closes[-20:]) / 20
    chg = closes[-1] / closes[-21] - 1
    ok = closes[-1] < sma and chg < 0
    return ok, f"{etf} {closes[-1]:.2f} vs 20d avg {sma:.2f}, 20d {chg:+.2%}", chg


def gamma_bearish(symbol, s):
    """(ok, detail, used, flip, spot) -- negative gamma or spot below flip."""
    rows = s[s["symbol"] == symbol]
    if len(rows) < MIN_GEX_ROWS:
        return None, f"{symbol}: only {len(rows)} captures", symbol, None, None
    r = rows.iloc[-1]
    spot, flip = float(r["spot"]), float(r["flip"])
    ok = r["regime"] == "negative" or spot < flip
    return ok, (f"{symbol} {r['regime']} gamma, spot {spot:.2f} vs flip "
                f"{flip:.2f} ({spot / flip - 1:+.2%}) at {r['capture_ts']}"), symbol, flip, spot


def bear_setups(bars_dir):
    import build_dashboard as bd
    import support_resistance as sr
    metrics = bd.load_metrics(HIST)
    s = pd.read_csv(os.path.join(HIST, "intraday_state.csv")).sort_values("capture_ts")
    out_rows = []
    for sym in INDEX_UNIVERSE:
        sub = metrics[metrics["symbol"] == sym]
        row = {"symbol": sym, "side": "bear", "eligible": False, "excluded_because": []}
        if sub.empty:
            row["excluded_because"].append("not captured by the GEX Monitor yet")
            out_rows.append(row)
            continue
        try:
            L = sr.levels(HIST, metrics, sym, bd.BIN_SIZE.get(sym, bd.DEFAULT_BIN),
                          sub.iloc[-1])
        except Exception as e:
            L = None
            row["excluded_because"].append(f"levels failed: {e}")
        if not L:
            row["excluded_because"].append("no graded levels inside 1 sigma")
            out_rows.append(row)
            continue
        live = s[s["symbol"] == sym]
        price = float(live.iloc[-1]["spot"]) if len(live) else float(L["spot"])

        checks = []
        ok, d, chg = downtrend(sym, bars_dir)
        checks.append({"check": f"{sym} falling", "ok": ok, "detail": d})
        g_ok, g_d, used, flip, _ = gamma_bearish(sym, s)
        if g_ok is None:
            g_ok, g_d, used, flip, _ = gamma_bearish(INDEX_PROXY.get(sym, "SPY"), s)
            g_ok = bool(g_ok)
            if used != sym:
                flip = None          # proxy's flip is not this ETF's level
        checks.append({"check": f"{used} gamma negative or below flip",
                       "ok": bool(g_ok), "detail": g_d})
        favorable = all(c["ok"] for c in checks)

        # resistance: nearest A/B graded level above price, or the flip if nearer
        cands = [{"strike": x["strike"], "grade": x["grade"], "label": x["label"],
                  "score": x["score"], "strength": x.get("strength")}
                 for x in L["resistance"] if x["grade"] in ("A", "B") and x["strike"] > price]
        if flip and flip > price:
            cands.append({"strike": round(flip, 2), "grade": "B", "label": "gamma flip",
                          "score": 60.0, "strength": None})
        res = min(cands, key=lambda x: x["strike"]) if cands else None
        tgt = None
        if res:
            below = [x for x in L["support"] if x["grade"] in ("A", "B")
                     and x["strike"] < res["strike"]]
            tgt = max(below, key=lambda x: x["strike"]) if below else None

        reasons = []
        if not res:
            reasons.append("no A/B-graded resistance or flip above the price inside 1 sigma")
        room = (1 - tgt["strike"] / res["strike"]) if (res and tgt) else None
        if res and (room is None or room < MIN_ROOM):
            reasons.append(f"no room to fall: next strong support {room:.1%} below resistance"
                           if room is not None else "no strong support target below resistance")
        if not favorable:
            reasons.append("regime not bearish")

        dist = (res["strike"] / price - 1) if res else None   # how far price is below resistance
        p_res = (res["score"] / 100) if res else 0.0
        p_trend = 0.5 * (1.0 if ok else 0.0) + 0.5 * clamp(0.5 - (chg or 0) / 0.10)
        p_room = clamp((room or 0) / 0.05)
        p_near = clamp(1 - dist / INDEX_NEAR) if (dist is not None and dist >= 0) else 0.0
        iv = L.get("iv")
        p_vol = clamp(1 - (iv or 0.35) / 0.35) if iv else 0.5
        total = round(35 * p_res + 25 * p_trend + 15 * p_room + 15 * p_near + 10 * p_vol, 1)
        row.update({
            "score": total, "grade": next(g for c, g in GRADE_CUTS if total >= c),
            "price": round(price, 2), "regime_bearish": favorable, "checks": checks,
            "resistance": res["strike"] if res else None,
            "resistance_grade": f"{res['grade']} {res['label']}" if res else None,
            "target": tgt["strike"] if tgt else None,
            "target_grade": f"{tgt['grade']} {tgt['label']}" if tgt else None,
            "room_from_resistance": round(room, 4) if room is not None else None,
            "to_resistance": round(dist, 4) if dist is not None else None,
            "band": [round(res["strike"] * (1 - BAND), 2), round(res["strike"] * (1 + BAND), 2)] if res else None,
            "iv30": round(iv, 4) if iv else None,
            "eligible": not reasons, "excluded_because": reasons,
        })
        out_rows.append(row)
    out_rows.sort(key=lambda x: (not x["eligible"], -(x.get("score") or 0)))
    best = next((x for x in out_rows if x["eligible"]), None)
    return {"best": best, "setups": out_rows}


def bear_check(ticker, bars, resistance, target=None, price=None):
    lo_band, hi_band = resistance * (1 - BAND), resistance * (1 + BAND)
    real = [b for b in bars if not b.get("interpolated")]
    if not real:
        return {"ticker": ticker, "state": "no_data"}
    high = max(float(b["high_price"]) for b in real)
    last = float(price) if price else float(real[-1]["close_price"])
    touched = high >= lo_band
    if touched and last > hi_band:
        state = "broken"          # rally took the level -- never trade
    elif touched:
        state = "at_resistance"
    else:
        state = "waiting"
    room = (1 - target / last) if target else None
    if state == "at_resistance" and (room is None or room < MIN_ROOM):
        state = "no_room"
    return {"ticker": ticker, "side": "bear", "state": state, "resistance": resistance,
            "target": target, "room_to_target": round(room, 4) if room is not None else None,
            "band": [round(lo_band, 2), round(hi_band, 2)],
            "high_since_check": high, "last": last,
            "distance_now": round(last / resistance - 1, 4)}


def put_value(S, K, T, vol):
    """Undiscounted lognormal put value, zero drift (put-call parity)."""
    return call_value(S, K, T, vol) - (S - K)


def bear_score(ticker, chain, resistance, target, today=None):
    """chain legs are PUTS. Bear put verticals: buy K1 (higher), sell K2 (lower)."""
    today = today or date.today()
    S = float(chain["spot"])
    legs = [l for l in chain["legs"] if num(l.get("bid")) is not None and num(l.get("ask"))]
    by_exp = {}
    for l in legs:
        by_exp.setdefault(l["expiration"], []).append(l)
    out_rows, skipped = [], {"illiquid": 0, "dte": 0}
    for exp, ls in by_exp.items():
        dte = (date.fromisoformat(exp) - today).days
        if not (DTE_MIN <= dte <= DTE_MAX):
            skipped["dte"] += len(ls)
            continue
        T = dte / 365.0
        longs = [l for l in ls if S * 0.99 <= float(l["strike"]) <= resistance]
        shorts = [l for l in ls if target <= float(l["strike"]) < S]
        for a in longs:
            for b in shorts:
                K1, K2 = float(a["strike"]), float(b["strike"])
                if K2 >= K1:
                    continue
                oi_min = min(int(a.get("open_interest") or 0), int(b.get("open_interest") or 0))
                if oi_min < LEG_MIN_OI:
                    skipped["illiquid"] += 1
                    continue
                ma = (float(a["bid"]) + float(a["ask"])) / 2
                mb = (float(b["bid"]) + float(b["ask"])) / 2
                debit, width = ma - mb, K1 - K2
                if debit <= 0.05 or debit >= width:
                    continue
                natural = float(a["ask"]) - float(b["bid"])
                vol = num(a.get("implied_volatility")) or num(b.get("implied_volatility"))
                if not vol:
                    continue
                fair = put_value(S, K1, T, vol) - put_value(S, K2, T, vol)
                edge = (fair - debit) / debit
                rr = (width - debit) / debit
                be = K1 - debit
                pop = 1 - prob_above(S, be, T, vol)
                spr = max((float(a["ask"]) - float(a["bid"])) / max(ma, 0.01),
                          (float(b["ask"]) - float(b["bid"])) / max(mb, 0.01))
                p_rr = clamp(rr / 2.0)
                p_edge = clamp(0.5 + edge / 0.30)
                p_liq = 0.6 * clamp((0.15 - spr) / 0.13) + \
                        0.4 * clamp(math.log10(oi_min / LEG_MIN_OI) / math.log10(50))
                p_dte = 1.0 if 21 <= dte <= 45 else 0.6
                total = 30 * p_rr + 30 * p_edge + 25 * p_liq + 15 * p_dte
                out_rows.append({
                    "ticker": ticker, "side": "bear put vertical", "expiration": exp, "dte": dte,
                    "long": K1, "short": K2, "width": width,
                    "debit_mid": round(debit, 2), "debit_natural": round(natural, 2),
                    "max_profit": round(width - debit, 2), "risk_reward": round(rr, 2),
                    "breakeven": round(be, 2), "prob_below_breakeven": round(pop, 3),
                    "fair_value": round(fair, 2), "price_edge": round(edge, 3),
                    "edge_per_day": round(edge / dte, 4),
                    "worst_leg_spread": round(spr, 3), "min_leg_oi": oi_min,
                    "iv": round(vol, 3), "score": round(total, 1),
                })
    out_rows.sort(key=lambda x: (-x["score"], -x["edge_per_day"]))
    best = out_rows[0] if out_rows else None
    note = None
    if S <= target:
        note = "spot is at or below the target -- no room for a vertical"
    elif best is None:
        note = "no liquid put spread between ATM/resistance and the target"
    elif best["score"] < MIN_SPREAD_SCORE:
        note = f"best spread scores {best['score']} (< {MIN_SPREAD_SCORE}) -- no edge"
    return {"ticker": ticker, "side": "bear", "spot": S, "resistance": resistance,
            "target": target,
            "tradeable": bool(best and best["score"] >= MIN_SPREAD_SCORE and S > target),
            "note": note, "candidates": len(out_rows), "skipped": skipped,
            "best": best, "top": out_rows[:5]}


# --------------------------------------------------------------------------
# score
# --------------------------------------------------------------------------

def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def call_value(S, K, T, vol):
    """Undiscounted lognormal call value, zero drift (short horizon)."""
    if T <= 0 or vol <= 0:
        return max(S - K, 0.0)
    sd = vol * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sd * sd) / sd
    return S * _ncdf(d1) - K * _ncdf(d1 - sd)


def prob_above(S, K, T, vol):
    if T <= 0 or vol <= 0:
        return 1.0 if S > K else 0.0
    sd = vol * math.sqrt(T)
    return _ncdf((math.log(S / K) - 0.5 * sd * sd) / sd)


def score(ticker, chain, put_wall, call_wall, today=None):
    """chain: {"spot": float, "legs": [{expiration, strike, bid, ask, mark,
    implied_volatility, delta, open_interest, volume}, ...]} -- calls only."""
    today = today or date.today()
    S = float(chain["spot"])
    legs = [l for l in chain["legs"] if num(l.get("bid")) is not None and num(l.get("ask"))]
    by_exp = {}
    for l in legs:
        by_exp.setdefault(l["expiration"], []).append(l)

    out_rows, skipped = [], {"illiquid": 0, "dte": 0}
    for exp, ls in by_exp.items():
        dte = (date.fromisoformat(exp) - today).days
        if not (DTE_MIN <= dte <= DTE_MAX):
            skipped["dte"] += len(ls)
            continue
        T = dte / 365.0
        ls = sorted(ls, key=lambda l: float(l["strike"]))
        longs = [l for l in ls if put_wall <= float(l["strike"]) <= S * 1.01]
        shorts = [l for l in ls if S < float(l["strike"]) <= call_wall]
        for a in longs:
            for b in shorts:
                K1, K2 = float(a["strike"]), float(b["strike"])
                if K2 <= K1:
                    continue
                if min(int(a.get("open_interest") or 0), int(b.get("open_interest") or 0)) < LEG_MIN_OI:
                    skipped["illiquid"] += 1
                    continue
                ma = (float(a["bid"]) + float(a["ask"])) / 2
                mb = (float(b["bid"]) + float(b["ask"])) / 2
                debit = ma - mb
                width = K2 - K1
                if debit <= 0.05 or debit >= width:
                    continue
                natural = float(a["ask"]) - float(b["bid"])
                vol = num(a.get("implied_volatility")) or num(b.get("implied_volatility"))
                if not vol:
                    continue
                fair = call_value(S, K1, T, vol) - call_value(S, K2, T, vol)
                edge = (fair - debit) / debit
                rr = (width - debit) / debit
                be = K1 + debit
                pop = prob_above(S, be, T, vol)
                spr = max((float(a["ask"]) - float(a["bid"])) / max(ma, 0.01),
                          (float(b["ask"]) - float(b["bid"])) / max(mb, 0.01))
                oi_min = min(int(a.get("open_interest") or 0), int(b.get("open_interest") or 0))

                p_rr = clamp(rr / 2.0)                       # 2:1 = full
                p_edge = clamp(0.5 + edge / 0.30)            # +15% edge = full
                p_liq = 0.6 * clamp((0.15 - spr) / 0.13) + \
                        0.4 * clamp(math.log10(oi_min / LEG_MIN_OI) / math.log10(50))
                p_dte = 1.0 if 21 <= dte <= 45 else 0.6
                total = 30 * p_rr + 30 * p_edge + 25 * p_liq + 15 * p_dte
                out_rows.append({
                    "ticker": ticker, "expiration": exp, "dte": dte,
                    "long": K1, "short": K2, "width": width,
                    "debit_mid": round(debit, 2), "debit_natural": round(natural, 2),
                    "max_profit": round(width - debit, 2), "risk_reward": round(rr, 2),
                    "breakeven": round(be, 2), "prob_above_breakeven": round(pop, 3),
                    "fair_value": round(fair, 2), "price_edge": round(edge, 3),
                    "edge_per_day": round(edge / dte, 4),
                    "worst_leg_spread": round(spr, 3), "min_leg_oi": oi_min,
                    "iv": round(vol, 3), "score": round(total, 1),
                    "short_at_or_below_call_wall": K2 <= call_wall,
                })
    out_rows.sort(key=lambda x: (-x["score"], -x["edge_per_day"]))
    best = out_rows[0] if out_rows else None
    note = None
    if S >= call_wall:
        note = "spot is at or above the call wall -- no room for a vertical"
    elif best is None:
        note = "no liquid spread between the wall and the call wall"
    elif best["score"] < MIN_SPREAD_SCORE:
        note = f"best spread scores {best['score']} (< {MIN_SPREAD_SCORE}) -- no edge"
    return {"ticker": ticker, "spot": S, "put_wall": put_wall, "call_wall": call_wall,
            "tradeable": bool(best and best["score"] >= MIN_SPREAD_SCORE and S < call_wall),
            "note": note, "candidates": len(out_rows), "skipped": skipped,
            "best": out_rows[0] if out_rows else None, "top": out_rows[:5]}


# --------------------------------------------------------------------------

def _load(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    p = argparse.ArgumentParser(description="Sector support agent arithmetic.")
    p.add_argument("mode", choices=["etfs", "indices", "rank", "check", "score",
                                   "bear-indices", "bear-check", "bear-score", "quality"])
    p.add_argument("--sector", default="XLK", choices=SECTORS)
    p.add_argument("--bars-dir", help="folder of <ETF>.json daily bars (rank)")
    p.add_argument("--short", help='JSON {"TICKER": short_float_fraction} (rank)')
    p.add_argument("--ticker")
    p.add_argument("--bars", help="JSON list of 5-minute bars since last check (check)")
    p.add_argument("--price", type=float, help="live last price (check)")
    p.add_argument("--chain", help="JSON chain for the ticker (score)")
    p.add_argument("--put-wall", type=float)
    p.add_argument("--call-wall", type=float)
    p.add_argument("--resistance", type=float, help="bear modes: resistance level")
    p.add_argument("--target", type=float, help="bear modes: downside target; quality: target")
    p.add_argument("--side", choices=["bear", "bull"], help="quality: trade direction")
    p.add_argument("--level", type=float, help="quality: support (bull) or resistance (bear)")
    p.add_argument("--daily", help="quality: JSON daily bars, ~3 years, live price appended")
    p.add_argument("--hourly", help="quality: JSON hourly bars, ~60 sessions")
    p.add_argument("--index-daily", help="quality: index daily bars (QQQ for tech, else SPY)")
    p.add_argument("--index-hourly", help="quality: index hourly bars")
    a = p.parse_args()

    if a.mode == "quality":
        import setup_quality as sq
        if not (a.side and a.ticker and a.daily and a.hourly
                and a.level is not None and a.target is not None):
            out({"error": "quality needs --side --ticker --daily --hourly --level --target"})
            return 1
        sector_of_industry = {etf: sec for sec, m in INDUSTRY.items() for etf in m}
        vz, note = sq.peer_valuation(a.ticker, INDUSTRY, sector_of_industry)
        if a.ticker in INDEX_UNIVERSE:
            idx = INDEX_PROXY.get(a.ticker, "SPY")
        else:
            idx = BROAD_PROXY.get(industry_sector(a.ticker) or a.sector, "SPY")
        out(sq.quality(a.side, a.ticker, _load(a.daily), _load(a.hourly), a.level, a.target,
                       idx if a.index_daily else None, _load(a.index_daily),
                       _load(a.index_hourly), vz, note))
        return 0

    if a.mode == "bear-indices":
        out(bear_setups(a.bars_dir))
        return 0
    if a.mode in ("bear-check", "bear-score"):
        if a.resistance is None or a.target is None:
            out({"error": "bear modes need --resistance and --target"})
            return 1
        if a.mode == "bear-check":
            out(bear_check(a.ticker, _load(a.bars), a.resistance, a.target, a.price))
        else:
            out(bear_score(a.ticker, _load(a.chain), a.resistance, a.target))
        return 0

    if a.mode == "etfs":
        out({"sector": a.sector, "bars_needed": needed_etfs(a.sector),
             "index_bars_needed": INDEX_UNIVERSE,
             "gamma_proxy": BROAD_PROXY[a.sector]})
        return 0
    if a.mode == "rank":
        out(rank(a.sector, a.bars_dir, _load(a.short)))
        return 0
    if a.mode == "indices":
        out(index_setups(a.bars_dir))
        return 0
    if a.put_wall and a.call_wall:          # index setups pass both levels
        if a.mode == "check":
            out(check(a.ticker, _load(a.bars), a.put_wall, a.call_wall, a.price))
        else:
            out(score(a.ticker, _load(a.chain), a.put_wall, a.call_wall))
        return 0
    r = rank(a.sector)
    row = next((x for x in r.get("ranked", []) if x["ticker"] == a.ticker), None)
    pw = a.put_wall or (row or {}).get("put_wall")
    cw = a.call_wall or (row or {}).get("call_wall")
    if pw is None:
        out({"error": f"no put wall for {a.ticker}"})
        return 1
    if a.mode == "check":
        out(check(a.ticker, _load(a.bars), pw, cw, a.price))
    else:
        out(score(a.ticker, _load(a.chain), pw, cw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
