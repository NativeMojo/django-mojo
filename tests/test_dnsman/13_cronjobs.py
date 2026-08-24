"""dnsman cron functions are thin dispatchers, not workers.

A cron function runs synchronously on whatever process the cron matcher fires
on. `poll_pending()` makes a registrar round trip PER open purchase row and
takes a row lock to settle each one, so running it inline would occupy that
process for an unbounded time with no retry, timeout or job visibility.

Every other app in the repo (account, assistant, fileman, incident, jobs,
logit, shortlink) follows the publish-and-return shape. These tests pin dnsman
to it.
"""

TESTIT_TIER = "extended"

from unittest import mock

from objict import objict
from testit import helpers as th


@th.django_unit_test("poll_domain_operations publishes a job instead of sweeping inline")
def test_poll_publishes_rather_than_running_inline(opts):
    from mojo.apps.dnsman import cronjobs
    from mojo.apps.dnsman.services import registrar

    def exploding_poll():
        raise AssertionError(
            "poll_pending ran INLINE in the cron function — it must be "
            "published to the job engine, not executed on the cron thread")

    # Scoped capture, not a plain mock: test modules run as parallel threads
    # and a swallowing publish mock here eats other modules' real publishes.
    with th.capture_publishes(
            lambda c: str(c.get("func", "")).startswith("mojo.apps.dnsman.")
    ) as published, \
            mock.patch.object(registrar, "poll_pending", exploding_poll):
        result = cronjobs.poll_domain_operations()

    assert len(published) == 1, \
        f"expected exactly one published job, got {len(published)}"
    assert published[0]["func"] == "mojo.apps.dnsman.asyncjobs.poll_domain_operations", \
        f"published the wrong job func: {published[0]['func']}"
    assert result == "fake-job-1", \
        "the cron function should return the published job id"


@th.django_unit_test("the poll job handler runs the sweep and reports its counts")
def test_poll_handler_runs_the_sweep(opts):
    from objict import objict
    from mojo.apps.dnsman import asyncjobs
    from mojo.apps.dnsman.services import registrar

    calls = []

    def fake_poll():
        calls.append(True)
        return objict(completed=2, failed=1, adopted=0, expired=3, pending=0, errors=0)

    job = objict(payload={})
    with mock.patch.object(registrar, "poll_pending", fake_poll):
        result = asyncjobs.poll_domain_operations(job)

    assert len(calls) == 1, "the handler must run the sweep exactly once"
    assert "completed=2" in result and "failed=1" in result and "expired=3" in result, \
        f"the handler should report the sweep counts, got {result!r}"


@th.django_unit_test("the published poll job func actually resolves to a handler")
def test_poll_job_func_resolves(opts):
    import importlib
    from mojo.apps.dnsman import cronjobs

    module_path, _, func_name = cronjobs.POLL_JOB.rpartition(".")
    module = importlib.import_module(module_path)
    # A published func path that does not resolve fails only at RUN time, on a
    # runner, hours later -- so it is worth asserting here.
    assert hasattr(module, func_name), \
        f"{cronjobs.POLL_JOB} does not resolve to a handler"
    assert callable(getattr(module, func_name)), \
        f"{cronjobs.POLL_JOB} resolves to something that is not callable"


@th.django_unit_test("the ACME lease sweep cron publishes retryable work")
def test_acme_sweep_publishes_job(opts):
    from mojo.apps.dnsman import cronjobs

    published = []
    with mock.patch.object(
            cronjobs.jobs, "publish",
            lambda func=None, payload=None, **kwargs: published.append((func, payload, kwargs)) or "job-acme"):
        result = cronjobs.sweep_acme_hub_leases()

    assert [(row[0], row[1]) for row in published] == [
        (cronjobs.ACME_HUB_SWEEP_JOB, {})], \
        f"expected exactly the ACME sweep handler, got {published}"
    assert result == "job-acme", "cron dispatcher should return the published job id"
    # The sweep takes a BLOCKING advisory lock per allocation, so successive
    # ticks must not be able to stack behind a slow pass.
    options = published[0][2]
    assert options.get("idempotency_key"), \
        f"the ACME sweep tick must be deduplicated, got {options}"
    assert options.get("expires_in") == cronjobs.ACME_HUB_SWEEP_INTERVAL, \
        f"a superseded sweep pass must expire rather than queue, got {options}"


@th.django_unit_test("the ACME sweep files one operator incident when a pass hits errors")
def test_acme_sweep_reports_errors(opts):
    from mojo.apps.dnsman import asyncjobs

    reports = []
    asyncjobs._report_acme_hub_sweep_errors(
        objict(expired=0, reconciled=3, errors=2, unconfirmed=1),
        report=lambda details, **kwargs: reports.append((details, kwargs)))

    assert len(reports) == 1, f"one suppressed incident per failing pass, got {reports}"
    details, options = reports[0]
    assert "2 error" in details and "unconfirmed=1" in details, \
        f"the incident must carry the counts an operator acts on, got {details}"
    assert options.get("category") == "dnsman:acme_hub:sweep", \
        f"the incident needs a stable category to alert on, got {options}"
    assert options.get("window"), \
        "an unsuppressed per-pass incident would file every five minutes"

    # Bookkeeping must never turn a completed sweep into a failed job.
    def explode(details, **kwargs):
        raise RuntimeError("incident backend down")

    asyncjobs._report_acme_hub_sweep_errors(
        objict(expired=0, reconciled=0, errors=1), report=explode)


@th.django_unit_test("the ACME lease sweep job reports bounded counts")
def test_acme_sweep_handler(opts):
    from mojo.apps.dnsman import asyncjobs
    from mojo.apps.dnsman.services import acme_hub

    with mock.patch.object(
            acme_hub, "sweep", return_value=objict(expired=2, reconciled=1, errors=0)):
        result = asyncjobs.sweep_acme_hub_leases(objict(payload={}))
    assert result == "completed:expired=2,reconciled=1,errors=0", \
        f"handler should expose counts only, got {result}"
