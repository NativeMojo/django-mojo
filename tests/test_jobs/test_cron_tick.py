"""
The resident cron tick (maestro #9757).

Every scheduler process ticks cron.run_now() once at start and then on every
wall-clock minute boundary, replacing the per-minute `2_mojo_cron` entry that
booted a Django process per node per minute. These tests drive the ticker
through its seams (run/load/clock/stop_event) — no patching of mojo.helpers
or mojo.apps.jobs.
"""
import datetime
import threading
import time

from testit import helpers as th


class ScriptedClock:
    """A clock the ticker reads; waiting on the event advances it."""

    def __init__(self, start):
        self.now = float(start)

    def __call__(self):
        return self.now


class ScriptedEvent(threading.Event):
    """stop_event whose wait() advances the scripted clock instead of sleeping."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if not self.is_set() and timeout:
            self.clock.now += timeout
        return self.is_set()


def _ticker(start_at, run, load=None):
    from mojo.apps.jobs.cron_tick import CronTicker
    clock = ScriptedClock(start_at)
    event = ScriptedEvent(clock)
    ticker = CronTicker(run=run, load=load or (lambda: None),
                        clock=clock, stop_event=event)
    return ticker, clock, event


@th.django_unit_test()
def test_next_boundary_is_the_next_whole_minute(opts):
    from mojo.apps.jobs.cron_tick import next_boundary

    th.assert_eq(next_boundary(125.4), 180,
                 "mid-minute input must round up to the next minute")
    th.assert_eq(next_boundary(120), 180,
                 "an exact boundary must schedule the following minute, not itself")
    th.assert_eq(next_boundary(119.999), 120,
                 "a clock a hair before the boundary must still target that boundary")
    th.assert_eq(next_boundary(0), 60, "epoch zero targets minute one")


@th.django_unit_test()
def test_ticks_once_at_start_then_on_every_minute_boundary(opts):
    seen = []

    def run(now=None):
        seen.append((now, clock.now))
        if len(seen) == 3:
            event.set()

    ticker, clock, event = _ticker(65, run)   # 00:01:05
    ticker.run_forever()

    th.assert_eq(len(seen), 3, f"expected the start tick plus two boundaries, got {seen}")
    minutes = [now.minute for now, _ in seen]
    th.assert_eq(minutes, [1, 2, 3],
                 "the start tick covers the minute the ticker started in, then "
                 f"each following minute once: {minutes}")
    at = [clock_at for _, clock_at in seen]
    th.assert_eq(at, [65.0, 120.0, 180.0],
                 f"boundary ticks must fire exactly on the minute, not drift: {at}")
    th.assert_true(all(isinstance(now, datetime.datetime) for now, _ in seen),
                   "run_now must receive a datetime, the matcher's input")
    th.assert_eq(ticker.ticks, 3, "ticks counter must count every call")
    th.assert_eq(ticker.failures, 0, "no failure was raised")


@th.django_unit_test()
def test_a_failing_run_is_logged_and_the_next_minute_still_ticks(opts):
    calls = []

    def run(now=None):
        calls.append(clock.now)
        if len(calls) == 1:
            raise RuntimeError("a cron function raised")
        event.set()

    ticker, clock, event = _ticker(10, run)
    ticker.run_forever()

    th.assert_eq(calls, [10.0, 60.0],
                 "the tick after a failure must still fire on the next boundary")
    th.assert_eq(ticker.failures, 1, "the failure must be counted, not swallowed silently")
    th.assert_eq(ticker.ticks, 2, "both ticks ran")


@th.django_unit_test()
def test_a_run_that_overruns_the_minute_skips_it_rather_than_replaying(opts):
    calls = []

    def run(now=None):
        calls.append(clock.now)
        if len(calls) == 2:
            clock.now += 130           # the batch took more than two minutes
        if len(calls) == 3:
            event.set()

    ticker, clock, event = _ticker(5, run)
    ticker.run_forever()

    th.assert_eq(calls, [5.0, 60.0, 240.0],
                 "after a 130s run ending at 190, the next tick is 240 — minutes "
                 f"120 and 180 are skipped, never replayed: {calls}")
    th.assert_eq(ticker.skipped_minutes, 1,
                 "an overrun must be recorded (one warning per overrunning tick)")


@th.django_unit_test()
def test_a_stop_during_the_sleep_never_starts_a_batch(opts):
    calls = []

    def run(now=None):
        calls.append(clock.now)

    ticker, clock, event = _ticker(30, run)
    original_wait = event.wait

    def wait_and_stop(timeout=None):
        event.set()                     # SIGTERM lands mid-sleep
        return original_wait(timeout)

    event.wait = wait_and_stop
    ticker.run_forever()

    th.assert_eq(calls, [30.0],
                 "only the start tick may run; a stop that lands during the wait "
                 f"must not take fleet-once claims for a batch it will not finish: {calls}")


@th.django_unit_test()
def test_a_broken_cron_module_disables_the_tick_without_running_anything(opts):
    calls = []

    def load():
        raise ImportError("broken cronjobs module")

    ticker, clock, event = _ticker(30, lambda now=None: calls.append(now), load)
    ticker.run_forever()

    th.assert_eq(calls, [], "run_now must not be called when the modules did not load")


@th.django_unit_test()
def test_start_spawns_a_daemon_thread_and_stop_joins_it_promptly(opts):
    from mojo.apps.jobs.cron_tick import CronTicker

    ran = threading.Event()

    def run(now=None):
        ran.set()

    ticker = CronTicker(run=run, load=lambda: None)
    ticker.start()
    th.assert_true(ran.wait(5), "the start tick must fire as soon as the thread runs")
    th.assert_true(ticker.thread.daemon, "the tick thread must never keep the process alive")
    th.assert_eq(ticker.thread.name, "cron-tick", "the thread is named for ps/py-spy")
    ticker.start()                      # idempotent: no second thread
    first = ticker.thread

    started = time.monotonic()
    ticker.stop()
    elapsed = time.monotonic() - started
    th.assert_true(not first.is_alive(), "stop() must end the thread")
    th.assert_true(elapsed < 2, f"stop() must return promptly, took {elapsed:.1f}s")
    ticker.stop()                       # idempotent


@th.django_unit_test()
def test_cron_tick_enabled_reads_the_setting_as_a_bool(opts):
    from mojo.apps.jobs.cron_tick import cron_tick_enabled

    table = [
        (True, True), (False, False),
        ("true", True), ("false", False),
        ("1", True), ("0", False),
        ("yes", True), ("no", False), ("off", False),
        (1, True), (0, False),
    ]
    for raw, expected in table:
        th.assert_eq(cron_tick_enabled(raw), expected,
                     f"JOBS_SCHEDULER_CRON_TICK={raw!r} must read as {expected}")
    th.assert_eq(cron_tick_enabled("garbage"), True,
                 "an uncoercible value degrades to the declared default (on)")
    th.assert_eq(cron_tick_enabled(), True,
                 "the test project does not set it, so the live default is on")
