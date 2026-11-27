# GEX

Daily gamma-exposure history for SPX, SPY, QQQ, IWM, XLK, SMH, XLF, GLD and TLT,
built from Schwab option chains.

## Status (2026-11-25)

**Schwab is the data source**, via the `gex-trading-agent` Docker container's
Schwab Trader API connection (shared login with the NMT monitor). Both
captures run on the PC as Windows scheduled tasks, not on GitHub Actions:
Schwab and tastytrade credentials cannot live on a public runner. See
`SCHWAB_FALLBACK.md` for the architecture.

The VIX complex -- VIX, VIX9D, VIX3M, VVIX and the front two /VX futures --
comes from tastytrade (`tastytrade_vix.py` on the PC, preferred since
2026-11-25), with Yahoo (`index_history.py`) as the fallback for VIX/VIX9D.
The daily closes behind the VIX 20-day change, realized vol (each symbol's
own series) and the long-vol block also come from tastytrade first, Yahoo
second (`index_history.py`).

**Dashboard:** enable GitHub Pages (Settings -> Pages -> Deploy from a branch
-> `main` / `/docs`), then the page lives at
`https://<your-username>.github.io/<repo>/`.

## What runs

- **Daily** (`schwab_capture_auto.bat` -> `schwab_daily.py`, ~7am ET on the
  PC): each symbol's chain from Schwab, stored raw, one row of derived
  metrics appended, dashboard rebuilt, committed and pushed.
- **Intraday** (`schwab_intraday_auto.bat` -> `gex_intraday.py --pull`,
  every 15 min in session on the PC): slim snapshot, state vector, VIX state.
- **GitHub Actions**: only the pipeline watchdog and the sector-agent jobs.

## Layout

| path | what |
|---|---|
| `gamma_exposure.py` | chart builder, importable analysis functions |
| `gex_capture.py` | daily snapshot + metrics |
| `flow_intensity.py` | Vanna/charm flow intensity: 0-100 score and A-F grade of today's dealer hedging flows against their own history; stores `history/flow_metrics.csv` |
| `flow_outcomes.py` | Opex / flow-intensity outcome log: each graded session's flow read, its flags (roll-off, post-opex, high vanna selling/buying, high charm bid) and the 1/5-day return and 5-day realized vol that followed; rebuilds `history/flow_outcomes.csv` daily; `python flow_outcomes.py --opex` prints the summary and per-expiry table |
| `rsi_divergence.py` | RSI divergence gate (G10): 1H / 1D / 1W state and A-F grade for a trade's bias; stdlib only, takes bars from the caller |
| `build_dashboard.py` | renders `docs/index.html` |
| `history/raw/<feed-date>/<SYM>.parquet` | full chain, every contract |
| `history/daily_metrics.csv` | one row per symbol per day |
| `docs/` | the published dashboard |

## Raw chains are replay material; the derived metrics are the record

`daily_metrics.csv` is a cache that can be rebuilt from `history/raw/` under a
new methodology, and the wall definition has already changed twice. So raw
chains are kept for a rolling window (see **Retention** below) and the derived
metrics are kept forever. Within the window a formula change can be replayed
over every stored session; before it, the old derived rows stand as computed.

## Agent feed: `docs/latest.json`

One record per symbol for the trading agent (gex-trading-agent): `spot`,
`flip`, `call_wall`, `put_wall`, `regime`, `net_gex_full`,
`flip_pct_vs_spot`. Since 2026-10-17 each record also carries a `vol` block
(`vol_metrics.py`), computed from the same raw chain as the walls:

| field | meaning |
|---|---|
| `atm_iv_7d`, `atm_iv_30d` | constant-maturity ATM IV (decimal), OTM quotes, variance-interpolated |
| `term_ratio` | `atm_iv_7d / atm_iv_30d`; above 1 = inverted front end |
| `skew_25d` | 25-delta put IV minus 25-delta call IV, ~30-day expiry |
| `vol_trigger` | price where the dealer gamma profile recovers to 25% of its peak; below it long-gamma support is weak or gone |
| `rv_20d`, `rv_source`, `rv_proxy` | 20-session realized vol: `realized_vol.csv` (index_history daily closes) when fresh, else this repo's own closes |
| `iv_rv_ratio` | `atm_iv_30d / rv_20d`; above 1 = options rich to recent movement |
| `smile` | put/call IV by strike for the expiry nearest 7 days, strikes within 8% of spot; the agent prices its spreads from it (model value, not a fill) |

and a `flows` block (`dealer_flows.py`): dealer vanna and charm hedge flows
in basis points of the 0.5-30 day open-interest notional (`charm_bp_day`,
`vanna_bp_per_volpt`, positive = dealer buying) and `charm_support_ratio`,
today's charm against its own median of the previous 20 sessions (null under
10). Same dealers-long-calls / short-puts convention as the GEX numbers;
an assumption, not observed positioning.

Since 2026-11-17 each record also carries a `flow_intensity` block
(`flow_intensity.py`) and `market.flow_intensity` the SPX/QQQ/IWM average.
Since the same day's update it measures every expiry from 0.5 to 65 calendar
days, which always holds the front monthly AND the next one, in dollars of
underlying dealers trade (same model and convention as `dealer_flows.py`,
whose own `flows` block keeps its 30-day window):

| Field | Meaning |
|---|---|
| `score`, `grade`, `label` | 0-100 = 0.5 x charm percentile + 0.5 x vanna percentile; A >= 80 very strong, B 60-79, C 40-59, D 20-39, F < 20. No grade under 15 prior sessions |
| `direction` | `dealer buying`, `dealer selling` or `mixed` (charm and vanna disagree) |
| `charm_usd_day`, `charm_pct` | charm flow in $/day and the percentile of its size over the last 60 sessions |
| `vanna_usd_per_volpt`, `vol_gap_pts`, `vanna_potential_usd`, `vanna_pct` | vanna in $ per vol point; (30d IV - 20d RV) in points, clipped +/-10; vanna flow if IV moves to realized; percentile of its size |
| `front`, `next` | the same flows split at the front monthly: `front` = expiries on or before it, `next` = after it (the next monthly and its weeklies); front + next = total |
| `opex_date`, `sessions_to_opex`, `opex_oi_share`, `front_charm_share`, `rolloff_risk` | front monthly, sessions to it, its share of the window's OI, the share of today's charm it carries; roll-off flag at <= 5 sessions and >= 50% |

Effect of including the next month (all 41 stored sessions re-scored, 104
graded SPX/SPY/QQQ/IWM readings): score rank correlation with the 30-day
version 0.85, median shift 5 points, same grade 65%, within one grade 94%.
Since Oct 21 (deeper history): correlation 0.94, median shift 3 points, same
grade 80%. The larger differences sit in early October (thin history) and on
expiration days, where the dollar charm now drops with the expiring monthly.

Information only: the weights and the IV-toward-realized assumption are not
fitted, and over the first 22 graded sessions the score showed no reliable
link to next-day or 5-day returns. Rebuild the stored series from the raw
chains with `python flow_intensity.py --backfill`.

Since 2026-11-26 each record also carries a `levels` block: the graded
support / resistance the GEX Monitor cards show (`support_resistance.py`),
inside +/-1 sigma of spot. `support` and `resistance` list up to three levels
each, nearest first: `strike`, `grade` (A strong, B solid, C moderate, D weak),
`strength` (persistence: new, fresh, steady, strong, shifting), `wall`
(`call_wall` / `put_wall` when the level is a wall), `score`, `dist_pct` and
`sigma` (distance from spot in % and in 1-sigma units); plus `band_lo` /
`band_hi`, `atm_iv` and `sessions_used`.

Since 2026-11-14 each record also carries a `surface` block (`vol_surface.py`),
the term structure across expirations from the same stored chain:

| field | meaning |
|---|---|
| `cm_iv` | ATM IV at 7, 14, 30, 60, 90 and 180 calendar days |
| `expiries` | parallel arrays, one entry per listed expiry out to 250 days: `atm_iv`, `fwd_iv` (forward vol from the previous expiry), `rich_rel` (IV against a line through the neighbouring expiries; +0.03 = 3% rich), `label`, `event` |
| `events` | short intervals whose forward vol stands 20% and 2.5 vol points above the front of the curve: a priced event (FOMC, CPI, earnings) |
| `windows` | per DTE window of the decision table, the expiry cheapest to `buy` and richest to `sell`, with the reason in a sentence |
| `calendars` | forward-vol gate for the calendar and diagonal cells: near-leg vol over the forward vol to the far leg; fails below 0.80 (near leg cheap), and above 1.15 only when VIX9D is above VIX |
| `noisy` | true when the chain is too thin to compare expiries (XLF, XLK most days): nothing is ranked or flagged |

Time between expiries runs on a variance clock (1 per session, 0.1 per
weekend day or holiday), so a weekend is neither cheap nor rich. Thresholds
are starting values from the 55 stored sessions, not backtested against
outcomes: the block informs expiry choice, it is not a signal.

Also a top-level `market.vix` block from `vix_state.csv` (VIX, VIX9D, term
spread and state, 1d and 20d change), and `market.long_vol`: VIX3M, VIX6M and
VIX1Y with their percentile over the last year and five years and a
CHEAP (30th or below) / NEUTRAL / RICH (70th or above) state, for timing
staged long-dated tranches. Its daily history is read fresh at most every six
hours and not stored. All additive: a reader that ignores them is unaffected.
Apart from `long_vol` and `credit_stress`, nothing new is fetched, and nothing new is stored.

`market.credit_stress` (`credit_stress.py`) is the HYG/IEF ratio's z-score against
its own 60-session mean and population stdev, a port of the thinkScript study
(`z < -2` = stress). Fields: `z`, `state` (calm / watch below -1 / stress below -2),
`trend` (5-session z change of 0.5 or more: deteriorating / improving, else steady),
`z_change_5d`, `z_min_20d`, `ratio_vs_mean_pct`, `sessions_below_threshold`,
`last_cross_below`, and the last 60 z values in `history`. Closes come from
tastytrade first, Yahoo second, read at most every six hours and not stored; a
failed read keeps the previous block marked stale. Shown as a card at the top of
the GEX Monitor and as a line on the app's Market tab. Information only, not a gate.
`py credit_stress.py` prints the current reading.
Tests: `python -m pytest test_credit_stress.py test_vol_metrics.py test_vol_surface.py test_rsi_divergence.py test_flow_intensity.py test_flow_outcomes.py`.

`rsi_divergence.py` is not wired into `latest.json`: this pipeline holds no
price bars for the index ETFs (it reads option chains), so each agent passes the
bars it already fetches (Robinhood, Yahoo or Schwab). Calibration, windows and
the honest read of the backtest are in the module docstring.

## Known limits

- Intraday rows are as current as the 15-minute Schwab pull. Nothing here is tick-level.
- Open interest settles overnight, so a morning capture is the *prior*
  session's positioning. `feed_date` is the date on the source file, not the
  trading session — a file dated the 5th carries the 4th's settled OI.
- Dealers are assumed long calls / short puts. Standard public convention,
  not a measurement of real positioning.
- Walls ignore strikes within 1% of spot: gamma peaks at the money, so
  without that exclusion the "wall" is just the ATM strike.
- The chart's two panels use different gamma sources — bars from the chain's
  reported gamma, the profile from Black-Scholes with IV held fixed. The
  flip *location* is robust; the profile's magnitudes are not comparable
  to the bars.
- No backfill exists. History starts the day capture starts.

## Retention

`prune_history.py` runs at the end of every capture (`capture.yml`, never fails
the job) and removes whole session folders older than the window:

| Path | Kept | Per session |
|---|---|---|
| `history/raw/` (settled chains) | 90 days | ~0.6 MB |
| `history/intraday/` (15-minute snapshots) | 30 days | ~14 MB |
| `daily_metrics.csv`, `intraday_state.csv`, `vix_state.csv`, `realized_vol.csv`, `official_closes.json`, `sector/` | forever | tiny |

The window is measured from the newest session on disk, not the clock, and a
floor (25 raw / 10 intraday sessions) always survives. Preview with
`python prune_history.py`; delete with `--apply`; change windows with
`--raw-days` / `--intraday-days` (0 = forever). **Widen the window before
running `backfill_walls.py` or any replay**, which can only reach back as far as
snapshots remain.

Pruning caps the working tree and future growth (~3.5 GB/year of intraday
snapshots otherwise). It does **not** shrink the existing git history (~580 MB
pack); only a `git filter-repo` rewrite would, and that breaks existing clones
and open PRs, so it is a deliberate owner decision.

## Data

Public market data: Schwab option chains (tastytrade open-interest patch),
Yahoo index levels. Nothing here is
account or position information.
