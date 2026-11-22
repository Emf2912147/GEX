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

**6. The jobs commit, then rebase, then push -- in that order.** A bare
push from the PC is rejected as non-fast-forward whenever anything else has
landed on `main` since this clone last pulled, and something else lands
constantly: the sector-agent workflows push on their own schedules, and
`capture.yml` / `intraday.yml` still fire and still commit their Cboe failure
logs. Observed for real on 2026-11-21. Committing before the rebase is what
leaves the working tree clean enough for the rebase to be permitted.

A rebase conflict is possible on `history/capture.log` and
`history/intraday.log`, which the GitHub runner appends to as well. The jobs
leave that for a human (`_GEX_PUSH_BLOCKED.txt`) rather than resolving it
blind; the capture is already committed locally either way, so the data is
never at risk -- only its publication is delayed.

**Worth considering:** `capture.yml` and `intraday.yml` can now only fail,
since Cboe is blocked, and every failed run still commits a log. Disabling
them would stop that noise *and* remove most of the push contention these
jobs now have to rebase around. Left enabled deliberately -- it is a CI
change and Eugenio's call -- but there is no longer an argument for keeping
them running.

**7. The vintage check compares like-for-like across shared expirations, not
totals.** Comparing totals is structurally broken across an expiration
boundary, and not hypothetically: 75% of SPX's <=30DTE open interest sat in
the 2026-12-18 monthly, so on the first session after it expired the total
would have come in at **0.25x and been refused -- every run, until someone
intervened.** The Nov 20 monthly had already depressed 2026-11-21's ratio to
0.77x, with 25.9% of the baseline expired. Opex is not a data fault and must
not read as one.

The like-for-like comparison is *sharper*, not merely more permissive. Tested
against the five scenarios this session actually produced: the real Robinhood
pull passes at 1.02x (0.77x on totals), the root collision is still refused at
0.28x (0.37x on totals), the post-Dec-18-opex session passes at 1.04x (0.25x
on totals, refused), a stale unrefreshed feed is still refused, and a gap long
enough to leave no shared expiries is refused because it cannot be validated
at all. `robinhood_oi_diag.py` is kept for diagnosing any future dispute: it
decomposes both books by expiry so a refusal can be attributed to the data or
to the baseline in one command.

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

## VIX / VIX9D and realized vol: restored from Yahoo (2026-11-22)

Cboe took two more feeds down with it besides the option chains:

    quotes/_{SYM}.json                        the VIX and VIX9D levels
    us_indices/daily_prices/{SYM}_History.csv the close history behind the
                                              20d change, realized vol, and
                                              vol_metrics' long_vol block

Both now come from Yahoo's chart API via **`index_history.py`** (committed --
it reads public index levels, no key, nothing to hide, so it is not in the
broker-script exclusion). Availability was probed before anything was
written: VIX 1257 bars, VIX9D / VIX3M / VIX6M / VXN / SPX / RUT / XSP 1255
each. The probe also agreed with this repo's own record -- ^GSPC's last close
came back 6910.39, matching official_closes.json for 2026-11-20 to the cent.

**This needed no new scheduling.** `gex_intraday.py`'s `main()` already runs
`capture_vix` and `capture_realized_vol` independently of the chain capture,
by deliberate design documented in its own comment, so fixing the three
fetchers was enough: the existing `intraday.yml` workflow -- which still
fails on chains -- resumes producing `vix_state.csv` and `realized_vol.csv`
rows on its next run. GitHub's runners reach Yahoo. The PC jobs are not
involved and should not be.

**VIX1Y stays absent on purpose.** Yahoo serves its current level but no
usable history, so there is nothing to place it against. `_fetch_closes`
returns `(None, None)` for it and `long_vol_block` keeps its prior value
marked stale. A percentile off a handful of points is arithmetic dressed as
information, and that block drives tranche timing.

**The bug worth not repeating.** `prev_day_close` must be derived from the
bars, never from Yahoo's `meta.chartPreviousClose` -- that field is the close
preceding the REQUESTED RANGE, so over 5 days it returns a close about six
sessions old. It gave SPX 7561.67 against stored closes of 7323.63 / 7175.83
/ 6910.39, and `capture_vix` divides by it to get `vix_chg_1d_pct`: week-long
moves would have been written as one-day moves into the term-structure state.
The replacement is `bars[-2]`, always: `current_price` belongs to the last
bar's session whether that bar is forming or settled, so the session before
it is the second-to-last. An intermediate version used "the last bar dated
before today", which is identical on weekdays and returns prev == current on
any non-trading day -- every daily change exactly zero. It was only visible
because it was caught on a Sunday.

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

## SPX from Robinhood: `robinhood_spx_oi.py` (built 2026-11-21)

`robinhood_spx_oi.py` (gitignored, PC-only) rebuilds the SPX chain from
Robinhood and merges it into the Schwab pull, so `schwab_daily.py` consumes
it with no change -- it emits rows in `schwab_fetch.py`'s exact format.

**It replaces the whole SPX block, not just the OI column.** Schwab's SPX
chain carries gamma on only ~37% of contracts, and a contract with
`gamma == 0` contributes nothing to net GEX however large its open interest.
Filling OI alone would leave SPX understated while looking healthy. Robinhood
supplies OI *and* gamma/delta/IV, so SPX comes from one source and stays
internally consistent. Schwab's spot is kept -- for SPX it matched the
official close to the cent, index settlement being fixed overnight.

Scale, measured from the 2026-11-21 pull: 11,870 SPX contracts within 30 DTE
across 20 expirations; 19,564 over the full 90-day horizon, 714 distinct
strikes spanning 200..13,400. At 50 ids per request that is ~240 requests for
the metrics window, ~390 for the full chain. The default horizon is 90 days
to match `schwab_fetch.py`, so the stored raw chain stays replayable under a
future `max_dte`. **Open interest only moves at overnight settlement, so this
runs once per session, never per intraday cycle**, and the instrument map is
cached -- only unseen expirations are enumerated.

Design points worth not undoing:
- **Addressed by chain UUID, not by symbol.** SPX resolves to two chains,
  SPXW (weeklies, PM-settled) and SPX (monthlies, AM-settled); both carry real
  SPX open interest and both are included. `robin_stocks`' symbol helpers
  resolve through an *equity* instrument, which an index does not have, so the
  chain ids are hardcoded (verified 2026-11-21) and re-discoverable via the
  Robinhood connector's `get_option_chains`.
- **Quotes are matched on the instrument id, never positionally.** Robinhood
  returns results positionally with nulls for unquotable contracts; trusting
  that would silently attach one contract's open interest to another the first
  time it omits a null instead. A quote whose id cannot be resolved is
  dropped, not guessed. There is a unit test for exactly this.
- **It never prompts.** An unattended job that blocks on an MFA prompt hangs
  forever holding the scheduler slot, so the stored session is probed with a
  cheap authenticated call and a stale one exits 2 with instructions plus
  `_ROBINHOOD_LOGIN_NEEDS_RENEWAL.txt`. It never takes a password and never
  calls `login()`.
- **Wired into `schwab_capture_auto.bat` as a NON-FATAL step.** If it is not
  set up, the session lapses, or the API changes, the run carries on with
  Schwab's eight ETFs and SPX simply stays missing -- the status quo, not a
  regression. It must never be able to take the working symbols down with it.

### Status: VERIFIED END TO END 2026-11-21

Live run: 21,666 contracts enumerated across 35 expirations, 19,564 quoted
(90.3%; the rest are contracts Robinhood lists but will not quote), gamma fill
80.4% against Schwab's 36.7%, ~95 seconds, no rate limiting. **The missing
2026-11-20 SPX row has been recovered and written** -- session 2026-11-20 now
has all 9 symbols and `check_integrity.py` passes 14/14.

Two independent validations, because a ratio alone proves little:

1. **Per-expiry against the last Cboe book** (`robinhood_oi_diag.py`, kept for
   exactly this purpose): every surviving expiration agrees at 1.01x-1.43x,
   and 2026-12-18 at 1.02x (7,409,656 vs Cboe's 7,283,829). Like-for-like
   across shared expiries, Robinhood is **1.04x** Cboe.
2. **Trend continuity.** The Robinhood-sourced row lands on the trend of the
   three Cboe-sourced rows before it, with no discontinuity:

   | session | source | net GEX | flip | spot |
   |---|---|---|---|---|
   | 11-16 | Cboe | -66.8Bn | 7585.24 | 7416.59 |
   | 11-17 | Cboe | -58.6Bn | 7545.60 | 7340.53 |
   | 11-18 | Cboe | -48.1Bn | 7522.08 | 7323.63 |
   | 11-20 | **Robinhood** | **-38.2Bn** | **7191.56** | 6910.39 |

Known caveats, none blocking:
- `contracts_total` is 19,564 against ~31,000 on the Cboe rows: the pull is
  capped at a 90-day horizon and Robinhood will not quote ~10% of what it
  lists. The <=30DTE metrics are unaffected; a replay at a much longer
  `max_dte` would be.
- `feed_ts` on the SPX row inherits the pull's top-level Schwab fetch time,
  about 70 minutes before the Robinhood fetch. Immaterial for a settled
  overnight book; worth knowing if provenance is ever audited.

### The bug that nearly wrote a 7x-wrong SPX row

Verified: the data and field names (OI, gamma, delta, IV, bid/ask, volume on
SPXW 2026-11-23 6900C/6900P -- OI 2 and 1,913), the row format, the
instrument-id matching, and the full merge-through-`schwab_daily.py` path
against a simulated Robinhood block (vintage check passed at 0.97x, 17,494
contracts, metrics computed). Not verified: auth, pagination, batching and
rate limits -- this sandbox can reach neither robinhood.com nor a login.

**Run `--probe` first** (7-day horizon, one batch, writes nothing) and read
the coverage line before trusting a full run:

```
pip install robin_stocks
python -c "import robin_stocks.robinhood as r; r.login()"    # once, interactive
python robinhood_spx_oi.py --probe
python robinhood_spx_oi.py --merge-into <pull.json> --expect 12932396
```

One thing to expect: SPX's last stored book is **2026-11-18**, because there
is no 11-19 or 11-20 SPX chain on file. By Tuesday that baseline is four
sessions and roughly a 5.6% index move old (7,323.63 -> 6,910.39), so the
vintage check's 0.6x-1.6x band is easier to trip for SPX than for the ETFs.
If it refuses on a ratio that looks plausible for a real book, the baseline is
the thing to question, not the pull -- check the `--expect` line first.

The first live run keyed the instrument map on `(expiry, strike, type)` --
**without the root.** SPX (AM-settled monthly) and SPXW (PM-settled weekly)
share expiries and strikes but are different contracts with separate open
interest, so whichever enumerated second silently overwrote the other. SPXW
won, and the AM-settled monthly -- where the institutional open interest sits
-- was discarded: 2026-12-18 came back 962,501 against a true 7,409,656, a
factor of 7.7.

It was caught only because the loss happened to push the total below the 0.6x
floor. A collision on a smaller expiry would have passed the check and written
a plausible-looking row understating the largest gamma concentration in the
book. **Two lessons kept in the code:** the map key includes the root, and the
instrument cache carries a version marker so the old collided cache is
discarded rather than trusted.

**A source does exist.** Robinhood returns `open_interest` plus full Greeks
for SPXW index contracts (verified 2026-11-21 on 2026-11-23 6900C/6900P:
OI 2 and 1,913, with gamma, delta and IV). It is per-contract by instrument
UUID, so reconstructing a whole chain through the MCP interface would take
thousands of round trips -- impractical as a daily job there, but plausible
against Robinhood's HTTP API directly from the PC or the container, where
batching is larger. Not built. Deciding whether Robinhood becomes the
canonical SPX source, or whether this is the argument for a licensed Cboe
DataShop / LiveVol subscription, is an open call.

## Schwab zeroes in-the-money open interest (found 2026-11-22)

Schwab reports `openInterest` 0 on roughly four fifths of IN-THE-MONEY
contracts while every neighbouring field on the same contract is exactly
right. Out-of-the-money open interest arrives largely intact.

Across the 127-name sector universe, established monthlies, contracts with a
live two-sided market:

| | ITM zero | OTM zero |
|---|---|---|
| calls | 3,361/4,095 (82.1%) | 305/1,696 (18.0%) |
| puts | 3,052/3,932 (77.6%) | 467/1,799 (26.0%) |

The OTM zeros are mostly legitimate -- a listed strike nobody has traded.
The ITM ones are not.

### How it was pinned down

Confirmed against Robinhood at the same timestamp, same contract:

| contract | Schwab | true | bid/ask/volume |
|---|---|---|---|
| MA 261218C00615000 | 0 | 229 | exact match |
| VRTX 261218C00520000 | 0 | 137 | exact match (vol 201) |
| MA 261218C00625000 | 62 | 62 | control, correct |
| MA 261218P00625000 | 215 | 215 | control, correct |
| SPY 261218C00690000 | 4,224 | 4,224 | control, correct |

Ruled out, each by direct test rather than argument:

- **Our parsing.** The raw response carries `openInterest` as an explicit `0`,
  key present. `_num()` was suspected of flattening an absent field to zero and
  was fixed anyway (see `_oi()` in `sector_chains.py`), but that was not this.
- **Response truncation.** A narrow window (2026-12-17..19, 236 KB) returns the
  same 0 as the 90-day window (1.7 MB). Not a size limit.
- **One bad endpoint.** `/marketdata/v1/quotes` returns the identical wrong
  values, so it is Schwab's open-interest store, not `/chains`.
- **An alternate field.** No other open-interest-like key exists on the
  contract; the full key list was dumped and checked.
- **Weekend staleness.** Every Schwab pull available at the time was taken on a
  Saturday or Sunday, so this could NOT be excluded from the data then. It is
  now considered unlikely on mechanism: stale refresh produces random or
  time-ordered gaps, not a clean split on moneyness, and Robinhood returned
  correct open interest on the same Sunday. Re-test on a weekday before
  concluding. `_probe_raw_oi.py` and `_probe_quotes_oi.py` do exactly this.

### Why it looked like two unrelated problems

Gamma peaks at the money and decays both ways, so a defect living in ITM
contracts sits where gamma is LOWEST. That single fact explains both symptoms:

- **Net gex barely moves.** Restoring the dropped open interest from the last
  Cboe book shifts the eight production ETFs by 0.1-7.2% (worst SMH 7.2%, XLF
  6.6%, GLD 5.2%). Call and put errors partly cancel. Call and put walls do not
  move at all. Gamma flip shifts 0.02-0.16% on SPY/QQQ/GLD/TLT and 1.71% on
  IWM.
- **`atm_oi` collapses outright.** It is the MEDIAN over the ten contracts
  nearest spot -- a window that straddles the money, so about half its slots
  are ITM. At an 80% ITM zero rate the median goes to 0. That is the 14 names
  reading zero, and it is the same defect, not a second one.

The one concrete loss on the production path: **XLF reports no gamma flip at
all.** Restoring the open interest puts one at 61.09 against a spot of 54.57.

### What was done about it

`atm_oi`, `net_gex_musd`, `gamma_flip`, `call_wall` and `put_wall` are WITHHELD
per name when open interest is not credible, rather than published wrong --
`gate_on_oi()` in `sector_technicals.py`. `atm_iv_30` and `atm_spread_pct` are
quote-derived and survive; Schwab's bid, ask, volume, IV and greeks were exact
on every contract checked.

Threshold is >=4 of the 10 near-ATM contracts showing a live market and zero
open interest. Set from measurement against the last Cboe session, not taste:

    >=2  flags 61 names, catches 24 of 25 known-bad   (31 false positives)
    >=3  flags 42 names, catches 24 of 25             (18 false positives)
    >=4  flags 30 names, catches 24 of 25             <-- chosen
    >=5  flags 22 names, catches 20 of 25             (5 missed)
    >=6  flags 13 names, catches 13 of 25             (12 missed)

The one name `>=4` misses is LRCX, at 0.41x Cboe with ZERO zeros in the window
-- that gap is the 5.6% spot move between the two sessions moving the window
onto different strikes, not a feed defect.

Measured near the money ON PURPOSE. A whole-chain zero rate flags everything:
across these 127 names it ran 39% to 98% with no separation between a healthy
name and a broken one, because far-from-the-money strikes carry a quote and no
open interest as a matter of course. MA reads 85.8% whole-chain and is fine.

The gate is CONDITIONAL, not a hardcoded suppression. If Schwab starts
reporting open interest correctly it fires on nothing and the same code is
still right. Nothing needs to be reverted.

`schwab_daily.py` reports its own ITM/OTM split on every capture
(`oi_integrity_report()`), so this cannot go quiet again. On session 2026-11-20
it marks XLF, GLD and TLT suspect. The test is an absolute margin (ITM >=50%
AND ITM-OTM >=15pp), not a ratio: ETFs list far more strikes than single names,
so untouched ones lift the OTM baseline and flatten any ratio. XLF read 71% vs
41% -- the symbol that actually lost its flip -- and a 2x ratio test let it
through, while equities ran 82% vs 18%. XLK and SMH sit near 55% on BOTH sides
and are correctly quiet: symmetric means a thin chain, not this defect.

### Still open

- **Re-test on a weekday.** See the weekend caveat above.
- **OTM-only gamma exposure** would sidestep the defect almost entirely, since
  OTM is the part that survives. NOT adopted unilaterally: it redefines the
  metric, and the history back to September is on the current definition, so
  comparability would break at the seam. Owner's call.
- **Session 2026-11-19 is permanently missing** for all nine symbols -- the
  capture was due the morning Cboe went dark. Open interest is a point-in-time
  snapshot and neither Schwab nor Robinhood serves it historically.
- **The eight ETF rows for session 2026-11-20 were captured on a Saturday** by
  an earlier code path that logged no `(Schwab)` tag and ran no vintage check.
  They carry the 0.1-7.2% understatement and cannot be recaptured.
- **The Cboe baseline is frozen.** `history/raw/2026-11-18/` is the last Cboe
  book; the free delayed-quote endpoint appears to have moved behind a
  DataShop subscription. Everything above was validated against it, and it
  cannot be refreshed.
