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

## Two things deliberately NOT done in the scheduled jobs

Both were in the first cut of these `.bat` files on 2026-11-21 and were
removed the same day. If a later session is tempted to "fix" either, this is
why not.

**1. `check_integrity.py` does not gate any commit.** It audits the whole
repo's history (14 checks), not the row just written. Its COVERAGE section
includes *"pipeline ran within 4 days"* and *"no session with fewer than 5
cycles"* -- both of which fail after any multi-day outage. Gating a commit on
it deadlocks: the check that reports the pipeline is stale would block the
commit of the very data that makes it un-stale, and every cycle after that
one. Worse, the write has already happened by then, so the result is data on
disk that never gets committed and a red scheduled task every 15 minutes.

What actually gates a row is upstream and row-specific -- the vintage check
and the official-close refusal in `schwab_daily.py`, the empty-Greeks refusal
and the two dedupe guards in `schwab_intraday.py`. The daily job still *runs*
the audit, advisory: it logs the result and writes
`_GEX_INTEGRITY_ATTENTION.txt` on failure, then commits anyway. The intraday
job does not run it at all (`check_integrity_auto.bat` and the monthly process
already cover it).

**2. The daily job does not pass `--prev`.** That flag is for comparing two
pulls taken the same evening by hand. In an unattended job the file goes
stale -- a Monday run would vintage-check against Friday's pull. Left off,
`schwab_daily.py` compares against the most recently *stored* raw book, which
is always the real last book and maintains itself. Nothing is lost: an
unrefreshed Schwab pull still comes back with `<=30DTE` OI exactly equal to
that stored book, which the unchanged-book branch catches.

**3. The daily job gates on `should_capture`, not just `session_for`.** Both
rules come from `gex_capture.py`'s `main()` and both are needed.
`session_for(today)` is simply yesterday; `should_capture(today)` asks whether
yesterday was a session at all. With only the first, a Monday 07:00 run
computes `session_for(Mon) = Sunday` and tries to capture a day that never
traded. It would be refused downstream (no official close on file for a
Sunday), but as a *failed* run -- so the task would go red every Monday, on a
non-event. A non-session day is a clean skip, exit 0, which is how
`capture.yml` treated it.

**4. `session_already_captured` is checked before the vintage check, not
after.** An already-captured session is a no-op (skip, exit 0), not a
rejection (failure, exit 1). Checked in the wrong order -- as it was until
2026-11-21 -- a re-run instead reports `<=30DTE OI unchanged -- Schwab has
not refreshed`, which is both misleading (the real reason is that the session
is already on file) and fatal in the scheduled job, whose dry run aborts the
cycle on any nonzero exit. Any double fire would have gone red: Task
Scheduler's "run as soon as possible after a missed start", a wake-from-sleep
catch-up, or a manual re-run. The run summary now counts `skipped`
separately and skips do not reach the exit code.

**5. Exit codes go by scope, and the jobs make a single pass.** This is
`gex_intraday.py`'s documented principle -- a couple of symbols refused is
the known condition, logged and counted, not an alarm; *every* symbol
refused means the feed is broken -- and on the Schwab path it is load
bearing rather than tidy. SPX is permanently rejected here (no index open
interest), so an any-failure exit code means every daily run exits nonzero
forever, and since the scheduled job treated a nonzero dry run as fatal,
**the eight symbols that did capture would never have been written. One
broken symbol would have taken the other eight down with it, every day.**

`schwab_daily.py` and `schwab_intraday.py` now exit nonzero only when
nothing succeeded and nothing skipped. Both `.bat` files make one pass with
`--write` instead of a dry run followed by a write: the per-symbol guards
are inside the Python, so a preceding dry run catches nothing the write pass
would not, while doubling the parse cost and supplying the abort that caused
the problem. Run them by hand without `--write` whenever you want a dry run;
the scheduled path does not need one.

## One guard that goes quiet on the Schwab path

`check_integrity.py`'s *"no chain staler than its limit (15 min, XLK 30)"*
measures `capture_ts - feed_ts`. On the Cboe path `feed_ts` was Cboe's own
publish stamp, so that check caught genuinely stale served chains (it exists
because of the 2026-09-23 incident where every symbol came back 9+ hours
old). On the Schwab path `feed_ts` is *our own fetch time*, so the lag is
always ~0 and the check always passes. It is not catching anything for Schwab
rows -- it has not been removed, but do not read a pass as evidence of
freshness here.

- **Known gap, not faked**: VIX/VIX9D state and realized-vol capture
  (`gex_intraday.py`'s `capture_vix` / `capture_realized_vol`) read separate
  Cboe endpoints (`quotes/_VIX.json`, `us_indices/daily_prices`) that
  `schwab_fetch.py` does not pull. Not wired into the Schwab path -- those
  two series simply stop updating while Cboe is blocked, same as before this
  work, rather than being filled with a substitute. Phone/alert wiring beyond
  the `_SCHWAB_LOGIN_NEEDS_RENEWAL.txt` flag file is also not wired in yet.

## SPX: Schwab does not supply index open interest

**The Nov 20 SPX row cannot be recovered from Schwab, and neither can any
future one.** The 2026-11-20 session logged `openInterest: 0` on all SPX
expiries and hypothesised that Schwab rebuilds it later. That hypothesis is
now disproven. A fresh pull on 2026-11-21 19:28 UTC -- 24 hours later, and
over a weekend, when the OI book is settled and static -- returned:

| symbol | contracts | total OI | gamma fill |
|---|---|---|---|
| SPX (index) | 19,564 | **0** | 36.7% |
| SPY | 6,250 | 9,593,623 | 62.1% |
| QQQ | 6,312 | 7,210,576 | 61.4% |

Zero across the *entire* chain, not just the near-dated contracts, while
every equity ETF in the same pull came back with a normal book. One variable
differs: SPX is the only index underlying. IV is 100% populated and Greeks
are partially populated, so this is a field-level omission for index options,
not an outage and not a mid-refresh artifact.

The vintage check refuses it correctly (`0 is 0.00x the last stored book`),
so nothing bad gets written -- but it means **the Schwab fallback covers 8 of
9 symbols, and SPX positioning goes dark for as long as Cboe is blocked.**
Given the book this feeds is SPX/QQQ/IWM-centric, that is an analytical hole,
not a cosmetic one. Do not paper over it by relaxing the vintage check.

**A source does exist.** Robinhood returns `open_interest` plus full Greeks
for SPXW index contracts (verified 2026-11-21 on 2026-11-23 6900C/6900P:
OI 2 and 1,913, with gamma, delta and IV). It is per-contract by instrument
UUID, so reconstructing a whole chain through the MCP interface would take
thousands of round trips -- impractical as a daily job there, but plausible
against Robinhood's HTTP API directly from the PC or the container, where
batching is larger. Not built. Deciding whether Robinhood becomes the
canonical SPX source, or whether this is the argument for a licensed Cboe
DataShop / LiveVol subscription, is an open call.
