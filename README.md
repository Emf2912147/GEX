# GEX

Daily gamma-exposure history for SPX, SPY, QQQ, IWM, XLK, SMH, XLF, GLD and TLT,
built from Cboe's
free delayed-quote endpoint.

**Dashboard:** enable GitHub Pages (Settings -> Pages -> Deploy from a branch
-> `main` / `/docs`), then the page lives at
`https://<your-username>.github.io/<repo>/`.

## What runs

`.github/workflows/capture.yml` fires daily at 11:30 UTC. It captures each
symbol's full option chain, stores it raw, appends a row of derived metrics,
rebuilds the dashboard, and commits.

## Layout

| path | what |
|---|---|
| `gamma_exposure.py` | chart builder, importable analysis functions |
| `gex_capture.py` | daily snapshot + metrics |
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
| `rv_20d`, `rv_source`, `rv_proxy` | 20-session realized vol: `realized_vol.csv` (Cboe history) when fresh, else this repo's own closes |
| `iv_rv_ratio` | `atm_iv_30d / rv_20d`; above 1 = options rich to recent movement |
| `smile` | put/call IV by strike for the expiry nearest 7 days, strikes within 8% of spot; the agent prices its spreads from it (model value, not a fill) |

and a `flows` block (`dealer_flows.py`): dealer vanna and charm hedge flows
in basis points of the 0.5-30 day open-interest notional (`charm_bp_day`,
`vanna_bp_per_volpt`, positive = dealer buying) and `charm_support_ratio`,
today's charm against its own median of the previous 20 sessions (null under
10). Same dealers-long-calls / short-puts convention as the GEX numbers;
an assumption, not observed positioning.

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
staged long-dated tranches. Its Cboe history is read fresh at most every six
hours and not stored. All additive: a reader that ignores them is unaffected.
Apart from `long_vol`, nothing new is fetched, and nothing new is stored.
Tests: `python -m pytest test_vol_metrics.py test_vol_surface.py test_rsi_divergence.py`.

`rsi_divergence.py` is not wired into `latest.json`: this pipeline holds no
price bars for the index ETFs (it reads option chains), so each agent passes the
bars it already fetches (Robinhood, Yahoo or Schwab). Calibration, windows and
the honest read of the backtest are in the module docstring.

## Known limits

- Cboe's feed is ~15 minutes delayed. Nothing here is real-time.
- Open interest settles overnight, so a morning capture is the *prior*
  session's positioning. `feed_date` is the date on the Cboe file, not the
  trading session — a file dated the 5th carries the 4th's settled OI.
- Dealers are assumed long calls / short puts. Standard public convention,
  not a measurement of real positioning.
- Walls ignore strikes within 1% of spot: gamma peaks at the money, so
  without that exclusion the "wall" is just the ATM strike.
- The chart's two panels use different gamma sources — bars from Cboe's
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

Public market data from Cboe's delayed-quote endpoint. Nothing here is
account or position information.
