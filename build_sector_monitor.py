#!/usr/bin/env python3
"""
Sector Trade Monitor -- builds docs/sector.html from the sector agent's
ranked candidate tables. Published by GitHub Pages next to the GEX monitor
(docs/index.html), at <pages-url>/sector.html.

    python build_sector_monitor.py

Reads only history/sector/sector_candidates_options.csv and
sector_candidates_stock.csv (latest capture), plus the two capture logs'
CSV timestamps for the "as of" line. Fetches nothing. Writes one
self-contained HTML file; no data files are added to docs/.

Display: per sector, up to 3 longs and 3 shorts in each table -- options
(liquid chains) and shares (illiquid options, liquid stock). The cap is
applied here as well as upstream, so a future change to TOP_K in
sector_candidates.py cannot lengthen the page.
"""
import argparse
import html
import os
from datetime import datetime, timezone

import pandas as pd

MAX_PER_SIDE = 3
SECTOR_NAMES = {
    "XLF": "Financials", "XLK": "Technology", "XLP": "Consumer Staples",
    "XLV": "Health Care",
}
GATE_LABELS = {
    "negative_fcf": "neg FCF",
    "compressing_gross_margin": "gross margin ↓",
    "compressing_operating_margin": "op margin ↓",
    "net_debt_over_3x_ebitda": "debt > 3x",
    "filing_estimated_in_window": "earnings ≤14d",
}
TABLES = (("options", "Options", "liquid chain: spread ≤ 10%, OI ≥ 100"),
          ("stock", "Shares", "options too thin; stock trades ≥ $50M/day"))


BASE_COLS = ["capture_ts", "sector_etf", "side", "rank", "ticker",
             "n_in_table", "n_names_in_sector", "trade_quality"]


def latest(path):
    empty = pd.DataFrame(columns=BASE_COLS)
    if not os.path.exists(path):
        return empty
    df = pd.read_csv(path, dtype={"next_filing_est": str, "failed_gates": str})
    if df.empty:
        return empty
    return df[df["capture_ts"] == df["capture_ts"].max()].copy()


def last_ts(path):
    try:
        s = pd.read_csv(path, usecols=["capture_ts"])["capture_ts"]
        return pd.to_datetime(s, format="mixed", utc=True).max()
    except Exception:
        return None


def esc(x):
    return html.escape(str(x))


def num(v):
    v = pd.to_numeric(v, errors="coerce")
    return None if pd.isna(v) else float(v)


def pct(v, nd=1, sign=False):
    v = num(v)
    if v is None:
        return "&mdash;"
    return f"{v:+.{nd}%}" if sign else f"{v:.{nd}%}"


def money(v):
    v = num(v)
    if v is None:
        return "&mdash;"
    return f"${v/1e9:.1f}B" if v >= 1e9 else f"${v/1e6:.0f}M"


def et(ts):
    """UTC timestamp -> 'Sat Oct 10, 7:50 AM ET' (EDT/EST by date)."""
    if ts is None:
        return "&mdash;"
    try:
        import market_calendar as cal
        local = cal.et_wall_clock(ts.to_pydatetime())
    except Exception:
        local = (ts - pd.Timedelta(hours=4)).to_pydatetime()
    return local.strftime("%a %b %-d, %-I:%M %p ET")


def grade_badge(r):
    g = str(r.get("grade") or "")
    tq = num(r.get("trade_quality"))
    if not g or tq is None:
        return "<span class='gr gr-x'>&mdash;</span>"
    return f"<span class='gr gr-{g.lower()}' title='Trade quality {tq:.0f}/100'>{g}<b>{tq:.0f}</b></span>"


def parts_bar(r):
    """Four-segment bar: conviction / momentum / execution / event+carry,
    each segment's fill = that component's 0..1 score."""
    keys = (("tq_conviction", "Conviction", 30), ("tq_momentum", "Momentum", 25),
            ("tq_execution", "Execution", 20), ("tq_event_carry", "Event / carry", 25))
    segs = []
    for k, label, w in keys:
        v = num(r.get(k))
        v = 0.0 if v is None else max(0.0, min(1.0, v))
        segs.append(f"<span class='seg' style='flex:{w}' title='{label} {v:.0%}'>"
                    f"<i style='width:{v*100:.0f}%'></i></span>")
    return f"<div class='parts'>{''.join(segs)}</div>"


def chips(r, side):
    out = []
    for g in str(r.get("failed_gates") or "").split(";"):
        g = g.strip()
        if g and g != "nan":
            out.append(f"<span class='chip chip-neg'>{esc(GATE_LABELS.get(g, g))}</span>")
    nf = str(r.get("next_filing_est") or "")
    if nf and nf != "nan" and "filing_estimated_in_window" not in str(r.get("failed_gates")):
        out.append(f"<span class='chip'>earnings ~{esc(nf[5:])}</span>")
    vt = str(r.get("value_turn") or "")
    if vt and vt != "nan":
        out.append(f"<span class='chip chip-pos' title='{esc(vt.replace(';', ', '))}'>"
                   f"deep value &middot; improving</span>")
    dy = num(r.get("dividend_yield"))
    if side == "short" and dy and dy >= 0.03:
        out.append(f"<span class='chip chip-warn'>pays {dy:.1%} div</span>")
    return "".join(out)


def valuation_line(r):
    bits = []
    pb, ev = num(r.get("price_to_book")), num(r.get("ev_to_ebitda"))
    fy, pe = num(r.get("fcf_yield")), num(r.get("forward_pe"))
    if pb: bits.append(f"P/B {pb:.1f}x")
    if ev: bits.append(f"EV/EBITDA {ev:.1f}x")
    if fy is not None: bits.append(f"FCF yld {fy:.1%}")
    if pe: bits.append(f"fwd P/E {pe:.1f}")
    vz = num(r.get("value_z"))
    if vz is not None and bits:
        word = "cheap" if vz >= 0.5 else ("rich" if vz <= -0.5 else "in line")
        bits.append(f"{word} vs sector ({vz:+.1f}&sigma;)")
    return " · ".join(bits) or "valuation n/a"


def row_html(r, side, table):
    rel20, rel60 = r.get("rel_strength_20d"), r.get("rel_strength_60d")
    if table == "options":
        exe = f"spread {pct(r.get('atm_spread_pct'))} · OI {num(r.get('atm_oi')) or 0:,.0f}"
    else:
        exe = f"{money(r.get('adv_usd_20d'))}/day"
    spot = num(r.get("spot"))
    spot_s = f"{spot:,.2f}" if spot else ""
    return f"""
<li class="pick">
  <div class="pick-top">
    <span class="rk">{int(num(r.get('rank')) or 0)}</span>
    <span class="tk">{esc(r['ticker'])}</span>
    <span class="px">{spot_s}</span>
    {grade_badge(r)}
  </div>
  {parts_bar(r)}
  <div class="meta">vs sector {pct(rel20, sign=True)} 20d · {pct(rel60, sign=True)} 60d
    <span class="dot">·</span> {exe} <span class="dot">·</span> score {num(r.get('score')) or 0:+.2f}</div>
  <div class="meta">{valuation_line(r)}</div>
  <div class="chips">{chips(r, side)}</div>
</li>"""


def side_html(df, side, table):
    sel = df[df["side"] == side].sort_values("rank").head(MAX_PER_SIDE)
    head = "Long" if side == "long" else "Short"
    if sel.empty:
        why = ("no name passes every quality gate" if side == "long"
               else "no candidate")
        body = f"<div class='empty'>None &mdash; {why}</div>"
    else:
        body = "<ol class='picks'>" + "".join(
            row_html(r, side, table) for _, r in sel.iterrows()) + "</ol>"
    return (f"<div class='side side-{side}'><div class='side-h'>{head}</div>"
            f"{body}</div>")


def sector_card(sym, opt, stk):
    n_total = None
    blocks = []
    for key, label, note in TABLES:
        df = (opt if key == "options" else stk)
        df = df[df["sector_etf"] == sym]
        n_in = int(df["n_in_table"].iloc[0]) if not df.empty else 0
        if not df.empty:
            n_total = int(df["n_names_in_sector"].iloc[0])
        blocks.append(f"""
  <div class="tbl">
    <div class="tbl-h"><span class="tbl-name">{label}</span>
      <span class="tbl-note">{n_in} names · {note}</span></div>
    <div class="sides">{side_html(df, 'long', key)}{side_html(df, 'short', key)}</div>
  </div>""")
    sub = f"top {n_total} holdings by weight" if n_total else ""
    return f"""
<section class="card">
  <div class="card-top"><div><span class="sym">{sym}</span>
    <span class="sname">{esc(SECTOR_NAMES.get(sym, ''))}</span></div>
    <span class="sub">{sub}</span></div>
  {''.join(blocks)}
</section>"""


LIQ_LABEL = {"options": "options liquid", "stock": "shares liquid (options thin)",
             "illiquid": "below the liquidity screens &mdash; scored by exception"}


def watch_card(wch):
    """Always-scored names (CPB), shown whatever their liquidity."""
    if wch.empty:
        return ""
    items = []
    for _, r in wch.iterrows():
        natural = str(r.get("table", "watch-illiquid"))[6:]
        side = str(r.get("side"))
        tbl = natural if natural in ("options", "stock") else "stock"
        if side == "none":
            verdict = ("<span class='chip chip-pos'>no short: deep value / below book</span>")
        else:
            verdict = (f"<span class='chip {'chip-pos' if side == 'long' else 'chip-neg'}'>"
                       f"scores as a {side}</span>")
        rank = f"ranks {int(num(r.get('rank')) or 0)} of {int(num(r.get('n_in_table')) or 0)} in {esc(r['sector_etf'])}"
        items.append(f"""
<div class="wl-head">{verdict} <span class="tbl-note">{rank} &middot; {LIQ_LABEL.get(natural, natural)}</span></div>
<ol class="picks">{row_html(r, side if side in ('long', 'short') else 'long', tbl)}</ol>""")
    return (f"<section class='card wl'><div class='k'>Watchlist &mdash; always scored</div>"
            f"{''.join(items)}</section>")


def best_setups(opt, stk, n=3):
    allr = pd.concat([opt.assign(tbl="Options"), stk.assign(tbl="Shares")],
                     ignore_index=True)
    if allr.empty:
        return ""
    allr["tq"] = pd.to_numeric(allr["trade_quality"], errors="coerce")
    out = []
    for side, label in (("long", "Longs"), ("short", "Shorts")):
        top = allr[allr["side"] == side].sort_values("tq", ascending=False).head(n)
        items = "".join(
            f"<li><span class='tk'>{esc(r['ticker'])}</span> {grade_badge(r)}"
            f"<span class='bs-meta'>{esc(r['sector_etf'])} · {r['tbl']}</span></li>"
            for _, r in top.iterrows())
        out.append(f"<div class='bs-col'><div class='side-h side-h-{side}'>{label}</div>"
                   f"<ul class='bs'>{items}</ul></div>")
    return (f"<section class='card'><div class='k'>Highest trade quality, all sectors</div>"
            f"<div class='bs-wrap'>{''.join(out)}</div></section>")


LEGEND = """
<details class="card legend"><summary>How picks and grades are made</summary>
<h3>Which table</h3>
<p><b>Options</b>: the monthly expiry nearest 30 days, ten contracts nearest
the money, median bid/ask spread &le; 10% and median open interest &ge; 100.
<b>Shares</b>: options fail that, but the stock's median daily dollar volume
over 20 sessions is &ge; $50M. Neither: not shown.</p>
<h3>Quality gates</h3>
<p>Negative free cash flow; gross or operating margin below the prior fiscal
year; net debt above 3&times; EBITDA; estimated earnings within 14 days.
A <b>long</b> must pass all of them. A <b>short</b> comes from names that fail
at least one, weakest first.</p>
<h3>Ranking</h3>
<p>Composite = 2 &times; quality + momentum + 0.25 &times; value
(+ 0.5 deep-value bonus). Quality = revenue growth + operating margin &minus;
net debt/EBITDA &divide; 10 &minus; 0.5 per failed gate. Momentum = average
20- and 60-day return relative to the sector ETF. Up to three longs and three
shorts per table.</p>
<h3>Valuation</h3>
<p><b>Value</b> = how cheap the stock is against its own sector, in standard
deviations: price-to-book for financials; EV/EBITDA and free-cash-flow yield
for the others. <b>Below book value (P/B &lt; 1) is never a short</b> &mdash;
deep value tends to range, not fall further. A <b>deep-value</b> stock (below
book, or 1&sigma;+ cheaper than its sector) that is <b>improving</b> &mdash; at
least two of: operating margin up, gross margin up, revenue growing, beating
its sector &mdash; gets a bonus as a long. Cheap and not improving gets
nothing extra.</p>
<h3>Watchlist</h3>
<p>CPB is scored every run by exception, even below the liquidity screens or
outside XLP&rsquo;s top 30. Its rank is among every scorable name in its
sector. It joins the ranked lists only if it qualifies on its own.</p>
<h3>Trade quality (0&ndash;100)</h3>
<p>The bar under each name shows its four parts, left to right:</p>
<ul>
<li><b>Conviction (30)</b> &mdash; how far the composite stands from the sector
average: 2 standard deviations in the trade's direction is full marks.</li>
<li><b>Momentum (25)</b> &mdash; price confirms the side: +10% vs the sector over
20/60 days is full for a long, &minus;10% for a short.</li>
<li><b>Execution (20)</b> &mdash; options: tighter spread and deeper open
interest; shares: daily dollar volume ($2B+ is full).</li>
<li><b>Event / carry (25)</b> &mdash; earnings within 14 days costs half; a
short also loses up to half for the dividend it pays (8%+ costs the half).</li>
</ul>
<p><span class='gr gr-a'>A</span> &ge; 75 &nbsp; <span class='gr gr-b'>B</span> 60&ndash;74
&nbsp; <span class='gr gr-c'>C</span> 45&ndash;59 &nbsp; <span class='gr gr-d'>D</span> &lt; 45</p>
<p class="fine">Fundamentals are annual (latest fiscal year vs prior) from
Yahoo Finance; options from Cboe delayed quotes; earnings dates are estimates.
Screening output, not a recommendation.</p>
</details>"""

CSS = """
:root{
  --bg:#fbfbfa; --surface:#fff; --border:#e4e2dd; --ink:#1a1a18;
  --ink-2:#55534e; --ink-3:#85827b;
  --pos:#2e9e5b; --neg:#c0392b; --warn:#b7791f; --line:#2c3e50;
  --pos-bg:#eaf6ef; --neg-bg:#fbeceb; --warn-bg:#fdf3e1; --chip:#f1efea;
  --ga:#2e9e5b; --gb:#3b7dd8; --gc:#b7791f; --gd:#c0392b;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#16161a; --surface:#1e1e23; --border:#33333b; --ink:#f0efec;
    --ink-2:#b3b0a8; --ink-3:#807d76;
    --pos:#4cc07c; --neg:#e15c4c; --warn:#e0a84a; --line:#93a7bd;
    --pos-bg:#1a2f22; --neg-bg:#33201d; --warn-bg:#33291a; --chip:#2a2a30;
    --ga:#4cc07c; --gb:#6aa5f0; --gc:#e0a84a; --gd:#e15c4c;
  }
}
:root[data-theme="dark"]{
  --bg:#16161a; --surface:#1e1e23; --border:#33333b; --ink:#f0efec;
  --ink-2:#b3b0a8; --ink-3:#807d76;
  --pos:#4cc07c; --neg:#e15c4c; --warn:#e0a84a; --line:#93a7bd;
  --pos-bg:#1a2f22; --neg-bg:#33201d; --warn-bg:#33291a; --chip:#2a2a30;
  --ga:#4cc07c; --gb:#6aa5f0; --gc:#e0a84a; --gd:#e15c4c;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  -webkit-text-size-adjust:100%}
.wrap{max-width:860px;margin:0 auto;padding:16px 14px 48px}
header{margin:4px 0 18px}
h1{font-size:20px;margin:0 0 4px;letter-spacing:-0.01em}
.sub{color:var(--ink-3);font-size:12.5px;margin:0}
.nav{font-size:12.5px;margin-top:6px}
.nav a{color:var(--ink-2)}
.card{background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:14px;margin-bottom:14px}
.card-top{display:flex;align-items:baseline;justify-content:space-between;gap:10px;flex-wrap:wrap}
.sym{font-size:19px;font-weight:650;letter-spacing:-0.01em}
.sname{color:var(--ink-2);font-size:14px;margin-left:6px}
.k{font-size:11px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px}
.tbl{margin-top:12px;padding-top:10px;border-top:1px solid var(--border)}
.tbl-h{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.tbl-name{font-weight:650;font-size:14px}
.tbl-note{font-size:11.5px;color:var(--ink-3)}
.sides{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media (max-width:620px){.sides{grid-template-columns:1fr}}
.side-h{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
.side-long .side-h,.side-h-long{color:var(--pos)}
.side-short .side-h,.side-h-short{color:var(--neg)}
.picks{list-style:none;margin:0;padding:0}
.pick{padding:7px 0;border-bottom:1px dashed var(--border)}
.pick:last-child{border-bottom:0}
.pick-top{display:flex;align-items:center;gap:8px}
.rk{font-size:11px;color:var(--ink-3);width:12px}
.tk{font-weight:650;letter-spacing:.01em}
.px{color:var(--ink-3);font-size:12px;font-variant-numeric:tabular-nums}
.gr{margin-left:auto;display:inline-flex;align-items:baseline;gap:5px;font-weight:700;
  font-size:12px;padding:2px 8px;border-radius:999px;color:#fff;white-space:nowrap}
.gr b{font-weight:500;font-size:11px;opacity:.9;font-variant-numeric:tabular-nums}
.legend .gr,.bs .gr{margin-left:0}
.gr-a{background:var(--ga)} .gr-b{background:var(--gb)}
.gr-c{background:var(--gc)} .gr-d{background:var(--gd)} .gr-x{background:var(--ink-3)}
.parts{display:flex;gap:2px;margin:5px 0 3px 20px;height:5px}
.seg{background:var(--chip);border-radius:2px;overflow:hidden;display:block}
.seg i{display:block;height:100%;background:var(--line);opacity:.75}
.meta{font-size:11.5px;color:var(--ink-2);margin-left:20px;font-variant-numeric:tabular-nums}
.dot{color:var(--ink-3)}
.chips{margin:3px 0 0 20px;display:flex;flex-wrap:wrap;gap:4px}
.chip{font-size:10.5px;padding:1px 7px;border-radius:999px;background:var(--chip);color:var(--ink-2)}
.chip-neg{background:var(--neg-bg);color:var(--neg)}
.chip-pos{background:var(--pos-bg);color:var(--pos)}
.chip-warn{background:var(--warn-bg);color:var(--warn)}
.wl .rk{display:none}
.wl .parts,.wl .meta,.wl .chips{margin-left:0}
.wl-head{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:2px}
.empty{font-size:12.5px;color:var(--ink-3);padding:6px 0}
.bs-wrap{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.bs{list-style:none;margin:0;padding:0}
.bs li{display:flex;align-items:center;gap:8px;padding:4px 0}
.bs-meta{font-size:11.5px;color:var(--ink-3)}
details.legend summary{cursor:pointer;font-size:13.5px;color:var(--ink-2)}
.legend h3{font-size:13px;margin:12px 0 2px}
.legend p,.legend li{font-size:12.5px;color:var(--ink-2);margin:2px 0}
.legend ul{padding-left:18px;margin:4px 0}
.fine{font-size:11px !important;color:var(--ink-3) !important;margin-top:10px !important}
"""


def build(histdir, out_path):
    opt = latest(os.path.join(histdir, "sector_candidates_options.csv"))
    stk = latest(os.path.join(histdir, "sector_candidates_stock.csv"))
    wch = latest(os.path.join(histdir, "sector_candidates_watch.csv"))
    f_ts = last_ts(os.path.join(histdir, "sector_fundamentals.csv"))
    t_ts = last_ts(os.path.join(histdir, "sector_technicals.csv"))

    sectors = sorted(set(opt["sector_etf"]) | set(stk["sector_etf"]))
    cards = "".join(sector_card(s, opt, stk) for s in sectors) or \
        "<section class='card'><div class='empty'>No candidates captured yet.</div></section>"

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sector Trade Monitor</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<header>
  <h1>Sector Trade Monitor</h1>
  <p class="sub">Prices &amp; options {et(t_ts)} &middot; fundamentals {et(f_ts)}</p>
  <p class="sub">Updates Tue &amp; Wed evenings after the close.</p>
  <div class="nav"><a href="index.html">&larr; GEX Monitor</a></div>
</header>
{watch_card(wch)}
{best_setups(opt, stk)}
{cards}
{LEGEND}
</div></body></html>"""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return out_path


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description="Build docs/sector.html")
    p.add_argument("--histdir", default=os.path.join(here, "history", "sector"))
    p.add_argument("--out", default=os.path.join(here, "docs", "sector.html"))
    a = p.parse_args()
    print(build(a.histdir, a.out))


if __name__ == "__main__":
    main()
