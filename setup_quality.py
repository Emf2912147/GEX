#!/usr/bin/env python3
"""
Setup quality -- Eugenio's trade-quality rules, scored 0-100 with two vetoes.

A level setup (bull call vertical at support, bear put vertical at resistance)
can pass every gate in sector_support_agent.py and still be a bad trade: a
bear spread sold into a stock that has already fallen to an oversold extreme,
or a bull spread bought into an overbought run. This module scores the PHASE
of the move. Bull is the exact mirror of bear.

BEAR wants: a market starting to trend down, with room to fall, overextended
upward WITH negative divergence, overvalued against its peers, and the index
in the same condition.
BULL wants: trending up, room to rise, oversold WITH positive divergence,
cheap against its peers, and the index starting to turn up.

RSI LEVELS ARE PER SYMBOL
    Every stock has its own overbought/oversold levels. They are taken from
    the symbol's own daily RSI(14) history (the daily bars supplied, ideally
    ~3 years): oversold = 10th percentile, overbought = 90th percentile
    (OVERSOLD_PCT / OVERBOUGHT_PCT). Fewer than MIN_CAL_BARS RSI readings and
    the levels fall back to 30/70, flagged "rsi_calibrated": false. The output
    also carries the forward 10-day return by RSI zone, so the levels can be
    checked against what the stock actually did.

VETOES (any one blocks the trade; the score is still reported)
    A  bear: daily RSI <= the symbol's oversold level  -> no bear setup
       bull: daily RSI >= the symbol's overbought level -> no bull setup
    B  bear: daily RSI >= overbought AND no bearish hourly divergence
       bull: daily RSI <= oversold  AND no bullish hourly divergence
       (an overextended run without divergence is never a trade)

SCORE (100)
    trend                 25  daily swing structure. Swings: a bar whose high
                              (low) is the extreme of the K_DAILY bars on each
                              side. Over the last 3 swing-high pairs and 3
                              swing-low pairs, count lower highs + lower lows
                              (bear) or higher highs + higher lows (bull);
                              4 or more = full marks, linear below.
    extension+divergence  40  both = 40, divergence alone = 15, extension
                              alone = 0 (extension is only worth something
                              when momentum confirms the turn).
    room                  15  (level - target) / daily ATR(14) >= MIN_ROOM_ATR
                              (2 ATR); 0 below.
    valuation vs peers    10  from the repo's own sector_fundamentals.csv:
                                constituent      value_z against its sector
                                industry ETF     median value_z of its mapped
                                                 constituents (e.g. SMH = semis
                                                 in XLK) -- the industry
                                                 against its sector peers
                                sector ETF       median forward P/E against the
                                                 other captured sectors
                                anything else    neutral (0.5), flagged
                              bear: expensive scores high; bull: cheap does.
    index                 10  the index (QQQ for tech, else SPY) in the same
                              condition: 0 if the index trips veto A itself;
                              else half for its swing trend, half for its own
                              hourly divergence in the trade's direction.

DIVERGENCE (hourly RSI(14))
    Hourly swing highs (lows) with K_HOURLY bars each side. Bearish: a later
    swing high at or above an earlier one while hourly RSI is lower. Bullish:
    a later swing low at or below an earlier one while RSI is higher. The two
    swings must be DIV_MIN_GAP..DIV_MAX_GAP hourly bars apart (about 1-9
    sessions) and the later one within DIV_RECENT bars (~3 sessions) of the
    last bar. Gap and K are starting values, to be tuned by backtest.

Prints JSON only; reads only history/sector/sector_fundamentals.csv.
"""

import json
import math
import os
import statistics as st

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SECT = os.path.join(HERE, "history", "sector")

RSI_N = 14
ATR_N = 14
OVERSOLD_PCT, OVERBOUGHT_PCT = 0.10, 0.90
MIN_CAL_BARS = 250
FALLBACK_OS, FALLBACK_OB = 30.0, 70.0
FWD_DAYS = 10
K_DAILY = 3
K_HOURLY = 3
DIV_MIN_GAP, DIV_MAX_GAP, DIV_RECENT = 7, 63, 21
MIN_ROOM_ATR = 2.0
TREND_FULL = 4
QUALITY_WEIGHTS = {"trend": 25, "extension_divergence": 40, "room": 15,
                   "valuation": 10, "index": 10}
DIV_ONLY_SHARE = 15 / 40
QUALITY_PASS = 60            # suggested minimum total for a recommendation


# --------------------------------------------------------------------------
# bars and indicators
# --------------------------------------------------------------------------

def real_bars(bars):
    return [b for b in (bars or []) if not b.get("interpolated")]


def rsi(closes, n=RSI_N):
    """Wilder RSI; None until n changes are available."""
    out = [None] * len(closes)
    g = l = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        up, dn = max(d, 0.0), max(-d, 0.0)
        if i <= n:
            g += up / n
            l += dn / n
        else:
            g = (g * (n - 1) + up) / n
            l = (l * (n - 1) + dn) / n
        if i >= n:
            out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def atr(bars, n=ATR_N):
    tr = []
    for i in range(1, len(bars)):
        h, lo = float(bars[i]["high_price"]), float(bars[i]["low_price"])
        pc = float(bars[i - 1]["close_price"])
        tr.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return sum(tr[-n:]) / min(n, len(tr)) if tr else None


def swings(vals, k):
    """Indices of swing highs and swing lows (extreme of k bars each side)."""
    hi = [i for i in range(k, len(vals) - k) if vals[i] == max(vals[i - k:i + k + 1])]
    lo = [i for i in range(k, len(vals) - k) if vals[i] == min(vals[i - k:i + k + 1])]
    return hi, lo


def calibrate_rsi(closes):
    """Per-symbol oversold/overbought levels + forward-return table."""
    r = rsi(closes)
    vals = sorted(v for v in r if v is not None)
    if len(vals) < MIN_CAL_BARS:
        return {"oversold": FALLBACK_OS, "overbought": FALLBACK_OB,
                "rsi_calibrated": False, "cal_bars": len(vals), "zones": []}
    pct = lambda p: vals[int(p * (len(vals) - 1))]
    os_, ob = pct(OVERSOLD_PCT), pct(OVERBOUGHT_PCT)
    fwd = [(r[i], closes[i + FWD_DAYS] / closes[i] - 1)
           for i in range(len(closes) - FWD_DAYS) if r[i] is not None]
    zones = []
    for name, lo, hi in (("oversold", -1, os_), ("middle", os_, ob), ("overbought", ob, 101)):
        f = [x for v, x in fwd if lo < v <= hi] if name != "middle" else \
            [x for v, x in fwd if lo < v < hi]
        if f:
            zones.append({"zone": name, "n": len(f),
                          "up_pct": round(100 * sum(x > 0 for x in f) / len(f)),
                          "mean_fwd_pct": round(100 * st.mean(f), 2)})
    if fwd:
        zones.append({"zone": "all", "n": len(fwd),
                      "up_pct": round(100 * sum(x > 0 for _, x in fwd) / len(fwd)),
                      "mean_fwd_pct": round(100 * st.mean(x for _, x in fwd), 2)})
    return {"oversold": round(os_, 1), "overbought": round(ob, 1),
            "rsi_calibrated": True, "cal_bars": len(vals), "zones": zones}


def trend_legs(daily):
    """Lower highs/lows and higher highs/lows over the last 3 swing pairs."""
    hs = [float(b["high_price"]) for b in daily]
    ls = [float(b["low_price"]) for b in daily]
    ph, _ = swings(hs, K_DAILY)
    _, pl = swings(ls, K_DAILY)
    pairs = lambda idx: list(zip(idx[-4:], idx[-3:]))
    lh = sum(hs[b] < hs[a] for a, b in pairs(ph))
    hh = sum(hs[b] > hs[a] for a, b in pairs(ph))
    ll = sum(ls[b] < ls[a] for a, b in pairs(pl))
    hl = sum(ls[b] > ls[a] for a, b in pairs(pl))
    return {"LH": lh, "LL": ll, "HH": hh, "HL": hl,
            "swing_highs": [[daily[i]["begins_at"][:10], hs[i]] for i in ph[-4:]],
            "swing_lows": [[daily[i]["begins_at"][:10], ls[i]] for i in pl[-4:]]}


def divergence(hourly, side):
    """Most recent hourly RSI divergence in the trade's direction, or None."""
    if len(hourly) < RSI_N + 2 * K_HOURLY + DIV_MIN_GAP:
        return None
    hr = rsi([float(b["close_price"]) for b in hourly])
    key = "high_price" if side == "bear" else "low_price"
    price = [float(b[key]) for b in hourly]
    hi, lo = swings(price, K_HOURLY)
    piv = [i for i in (hi if side == "bear" else lo) if hr[i] is not None]
    last = len(hourly) - 1
    for j in range(len(piv) - 1, 0, -1):
        b = piv[j]
        if last - b > DIV_RECENT:
            break
        for i in range(j - 1, -1, -1):
            a = piv[i]
            gap = b - a
            if gap < DIV_MIN_GAP:
                continue
            if gap > DIV_MAX_GAP:
                break
            if side == "bear" and price[b] >= price[a] and hr[b] < hr[a]:
                pass
            elif side == "bull" and price[b] <= price[a] and hr[b] > hr[a]:
                pass
            else:
                continue
            return {"from": hourly[a]["begins_at"], "to": hourly[b]["begins_at"],
                    "bars_apart": gap, "price": [price[a], price[b]],
                    "rsi": [round(hr[a], 1), round(hr[b], 1)]}
    return None


# --------------------------------------------------------------------------
# valuation vs peers (from the repo's own captures)
# --------------------------------------------------------------------------

def peer_valuation(ticker, industry_map, sector_of_industry, fund=None):
    """value_z style number: positive = CHEAPER than peers. None = no peers."""
    import sector_candidates as sc
    if fund is None:
        fund = sc.latest_rows(os.path.join(SECT, "sector_fundamentals.csv"))
    if fund is None or fund.empty:
        return None, "no sector_fundamentals captured"
    f = sc.add_value_z(fund)
    row = f[f["ticker"] == ticker]
    if not row.empty:
        vz = float(row.iloc[0]["value_z"])
        return vz, f"{ticker} value_z {vz:+.2f} vs {row.iloc[0]['sector_etf']} peers"
    if ticker in sector_of_industry:                       # industry ETF, e.g. SMH
        sector = sector_of_industry[ticker]
        names = industry_map.get(sector, {}).get(ticker, [])
        g = f[(f["sector_etf"] == sector) & (f["ticker"].isin(names))]
        if g.empty:
            return None, f"no {ticker} constituents captured"
        vz = float(g["value_z"].median())
        return vz, f"{ticker} constituents median value_z {vz:+.2f} vs {sector} peers ({len(g)} names)"
    sectors = f["sector_etf"].dropna().unique().tolist()
    if ticker in sectors and len(sectors) >= 3:            # sector ETF vs other sectors
        pe = pd.to_numeric(f["forward_pe"], errors="coerce")
        med = f.assign(_pe=pe.where(pe > 0)).groupby("sector_etf")["_pe"].median().dropna()
        if ticker in med.index and med.std(ddof=0):
            z = -float((math.log(med[ticker]) - med.apply(math.log).mean())
                       / med.apply(math.log).std(ddof=0))
            z = max(-2.0, min(2.0, z))
            return z, (f"{ticker} median forward P/E {med[ticker]:.1f} vs "
                       + ", ".join(f"{k} {v:.1f}" for k, v in med.items() if k != ticker))
    return None, f"no peer valuation for {ticker} in the repo -- neutral"


# --------------------------------------------------------------------------
# score
# --------------------------------------------------------------------------

def _clamp(x):
    return max(0.0, min(1.0, float(x)))


def quality(side, ticker, daily, hourly, level, target,
            index_symbol=None, index_daily=None, index_hourly=None,
            value_z=None, value_note=None):
    """side 'bear' | 'bull'. daily: ~3y of daily bars, live price appended as
    the last bar's close. hourly: ~60 sessions of hourly bars."""
    bear = side == "bear"
    daily, hourly = real_bars(daily), real_bars(hourly)
    if len(daily) < RSI_N + 21:
        return {"ticker": ticker, "side": side, "error": f"only {len(daily)} daily bars"}
    closes = [float(b["close_price"]) for b in daily]
    cal = calibrate_rsi(closes)
    r_now = rsi(closes)[-1]
    over = r_now >= cal["overbought"] if bear else r_now <= cal["oversold"]
    against = r_now <= cal["oversold"] if bear else r_now >= cal["overbought"]
    div = divergence(hourly, side)

    vetoes = []
    if against:
        vetoes.append(f"VETO A: {'oversold' if bear else 'overbought'} -- daily RSI "
                      f"{r_now:.1f} vs {ticker}'s {'oversold' if bear else 'overbought'} level "
                      f"{cal['oversold'] if bear else cal['overbought']}")
    if over and not div:
        vetoes.append(f"VETO B: {'overbought' if bear else 'oversold'} run with no "
                      f"{'bearish' if bear else 'bullish'} hourly divergence")

    tl = trend_legs(daily)
    legs = (tl["LH"] + tl["LL"]) if bear else (tl["HH"] + tl["HL"])
    a = atr(daily)
    room_atr = (abs(level - target) / a) if a else 0.0

    pts = {}
    pts["trend"] = QUALITY_WEIGHTS["trend"] * min(1.0, legs / TREND_FULL)
    pts["extension_divergence"] = QUALITY_WEIGHTS["extension_divergence"] * (
        1.0 if (over and div) else (DIV_ONLY_SHARE if div else 0.0))
    pts["room"] = QUALITY_WEIGHTS["room"] if room_atr >= MIN_ROOM_ATR else 0.0
    if value_z is None:
        p_val = 0.5
    else:
        p_val = _clamp(0.5 - value_z / 4) if bear else _clamp(0.5 + value_z / 4)
    pts["valuation"] = QUALITY_WEIGHTS["valuation"] * p_val

    ix = None
    if index_symbol and index_daily:
        idd = real_bars(index_daily)
        ic = [float(b["close_price"]) for b in idd]
        ical = calibrate_rsi(ic)
        ir = rsi(ic)[-1]
        i_against = ir <= ical["oversold"] if bear else ir >= ical["overbought"]
        itl = trend_legs(idd)
        ilegs = (itl["LH"] + itl["LL"]) if bear else (itl["HH"] + itl["HL"])
        idiv = divergence(real_bars(index_hourly), side)
        p_ix = 0.0 if i_against else 0.5 * min(1.0, ilegs / TREND_FULL) + (0.5 if idiv else 0.0)
        pts["index"] = QUALITY_WEIGHTS["index"] * p_ix
        ix = {"symbol": index_symbol, "rsi": round(ir, 1), "oversold": ical["oversold"],
              "overbought": ical["overbought"], "against": i_against,
              "trend_legs": {k: itl[k] for k in ("LH", "LL", "HH", "HL")},
              "divergence": idiv}
    else:
        pts["index"] = QUALITY_WEIGHTS["index"] * 0.5

    total = round(sum(pts.values()), 1)
    return {
        "ticker": ticker, "side": side, "quality": total,
        "tradeable": not vetoes, "passes": (not vetoes) and total >= QUALITY_PASS,
        "vetoes": vetoes, "points": {k: round(v, 1) for k, v in pts.items()},
        "rsi": {"daily": round(r_now, 1), **{k: cal[k] for k in
                ("oversold", "overbought", "rsi_calibrated", "cal_bars", "zones")}},
        "overextended": over, "divergence": div,
        "trend": tl, "atr": round(a, 2) if a else None,
        "room_atr": round(room_atr, 2), "level": level, "target": target,
        "valuation": {"value_z": None if value_z is None else round(value_z, 2),
                      "note": value_note},
        "index": ix,
    }
