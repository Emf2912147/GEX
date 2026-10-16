#!/usr/bin/env python3
"""
XLK support agent -- the arithmetic behind the scheduled trade alert.

Reads only what the repo already stores. Writes nothing to the repo: every
mode prints JSON to stdout. Live inputs (quotes, bars, option chains, short
interest) are gathered by the Claude session that runs it, through the
Robinhood connector, and passed in as JSON files.

    python xlk_support_agent.py regime [--xlk xlk_bars.json]
    python xlk_support_agent.py rank   [--short si.json]
    python xlk_support_agent.py check  --ticker T --bars bars.json
    python xlk_support_agent.py score  --ticker T --chain chain.json

REGIME (gate 1 -- sector favourable)
    rising     XLK 20-day momentum > 0 (sector_technicals) AND XLK's last
               close above its 20-day average (xlk_bars.json, daily closes)
    stable     QQQ in positive gamma with spot >= FLIP_BUFFER above the flip
               (latest intraday row), VIX term not in backwardation, VIX not
               at a 20-day high (latest vix_state row). SMH in negative gamma
               also blocks once SMH has MIN_SMH_ROWS intraday rows.
    Any failure blocks the alert and the trade, and says which.

RANK (gate 2 -- which name)
    Every XLK constituent with liquid options (ATM spread <= 10%, ATM OI >=
    100) is scored 0-100 on how likely it is to rise from here:
      technical location 35  closeness to the put wall (best inside 3%),
                             wall strength (sector_candidates.wall_stability),
                             spot above the gamma flip, relative strength
      valuation          25  value_z vs XLK (sector_candidates.add_value_z),
                             improvement signals, deep-value-and-improving
      volume / liquidity 15  20d dollar volume, ATM OI, ATM spread
      short interest     15  % of float short -- squeeze fuel at support.
                             Neutral (0.5) when not supplied; flagged.
      event              10  full marks unless earnings fall within 14 days
    A put wall that MOVED last session, or is NEW, cannot be the watch name:
    an untested level is not support.

CHECK (gate 3 -- price at support)
    Fires when price is within BAND (0.25%) of the put wall: any 5-minute
    bar since the last check with low <= wall x 1.0025, and the latest price
    still >= wall x 0.9975. A close below that is a broken level -- reported,
    never traded.

SCORE (the trade)
    Bull call verticals, long strike from the wall up to ~ATM, short strike
    at or below the call wall, expirations 14-60 DTE. Each spread is scored:
      risk/reward  30  max profit / debit at the mid
      price edge   30  expected payoff under a lognormal at the long leg's IV
                       minus the debit, per $ of debit
      liquidity    25  leg bid-ask as % of mid, open interest on both legs
      DTE          15  best 21-45 days; edge per day of exposure as tiebreak
    A best score under MIN_SPREAD_SCORE (55) is reported as "at support, no
    edge" rather than as a trade.
    Prices are mids; the natural (pay-the-ask) debit is shown beside it.
    Nothing here places an order.
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

SECTOR = "XLK"
BAND = 0.0025
FLIP_BUFFER = 0.005
MIN_SMH_ROWS = 60          # ~2 sessions of 15-minute captures
NEAR_WALL = 0.03
MAX_ATM_SPREAD = 0.10
MIN_ATM_OI = 100
EVENT_DAYS = 14
DTE_MIN, DTE_MAX = 14, 60
LEG_MIN_OI = 100
MIN_SPREAD_SCORE = 55      # below this the alert says "at support, no edge"
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


# --------------------------------------------------------------------------
# regime
# --------------------------------------------------------------------------

def regime(xlk_bars=None):
    checks, blocks = [], []

    def chk(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            blocks.append(f"{name}: {detail}")

    tech = sc.latest_rows(os.path.join(SECT, "sector_technicals.csv"))
    mom = None
    if tech is not None:
        x = tech[tech["sector_etf"] == SECTOR]
        if len(x):
            mom = num(x["sector_mom_20d"].median())
    chk("XLK 20d momentum > 0", mom is not None and mom > 0,
        f"{mom:+.2%}" if mom is not None else "missing")

    if xlk_bars:
        closes = [float(b["close_price"]) for b in xlk_bars
                  if not b.get("interpolated")]
        if len(closes) >= 20:
            sma = sum(closes[-20:]) / 20
            chk("XLK above 20-day average", closes[-1] > sma,
                f"{closes[-1]:.2f} vs {sma:.2f}")
        else:
            chk("XLK above 20-day average", False, f"only {len(closes)} closes")
    else:
        chk("XLK above 20-day average", False, "no XLK bars supplied")

    s = pd.read_csv(os.path.join(HIST, "intraday_state.csv")).sort_values("capture_ts")
    q = s[s["symbol"] == "QQQ"].tail(1)
    if len(q):
        r = q.iloc[0]
        spot, flip = float(r["spot"]), float(r["flip"])
        above = spot / flip - 1
        chk("QQQ positive gamma, >= 0.5% above flip",
            r["regime"] == "positive" and above >= FLIP_BUFFER,
            f"{r['regime']}, spot {spot:.2f} vs flip {flip:.2f} ({above:+.2%}) at {r['capture_ts']}")
    else:
        chk("QQQ positive gamma, >= 0.5% above flip", False, "no QQQ rows")

    smh = s[s["symbol"] == "SMH"]
    if len(smh) >= MIN_SMH_ROWS:
        r = smh.iloc[-1]
        chk("SMH not in negative gamma", r["regime"] != "negative",
            f"{r['regime']} at {r['capture_ts']}")
    else:
        checks.append({"check": "SMH not in negative gamma", "ok": None,
                       "detail": f"not enforced yet ({len(smh)} of {MIN_SMH_ROWS} rows)"})

    v = pd.read_csv(os.path.join(HIST, "vix_state.csv")).tail(1)
    if len(v):
        r = v.iloc[0]
        term = str(r["term_state"])
        chk("VIX term not in backwardation", not term.startswith("backwardation"),
            term)
        hi = str(r.get("vix_20d_high")).lower() == "true"
        chk("VIX not at a 20-day high", not hi, f"VIX {r['vix']} ({'20d high' if hi else 'below 20d high'})")
    else:
        chk("VIX term not in backwardation", False, "no VIX rows")

    return {"favorable": not blocks, "blocks": blocks, "checks": checks}


# --------------------------------------------------------------------------
# rank
# --------------------------------------------------------------------------

def rank(short=None):
    tech = sc.latest_rows(os.path.join(SECT, "sector_technicals.csv"))
    fund = sc.latest_rows(os.path.join(SECT, "sector_fundamentals.csv"))
    if tech is None:
        return {"error": "no sector technicals"}
    tech = tech[tech["sector_etf"] == SECTOR]
    m = tech.merge(fund.drop(columns=["capture_ts", "sector_etf"]), on="ticker",
                   how="left", suffixes=("", "_f")) if fund is not None else tech
    m = sc.add_value_z(m)
    sc.WALL_HIST.update(sc.wall_history(os.path.join(SECT, "sector_technicals.csv")))
    short = short or {}
    now = datetime.now(timezone.utc)

    rows = []
    for _, r in m.iterrows():
        t = r["ticker"]
        spot, pw, cw = num(r["spot"]), num(r["put_wall"]), num(r["call_wall"])
        spread, oi = num(r.get("atm_spread_pct")), num(r.get("atm_oi"))
        reasons = []
        if spread is None or spread > MAX_ATM_SPREAD or oi is None or oi < MIN_ATM_OI:
            reasons.append("options not liquid enough for a vertical")
        if not (spot and pw and pw < spot):
            reasons.append("no put wall below spot")
        st = sc.stability_fields(t)
        strength = st.get("put_wall_strength") or ""
        if strength in ("moved", "new", ""):
            reasons.append(f"put wall {strength or 'unknown'} -- untested")

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
        si_part = clamp(si / 0.10) if si is not None else 0.5   # 10% of float short = full

        d = sc.days_to_filing(r, now)
        event = 0.0 if (d is not None and 0 <= d <= EVENT_DAYS) else 1.0

        parts = {"location": location, "valuation": valuation, "liquidity": liquidity,
                 "short_interest": si_part, "event": event}
        total = round(sum(WEIGHTS[k] * v for k, v in parts.items()), 1)
        rows.append({
            "ticker": t, "score": total,
            "grade": next(g for c, g in GRADE_CUTS if total >= c),
            "eligible": not reasons, "excluded_because": reasons,
            "spot": spot, "put_wall": pw, "call_wall": cw, "gamma_flip": flip,
            "to_put_wall": round(dist, 4) if dist is not None else None,
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
    return {"sector": SECTOR, "as_of": str(tech["capture_ts"].max()),
            "watch": watch, "ranked": rows}


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------

def check(ticker, bars, put_wall):
    lo_band, hi_band = put_wall * (1 - BAND), put_wall * (1 + BAND)
    real = [b for b in bars if not b.get("interpolated")]
    if not real:
        return {"ticker": ticker, "state": "no_data"}
    low = min(float(b["low_price"]) for b in real)
    last = float(real[-1]["close_price"])
    touched = low <= hi_band
    if touched and last < lo_band:
        state = "broken"          # went through support -- never trade
    elif touched:
        state = "at_support"
    else:
        state = "waiting"
    return {"ticker": ticker, "state": state, "put_wall": put_wall,
            "band": [round(lo_band, 2), round(hi_band, 2)],
            "low_since_check": low, "last": last,
            "distance_now": round(last / put_wall - 1, 4)}


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
    p = argparse.ArgumentParser(description="XLK support agent arithmetic.")
    p.add_argument("mode", choices=["regime", "rank", "check", "score"])
    p.add_argument("--xlk", help="JSON list of XLK daily bars (regime)")
    p.add_argument("--short", help='JSON {"TICKER": short_float_fraction} (rank)')
    p.add_argument("--ticker")
    p.add_argument("--bars", help="JSON list of 5-minute bars since last check (check)")
    p.add_argument("--chain", help="JSON chain for the ticker (score)")
    p.add_argument("--put-wall", type=float)
    p.add_argument("--call-wall", type=float)
    a = p.parse_args()

    if a.mode == "regime":
        out(regime(_load(a.xlk)))
    elif a.mode == "rank":
        out(rank(_load(a.short)))
    else:
        r = rank()
        row = next((x for x in r["ranked"] if x["ticker"] == a.ticker), None)
        pw = a.put_wall or (row or {}).get("put_wall")
        cw = a.call_wall or (row or {}).get("call_wall")
        if pw is None:
            out({"error": f"no put wall for {a.ticker}"})
            return 1
        if a.mode == "check":
            out(check(a.ticker, _load(a.bars), pw))
        else:
            out(score(a.ticker, _load(a.chain), pw, cw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
