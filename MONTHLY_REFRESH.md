# Monthly refresh

Run on the first weekend of each month. Takes about ten minutes, most of it
waiting for `git pull`.

Two things need doing on a schedule, and one thing needs watching:

- **Official closes** go stale. The live capture anchors ETF spot on the last
  intraday snapshot (~20:55 UTC, roughly an hour after the close), which is
  good to about 0.07% but not exact. Refreshing `official_closes.json` and
  re-running the migration makes the whole series exact.
- **Integrity** is checked by `check_integrity.py`, which encodes every defect
  that has actually occurred here. All of them produced output that looked
  correct at the time.
- **Walls** only need re-deriving when the wall definition or `wall_exclude`
  changes. Not a monthly job.

---

## 1. Sync

```
cd C:\Users\EMFS\GEX
git checkout -- history/intraday.log history/capture.log
git pull
```

The `checkout` discards log lines any local dry run appended. The runner writes
those same files, so leaving them modified causes a pull conflict.

## 2. Check integrity first

```
python check_integrity.py
```

Read this **before** changing anything — it tells you whether the month was
clean and what to investigate. Exit code 0 means all checks passed.

If `pipeline ran within 4 days` FAILS, stop and check the GitHub Actions tab.
A runner that silently stops is the one failure mode nothing here detects, and
it is what happened on 2026-09-23. Everything below is pointless against a dead
feed.

If `no unexplained missing sessions` FAILS, find out why that session is empty
before doing anything else. Once established and unfixable, add it to
`KNOWN_GAPS` in `check_integrity.py` with a comment saying why.

## 3. Refresh the official closes

This is the one step that needs a Claude session, because the closes come from
the Robinhood connector and the GitHub runner has no access to it.

In a chat with the Robinhood connector enabled, ask:

> Pull official daily closes for SPY, QQQ, IWM and SPX from <first missing
> date> to <last session> and give me an updated `official_closes.json` for
> the GEX repo.

Claude uses `get_equity_historicals` (interval `day`, bounds `regular`,
adjustment `none`) for the three ETFs and `get_index_historicals` for SPX, and
returns the merged file. Save it over `history/official_closes.json` — note
the browser saves to Downloads, and it must end up in `history/`, not the
repo root.

`check_integrity.py` tells you the date range to ask for: the
`official close on file for every daily row` check names what is missing.

## 4. Re-anchor and rebuild

```
python migrate_daily_spot.py --dry-run
```

Confirm the header reads `84 official closes on file` (or more) and that
`no session close` is **0**. If it reads `0 official closes on file`, the JSON
is in the wrong folder — see step 3.

The migration is safe to re-run. It reads `feed_spot`, which always holds the
original Cboe value, so corrections never compound.

```
python migrate_daily_spot.py
python build_dashboard.py
python check_integrity.py
```

The second integrity run confirms the migration did not break anything.

## 5. Commit

```
git status --short
git add migrate_daily_spot.py history/ docs/
git commit -m "monthly refresh: official closes through <date>"
git push
```

Check `git status --short` first. You should see only `history/` and `docs/`
files. `.bak.*` files, `*.patch`, and the Schwab/Rader scripts are ignored —
if any of them appear, the `.gitignore` is not being read and **nothing should
be pushed until that is fixed**, because this repo is public.

---

## Only when the wall definition changes

```
python backfill_walls.py --dry-run
```

Confirm the header reports the current `wall_exclude` before letting it write.
It reads the live default from `find_walls` via `inspect`, so it cannot drift
from the capture code — but check anyway, since this restates the whole wall
history.

## Known limitations

These are properties of the framework, not bugs, and no amount of maintenance
addresses them:

- Dealers are **assumed** long calls and short puts. This is a convention, not
  a measurement, and it is the largest single source of error here — larger
  than everything this runbook corrects.
- Open interest settles overnight, so intraday readings are yesterday's
  positioning repriced against today's spot.
- The Cboe feed is ~15 minutes delayed.
- SPX needs no spot correction and never has — index settlement is fixed
  overnight. All ETF drift comes from the quote feed being live while the open
  interest is not.
