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
| `build_dashboard.py` | renders `docs/index.html` |
| `history/raw/<feed-date>/<SYM>.parquet` | full chain, every contract |
| `history/daily_metrics.csv` | one row per symbol per day |
| `docs/` | the published dashboard |

## Raw chains are the asset

`daily_metrics.csv` is a cache. It can always be rebuilt from `history/raw/`
under a new methodology — and it will need to be. The wall definition changed
twice on the day this was written; had only derived numbers been kept, every
historical row would be wrong and unrecoverable. **Never delete `history/raw/`.**

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

and a top-level `market.vix` block from `vix_state.csv` (VIX, VIX9D, term
spread and state, 1d and 20d change). All additive: a reader that ignores
them is unaffected. Nothing new is fetched or stored; these are derived from
data already captured. Tests: `python -m pytest test_vol_metrics.py`.

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

## Repo growth

Roughly 1-3 MB/day of raw chains, so 0.3-0.8 GB/year in git. Fine for a few
years; past that, archive older years to a release asset and drop them from
the working tree.

## Data

Public market data from Cboe's delayed-quote endpoint. Nothing here is
account or position information.
