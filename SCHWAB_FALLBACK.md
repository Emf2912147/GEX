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

## Built 2026-11-21 (gitignored, PC-only -- never committed)

Both pieces described above now exist on the PC, in `C:\Users\EMFS\GEX\`:

- **`schwab_daily.py`** -- the missing row-builder. Reshapes a
  `schwab_fetch.py` pull into the exact payload shape
  `gamma_exposure.fetch_chain` would have returned from Cboe, then runs the
  *same* `parse_chain` / `normalize_iv` / `gamma_profile` / `gex_by_strike` /
  `find_walls` math `gex_capture.capture_symbol` uses -- no GEX math is
  duplicated, so a Schwab-sourced row is computed identically to a
  Cboe-sourced one. It reuses `gex_capture.py`'s `official_close`,
  `session_close`, `write_raw`, `append_metrics`, `log` directly (all take an
  explicit `session_date`/`outdir`, so they drop in unmodified); only
  `capture_symbol`'s top few lines are replicated, with `session_date` taken
  from `--session` instead of inferred from "now" (`capture_symbol` assumes
  it's being run same-day, which a Schwab backfill can't assume).

  **Vintage check**, confirmed against the thresholds agreed on 2026-11-21:
  any OI left on an already-expired contract rejects outright; `<=30DTE`
  total OI outside **0.6x-1.6x the last stored book** (or unchanged from it)
  rejects; comparison is against the most recently stored
  `history/raw/<date>/<SYM>.parquet` by default, or an explicit `--prev
  <earlier_pull.json>` when you want to compare two same-evening pulls
  directly. **A prior book that exists but can't be read (e.g. a missing
  `pyarrow`) is treated as a hard rejection, never a silent pass** -- a read
  failure must never look like "nothing to compare against." Dry-run tested
  against all three paths (reject-out-of-range, pass-in-range,
  reject-unchanged) plus the read-failure fail-safe.

  ```
  python schwab_daily.py <pull.json> --session 2026-11-20 [--prev <earlier.json>] [--write]
  ```

  Always dry-run first (no `--write`) -- exits 1 if anything was rejected,
  prints what *would* be written otherwise. Add `--write` only once that's
  clean.

- **`schwab_capture_auto.bat`** -- the scheduled job: checks the login
  (aborts loudly, writes `_SCHWAB_LOGIN_NEEDS_RENEWAL.txt`, if expired, never
  retries or skips silently) -> fetches via the same `docker exec -i
  gex-trading-agent python -` invocation as the manual flow -> dry-run ->
  `--write` -> `check_integrity.py` (abort, don't commit, if it fails) ->
  commits `history/raw`, `history/daily_metrics.csv`, `history/capture.log`
  only (never `schwab_*.py`/`.json`/logs) -> pushes. Logs to
  `schwab_capture_log.txt`, same pattern as `resync_gex_auto.bat` /
  `check_integrity_auto.bat` already in this repo.

  Register it once:
  ```
  schtasks /Create /SC DAILY /ST 07:00 /TN "GEX Schwab Capture" ^
    /TR "C:\Users\EMFS\GEX\schwab_capture_auto.bat" /RL LIMITED
  ```

  This does **not** touch the 7-day login -- that stays exactly as it is
  today: manual, weekly, via `NMT GEX\Renew Schwab Login.bat` / the
  "Schwab Login Sunday" shortcut.

## Built 2026-11-21, part 2: intraday

- **`schwab_intraday.py`** -- the flow-side companion, same relationship to
  `schwab_daily.py` that `gex_intraday.py` has to `gex_capture.py`: volume
  and the live state vector (flip, walls, skew, ATM IV, term slope), not
  settled positioning. Reuses `gex_intraday.py`'s own dedupe
  (`last_snapshot`/`same_values` -- skips an unchanged feed_ts, and
  separately skips a republished feed that advanced the timestamp but not
  spot/net-GEX), its chain-integrity guard (`check_chain_integrity` --
  refuses a chain with zero gamma/IV fill rather than writing a
  silently-wrong row), and its window/state-vector math and `append_state`,
  all unmodified. Dry-run tested: first write, unchanged-feed_ts skip,
  republished-content skip, and an empty-Greeks chain correctly refused with
  a nonzero exit.

  No vintage check here -- intraday flow is new volume each cycle, not a
  settled book to compare against a prior one.

  ```
  python schwab_intraday.py <schwab_pull.json> [--write]
  ```

- **`schwab_intraday_auto.bat`** -- every-15-min job: a trading-day + session
  window gate that mirrors `gex_intraday.py`'s own 09:00-17:15 ET window
  *exactly* (not the simpler 09:30-16:00 `is_market_open` -- that would drop
  the deliberate pre-open and 16:22-16:58 ET closing-print captures this repo
  already depends on) -> login check -> fetch -> dry-run -> `--write` ->
  `check_integrity.py` -> commits `history/intraday`,
  `history/intraday_state.csv`, `history/intraday.log` only -> pushes.

  Register once (verified working 2026-11-21):
  ```
  schtasks /Create /SC DAILY /ST 09:35 /ET 16:15 /RI 15 ^
    /TN "GEX Schwab Intraday" /TR "C:\Users\EMFS\GEX\schwab_intraday_auto.bat" ^
    /RL LIMITED
  ```
  No `/DU`: schtasks refuses `/DU` together with `/ET` ("ERROR: /DU cannot be
  specified with /ET"), and `/ET` already bounds the repetition.

  On the window: 09:35-16:15 ET is narrower than the script's own
  09:00-17:15 ET gate, and that is deliberate. `gex_intraday.py`'s window
  runs to 17:15 because the *Cboe* feed is ~15 min delayed, so 16:22-16:58 ET
  was when the closing print finally arrived. Schwab's Trader API is not
  delayed, so a 16:15 capture already carries the close and the late tail
  buys nothing. Move `/ST` earlier (09:05) if you want the pre-open state
  vector the Cboe setup used to capture at 09:07.

- **Known gap, not faked**: VIX/VIX9D state and realized-vol capture
  (`gex_intraday.py`'s `capture_vix` / `capture_realized_vol`) read separate
  Cboe endpoints (`quotes/_VIX.json`, `us_indices/daily_prices`) that
  `schwab_fetch.py` does not pull. Not wired into the Schwab path -- those
  two series simply stop updating while Cboe is blocked, same as before this
  work, rather than being filled with a substitute. Phone/alert wiring beyond
  the `_SCHWAB_LOGIN_NEEDS_RENEWAL.txt` flag file is also not wired in yet.

## Still open from 2026-11-20

SPX's Nov 20 row is still missing: Schwab was returning `openInterest: 0` on
all SPX expiries that evening, rejected by the vintage check. Re-pull and
retry before trusting SPX figures for that date.
