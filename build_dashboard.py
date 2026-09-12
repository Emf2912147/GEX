#!/usr/bin/env python3
"""
Build the mobile dashboard from captured GEX history.

Reads history/daily_metrics.csv and the most recent raw chain per symbol,
renders one chart per symbol, and writes a self-contained docs/index.html
sized for a phone.

    python build_dashboard.py

Charts are rendered from the RAW chain that was just captured, not by
re-fetching, so the page can never disagree with the stored history.
"""

import argparse
import glob
import html
import json
import os
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import numpy as np
import pandas as pd

import gamma_exposure as gx

SYMBOL_ORDER = ["SPX", "SPY", "QQQ", "IWM"]
TRAIL = 90          # sparkline lookback, observations


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def load_metrics(histdir):
    path = os.path.join(histdir, "daily_metrics.csv")
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["feed_ts"] = df["feed_ts"].astype(str)
    return df.sort_values("feed_ts")


def latest_raw(histdir, symbol):
    """Newest stored chain for a symbol, whichever format it landed in."""
    hits = sorted(glob.glob(os.path.join(histdir, "raw", "*", f"{symbol}.parquet")))
    hits += sorted(glob.glob(os.path.join(histdir, "raw", "*", f"{symbol}.csv.gz")))
    if not hits:
        return None
    path = sorted(hits, key=lambda p: os.path.basename(os.path.dirname(p)))[-1]
    if path.endswith(".parquet"):
        return pd.read_parquet(path)
    return pd.read_csv(path)


def render_chart(raw, row, outpath, max_dte=30, plot_window=0.10,
                 grid_window=0.15, wall_exclude=0.01, bin_size=25):
    df = raw.copy()
    df["dte"] = pd.to_numeric(df["dte"], errors="coerce")
    df = df[df["dte"] > 0]
    spot = float(row["spot"])
    chain = df[df["dte"] <= max_dte].copy()
    if chain.empty:
        return False
    chain["T"] = chain["dte"] / 365.0

    glo, ghi = spot * (1 - grid_window), spot * (1 + grid_window)
    grid, net, flip = gx.gamma_profile(chain, glo, ghi)

    plo, phi = spot * (1 - plot_window), spot * (1 + plot_window)
    win = chain[(chain["strike"] >= plo) & (chain["strike"] <= phi)]
    if win.empty:
        return False
    _, calls, puts = gx.gex_by_strike(win, spot, False, bin_size)
    cw, pw, _ = gx.find_walls(calls, puts, spot, wall_exclude)

    os.makedirs(os.path.dirname(outpath), exist_ok=True)
    gx.plot(row["symbol"], row["feed_ts"], spot, calls, puts, grid, net, flip,
            outpath, "open interest (prior close)", bin_size, cw, pw)
    return True


# --------------------------------------------------------------------------
# Sparkline — one series, so no legend; the card heading names it.
# --------------------------------------------------------------------------

def sparkline(values, width=260, height=44, pad=4):
    vals = [v for v in values if v is not None and not pd.isna(v)]
    if len(vals) < 2:
        return '<div class="spark-empty">not enough history yet</div>'
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts, zero_y = [], None
    for i, v in enumerate(vals):
        x = pad + i * (width - 2 * pad) / (n - 1)
        y = height - pad - (v - lo) / rng * (height - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    if lo <= 0 <= hi:
        zero_y = height - pad - (0 - lo) / rng * (height - 2 * pad)
    last_x, last_y = pts[-1].split(",")
    zero = (f'<line x1="0" y1="{zero_y:.1f}" x2="{width}" y2="{zero_y:.1f}" '
            f'class="spark-zero"/>') if zero_y is not None else ""
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img" '
        f'aria-label="trend, {n} observations, latest {vals[-1]:+.2%}">'
        f'{zero}<polyline points="{" ".join(pts)}" class="spark-line"/>'
        f'<circle cx="{last_x}" cy="{last_y}" r="3.5" class="spark-dot"/></svg>'
    )


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

CSS = """
:root{
  --bg:#fbfbfa; --surface:#fff; --border:#e4e2dd; --ink:#1a1a18;
  --ink-2:#55534e; --ink-3:#85827b;
  --pos:#2e9e5b; --neg:#c0392b; --flip:#8e44ad; --line:#2c3e50;
  --pos-bg:#eaf6ef; --neg-bg:#fbeceb;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#16161a; --surface:#1e1e23; --border:#33333b; --ink:#f0efec;
    --ink-2:#b3b0a8; --ink-3:#807d76;
    --pos:#4cc07c; --neg:#e15c4c; --flip:#b07cd6; --line:#93a7bd;
    --pos-bg:#1a2f22; --neg-bg:#33201d;
  }
}
:root[data-theme="dark"]{
  --bg:#16161a; --surface:#1e1e23; --border:#33333b; --ink:#f0efec;
  --ink-2:#b3b0a8; --ink-3:#807d76;
  --pos:#4cc07c; --neg:#e15c4c; --flip:#b07cd6; --line:#93a7bd;
  --pos-bg:#1a2f22; --neg-bg:#33201d;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  -webkit-text-size-adjust:100%}
.wrap{max-width:720px;margin:0 auto;padding:16px 14px 48px}
header{margin:4px 0 18px}
h1{font-size:20px;margin:0 0 4px;letter-spacing:-0.01em}
.sub{color:var(--ink-3);font-size:12.5px;margin:0}
.card{background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:14px;margin-bottom:14px}
.card-top{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.sym{font-size:19px;font-weight:650;letter-spacing:-0.01em}
.spot{color:var(--ink-2);font-variant-numeric:tabular-nums;font-size:14px}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;
  font-weight:600;padding:3px 9px;border-radius:999px;white-space:nowrap}
.badge.pos{background:var(--pos-bg);color:var(--pos)}
.badge.neg{background:var(--neg-bg);color:var(--neg)}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px 14px;margin:13px 0 4px}
.metric{min-width:0}
.k{font-size:11px;color:var(--ink-3);text-transform:uppercase;
  letter-spacing:0.04em;margin-bottom:2px}
.v{font-size:16px;font-variant-numeric:tabular-nums;letter-spacing:-0.01em}
.v small{font-size:12px;color:var(--ink-2);font-weight:400}
.spark{width:100%;height:44px;display:block;margin-top:4px}
.spark-line{fill:none;stroke:var(--line);stroke-width:1.6;
  stroke-linejoin:round;stroke-linecap:round}
.spark-dot{fill:var(--line)}
.spark-zero{stroke:var(--border);stroke-width:1;stroke-dasharray:3 3}
.spark-empty{color:var(--ink-3);font-size:12px;padding:12px 0}
.coil{margin-top:9px;font-size:12.5px;line-height:1.45;color:var(--flip);
  display:flex;gap:6px;align-items:flex-start}
.coil svg{flex:0 0 auto;margin-top:2px}
.sparkwrap{margin-top:10px;padding-top:10px;border-top:1px solid var(--border)}
details{margin-top:10px}
summary{cursor:pointer;font-size:13px;color:var(--ink-2);padding:4px 0}
.chartbox{overflow-x:auto;margin-top:8px}
.chartbox img{width:100%;min-width:520px;border-radius:8px;display:block}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
th,td{text-align:right;padding:5px 6px;border-bottom:1px solid var(--border);
  font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
th{color:var(--ink-3);font-weight:600;font-size:11px;text-transform:uppercase}
footer{color:var(--ink-3);font-size:12px;line-height:1.65;margin-top:26px;
  border-top:1px solid var(--border);padding-top:14px}
footer b{color:var(--ink-2);font-weight:600}
.empty{background:var(--surface);border:1px dashed var(--border);
  border-radius:12px;padding:26px 16px;text-align:center;color:var(--ink-3)}
"""


def fmt(v, nd=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "&mdash;"
    if pd.isna(f):
        return "&mdash;"
    return f"{f:,.{nd}f}"


def card(row, hist, has_chart):
    sym = row["symbol"]
    spot = float(row["spot"])
    pos = str(row["regime"]) == "positive"
    badge = ("positive gamma &middot; mean-reverting" if pos
             else "negative gamma &middot; trend-amplifying")
    net_bn = float(row["net_gex_full"]) / 1e9

    flip = row.get("flip", "")
    flip_pct = row.get("flip_pct_vs_spot", "")
    try:
        flip_s = f"{float(flip):,.2f}"
        fp = float(flip_pct)
        flip_sub = f"<small> {fp:+.2%} vs spot</small>"
        near = abs(fp) < 0.005
    except (TypeError, ValueError):
        flip_s, flip_sub, near = "&mdash;", "", False

    h = hist[hist["symbol"] == sym].tail(TRAIL)
    spark = sparkline(pd.to_numeric(h["flip_pct_vs_spot"], errors="coerce").tolist())

    rows = ""
    for _, r in h.tail(7)[::-1].iterrows():
        rows += (f"<tr><td>{html.escape(str(r['feed_date']))}</td>"
                 f"<td>{fmt(r['spot'])}</td><td>{fmt(r['flip'])}</td>"
                 f"<td>{fmt(float(r['net_gex_full'])/1e9)}</td>"
                 f"<td>{html.escape(str(r['regime']))}</td></tr>")

    coil = ("<div class='coil'>"
            "<svg width='13' height='13' viewBox='0 0 16 16' aria-hidden='true'>"
            "<circle cx='8' cy='8' r='7' fill='none' stroke='currentColor' "
            "stroke-width='1.5'/><path d='M8 4.5v4.2M8 11.2v.6' "
            "stroke='currentColor' stroke-width='1.5' stroke-linecap='round'/>"
            "</svg><span>Spot is within 0.5% of the flip &mdash; regime "
            "boundary, expect chop.</span></div>") if near else ""

    chart = ""
    if has_chart:
        chart = (f"<details><summary>Chart</summary><div class='chartbox'>"
                 f"<img src='charts/{sym}.png' alt='{sym} gamma exposure by strike' "
                 f"loading='lazy'></div></details>")

    return f"""
<section class="card">
  <div class="card-top">
    <div><span class="sym">{sym}</span>
      <span class="spot">&nbsp;{fmt(spot)}</span></div>
    <span class="badge {'pos' if pos else 'neg'}">{badge}</span>
  </div>
  <div class="grid">
    <div class="metric"><div class="k">Gamma flip</div>
      <div class="v">{flip_s}{flip_sub}</div></div>
    <div class="metric"><div class="k">Net GEX</div>
      <div class="v">{net_bn:,.2f}<small> $Bn / 1%</small></div></div>
    <div class="metric"><div class="k">Call wall</div>
      <div class="v">{fmt(row.get('call_wall'))}</div></div>
    <div class="metric"><div class="k">Put wall</div>
      <div class="v">{fmt(row.get('put_wall'))}</div></div>
  </div>
  {coil}
  <div class="sparkwrap">
    <div class="k">Flip distance from spot &middot; last {len(h)} obs</div>
    {spark}
  </div>
  <details><summary>Recent history</summary>
    <table><thead><tr><th>Feed date</th><th>Spot</th><th>Flip</th>
      <th>Net GEX $Bn</th><th>Regime</th></tr></thead>
      <tbody>{rows}</tbody></table>
  </details>
  {chart}
</section>"""


def build(histdir, docsdir, charts=True):
    metrics = load_metrics(histdir)
    os.makedirs(docsdir, exist_ok=True)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if metrics.empty:
        body = ("<div class='empty'>No captures yet. The dashboard fills in "
                "after the first successful run.</div>")
        feed = "&mdash;"
    else:
        feed = html.escape(str(metrics["feed_ts"].iloc[-1]))
        body = ""
        order = [s for s in SYMBOL_ORDER if s in set(metrics["symbol"])]
        order += [s for s in sorted(set(metrics["symbol"])) if s not in SYMBOL_ORDER]
        for sym in order:
            sub = metrics[metrics["symbol"] == sym]
            row = sub.iloc[-1]
            ok = False
            if charts:
                raw = latest_raw(histdir, sym)
                if raw is not None:
                    try:
                        ok = render_chart(raw, row,
                                          os.path.join(docsdir, "charts", f"{sym}.png"))
                    except Exception as e:
                        print(f"{sym}: chart failed -- {e}")
            body += card(row, metrics, ok)

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>GEX Monitor</title>
<style>{CSS}</style>
</head><body>
<div class="wrap">
  <header>
    <h1>GEX Monitor</h1>
    <p class="sub">Cboe feed {feed} &middot; built {built}</p>
  </header>
  {body}
  <footer>
    <b>Feed date is not the session date.</b> A morning file dated the 5th
    carries the 4th session's settled open interest.<br>
    <b>Open interest settles overnight</b>, so this is prior-close
    positioning, not live. The Cboe feed is ~15 minutes delayed.<br>
    <b>Dealers assumed long calls, short puts</b> &mdash; the standard public
    convention, not a measurement of real positioning.<br>
    <b>Walls ignore strikes within 1% of spot.</b> Gamma peaks at the money,
    so without that exclusion the "wall" is just the ATM strike.<br>
    <b>Chart panels use different gamma sources</b> &mdash; bars from Cboe's
    reported gamma, the profile from Black-Scholes with IV held fixed. The
    flip location is robust; the profile's magnitudes are not comparable to
    the bars.
  </footer>
</div>
</body></html>"""

    out = os.path.join(docsdir, "index.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"dashboard -> {out}  ({len(page)/1024:.1f} KB, "
          f"{0 if metrics.empty else len(metrics)} metric rows)")
    return out


def main():
    p = argparse.ArgumentParser(description="Build the GEX dashboard page.")
    here = os.path.dirname(os.path.abspath(__file__))
    p.add_argument("--histdir", default=os.path.join(here, "history"))
    p.add_argument("--docsdir", default=os.path.join(here, "docs"))
    p.add_argument("--no-charts", action="store_true")
    a = p.parse_args()
    build(a.histdir, a.docsdir, charts=not a.no_charts)


if __name__ == "__main__":
import json
import pandas as pd


def build_latest_json():

    df = pd.read_csv(
        "history/daily_metrics.csv"
    )

    latest = (
        df.sort_values("feed_date")
          .groupby("symbol")
          .tail(1)
    )

    output = {}

    for _, row in latest.iterrows():

        output[row["symbol"]] = {

            "spot": float(row["spot"]),
            "flip": float(row["flip"]),
            "call_wall": float(row["call_wall"]),
            "put_wall": float(row["put_wall"]),

            "plus_1_sigma":
                float(row["plus_1_sigma"]),

            "minus_1_sigma":
                float(row["minus_1_sigma"])
        }

    with open(
        "docs/latest.json",
        "w"
    ) as f:

        json.dump(
            output,
            f,
            indent=2
        )
    
    main()
    def build_latest_json(histdir, docsdir):
    """
    Build lightweight JSON feed for the trading agent.

    Output:
        docs/latest.json
    """

    metrics_file = os.path.join(
        histdir,
        "daily_metrics.csv"
    )

    if not os.path.exists(metrics_file):
        print("latest.json skipped - daily_metrics.csv not found")
        return

    df = pd.read_csv(metrics_file)

    if df.empty:
        print("latest.json skipped - no rows")
        return

    latest = (
        df.sort_values("feed_ts")
          .groupby("symbol")
          .tail(1)
    )

    output = {
        "generated_utc":
        datetime.now(timezone.utc).isoformat()
    }

    for _, row in latest.iterrows():

        symbol = str(row["symbol"])

        output[symbol] = {

            "spot":
                float(row["spot"]),

            "flip":
                float(row["flip"]),

            "call_wall":
                float(row["call_wall"]),

            "put_wall":
                float(row["put_wall"]),

            "regime":
                str(row["regime"]),

            "net_gex_full":
                float(row["net_gex_full"]),

            "flip_pct_vs_spot":
                float(row["flip_pct_vs_spot"])
        }

    outfile = os.path.join(
        docsdir,
        "latest.json"
    )

    with open(
        outfile,
        "w",
        encoding="utf-8"
    ) as fh:

        json.dump(
            output,
            fh,
            indent=2
        )

    print(f"agent feed -> {outfile}")


def main():

    p = argparse.ArgumentParser(
        description="Build the GEX dashboard page."
    )

    here = os.path.dirname(
        os.path.abspath(__file__)
    )

    p.add_argument(
        "--histdir",
        default=os.path.join(
            here,
            "history"
        )
    )

    p.add_argument(
        "--docsdir",
        default=os.path.join(
            here,
            "docs"
        )
    )

    p.add_argument(
        "--no-charts",
        action="store_true"
    )

    a = p.parse_args()

    build(
        a.histdir,
        a.docsdir,
        charts=not a.no_charts
    )

    build_latest_json(
        a.histdir,
        a.docsdir
    )


if __name__ == "__main__":
    main()
