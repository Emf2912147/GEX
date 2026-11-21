# Schwab fallback (Cboe deprecated 2026-11-20)

Cboe's `cdn-api.cboe.com` delayed-quote JSON feed — the only thing this
pipeline ever depended on — is blocked (see README `## Status`). This doc
covers the Schwab connector that replaced it: what already exists, the one
constraint that rules out a GitHub-Actions-style unattended pipeline, and
what still needs to be built to automate the rest.

## What already exists

- **Schwab Trader API access** runs through the `gex-trading-agent` Docker
  container on the PC (`C:\Users\EMFS\PROJECTS\gex-trading-agent`), which is
  the only thing in this whole setup that can reach both Schwab and (for
  intraday work) the market. The cloud sandbox/VM cannot reach Schwab or
  Cboe at all — only that container can.
- **Credentials and login are shared** with the NMT monitor
  (`C:\Users\EMFS\NMT GEX\secrets\schwab_app.json` + `schwab_token.json`),
  bind-mounted into the container at `/schwab`
  (`gex-trading-agent/docker-compose.yml`). One login serves both projects.
  Never read, print, or commit these files or their contents.
- **`ticker_analysis/schwab.py`** (in `gex-trading-agent`) already
  auto-refreshes the 30-minute access token from the stored refresh token
  with no human involved, as long as the refresh token itself hasn't
  expired. This part is already automatable today.
- **`schwab_fetch.py`** (in `gex-trading-agent`, gitignored, PC-only) already
  pulls the full option chain (to +90 days) for all 9 GEX symbols from
  Schwab and prints one JSON document — this is the data-fetch half of a
  daily/intraday capture, already working, currently run by hand via
  `docker exec`.
- **The row-building half** (anchor each symbol's close, vintage-check the
  pulled OI against the last stored book, append `daily_metrics.csv`, write
  the raw chain) was prototyped as `schwab_daily.py` during the 2026-11-20
  session, but by design it was never committed (`.gitignore` blocks
  `schwab_*.py` — "don't add Schwab/Rader data or code to the repo") and it
  does not currently exist as a persistent file anywhere. **It needs to be
  rewritten** before any automation can call it. Doing that faithfully means
  settling the vintage-check thresholds and anchoring rule explicitly with
  Eugenio rather than guessing them from one session's commit message — the
  2026-11-20 session rejected at least one pull for exactly this reason
  (mixed-vintage OI), so getting this wrong writes bad data into a
  historical trading record.

## The constraint that matters: Schwab logins cannot be automated

Schwab's OAuth refresh token is valid for **7 days** and can only be renewed
through an interactive browser login — there is no unattended way to do it.
This is already documented in two places (`GEX/schwab_auth.py`'s docstring
and `ticker_analysis/schwab.py`'s docstring) and is why the pipeline stayed
on Cboe for as long as it could: **Schwab access was deliberately kept out
of the unattended path.**

The weekly renewal already happens by hand: `NMT GEX\Renew Schwab Login.bat`
/ the `Schwab Login Sunday.lnk` shortcut runs `run_emf.bat` →
`emf_capture.py`, which does the interactive login and writes
`schwab_token.json` (shared by both projects). As of this writing that login
was refreshed today (2026-11-21), so it's good for about a week.

**Implication:** nothing in this pipeline can be a true "set it and forget
it" unattended job the way `capture.yml` was. The best achievable automation
is unattended *within* each 7-day login window, with the weekly browser
login remaining a human step — which already exists and doesn't need
rebuilding.

## What still needs to be built

A PC-side scheduled job (Windows Task Scheduler, not GitHub Actions — only
the PC's Docker container can reach Schwab) that, once a day (mirroring
`capture.yml`'s ~7am ET cadence, after overnight OCC settlement) and
optionally every 15 minutes intraday:

1. **Checks the login is still valid** before doing anything else —
   `docker exec gex-trading-agent python -m ticker_analysis.schwab` already
   prints login status; a scheduled job should parse/check this and alert
   (not silently skip) if the login is expired or expiring within ~24h.
2. **Fetches the chain**: `docker exec gex-trading-agent python schwab_fetch.py
   > schwab_chains_YYYYMMDD.json` (already works).
3. **Builds the row**: a rewritten `schwab_daily.py` — anchors each symbol's
   close (from `official_closes.json`, refusing to run without one, same as
   `gex_capture.py`'s `official_close()`), vintage-checks the pulled OI
   against the last stored book, writes `history/raw/<date>/<SYM>.parquet`,
   appends `history/daily_metrics.csv`. This is the missing piece above.
4. **Runs `check_integrity.py`** and aborts the commit if it fails.
5. **Commits and pushes** — same pattern as `resync_gex_auto.bat` /
   `check_integrity_auto.bat` already in this repo, logging to a dated log
   file the same way `capture.log` does.
6. **Alerts on failure** (login expired, fetch failed, integrity check
   failed, vintage check rejected the pull) using the same phone-alert
   mechanism already in use (`NMT GEX\emf_alert.py` / the
   "SCHWAB_DATA: ... HTTP 403" style alert) rather than failing silently —
   this is how the 2026-11-20 Cboe outage was first noticed.

None of step 1-6 touches the 7-day login itself; that stays manual,
Sundays, as it already is.

## Still open from 2026-11-20

SPX's Nov 20 row is still missing: Schwab was returning `openInterest: 0` on
all SPX expiries that evening, rejected by the vintage check. Re-pull and
retry before trusting SPX figures for that date.
