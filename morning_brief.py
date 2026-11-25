#!/usr/bin/env python3
"""
Morning brief for QQQ, SPY, SPX, IWM, SMH (requested by Eugenio, Nov 7 2026).

Read-only: uses only what the GEX repo already stores (docs/latest.json,
history/daily_metrics.csv, history/intraday_state.csv, history/raw).
Prints the plain-text email (default) or JSON (--json).

Per symbol
  levels      graded resistance/support from support_resistance.levels();
              the call/put walls are marked. A wall outside the 1-sigma band
              is shown ungraded.
  last price  last regular-session capture in intraday_state.csv.
  gamma       regime from the settled book; net GEX in $bn, its rank against
              the stored sessions, and the flip's distance from the last price.
  IV          ATM IV30 now against its average over the stored sessions
              (end-of-day value per session, same capture source), plus
              IV/RV and the 7d/30d term ratio from the settled book.
  divergence  hourly RSI(14) divergence from hourly bars built out of the
              15-minute spot captures (setup_quality.divergence, same rules as
              the trade agent). Bearish = price higher high / RSI lower high;
              bullish = price lower low / RSI higher low.
  flows       vanna/charm flow intensity (flow_intensity.py): 0-100 score
              and A-F grade ranking today's dealer hedging flows (every expiry
              to 65 days: the front AND next monthly) against their own
              history, the direction, the front/next split and the roll-off.
              Information only. Read from docs/latest.json, else computed
              read-only from history/flow_metrics.csv.
"""
import argparse
import datetime as dt
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import setup_quality as sq                      # noqa: E402
import support_resistance as sr                 # noqa: E402

SYMBOLS = ["QQQ", "SPY", "SPX", "IWM", "SMH"]
BIN_SIZE = {"SPX": 25, "SPY": 5, "QQQ": 5, "IWM": 2.5, "SMH": 5}
HIST = os.path.join(HERE, "history")
ET = "America/New_York"

NMT_DATA = os.path.join(HERE, "..", "NMT GEX", "data")
_ndx_ratio_cache = None


def nasdaq_ratio():
    """QQQ -> NDX conversion ratio (for translating QQQ GEX walls into
    NQ/MNQ futures terms), averaged over the last few matching sessions in
    the sibling NMT GEX capture (data/QQQ.csv, data/NDX.csv -- same Schwab
    source as the rest of that pipeline). Returns None if that folder or
    data isn't reachable, rather than guessing a number."""
    global _ndx_ratio_cache
    if _ndx_ratio_cache is not None:
        return _ndx_ratio_cache
    try:
        import csv

        def last_closes(fname, n=5):
            with open(os.path.join(NMT_DATA, fname), newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            return {row["date"]: float(row["close"]) for row in rows[-n:] if row.get("close")}

        qqq = last_closes("QQQ.csv")
        ndx = last_closes("NDX.csv")
        ratios = [ndx[d] / qqq[d] for d in qqq if d in ndx and qqq[d]]
        _ndx_ratio_cache = (sum(ratios) / len(ratios)) if ratios else None
    except Exception:
        _ndx_ratio_cache = None
    return _ndx_ratio_cache



def pct_rank(series, value):
    s = pd.Series(series).dropna()
    return float((s <= value).mean() * 100) if len(s) else None


def hourly_bars(state, sym):
    s = state[state["symbol"] == sym].copy()
    s = s[(s["t"].dt.hour >= 9) & (s["t"].dt.hour <= 15)]
    s = s.sort_values("t")
    if s.empty:
        return []
    s["hour"] = s["t"].dt.floor("h")
    bars = []
    for h, g in s.groupby("hour"):
        sp = g["spot"].astype(float)
        bars.append({"begins_at": h.isoformat(), "open_price": float(sp.iloc[0]),
                     "high_price": float(sp.max()), "low_price": float(sp.min()),
                     "close_price": float(sp.iloc[-1])})
    return bars


def iv_block(state, sym):
    s = state[(state["symbol"] == sym) & state["atm_iv_30"].notna()].copy()
    s = s[(s["t"].dt.hour >= 9) & (s["t"].dt.hour <= 15)]
    if s.empty:
        return None
    s["day"] = s["t"].dt.date
    # per-session value: median of the 15:00-16:00 captures (avoids open noise)
    late = s[s["t"].dt.hour == 15]
    daily = late.groupby("day")["atm_iv_30"].median()
    if daily.empty:
        daily = s.groupby("day")["atm_iv_30"].last()
    now = float(daily.iloc[-1])
    hist = daily.iloc[:-1] if len(daily) > 1 else daily
    avg = float(hist.mean())
    return {"iv30_now": now, "iv30_avg": avg, "ratio": now / avg if avg else None,
            "pctile": pct_rank(daily.values, now), "sessions": int(len(daily)),
            "asof_day": str(daily.index[-1])}


def divergence_block(bars):
    if len(bars) < sq.RSI_N + 2 * sq.K_HOURLY + sq.DIV_MIN_GAP:
        return {"state": "n/a", "note": "not enough hourly bars"}
    rs = sq.rsi([b["close_price"] for b in bars])
    bull = sq.divergence(bars, "bull")
    bear = sq.divergence(bars, "bear")
    state, d = "neutral", None
    if bull and bear:
        d = bull if bull["to"] >= bear["to"] else bear
        state = "positive" if d is bull else "negative"
    elif bull:
        state, d = "positive", bull
    elif bear:
        state, d = "negative", bear
    return {"state": state, "detail": d, "rsi_hourly": round(rs[-1], 1) if rs[-1] else None,
            "bars": len(bars)}


def money(x):
    sign = "-" if x < 0 else ""
    x = abs(x)
    return f"{sign}${x/1e9:.2f}bn" if x >= 1e9 else f"{sign}${x/1e6:.0f}m"


def flow_reads(latest=None):
    """{symbol: flow_intensity block} plus the market composite, from
    latest.json; falls back to the stored series (read-only) when the feed
    predates the indicator. Never raises: the email must still go out."""
    try:
        if latest is None:
            latest = json.load(open(os.path.join(HERE, "docs", "latest.json")))
        blocks = {s: (latest.get(s) or {}).get("flow_intensity") for s in SYMBOLS}
        market = (latest.get("market") or {}).get("flow_intensity")
        # a feed built before the 65-day update carries bp fields only: use the
        # stored series instead (it is rebuilt by the same update)
        if all(b is None or "charm_usd_day" not in b for b in blocks.values()):
            import flow_intensity as fi                 # noqa: E402
            hist = fi.load_history(HIST)
            blocks = {s: fi.score_from_history(hist, s) for s in SYMBOLS}
            market = fi.composite(blocks)
        return blocks, market
    except Exception as e:                              # module not pulled yet, etc.
        return {s: None for s in SYMBOLS}, {"error": str(e)}


def _ord(p):
    p = int(round(p))
    suf = "th" if 10 <= p % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(p % 10, "th")
    return f"{p}{suf}"


def _usd(x):
    if x is None:
        return "n/a"
    sign = "+" if x >= 0 else "-"
    x = abs(float(x))
    return f"{sign}${x / 1e9:.1f}bn" if x >= 1e9 else f"{sign}${x / 1e6:.0f}m"


def flow_line(f):
    """Two lines per symbol for the vanna/charm flow intensity: the graded
    total over the front AND next monthly, then the split between them."""
    if not f:
        return "Flows (vanna/charm): n/a."
    if "charm_usd_day" not in f:                      # feed from before the 65-day update
        return "Flows (vanna/charm): n/a — run git pull in C:\\Users\\EMFS\\GEX for the updated indicator."
    if f.get("grade") is None:
        head = f"Flows (vanna/charm): not graded — {f.get('note') or 'no history'}."
    else:
        head = (f"Flows (vanna/charm): {f['score']:.0f}/100, grade {f['grade']} ({f['label']}), "
                f"{f['direction']}.")
    charm = f["charm_usd_day"]
    parts = [f"charm {_usd(charm)}/day ({'dealer buying' if charm >= 0 else 'dealer selling'}"
             + (f", {_ord(f['charm_pct'])} pct" if f.get("charm_pct") is not None else "") + ")"]
    vp, gap = f.get("vanna_potential_usd"), f.get("vol_gap_pts")
    if vp is not None and gap is not None:
        move = "falls" if gap > 0 else "rises"
        parts.append(f"vanna {_usd(vp)} if IV {move} {abs(gap):.1f} pts to realized "
                     f"({'buying' if vp >= 0 else 'selling'}"
                     + (f", {_ord(f['vanna_pct'])} pct" if f.get("vanna_pct") is not None else "") + ")")
    L = [head + " " + "; ".join(parts) + "."]
    fr, nx = f.get("front"), f.get("next")
    if fr and nx:
        def piece(x):
            t = f"charm {_usd(x['charm_usd_day'])}/day, vanna {_usd(x['vanna_usd_per_volpt'])} per vol pt"
            return t
        n = f.get("sessions_to_opex")
        when = f" in {n} session{'s' if n != 1 else ''}" if n is not None else ""
        line = (f"   Front {fr['expiry'][5:]}{when}: {piece(fr)} | "
                f"Next {nx['expiry'][5:]} and its weeklies: {piece(nx)}.")
        if f.get("rolloff_risk"):
            share = f.get("front_charm_share")
            line += (f" {share * 100:.0f}% of the charm bid ends with the front monthly; "
                     "the next month's piece is what remains.")
        L.append(line)
    if f.get("grade") is not None and f.get("note"):
        L[0] += f" Note: {f['note']}."
    return "\n".join(L)


def flow_market_lines(m, blocks):
    if not m:
        return []
    if "error" in m:
        return [f"FLOW INTENSITY: not available on this PC yet ({m['error']}); "
                "run git pull in C:\\Users\\EMFS\\GEX.", ""]
    asof = next((b["session_date"] for b in blocks.values() if b), "?")
    return [f"FLOW INTENSITY (vanna/charm, SPX/QQQ/IWM, front + next monthly, book {asof}): "
            f"{m['score']:.0f}/100, grade {m['grade']} ({m['label']}), {m['direction']}. "
            "Dollar amounts are model estimates (dealers assumed long calls / short puts). "
            "Information only — not a trade signal.", ""]


def build():
    latest = json.load(open(os.path.join(HERE, "docs", "latest.json")))
    metrics = pd.read_csv(os.path.join(HIST, "daily_metrics.csv"), dtype={"feed_ts": str})
    metrics = metrics.sort_values("feed_ts").drop_duplicates(["symbol", "session_date"], keep="last")
    state = pd.read_csv(os.path.join(HIST, "intraday_state.csv"))
    state["t"] = pd.to_datetime(state["capture_ts"], utc=True).dt.tz_convert(ET)

    out = {"generated_utc": latest.get("generated_utc"), "symbols": {}}
    for sym in SYMBOLS:
        d = latest.get(sym)
        if not d:
            out["symbols"][sym] = {"error": "not in latest.json"}
            continue
        r = {}
        st = state[(state["symbol"] == sym) & (state["t"].dt.hour <= 16)]
        st = st[~((st["t"].dt.hour == 16) & (st["t"].dt.minute > 0))]
        last = st.sort_values("t").iloc[-1] if len(st) else None
        spot = float(last["spot"]) if last is not None else float(d["spot"])
        r["last"] = spot
        r["last_at"] = last["t"].isoformat() if last is not None else None
        r["book_spot"] = float(d["spot"])

        # --- levels
        row = metrics[metrics["symbol"] == sym].iloc[-1]
        r["book_session"] = str(row.get("session_date"))
        L = None
        try:
            L = sr.levels(HIST, metrics, sym, BIN_SIZE[sym], row)
        except Exception as e:                       # keep going, say so
            r["levels_error"] = str(e)
        res, sup = [], []
        if L:
            def pack(x):
                return {"strike": x["strike"], "grade": x["grade"], "label": x["label"],
                        "wall": x["wall"], "dist_pct": round((x["strike"] / spot - 1) * 100, 2)}
            res = [pack(x) for x in L["resistance"]]
            sup = [pack(x) for x in L["support"]]
        for col, lst, wl in (("call_wall", res, "call_wall"), ("put_wall", sup, "put_wall")):
            w = d.get(col)
            near = [x for x in lst if abs(x["strike"] - w) <= BIN_SIZE[sym] / 2 + 1e-9] if w is not None else []
            if any(x["wall"] == wl for x in lst):    # already marked by support_resistance
                pass
            elif near:                                # wall sits on a graded bin: mark it
                min(near, key=lambda x: abs(x["strike"] - w))["wall"] = wl
            elif w is not None:
                lst.append({"strike": float(w), "grade": None, "label": "ungraded (outside 1-sigma band)",
                            "wall": wl, "dist_pct": round((w / spot - 1) * 100, 2)})
        # keep only the right side of the CURRENT price, nearest first
        r["resistance"] = sorted([x for x in res if x["strike"] > spot], key=lambda x: x["strike"])
        r["support"] = sorted([x for x in sup if x["strike"] < spot], key=lambda x: -x["strike"])
        r["walls_crossed"] = [x for x in res + sup
                              if x["wall"] and not (x in r["resistance"] or x in r["support"])]

        # --- gamma
        net = float(d["net_gex_full"])
        hist = metrics[metrics["symbol"] == sym]["net_gex_full"].astype(float)
        flip = float(d["flip"]) if d.get("flip") is not None else None
        r["gamma"] = {"regime": d["regime"], "net_gex": net,
                      "net_rank_pct": pct_rank(hist.values, net), "sessions": int(len(hist)),
                      "flip": flip,
                      "flip_vs_last_pct": round((flip / spot - 1) * 100, 2) if flip else None,
                      "vol_trigger": d["vol"].get("vol_trigger")}
        # --- IV
        ivb = iv_block(state, sym)
        r["iv"] = ivb
        v = d["vol"]
        r["vol"] = {k: v.get(k) for k in ("atm_iv_7d", "atm_iv_30d", "term_ratio", "skew_25d",
                                          "rv_20d", "iv_rv_ratio")}
        # --- divergence
        r["divergence"] = divergence_block(hourly_bars(state, sym))
        out["symbols"][sym] = r
    blocks, out["flow_market"] = flow_reads(latest)
    for sym, r in out["symbols"].items():
        if "error" not in r:
            r["flows"] = blocks.get(sym)
    out["flow_blocks"] = blocks
    out["vix"] = latest.get("market", {}).get("vix")
    # freshness: the settled book should describe the last completed session
    import market_calendar as mc
    today = pd.Timestamp.now(tz=ET).date()
    prev = today - dt.timedelta(days=1)
    while not mc.is_trading_day(prev):
        prev -= dt.timedelta(days=1)
    sess = str(metrics["session_date"].max())
    out["book_session"] = sess
    out["prev_session"] = str(prev)
    out["stale"] = sess < str(prev)
    return out



def gamma_iv_div_lines(r, sym=None):
    """The gamma / IV / divergence lines shared by both emails."""
    g, iv, dv = r["gamma"], r["iv"], r["divergence"]
    L = []
    flip_ok = g["flip"] is not None and g["flip_vs_last_pct"] is not None
    where = "above" if flip_ok and g["flip_vs_last_pct"] > 0 else "below"
    flip_desc = (f"flip {g['flip']:.2f}, {abs(g['flip_vs_last_pct']):.1f}% {where} price"
                 if flip_ok else "flip n/a")
    if g["regime"] == "negative":
        L.append(f"Gamma: NEGATIVE ({flip_desc}). "
                 f"Net GEX {money(g['net_gex'])}, more negative than "
                 f"{100 - g['net_rank_pct']:.0f}% of {g['sessions']} stored sessions.")
    else:
        L.append(f"Gamma: positive ({flip_desc}). "
                 f"Net GEX {money(g['net_gex'])}, larger than "
                 f"{g['net_rank_pct']:.0f}% of {g['sessions']} stored sessions.")
    if iv:
        side = "ABOVE" if iv["ratio"] > 1.05 else "BELOW" if iv["ratio"] < 0.95 else "near"
        vv = r.get("vol") or {}
        extra = f"; IV/RV {vv['iv_rv_ratio']:.2f}" if vv.get("iv_rv_ratio") else ""
        term = f"; 7d/30d term {vv['term_ratio']:.2f}" if vv.get("term_ratio") else ""
        L.append(f"IV: 30d ATM {iv['iv30_now']*100:.1f}% vs {iv['iv30_avg']*100:.1f}% average "
                 f"({iv['ratio']:.2f}x, {side} average; {iv['pctile']:.0f}th percentile of "
                 f"{iv['sessions']} sessions){term}{extra}.")
    else:
        L.append("IV: no stored history.")
    if dv["state"] == "n/a":
        L.append(f"Divergence (hourly RSI): n/a — {dv['note']}.")
    elif dv["state"] == "neutral":
        L.append(f"Divergence (hourly RSI): NEUTRAL — none in the last ~3 sessions (hourly RSI {dv['rsi_hourly']}).")
    else:
        d = dv["detail"]
        kind = "bearish" if dv["state"] == "negative" else "bullish"
        L.append(f"Divergence (hourly RSI): {dv['state'].upper()} ({kind}) — price {d['price'][0]:g} → "
                 f"{d['price'][1]:g}, RSI {d['rsi'][0]} → {d['rsi'][1]} "
                 f"({d['from'][5:13].replace('T', ' ')}h → {d['to'][5:13].replace('T', ' ')}h ET); "
                 f"hourly RSI now {dv['rsi_hourly']}.")
    if "flows" in r:
        L.append(flow_line(r["flows"]))
    if sym == "QQQ":
        ratio = nasdaq_ratio()
        if ratio:
            parts = [f"last {r['last']*ratio:,.0f}"]
            if g.get("flip"):
                parts.append(f"flip {g['flip']*ratio:,.0f}")
            if r.get("support"):
                parts.append(f"support {r['support'][0]['strike']*ratio:,.0f}")
            if r.get("resistance"):
                parts.append(f"resistance {r['resistance'][0]['strike']*ratio:,.0f}")
            L.append(f"NDX/NQ/MNQ equiv (~{ratio:.1f}x cash-index terms, check NQ basis vs cash): "
                      + ", ".join(parts) + ".")
    return L


def build_close():
    """Closing-data version: only what yesterday's 4pm captures already hold."""
    state = pd.read_csv(os.path.join(HIST, "intraday_state.csv"))
    state["t"] = pd.to_datetime(state["capture_ts"], utc=True).dt.tz_convert(ET)
    import market_calendar as mc
    today = pd.Timestamp.now(tz=ET).date()
    prev = today - dt.timedelta(days=1)
    while not mc.is_trading_day(prev):
        prev -= dt.timedelta(days=1)
    out = {"mode": "close", "prev_session": str(prev), "symbols": {}}
    last_days = []
    for sym in SYMBOLS:
        s = state[(state["symbol"] == sym) & (state["t"].dt.hour <= 15)].sort_values("t")
        if s.empty:
            out["symbols"][sym] = {"error": "no intraday captures"}
            continue
        last = s.iloc[-1]
        last_days.append(last["t"].date())
        spot = float(last["spot"])
        s["day"] = s["t"].dt.date
        daily_net = s.groupby("day")["net_gex_window"].last().astype(float)
        net = float(last["net_gex_window"])
        flip = float(last["flip"]) if pd.notna(last["flip"]) else None
        r = {"last": spot, "last_at": last["t"].isoformat(),
             "call_wall": float(last["call_wall"]), "put_wall": float(last["put_wall"]),
             "gamma": {"regime": last["regime"], "net_gex": net,
                       "net_rank_pct": pct_rank(daily_net.values, net), "sessions": int(len(daily_net)),
                       "flip": flip,
                       "flip_vs_last_pct": round((flip / spot - 1) * 100, 2) if flip else None},
             "iv": iv_block(state, sym), "vol": None,
             "divergence": divergence_block(hourly_bars(state, sym))}
        out["symbols"][sym] = r
    out["stale"] = bool(last_days) and min(last_days) < prev
    out["last_capture_day"] = str(min(last_days)) if last_days else None
    blocks, out["flow_market"] = flow_reads()
    for sym, r in out["symbols"].items():
        if "error" not in r:
            r["flows"] = blocks.get(sym)
    out["flow_blocks"] = blocks
    return out


def render_close(o):
    now = dt.datetime.now().strftime("%a %b %d")
    L = [f"MORNING LEVELS (CLOSING DATA) — {now}",
         f"Built from the {o['prev_session']} 4:00 PM captures: walls, flip, gamma, IV and divergence. "
         "Walls here are the near-term intraday walls, so they can differ from the settled-book walls in the 7:15 AM ET email, " 
         "which also adds the A–D grades and next levels.", ""]
    if o.get("stale"):
        L += [f"WARNING: the newest capture on this PC is from {o['last_capture_day']} "
              f"(expected {o['prev_session']}) — the PC's GitHub pull may have stalled.", ""]
    L += flow_market_lines(o.get("flow_market"), o.get("flow_blocks") or {})
    for sym, r in o["symbols"].items():
        if "error" in r:
            L += [f"{sym}: {r['error']}", ""]
            continue
        cw, pw, px = r["call_wall"], r["put_wall"], r["last"]
        L.append(f"{sym} Levels:")
        n = 1
        walls = [("Call Wall", cw), ("Put Wall", pw)]
        above = sorted([w for w in walls if w[1] > px], key=lambda w: w[1])
        below = sorted([w for w in walls if w[1] <= px], key=lambda w: -w[1])
        for name, w in above:
            L.append(f"{n}. {name}:  Value: {w:g}  [{(w / px - 1) * 100:+.1f}%]")
            n += 1
        L.append(f"{n}. Last price:  Value: {px:.2f}  ({r['last_at'][:16].replace('T', ' ')} ET)")
        n += 1
        for name, w in below:
            L.append(f"{n}. {name}:  Value: {w:g}  [{(w / px - 1) * 100:+.1f}%]")
            n += 1
        L.append("")
        L += gamma_iv_div_lines(r, sym)
        L.append("")
    return "\n".join(L)


def render(o):
    now = dt.datetime.now().strftime("%a %b %d")
    L = [f"MORNING LEVELS — {now}",
         f"Walls/gamma/IV from the settled book of {o['book_session']}; prices are the {o['prev_session']} close.", ""]
    if o.get("stale"):
        L += [f"WARNING: the local GEX copy only has the {o['book_session']} book (expected {o['prev_session']}) — "
              "run git pull in C:\\Users\\EMFS\\GEX; levels below may be a session old.", ""]
    L += flow_market_lines(o.get("flow_market"), o.get("flow_blocks") or {})
    for sym, r in o["symbols"].items():
        if "error" in r:
            L += [f"{sym}: {r['error']}", ""]
            continue
        g, iv, dv = r["gamma"], r["iv"], r["divergence"]
        L.append(f"{sym} Levels:")
        n = 1
        for x in r["resistance"]:
            if x["wall"] == "call_wall":
                name = "Call Wall Resistance"
            else:
                name = "Next Resistance"
            gr = x["label"] if x["grade"] is None else f"{x['label']} ({x['grade']})"
            L.append(f"{n}. {name}:  Value: {x['strike']:g}: Grade: {gr}  [{x['dist_pct']:+.1f}%]")
            n += 1
        L.append(f"{n}. Last price:  Value: {r['last']:.2f}  ({(r['last_at'] or '')[:16].replace('T', ' ')} ET)")
        n += 1
        for x in r["support"]:
            name = "Put Wall" if x["wall"] == "put_wall" else "Next Support"
            gr = x["label"] if x["grade"] is None else f"{x['label']} ({x['grade']})"
            L.append(f"{n}. {name}:  Value: {x['strike']:g}: Grade: {gr}  [{x['dist_pct']:+.1f}%]")
            n += 1
        for x in r["walls_crossed"]:
            L.append(f"   note: {x['wall'].replace('_', ' ')} {x['strike']:g} is on the other side of the last price")
        L.append("")
        L += gamma_iv_div_lines(r, sym)
        L.append("")
    v = o.get("vix")
    if v:
        L.append(f"VIX {v['vix']} (9d {v['vix9d']}, {v['term_state']}).")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--close", action="store_true",
                    help="closing-data version from yesterday's 4pm captures (no graded levels)")
    a = ap.parse_args()
    o = build_close() if a.close else build()
    if a.json:
        print(json.dumps(o, indent=1, default=str))
    else:
        print(render_close(o) if a.close else render(o))
