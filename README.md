# GEX

Daily gamma-exposure history for SPX, SPY, QQQ and IWM, built from Cboe's
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
