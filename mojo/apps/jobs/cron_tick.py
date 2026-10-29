"""
Resident cron tick for the jobs scheduler process.

Every deployed node used to run ``bin/cron.py --run`` from a per-minute
``/etc/cron.d`` entry, booting a full Django process (about 1.8 s of CPU and
117 MB) just to find out which ``@schedule`` functions were due. The scheduler
is already resident on every node, so it hosts the tick instead: a daemon
thread calls ``cron.run_now()`` at each wall-clock minute boundary.

The fleet-once claims, ``per_node`` and the heartbeat payload are
``run_now``'s own and are unchanged — this module only decides *when* to call
it. Every scheduler instance ticks (not just the leadership-lock holder), so
the claims keep exactly the meaning they had under crond.
"""
import datetime
import threading
import time

from django.db import close_old_connections

from mojo.helpers import logit
from mojo.helpers.settings import settings
from mojo.helpers.settings.helper import SettingsHelper

SETTING = "JOBS_SCHEDULER_CRON_TICK"

logger = logit.get_logger("scheduler", "scheduler.log")

# jobman's recycle sends TERM, waits 2 s, then KILLs — joining for longer
# than that buys nothing (mojo/apps/edge/asyncjobs.py).
STOP_JOIN_SECONDS = 5


def cron_tick_enabled(value=None):
    """
    Whether the scheduler should tick cron: ``JOBS_SCHEDULER_CRON_TICK``.

    ``kind="bool"`` is what parses ``"false"`` / ``"0"`` from a config file —
    a bare ``bool()`` would read the string ``"false"`` as on. ``value`` is a
    test seam for the truth table (the raw value as a settings file would hold
    it); production passes nothing and reads the live setting.
    """
    source = settings if value is None else SettingsHelper({SETTING: value})
    return source.get_static(SETTING, True, kind="bool")


def next_boundary(now):
    """The epoch second of the next wall-clock minute after ``now``."""
    return (int(now) // 60 + 1) * 60


class CronTicker:
    """
    Calls ``run`` once at start and then once per minute from a daemon thread.

    The seams (``run``, ``load``, ``clock``, ``stop_event``) exist because tests
    may not patch ``mojo.helpers.*`` or ``mojo.apps.jobs.*``; production passes
    none of them.
    """

    def __init__(self, run=None, load=None, clock=None, stop_event=None):
        if run is None or load is None:
            from mojo.helpers import cron
            run = run or cron.run_now
            load = load or cron.load_app_cron
        self.run = run
        self.load = load
        self.clock = clock or time.time
        self.stop_event = stop_event or threading.Event()
        self.thread = None
        self.ticks = 0
        self.failures = 0
        self.skipped_minutes = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        """Spawn the tick thread. Idempotent."""
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self.run_forever, name="cron-tick", daemon=True)
        self.thread.start()

    def stop(self, timeout=STOP_JOIN_SECONDS):
        """Ask the thread to stop and wait briefly for it. Idempotent."""
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread.is_alive() and \
                thread is not threading.current_thread():
            thread.join(timeout)

    # -- the loop ----------------------------------------------------------

    def run_forever(self):
        """
        Load the cron modules, tick once immediately, then tick on every
        minute boundary until stopped.

        The start tick is what makes a scheduler restart lossless: a process
        that comes up at hh:mm:03 covers minute hh:mm instead of waiting for
        hh:mm+1. For a minute the previous scheduler already ran, the
        fleet-once claims make it a no-op.
        """
        try:
            self.load()
        except Exception as exc:
            logger.error(f"cron tick: loading cron modules failed, not ticking: {exc}")
            return
        if self.stop_event.is_set():
            return
        self.tick()
        while not self.stop_event.is_set():
            boundary = next_boundary(self.clock())
            self.wait_until(boundary)
            if self.stop_event.is_set():
                # A stop that lands during the sleep never starts a batch, so
                # no fleet-once claim is taken for work this process will not
                # finish.
                return
            self.tick()
            overran = self.clock() - boundary
            if overran >= 60:
                self.skipped_minutes += 1
                logger.warning(
                    f"cron tick overran the minute by {overran:.0f}s; the next "
                    "scheduled minute is skipped, not replayed")

    def wait_until(self, boundary):
        """Sleep until ``boundary`` (epoch seconds) or until stopped."""
        delay = boundary - self.clock()
        if delay > 0:
            self.stop_event.wait(delay)

    def tick(self):
        """One ``run_now`` call. Errors are logged; the thread lives on."""
        self.ticks += 1
        try:
            # Cron functions such as dispatch_scheduled_tasks read the DB from
            # this thread; drop any connection that went stale while sleeping.
            close_old_connections()
            # The matcher works in local wall-clock time, as crond did.
            self.run(now=datetime.datetime.fromtimestamp(self.clock()))
        except Exception as exc:
            self.failures += 1
            logger.error(f"cron tick failed: {exc}")
