#!/usr/bin/env python3
"""Hold one runner for the session and capture on a fixed 15-minute grid.

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
    as the 19 intraday slots in it did. What helps is needing fewer fires.

    So this script needs ONE. Whatever starts the workflow -- a cron that
    happens to fire, a manual click, an outside service calling the dispatch
    API -- the job then stays up and takes every remaining slot itself,
    sleeping between them. The scheduler is asked for a start, not for a
    cadence, and a runner already held cannot be "not acquired".

WHAT IT DOES
    1. Works out today's slots: :07/:22/:37/:52 from 09:07 to 16:52 ET, the
       same grid the old cron aimed at, in Eastern time so the November clock
       change needs no edit here.
    2. Runs gex_intraday.py as a fresh process at each slot, then commits and
       pushes exactly as the old workflow step did. gex_intraday.py is not
       modified and keeps every guard it has.
    3. A GitHub-hosted job dies at six hours and the session grid is 7h45m, so
       before its time is up the runner dispatches its own successor through
       the API and exits. The workflow's concurrency group queues the
       successor behind it, so the two never overlap.
    4. Backstops the daily capture: if settled OI for the prior session is
       not on file by 12:30 UTC, it dispatches capture.yml. That capture is
       the one thing that can never be re-fetched.

    Every start that finds the session not in progress exits in seconds, so
    spare starts are harmless. Spare starts DURING the session wait in the
    concurrency queue and take over the moment the running job ends for any
    reason -- its time limit, a lost runner, anything.

WHAT IT DOES NOT FIX
    Something still has to start it once. GitHub's own cron is the weakest
    possible thing to rely on for that -- see the note in intraday.yml about
    an outside trigger. With only GitHub crons, 10/07 would have been 7
    cycles instead of 1: better, and still not a session.

    The session-hours gate inside gex_intraday.py is hard-coded 13:00-21:15
    UTC. That is correct until US clocks change on 2026-11-01 and one hour
    wrong after. The grid here will follow the clock; the gate will not, and
    it will silently drop the 16:22-16:52 ET slots until it is fixed.

Run `python session_runner.py --selftest` after any edit. It replays whole
sessions against a fake clock, including the three bad days above.
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

# A start earlier than this before the first slot exits instead of sleeping:
# time spent asleep before the open comes off the six-hour limit.
EARLY_START = dt.timedelta(minutes=30)
# A slot reached this late (the previous cycle overran) still runs. Later
# than this it is logged as missed and the loop moves on to the next one.
SLOT_GRACE = dt.timedelta(minutes=7)
# On a start that joins a session already under way: capture straight away
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
CAPTURE_TIMEOUT_S = 480

INTRADAY_WORKFLOW = "intraday.yml"
CAPTURE_WORKFLOW = "capture.yml"
DAILY_DUE_UTC = dt.time(12, 30)      # same threshold watchdog.py uses
DAILY_RETRY = dt.timedelta(minutes=60)
DAILY_MAX_DISPATCHES = 2
DAILY_SYMBOLS = 4


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
    guess = now_utc.date()
    return (now_utc + et_offset(guess)).date()


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


# --------------------------------------------------------------------------
# side effects


class RealIO:
    """Everything the loop does to the outside world.

    Kept in one object so the self-test can replace all of it and run a whole
    session in milliseconds against a fake clock.
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
        from git alone when a runner started, what it missed and whether it
        handed off -- without the Actions tab.
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

    def publish(self, what="intraday"):
        """Commit whatever the cycle wrote and push it. True if origin has it.

        A failed push is not fatal: the commit stays local and the next
        cycle's push carries both. Only a runner that dies with unpushed
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
            pull = self._git("pull", "--rebase", "--autostash")
            if pull.returncode == 0:
                push = self._git("push")
                if push.returncode == 0:
                    return True
                self.say(f"push attempt {attempt} failed: {push.stderr.strip()[-300:]}")
            else:
                # Never leave the checkout mid-rebase: every later cycle
                # would fail on it and the rest of the session would be lost.
                self._git("rebase", "--abort")
                self.say(f"pull attempt {attempt} failed: {pull.stderr.strip()[-300:]}")
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

    def dispatch(self, workflow):
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
        ref = os.environ.get("GITHUB_REF_NAME", "main")
        req = urllib.request.Request(
            f"{api}/repos/{repo}/actions/workflows/{workflow}/dispatches",
            data=json.dumps({"ref": ref}).encode(),
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "gex-session-runner",
            },
        )
        for attempt in (1, 2, 3):
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
            self.sleep(10 * attempt)
        return False


# --------------------------------------------------------------------------
# the loop


def run(io, max_runtime_min=MAX_RUNTIME_MIN):
    """Capture every remaining slot of today's session. Returns an exit code.

    max_runtime_min=None means no time limit (a run outside Actions): the
    loop then covers the whole session and never hands off.
    """
    start = io.now()
    day = et_date(start)

    if not cal.is_trading_day(day):
        io.say(f"{day:%Y-%m-%d %a} is not an NYSE session -- nothing to do")
        return 0

    slots = slots_utc(day)
    first, last = slots[0], slots[-1]
    if start < first - EARLY_START:
        io.say(f"{(first - start).total_seconds() / 60:.0f} min before the first "
               f"slot ({first:%H:%M} UTC) -- too early, a later start will take it")
        return 0
    if start > last + FINAL_CATCHUP:
        io.say(f"session over (last slot {last:%H:%M} UTC) -- nothing to do")
        return 0

    deadline = (start + dt.timedelta(minutes=max_runtime_min)
                if max_runtime_min else None)
    todo = [s for s in slots if s > start]
    gone = [s for s in slots if s <= start]

    io.note(f"--- runner start  run={os.environ.get('GITHUB_RUN_ID', 'local')} "
            f"event={os.environ.get('GITHUB_EVENT_NAME', 'local')}  "
            f"{len(todo)} of {len(slots)} slots ahead"
            + (f"  must hand off by {deadline:%H:%M} UTC" if deadline else ""))

    done, failed, pushed = 0, [], True
    daily = {"n": 0, "last": None}

    def cycle(label):
        nonlocal done, pushed
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
        today = now.date()
        if not cal.should_capture(today) or now.time() < DAILY_DUE_UTC:
            return
        if not io.can_dispatch() or daily["n"] >= DAILY_MAX_DISPATCHES:
            return
        if daily["last"] and now - daily["last"] < DAILY_RETRY:
            return
        session = str(cal.session_for(today))
        have = io.daily_rows(session)
        if have >= DAILY_SYMBOLS:
            return
        ok = io.dispatch(CAPTURE_WORKFLOW)
        daily["n"] += 1
        daily["last"] = now
        io.note(f"--- runner: daily capture for {session} has {have}/{DAILY_SYMBOLS} "
                f"rows -- dispatched {CAPTURE_WORKFLOW}"
                f"{'' if ok else ' (DISPATCH FAILED)'}")

    ensure_daily()

    # Joining a session already under way.
    if gone:
        prev = io.last_capture()
        stale = prev is None or start - prev > CATCHUP_GAP
        room = not todo or todo[0] - start > CATCHUP_MIN_WAIT
        if stale:
            io.note(f"--- runner: joined late, {len(gone)} slot(s) already past "
                    f"({gone[0]:%H:%M}-{gone[-1]:%H:%M} UTC), last capture on file "
                    f"{prev.isoformat(timespec='seconds') if prev else 'none'}")
        if stale and room:
            cycle(f"{start:%H:%M} UTC (catch-up)")

    handoff_failed = False
    for slot in todo:
        if deadline and slot + CYCLE_BUDGET > deadline:
            ok = io.dispatch(INTRADAY_WORKFLOW)
            handoff_failed = not ok
            io.note(f"--- runner handoff before the {slot:%H:%M} UTC slot: successor "
                    f"{'dispatched' if ok else 'DISPATCH FAILED -- the next start must pick this up'}")
            break

        now = io.now()
        if now > slot + SLOT_GRACE:
            io.note(f"--- runner: slot {slot:%H:%M} UTC missed "
                    f"(reached at {now:%H:%M:%S}, the previous cycle overran)")
            continue
        while (wait := (slot - io.now()).total_seconds()) > 0:
            io.sleep(min(wait, 20))

        cycle(f"{slot:%H:%M} UTC")
        ensure_daily()
    else:
        io.note(f"--- runner: session complete, {done} capture(s) this run")

    # Last chance for anything still local. Unpushed commits die with the job.
    pushed = io.publish("runner")      # the handoff / completion log lines
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
        problems.append("could not dispatch a successor")
    for p in problems:
        io.say(f"PROBLEM: {p}")
    return 1 if problems else 0


# --------------------------------------------------------------------------
# self-test


class FakeIO:
    """A whole session against a fake clock. Nothing touches disk or network."""

    def __init__(self, start, last_capture=None, capture_s=40, slow=None,
                 daily=4, daily_lands_after=None, push_ok=True, dispatch_ok=True):
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
            return 4
        return self.daily

    def can_dispatch(self):
        return True

    def dispatch(self, workflow):
        self.dispatched.append((workflow, self.t))
        return self.dispatch_ok


def _u(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=UTC)


def _session(first_start, later_starts=(), runner_dies_at=None):
    """Chain runs the way the workflow's concurrency group does.

    One run at a time. A start that arrives while a run is up waits and takes
    over when that run ends; a self-dispatched successor does the same.
    Returns every capture time across all runs.
    """
    caps, t, prev = [], _u(first_start), None
    queue = sorted(_u(x) for x in later_starts)
    while True:
        io = FakeIO(t, last_capture=prev)
        run(io)
        caps += io.captures
        prev = caps[-1] if caps else prev
        end = io.t
        handed = [w for w, _ in io.dispatched if w == INTRADAY_WORKFLOW]
        waiting = [q for q in queue if q <= end]
        queue = [q for q in queue if q > end]
        if handed or waiting:
            t = end + dt.timedelta(seconds=60)      # boot time of the next job
        elif queue:
            t = queue.pop(0)
        else:
            return caps


def selftest():
    hm = lambda xs: [x.strftime("%H:%M") for x in xs]

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

    # --- a normal day: one start, two runs, all 32 slots, none twice -------
    caps = _session("2026-10-08T12:53:00")
    assert hm(caps) == hm(s), hm(caps)
    assert all(0 <= (c - x).total_seconds() < 5 for c, x in zip(caps, s)), "off grid"

    io = FakeIO(_u("2026-10-08T12:53:00"))
    assert run(io) == 0
    assert hm(io.captures)[-1] == "18:07", hm(io.captures)[-1]
    assert [w for w, _ in io.dispatched] == [INTRADAY_WORKFLOW]
    assert io.t < _u("2026-10-08T12:53:00") + dt.timedelta(minutes=MAX_RUNTIME_MIN)

    # The successor boots a minute after a capture: no catch-up, no double.
    io2 = FakeIO(_u("2026-10-08T18:09:00"), last_capture=_u("2026-10-08T18:07:01"))
    assert run(io2) == 0
    assert hm(io2.captures)[0] == "18:22" and len(io2.captures) == 11

    # --- winter needs no edit here -----------------------------------------
    assert hm(_session("2026-11-02T13:53:00")) == hm(w)

    # --- starts that must do nothing ---------------------------------------
    for when in ("2026-10-10T14:00:00",       # Saturday
                 "2026-09-07T14:00:00",       # Labor Day
                 "2026-10-08T11:40:00",       # too early
                 "2026-10-08T21:10:00",       # session over
                 "2026-10-08T02:30:00"):      # deferred overnight
        io = FakeIO(_u(when))
        assert run(io) == 0 and not io.captures and not io.notes, when

    # --- joining late ------------------------------------------------------
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

    # --- a cycle that overruns costs its neighbour, not the session --------
    io = FakeIO(_u("2026-10-08T13:00:00"), slow={3: 25 * 60})
    run(io)
    assert "13:52" not in hm(io.captures) and "14:07" in hm(io.captures)
    assert any("13:52 UTC missed" in n for n in io.notes)

    # --- failures are reported, and do not stop the captures ---------------
    io = FakeIO(_u("2026-10-08T18:09:00"), last_capture=_u("2026-10-08T18:07:01"),
                push_ok=False)
    assert run(io) == 1 and len(io.captures) == 11
    io = FakeIO(_u("2026-10-08T12:53:00"), dispatch_ok=False)
    assert run(io) == 1 and len(io.captures) == 21

    # --- daily backstop ----------------------------------------------------
    io = FakeIO(_u("2026-10-08T12:53:00"), daily=0,
                daily_lands_after=_u("2026-10-08T12:58:00"))
    run(io)                                   # missing at start, lands 5 min later
    assert [w for w, _ in io.dispatched].count(CAPTURE_WORKFLOW) == 1
    io = FakeIO(_u("2026-10-08T12:53:00"), daily=0)
    run(io)                                   # never lands: capped, an hour apart
    d = [t for w, t in io.dispatched if w == CAPTURE_WORKFLOW]
    assert len(d) == DAILY_MAX_DISPATCHES and d[1] - d[0] >= DAILY_RETRY
    io = FakeIO(_u("2026-10-12T12:53:00"), daily=0)
    run(io)                                   # Monday: Friday was captured Saturday
    assert CAPTURE_WORKFLOW not in [w for w, _ in io.dispatched]
    io = FakeIO(_u("2026-10-08T12:53:00"), daily=4)
    run(io)
    assert CAPTURE_WORKFLOW not in [w for w, _ in io.dispatched]

    # --- the three bad days, replayed from the fire times GitHub DID deliver
    # 10/06: runs were created at 13:15, 18:56 and 22:56 UTC. Actual: 2 cycles.
    caps = _session("2026-10-06T13:15:00",
                    ["2026-10-06T18:56:00", "2026-10-06T22:56:00"])
    assert len(caps) == 31 and hm(caps)[0] == "13:22" and hm(caps)[-1] == "20:52"
    # 10/07: one run, created 19:17 UTC. Actual: 1 cycle.
    assert len(_session("2026-10-07T19:17:00")) == 7
    # 10/05: first run created 13:18. Actual: 13 cycles. The first runner alone
    # holds 13:22-18:37 = 22. The other 9 depend on its successor getting a
    # runner at ~18:38, half an hour before that day's Actions incident was
    # declared -- likely, not certain, so only the 22 are asserted.
    io = FakeIO(_u("2026-10-05T13:18:00"))
    run(io)
    assert len(io.captures) == 22 and hm(io.captures)[-1] == "18:37"

    print("session_runner: all self-tests passed")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--selftest", action="store_true",
                   help="replay sessions against a fake clock and exit")
    p.add_argument("--no-limit", action="store_true",
                   help="no six-hour handoff (for a machine that is not a "
                        "GitHub-hosted runner)")
    a = p.parse_args()
    if a.selftest:
        selftest()
        return 0
    hosted = os.environ.get("GITHUB_ACTIONS") == "true" and not a.no_limit
    return run(RealIO(), MAX_RUNTIME_MIN if hosted else None)


if __name__ == "__main__":
    sys.exit(main())
