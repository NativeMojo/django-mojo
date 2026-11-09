"""
The scheduler owns the cron ticker (maestro #9757).

Serial package: Scheduler.start() publishes fleet state and takes the
leadership lock against the real Redis. The ticker itself is a recording fake
passed through the constructor seam, so no cron function runs here.
"""
import inspect
import threading

from testit import helpers as th


class RecordingTicker:
    def __init__(self):
        self.events = []

    def start(self):
        self.events.append("start")

    def stop(self):
        self.events.append("stop")


@th.django_unit_test()
def test_scheduler_has_no_ticker_unless_asked(opts):
    from mojo.apps.jobs.scheduler import Scheduler

    th.assert_true(Scheduler(channels=["default"]).cron_ticker is None,
                   "a Scheduler built directly (tests, libraries) must not tick real cron")
    from mojo.apps.jobs.cron_tick import CronTicker
    ticking = Scheduler(channels=["default"], cron_tick=True)
    th.assert_true(isinstance(ticking.cron_ticker, CronTicker),
                   "cron_tick=True must give the scheduler a real ticker")


@th.django_unit_test()
def test_start_and_stop_own_the_ticker_and_work_off_the_main_thread(opts):
    from mojo.apps.jobs.scheduler import Scheduler

    ticker = RecordingTicker()
    scheduler = Scheduler(channels=["default"], cron_ticker=ticker,
                          scheduler_id="t9757-cron-tick")
    failure = []

    def run():
        try:
            scheduler.start()
        except BaseException as exc:    # noqa: BLE001 — surfaced below
            failure.append(exc)

    worker = threading.Thread(target=run, name="t9757-scheduler")
    worker.start()
    deadline = 10
    while deadline > 0 and ticker.events != ["start"] and not failure:
        threading.Event().wait(0.05)
        deadline -= 0.05
    th.assert_eq(failure, [],
                 f"start() off the main thread must not raise (signal handlers): {failure}")
    th.assert_eq(ticker.events, ["start"],
                 "start() must start the ticker before entering the lock loop")

    scheduler.stop()
    worker.join(10)
    th.assert_true(not worker.is_alive(), "stop() must end the scheduler loop")
    th.assert_eq(ticker.events, ["start", "stop"],
                 "stop() must stop the ticker exactly once")


@th.django_unit_test()
def test_cli_builds_the_scheduler_with_the_setting(opts):
    from mojo.apps.jobs import cli

    seen = {}

    def factory(**kwargs):
        seen.update(kwargs)
        return "built"

    built = cli.build_scheduler(["default"], scheduler_factory=factory)
    th.assert_eq(built, "built", "the factory's product is what the CLI runs")
    th.assert_eq(seen, {"channels": ["default"], "cron_tick": True},
                 "the CLI must pass cron_tick from JOBS_SCHEDULER_CRON_TICK (default on)")

    for entry in (cli.start_scheduler_foreground, cli.start_scheduler_daemon):
        source = inspect.getsource(entry)
        th.assert_in("build_scheduler(", source,
                     f"{entry.__name__} must construct its Scheduler through build_scheduler "
                     "— that is the path jobman's `scheduler foreground` takes on a node")
