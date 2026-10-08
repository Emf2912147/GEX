#!/usr/bin/env python3
"""Keep one runner alive around the clock and capture on a fixed 15-minute grid.

WHY THIS EXISTS
    intraday.yml used to ask GitHub's scheduler for 32 separate runs a
    session, one per capture. Scheduled runs are best-effort, and when the
    scheduler degrades it degrades for hours at a stretch:

        2026-10-02   21 cycles
        2026-10-05   13   (the last five runs fired but never got a runner)
        2026-10-06    2   in session, plus one at 22:57
        2026-10-07    1   of 32 slots, under the offset-minute schedule

    Asking more often does not help. The misses are not independent draws --
    on 10/07 the scheduler created nothing for this repo between 01:11 and
    17:52 UTC, so 60 crons in that window would have produced the same zero
    as the 19 intraday slots in it did.

    So the pipeline no longer waits to be started. It is a RELAY: a chain of
    jobs, each of which starts the next one itself through the dispatch API
    just before its own six-hour limit, and none of which ends without doing
    so. A dispatch is an API call, not a scheduled event -- nothing in the
    chain passes through the scheduler, and nothing outside GitHub is
    involved. The chain runs through the night and the weekend, so at 09:07
    ET there is already a job awake to take the first slot.

WHAT ONE LINK DOES
    1. Works out the next capture slot: :07/:22/:37/:52 from 09:07 to 16:52
       ET on NYSE sessions, in Eastern time so the November clock change
       needs no edit here.
    2. If that slot is within its own lifetime, sleeps until it, runs
       gex_intraday.py as a fresh process, commits and pushes exactly as the
       old workflow step did, and repeats. gex_intraday.py is not modified
       and keeps every guard it has.
    3. If the next slot is beyond its lifetime -- mid-session, overnight, a
       weekend, a holiday -- it waits until its time is nearly up, dispatches
       its successor and exits. The workflow's concurrency group queues the
       successor behind it, so two links never capture at once.
    4. Owns the daily capture too: if settled OI for the prior session is not
       on file by 11:45 UTC, it dispatches capture.yml. Saturdays included --
       that capture is the one thing that can never be re-fetched.

WHAT CAN STILL BREAK IT
    The chain breaks if a link dies without dispatching: a runner lost
    mid-job, or a GitHub incident at the moment of a handoff. The crons in
    intraday.yml exist ONLY for that -- any one that fires starts a new link,
    or queues behind a live one as a spare and takes over the instant it
    ends. They repair the chain; they do not drive it.

    The session-hours gate inside gex_intraday.py must agree with the grid
    here. Both are in Eastern time (the gate was UTC until 2026-10-08), so
    the November and March clock changes need no edit in either.

    A link keeps the copy of THIS file it started with. After pushing a
    change to session_runner.py, the running link picks it up at its next
    handoff (within 5.5 hours). To apply it at once: cancel the running job
    in the Actions tab, then click "Run workflow".

Run `python session_runner.py --selftest` after any edit. It replays whole
weeks against a fake clock, including the three bad days above.
"""

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import market_calendar as cal

UTC = dt.timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")

# The grid, in Eastern wall-clock time.
FIRST_SLOT_ET = (9, 7)
LAST_SLOT_ET = (16, 52)
CADENCE = dt.timedelta(minutes=15)

# A slot reached this late (the previous cycle overran) still runs. Later
# than this it is logged as missed and the loop moves on to the next one.
SLOT_GRACE = dt.timedelta(minutes=7)
# On a link that starts while a session is under way: capture straight away
# if nothing has been captured for this long AND the next slot is more than
# half a cadence off. A handoff fails the first test; a start five minutes
# before a slot fails the second.
CATCHUP_GAP = dt.timedelta(minutes=10)
CATCHUP_MIN_WAIT = CADENCE / 2
# A start just after the last slot may still take one closing capture. Ends
# at 17:00 ET = 21:00 UTC in summer, which is the limit check_integrity.py
# enforces and inside the 21:15 gate in gex_intraday.py.
FINAL_CATCHUP = dt.timedelta(minutes=8)

# GitHub kills a hosted job at 360 minutes. Leave room for setup and for the
# last cycle to finish and push.
MAX_RUNTIME_MIN = 330
CYCLE_BUDGET = dt.timedelta(minutes=6)
# A successor is dispatched at least this long before the slot it must take.
# A link boots in under a minute; ten gives it room on a slow day.
HANDOFF_LEAD = dt.timedelta(minutes=10)
CAPTURE_TIMEOUT_S = 480

INTRADAY_WORKFLOW = "intraday.yml"
CAPTURE_WORKFLOW = "capture.yml"
# capture.yml's own cron is 11:30 UTC. Give it fifteen minutes, then stop
# waiting for the scheduler and dispatch it.
DAILY_DUE_UTC = dt.time(11, 45)
DAILY_RETRY = dt.timedelta(minutes=60)
DAILY_MAX_DISPATCHES = 2
# One row per symbol in gex_capture.DEFAULT_SYMBOLS (GLD and TLT added
# 2026-10-08). Keep in step with it, and with watchdog.py.
DAILY_SYMBOLS = 6
DAILY_CHECK_EVERY = dt.timedelta(minutes=10)

# Local mode only (no relay): a start earlier than this before the first slot
# exits instead of waiting.
EARLY_START = dt.timedelta(minutes=30)


# --------------------------------------------------------------------------
# time


def _nth_sunday(year, month, n):
    d = dt.date(year, month, 1)
    while d.weekday() != 6:
        d += dt.timedelta(days=1)
    return d + dt.timedelta(weeks=n - 1)


def et_offset(day):
    """Eastern's UTC offset on `day`. Stdlib only, like market_calendar.

    DST runs from the second Sunday of March to the first Sunday of November.
    Both switches happen on a Sunday, so for a trading day the date alone
    decides it.
    """
    in_dst = _nth_sunday(day.year, 3, 2) <= day < _nth_sunday(day.year, 11, 1)
    return dt.timedelta(hours=-4 if in_dst else -5)


def et_date(now_utc):
    """The Eastern calendar date at `now_utc`."""
    return (now_utc + et_offset(now_utc.date())).date()


def slots_utc(day):
    """Every capture slot for session `day`, as aware UTC datetimes."""
    off = et_offset(day)
    t = dt.datetime(day.year, day.month, day.day, *FIRST_SLOT_ET, tzinfo=UTC) - off
    end = dt.datetime(day.year, day.month, day.day, *LAST_SLOT_ET, tzinfo=UTC) - off
    out = []
    while t <= end:
        out.append(t)
        t += CADENCE
    return out


def next_slot(after):
    """The first slot strictly later than `after`, and its session's full grid.

    Walks forward over weekends and holidays, so the answer on a Friday
    evening is Monday's (or Tuesday's) first slot.
    """
    day = et_date(after)
    for _ in range(12):
        if cal.is_trading_day(day):
            grid = slots_utc(day)
            for s in grid:
                if s > after:
                    return s, grid
        day += dt.timedelta(days=1)
    raise RuntimeError(f"no NYSE session found in the 12 days after {after}")


# --------------------------------------------------------------------------
# side effects


class RealIO:
    """Everything the loop does to the outside world.

    Kept in one object so the self-test can replace all of it and run whole
    weeks in milliseconds against a fake clock.
    """

    def now(self):
        return dt.datetime.now(UTC)

    def sleep(self, seconds):
        time.sleep(seconds)

    def say(self, msg):
        print(f"{self.now().isoformat(timespec='seconds')}  {msg}", flush=True)

    def note(self, msg):
        """Print AND write to intraday.log, in gex_intraday.py's own format.

        The log is committed, so these lines are what let you reconstruct
        from git alone when a link took up a session, what it missed and
        whether it handed off -- without the Actions tab. Only ever called
        around a capture: a link that waits out a night writes nothing, so
        the repo does not collect a commit every six hours.
        """
        line = f"{self.now().isoformat(timespec='seconds')}  {msg}"
        print(line, flush=True)
        os.makedirs(HIST, exist_ok=True)
        with open(os.path.join(HIST, "intraday.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def capture(self):
        """One gex_intraday.py cycle in its own process. Returns its exit code."""
        try:
            r = subprocess.run([sys.executable, os.path.join(HERE, "gex_intraday.py")],
                               cwd=HERE, timeout=CAPTURE_TIMEOUT_S)
            return r.returncode
        except subprocess.TimeoutExpired:
            self.note(f"--- runner: capture exceeded {CAPTURE_TIMEOUT_S}s and was killed")
            return 124

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)

    def refresh(self):
        """Bring the checkout up to date with origin. True on success.

        A link can be hours old by the time it captures. Without this it
        would run the gex_intraday.py it was checked out with, not the one on
        main, and would not see daily rows another workflow pushed.
        """
        pull = self._git("pull", "--rebase", "--autostash")
        if pull.returncode != 0:
            # Never leave the checkout mid-rebase: every later cycle would
            # fail on it and the rest of the session would be lost.
            self._git("rebase", "--abort")
            self.say(f"pull failed: {pull.stderr.strip()[-300:]}")
            return False
        return True

    def publish(self, what="intraday"):
        """Commit whatever the cycle wrote and push it. True if origin has it.

        A failed push is not fatal: the commit stays local and the next
        cycle's push carries both. Only a link that dies with unpushed
        commits loses data, and run() goes red if it exits that way.

        `what` is the commit-message prefix. Captures keep the old workflow's
        "intraday <date> <time> UTC"; the runner's own closing log lines go
        in as "runner ...", so counting "intraday" commits still counts
        captures and nothing else.
        """
        self._git("add", "-A")
        if self._git("diff", "--staged", "--quiet").returncode != 0:
            stamp = self.now().strftime("%Y-%m-%d %H:%M UTC")
            c = self._git("commit", "-m", f"{what} {stamp}")
            if c.returncode != 0:
                self.say(f"commit failed: {c.stderr.strip()[-300:]}")
                return False

        ahead = self._git("rev-list", "--count", "@{u}..HEAD")
        if ahead.returncode == 0 and ahead.stdout.strip() == "0":
            return True

        for attempt in (1, 2, 3):
            if self.refresh():
                push = self._git("push")
                if push.returncode == 0:
                    return True
                self.say(f"push attempt {attempt} failed: {push.stderr.strip()[-300:]}")
            self.sleep(5 * attempt)
        return False

    def last_capture(self):
        """capture_ts of the newest row in intraday_state.csv, or None."""
        path = os.path.join(HIST, "intraday_state.csv")
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
            col = rows[0].index("capture_ts")
            ts = dt.datetime.fromisoformat(rows[-1][col])
            return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        except (OSError, ValueError, IndexError):
            return None

    def daily_rows(self, session):
        """Rows on file in daily_metrics.csv for `session` (YYYY-MM-DD)."""
        path = os.path.join(HIST, "daily_metrics.csv")
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                return sum(1 for r in csv.DictReader(fh)
                           if r.get("session_date") == session)
        except OSError:
            return 0

    def can_dispatch(self):
        return bool(os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_TOKEN"))

    def dispatch(self, workflow, inputs=None):
        """Start `workflow` through the REST API. True on success.

        A dispatched run is created by an API call, not by the scheduler, so
        it is not subject to whatever is dropping the crons. It is also the
        one event a workflow's own token is allowed to trigger.
        """
        repo = os.environ.get("GITHUB_REPOSITORY")
        token = os.environ.get("GITHUB_TOKEN")
        if not (repo and token):
            self.say(f"cannot dispatch {workflow}: not running inside Actions")
            return False
        api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        body = {"ref": os.environ.get("GITHUB_REF_NAME", "main")}
        if inputs:
            body["inputs"] = inputs
        req = urllib.request.Request(
            f"{api}/repos/{repo}/actions/workflows/{workflow}/dispatches",
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "gex-session-runner",
            },
        )
        # Six tries over about five minutes. This call is the chain: if it
        # does not land, nothing follows this link until a cron repairs it.
        for attempt in range(1, 7):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    if 200 <= resp.status < 300:
                        return True
                    self.say(f"dispatch {workflow}: HTTP {resp.status}")
            except urllib.error.HTTPError as e:
                self.say(f"dispatch {workflow} attempt {attempt}: HTTP {e.code} "
                         f"{e.read()[:200]!r}")
            except Exception as e:           # network, DNS, timeout
                self.say(f"dispatch {workflow} attempt {attempt}: {e}")
            self.sleep(20 * attempt)
        return False


# --------------------------------------------------------------------------
# the loop


def run(io, max_runtime_min=MAX_RUNTIME_MIN, relay=True, test_links=0):
    """One link of the relay. Returns an exit code.

    relay=True (inside Actions): never ends without dispatching a successor.
    relay=False (a machine of your own): covers today's session and returns;
    max_runtime_min=None then means no time limit at all.

    test_links=N: hand off immediately, N times in a row, then carry on as a
    normal link. Proves the chain in a few minutes instead of overnight.
    """
    start = io.now()

    if test_links > 0:
        left = test_links - 1
        ok = io.dispatch(INTRADAY_WORKFLOW, {"test_links": str(left)} if left else None)
        io.say(f"TEST LINK: successor {'dispatched' if ok else 'DISPATCH FAILED'}, "
               f"{left} test link(s) to go" + ("" if left else " -- the next one is a normal link"))
        return 0 if ok else 1

    deadline = (start + dt.timedelta(minutes=max_runtime_min)
                if max_runtime_min else None)
    day = et_date(start)
    today = slots_utc(day) if cal.is_trading_day(day) else None

    if not relay:
        if not today:
            io.say(f"{day:%Y-%m-%d %a} is not an NYSE session -- nothing to do")
            return 0
        if start < today[0] - EARLY_START:
            io.say(f"{(today[0] - start).total_seconds() / 60:.0f} min before the "
                   f"first slot ({today[0]:%H:%M} UTC) -- too early")
            return 0
        if start > today[-1] + FINAL_CATCHUP:
            io.say(f"session over (last slot {today[-1]:%H:%M} UTC) -- nothing to do")
            return 0

    io.say(f"link start  run={os.environ.get('GITHUB_RUN_ID', 'local')} "
           f"event={os.environ.get('GITHUB_EVENT_NAME', 'local')}"
           + (f"  must hand off by {deadline:%Y-%m-%d %H:%M} UTC" if deadline else ""))

    done, failed, pushed = 0, [], True
    announced = None                       # the session this link has logged a start for
    daily = {"n": 0, "last": None, "done": None, "next_check": start}

    def announce(grid, ahead):
        """One 'runner start' line per session, written just before this
        link's first capture of it, so it is committed with that capture."""
        nonlocal announced
        if announced == grid[0]:
            return
        announced = grid[0]
        io.note(f"--- runner start  run={os.environ.get('GITHUB_RUN_ID', 'local')} "
                f"event={os.environ.get('GITHUB_EVENT_NAME', 'local')}  "
                f"{ahead} of {len(grid)} slots ahead"
                + (f"  must hand off by {deadline:%H:%M} UTC" if deadline else ""))

    def cycle(label):
        nonlocal done, pushed
        io.refresh()
        rc = io.capture()
        done += 1
        if rc != 0:
            failed.append(label)
            io.note(f"--- runner: capture at {label} exited {rc}")
        pushed = io.publish()
        if not pushed:
            io.note("--- runner: push failed, commit kept locally for the next cycle")

    def ensure_daily():
        now = io.now()
        today_utc = now.date()
        daily["next_check"] = now + DAILY_CHECK_EVERY
        if daily["done"] == today_utc or not cal.should_capture(today_utc):
            return
        if now.time() < DAILY_DUE_UTC:
            return
        if not io.can_dispatch() or daily["n"] >= DAILY_MAX_DISPATCHES:
            return
        if daily["last"] and now - daily["last"] < DAILY_RETRY:
            return
        io.refresh()
        session = str(cal.session_for(today_utc))
        have = io.daily_rows(session)
        if have >= DAILY_SYMBOLS:
            daily["done"] = today_utc
            return
        ok = io.dispatch(CAPTURE_WORKFLOW)
        daily["n"] += 1
        daily["last"] = now
        io.say(f"daily capture for {session} has {have}/{DAILY_SYMBOLS} rows -- "
               f"dispatched {CAPTURE_WORKFLOW}{'' if ok else ' (DISPATCH FAILED)'}")

    def idle_until(when):
        """Sleep to `when`, checking on the daily capture along the way."""
        while (wait := (when - io.now()).total_seconds()) > 0:
            if wait > 90 and io.now() >= daily["next_check"]:
                ensure_daily()
                continue
            io.sleep(min(wait, 20))

    # A link that starts with a session already under way.
    if today and today[0] <= start <= today[-1] + FINAL_CATCHUP:
        gone = [s for s in today if s <= start]
        left = [s for s in today if s > start]
        prev = io.last_capture()
        stale = prev is None or start - prev > CATCHUP_GAP
        if stale:
            announce(today, len(left))
            io.note(f"--- runner: joined late, {len(gone)} slot(s) already past "
                    f"({gone[0]:%H:%M}-{gone[-1]:%H:%M} UTC), last capture on file "
                    f"{prev.isoformat(timespec='seconds') if prev else 'none'}")
            if not left or left[0] - start > CATCHUP_MIN_WAIT:
                cycle(f"{start:%H:%M} UTC (catch-up)")

    handoff_failed = False
    after = start                          # every slot up to here is dealt with
    while True:
        slot, grid = next_slot(after)
        if not relay and grid[0] != today[0]:
            break                          # local mode: today's session is finished

        if deadline and slot + CYCLE_BUDGET > deadline:
            # This link cannot take `slot`. Wait as long as it safely can --
            # overnight that is hours, mid-session it is minutes -- then
            # start the link that will.
            idle_until(min(deadline - CYCLE_BUDGET, slot - HANDOFF_LEAD))
            ok = io.dispatch(INTRADAY_WORKFLOW)
            handoff_failed = not ok
            msg = (f"--- runner handoff before the {slot:%Y-%m-%d %H:%M} UTC slot: successor "
                   f"{'dispatched' if ok else 'DISPATCH FAILED -- a cron must repair the chain'}")
            # Logged to the repo only mid-session, where it explains a gap.
            (io.note if announced == grid[0] else io.say)(msg)
            break

        idle_until(slot)
        after = slot
        now = io.now()
        if now > slot + SLOT_GRACE:
            announce(grid, len([s for s in grid if s > slot]))
            io.note(f"--- runner: slot {slot:%H:%M} UTC missed "
                    f"(reached at {now:%H:%M:%S}, the previous cycle overran)")
            continue

        announce(grid, len([s for s in grid if s >= slot]))
        cycle(f"{slot:%H:%M} UTC")
        if slot == grid[-1]:
            io.note(f"--- runner: session complete, {done} capture(s) by this link")
            pushed = io.publish("runner")

    # Last chance for anything still local. Unpushed commits die with the job.
    pushed = io.publish("runner")
    for _ in range(3):
        if pushed:
            break
        io.sleep(30)
        pushed = io.publish("runner")

    problems = []
    if failed:
        problems.append(f"{len(failed)} capture(s) exited non-zero: {', '.join(failed)}")
    if not pushed:
        problems.append("commits left unpushed -- that data is LOST unless the "
                        "uploaded artifact is recovered")
    if handoff_failed:
        problems.append("could not dispatch a successor -- THE CHAIN IS BROKEN until "
                        "a cron fires or someone clicks Run workflow")
    for p in problems:
        io.say(f"PROBLEM: {p}")
    return 1 if problems else 0


# --------------------------------------------------------------------------
# self-test


class FakeIO:
    """A link against a fake clock. Nothing touches disk or network."""

    def __init__(self, start, last_capture=None, capture_s=40, slow=None,
                 daily=DAILY_SYMBOLS, daily_lands_after=None, push_ok=True, dispatch_ok=True):
        self.t = start
        self._last = last_capture
        self.capture_s, self.slow = capture_s, slow or {}
        self.daily, self.daily_lands_after = daily, daily_lands_after
        self.push_ok, self.dispatch_ok = push_ok, dispatch_ok
        self.captures, self.dispatched, self.notes = [], [], []

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += dt.timedelta(seconds=s)

    def say(self, msg):
        pass

    def note(self, msg):
        self.notes.append(msg)

    def refresh(self):
        return True

    def capture(self):
        self.captures.append(self.t)
        self.t += dt.timedelta(seconds=self.slow.get(len(self.captures), self.capture_s))
        self._last = self.captures[-1]
        return 0

    def publish(self, what="intraday"):
        return self.push_ok

    def last_capture(self):
        return self._last

    def daily_rows(self, session):
        if self.daily_lands_after and self.t >= self.daily_lands_after:
            return DAILY_SYMBOLS
        return self.daily

    def can_dispatch(self):
        return True

    def dispatch(self, workflow, inputs=None):
        self.dispatched.append((workflow, self.t, inputs))
        return self.dispatch_ok


def _u(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=UTC)


def _chain(first_start, until, last_capture=None, **kw):
    """Follow the relay from one start until `until`, the way the workflow's
    concurrency group runs it: one link at a time, each successor booting a
    minute after the link that dispatched it. Returns (captures, links,
    daily_dispatches, notes_by_link)."""
    t, end, prev = _u(first_start), _u(until), last_capture
    caps, links, dailies, notes = [], 0, [], []
    while t < end:
        io = FakeIO(t, last_capture=prev, **kw)
        run(io)
        links += 1
        caps += io.captures
        notes.append(io.notes)
        dailies += [x[1] for x in io.dispatched if x[0] == CAPTURE_WORKFLOW]
        prev = caps[-1] if caps else prev
        if not [x for x in io.dispatched if x[0] == INTRADAY_WORKFLOW]:
            break                                   # the chain broke
        assert io.t <= t + dt.timedelta(minutes=MAX_RUNTIME_MIN), "link outlived its limit"
        t = io.t + dt.timedelta(seconds=60)
    return [c for c in caps if c < end], links, dailies, notes


def selftest():
    hm = lambda xs: [x.strftime("%H:%M") for x in xs]
    on = lambda xs, d: [x for x in xs if x.strftime("%Y-%m-%d") == d]

    # --- the grid ----------------------------------------------------------
    s = slots_utc(dt.date(2026, 10, 8))
    assert len(s) == 32 and hm(s)[0] == "13:07" and hm(s)[-1] == "20:52", hm(s)
    w = slots_utc(dt.date(2026, 11, 2))               # first session on EST
    assert hm(w)[0] == "14:07" and hm(w)[-1] == "21:52", hm(w)
    assert et_offset(dt.date(2026, 10, 30)) == dt.timedelta(hours=-4)
    assert et_offset(dt.date(2026, 11, 2)) == dt.timedelta(hours=-5)
    assert et_offset(dt.date(2027, 3, 12)) == dt.timedelta(hours=-5)
    assert et_offset(dt.date(2027, 3, 15)) == dt.timedelta(hours=-4)
    assert et_date(_u("2026-10-08T02:30:00")) == dt.date(2026, 10, 7)
    assert next_slot(_u("2026-10-08T12:00:00"))[0] == _u("2026-10-08T13:07:00")
    assert next_slot(_u("2026-10-08T13:07:00"))[0] == _u("2026-10-08T13:22:00")
    assert next_slot(_u("2026-10-09T20:52:00"))[0] == _u("2026-10-12T13:07:00")   # Fri -> Mon
    assert next_slot(_u("2026-11-25T23:00:00"))[0] == _u("2026-11-27T14:07:00")   # Thanksgiving

    # --- THE POINT: one click on Wednesday evening, then nothing ----------
    # No cron ever fires in this replay. The chain alone must deliver every
    # slot of Thursday and Friday, hold the weekend, and deliver Monday.
    caps, links, dailies, notes = _chain("2026-10-07T23:50:00", "2026-10-12T22:00:00")
    for d in ("2026-10-08", "2026-10-09", "2026-10-12"):
        assert hm(on(caps, d)) == hm(s), (d, hm(on(caps, d)))
    assert len(caps) == 96, len(caps)                 # and nothing on Sat/Sun
    assert all(c.second < 5 and c.minute % 15 == 7 for c in caps), "off grid"
    assert 20 <= links <= 26, links                   # ~4.4 a day
    assert not dailies, "the daily capture was on file throughout this replay"

    # --- a link that waits out the night writes nothing to the repo --------
    io = FakeIO(_u("2026-10-10T03:00:00"))            # Saturday
    assert run(io) == 0 and not io.captures and not io.notes
    assert [x[0] for x in io.dispatched] == [INTRADAY_WORKFLOW]
    assert io.t - _u("2026-10-10T03:00:00") >= dt.timedelta(minutes=MAX_RUNTIME_MIN - 7)

    # --- the clock change, and a holiday, need no edit ---------------------
    caps, *_ = _chain("2026-10-30T21:30:00", "2026-11-02T23:00:00")
    assert hm(caps) == hm(w), hm(caps)                # Mon 11/02 on the winter grid
    caps, *_ = _chain("2026-11-25T22:30:00", "2026-11-27T23:00:00")
    assert not on(caps, "2026-11-26") and len(on(caps, "2026-11-27")) == 32

    # --- the handoff inside a session: no slot lost, none taken twice ------
    io = FakeIO(_u("2026-10-08T12:53:00"))
    assert run(io) == 0
    assert hm(io.captures)[0] == "13:07" and hm(io.captures)[-1] == "18:07"
    assert [x[0] for x in io.dispatched] == [INTRADAY_WORKFLOW]
    assert io.dispatched[0][1] <= _u("2026-10-08T18:22:00") - HANDOFF_LEAD
    assert any("runner handoff" in n for n in io.notes)
    io2 = FakeIO(io.t + dt.timedelta(seconds=60), last_capture=io.captures[-1])
    run(io2)                                          # boots a minute later
    assert hm(io2.captures)[0] == "18:22" and len(io2.captures) == 11
    assert any("session complete" in n for n in io2.notes)

    # --- joining a session late -------------------------------------------
    stale = _u("2026-10-07T19:18:31")
    io = FakeIO(_u("2026-10-08T17:53:00"), last_capture=stale)
    run(io)                                   # 14 min to the next slot: catch up
    assert hm(io.captures)[:2] == ["17:53", "18:07"] and len(io.captures) == 13
    io = FakeIO(_u("2026-10-08T19:17:00"), last_capture=stale)
    run(io)                                   # 5 min to the next slot: wait for it
    assert hm(io.captures)[0] == "19:22" and len(io.captures) == 7
    io = FakeIO(_u("2026-10-08T20:55:00"), last_capture=stale)
    run(io)                                   # after the last slot: one closing capture
    assert hm(io.captures) == ["20:55"]
    io = FakeIO(_u("2026-10-08T21:10:00"), last_capture=stale)
    run(io)                                   # too late for today: wait for tomorrow
    assert not io.captures and not io.notes

    # --- a cycle that overruns costs its neighbour, not the session --------
    io = FakeIO(_u("2026-10-08T13:00:00"), slow={3: 25 * 60})
    run(io)
    assert "13:52" not in hm(io.captures) and "14:07" in hm(io.captures)
    assert any("13:52 UTC missed" in n for n in io.notes)

    # --- failures are reported, and do not stop the captures ---------------
    io = FakeIO(_u("2026-10-08T18:13:00"), last_capture=_u("2026-10-08T18:07:01"),
                push_ok=False)
    assert run(io) == 1 and len(io.captures) == 11
    io = FakeIO(_u("2026-10-08T12:53:00"), dispatch_ok=False)
    assert run(io) == 1 and len(io.captures) == 21

    # --- the chain owns the daily capture ----------------------------------
    # Missing all week: dispatched Tue-Sat only, never before 11:45 UTC.
    _, _, d, _ = _chain("2026-10-12T22:00:00", "2026-10-19T22:00:00", daily=0)
    days = sorted({x.strftime("%a") for x in d})
    assert days == sorted(["Tue", "Wed", "Thu", "Fri", "Sat"]), days
    assert all(x.time() >= DAILY_DUE_UTC for x in d)
    # Lands a few minutes after the first dispatch: asked for exactly once,
    # within one check interval of coming due.
    io = FakeIO(_u("2026-10-08T08:00:00"), daily=0,
                daily_lands_after=_u("2026-10-08T11:58:00"))
    run(io)
    d = [x[1] for x in io.dispatched if x[0] == CAPTURE_WORKFLOW]
    assert len(d) == 1, d
    assert DAILY_DUE_UTC <= d[0].time() <= dt.time(11, 55), d
    # Never lands: capped per link, an hour apart.
    io = FakeIO(_u("2026-10-08T11:00:00"), daily=0)
    run(io)
    d = [x[1] for x in io.dispatched if x[0] == CAPTURE_WORKFLOW]
    assert len(d) == DAILY_MAX_DISPATCHES and d[1] - d[0] >= DAILY_RETRY
    # Already on file: never asked for.
    io = FakeIO(_u("2026-10-08T11:00:00"), daily=DAILY_SYMBOLS)
    run(io)
    assert CAPTURE_WORKFLOW not in [x[0] for x in io.dispatched]

    # --- the test switch ---------------------------------------------------
    io = FakeIO(_u("2026-10-08T00:00:00"))
    assert run(io, test_links=3) == 0 and not io.captures
    assert io.dispatched == [(INTRADAY_WORKFLOW, io.t, {"test_links": "2"})]
    io = FakeIO(_u("2026-10-08T00:00:00"))
    run(io, test_links=1)
    assert io.dispatched[0][2] is None                # the next link is a normal one

    # --- local mode: today's session and out -------------------------------
    io = FakeIO(_u("2026-10-08T12:53:00"))
    assert run(io, max_runtime_min=None, relay=False) == 0
    assert hm(io.captures) == hm(s) and not io.dispatched
    for when in ("2026-10-10T14:00:00", "2026-10-08T11:40:00", "2026-10-08T21:10:00"):
        io = FakeIO(_u(when))
        assert run(io, max_runtime_min=None, relay=False) == 0
        assert not io.captures and not io.dispatched, when

    # --- the three bad days, replayed from the fire times GitHub DID deliver
    # These assume NO chain was running, only the one late start.
    # 10/06: first run created 13:15 UTC. Actual: 2 cycles.
    caps, *_ = _chain("2026-10-06T13:15:00", "2026-10-06T21:30:00")
    assert len(caps) == 31 and hm(caps)[0] == "13:22"
    # 10/07: one run, created 19:17 UTC. Actual: 1 cycle.
    caps, *_ = _chain("2026-10-07T19:17:00", "2026-10-07T21:30:00")
    assert len(caps) == 7
    # 10/05: first run created 13:18. Actual: 13 cycles. The first link alone
    # holds 13:22-18:37 = 22; the rest needs its successor to get a runner at
    # 18:42, half an hour before that day's incident was declared.
    io = FakeIO(_u("2026-10-05T13:18:00"))
    run(io)
    assert len(io.captures) == 22 and hm(io.captures)[-1] == "18:37"

    print("session_runner: all self-tests passed")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--selftest", action="store_true",
                   help="replay whole weeks against a fake clock and exit")
    p.add_argument("--test-links", type=int, default=0, metavar="N",
                   help="hand off immediately N times, then run normally "
                        "(proves the chain in minutes)")
    p.add_argument("--local", action="store_true",
                   help="not a GitHub runner: cover today's session and exit, "
                        "no time limit, no relay")
    a = p.parse_args()
    if a.selftest:
        selftest()
        return 0
    hosted = os.environ.get("GITHUB_ACTIONS") == "true" and not a.local
    if hosted:
        return run(RealIO(), MAX_RUNTIME_MIN, relay=True, test_links=a.test_links)
    return run(RealIO(), None, relay=False)


if __name__ == "__main__":
    sys.exit(main())
