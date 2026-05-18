"""Tests for the two-tier webhook fan-out: dispatch() queues a fan-out job,
handle_fanout() queries subscriptions and publishes per-receiver webhook jobs
with signing inherited from publish_webhook(group=...).
"""
from unittest import mock
from testit import helpers as th


GROUP_NAME = "wsub_disp_group"
OTHER_GROUP_NAME = "wsub_disp_other"


@th.django_unit_setup()
def setup_dispatch(opts):
    from mojo.apps.account.models import Group, WebhookSubscription

    WebhookSubscription.objects.filter(url__contains="dispatch.example.test").delete()
    Group.objects.filter(name__in=[GROUP_NAME, OTHER_GROUP_NAME]).delete()
    g = Group.objects.create(name=GROUP_NAME, kind="organization")
    other = Group.objects.create(name=OTHER_GROUP_NAME, kind="organization")
    opts.group_id = g.pk
    opts.other_group_id = other.pk


def _make_sub(group, url_path, events, is_active=True):
    """Create a WebhookSubscription directly, bypassing REST. Tests own setup —
    no auth required.
    """
    from mojo.apps.account.models import WebhookSubscription
    sub = WebhookSubscription.objects.create(
        group=group,
        url=f"https://dispatch.example.test{url_path}",
        events=events,
        is_active=is_active,
    )
    return sub


# ---------------------------------------------------------------------------
# dispatch() — sync entry point, queues a fan-out job
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_dispatch_returns_none_for_none_group(opts):
    from mojo.apps.account.services.webhooks import dispatch

    job_id = dispatch(None, "evt.a", {"x": 1})
    assert job_id is None, (
        f"dispatch(None group) must return None (no-op for callers), got {job_id!r}"
    )


@th.django_unit_test()
def test_dispatch_queues_fanout_job_with_correct_payload(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.account.services.webhooks import dispatch, FANOUT_FUNC
    from mojo.apps.jobs.models import Job

    Job.objects.filter(func=FANOUT_FUNC).delete()
    g = Group.objects.get(pk=opts.group_id)

    job_id = dispatch(g, "evt.a", {"v": 1}, idempotency_key="key1")
    assert job_id, "dispatch must return a fan-out job id when group is set"

    job = Job.objects.get(id=job_id)
    assert job.func == FANOUT_FUNC, f"fan-out job must use the fan-out handler, got {job.func!r}"
    assert job.payload.get("group_id") == g.pk, "payload.group_id must be the group pk"
    assert job.payload.get("event_type") == "evt.a", "payload.event_type must round-trip"
    assert job.payload.get("data") == {"v": 1}, "payload.data must round-trip"
    assert job.payload.get("idempotency_key") == "key1", "payload.idempotency_key must round-trip"

    Job.objects.filter(id=job_id).delete()


# ---------------------------------------------------------------------------
# handle_fanout() — worker-side: query + per-receiver publish_webhook
# ---------------------------------------------------------------------------

class _StubJob:
    """Minimal Job stand-in for direct handler invocation."""
    def __init__(self, payload):
        self.payload = payload
        self.metadata = {}
        self.id = "stub-fanout-job"
        self.attempt = 1
        self.cancel_requested = False


@th.django_unit_test()
def test_fanout_publishes_one_signed_job_per_matching_subscription(opts):
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps.account.services.webhooks import handle_fanout
    from mojo.apps.jobs.models import Job

    Job.objects.filter(channel__in=["webhooks", "default"]).delete()
    g = Group.objects.get(pk=opts.group_id)
    WebhookSubscription.objects.filter(group=g).delete()

    a = _make_sub(g, "/a", events=["evt.a"])
    b = _make_sub(g, "/b", events=["evt.a", "evt.b"])
    c = _make_sub(g, "/c", events=["evt.b"])

    job = _StubJob(payload={
        "group_id": g.pk,
        "event_type": "evt.a",
        "data": {"hello": "world"},
        "idempotency_key": None,
        "channel": "webhooks",
    })
    result = handle_fanout(job)

    assert result == "success", f"handler must succeed, got {result!r}; metadata={job.metadata}"
    assert job.metadata["matched_count"] == 2, (
        f"exactly 2 subs match 'evt.a' (A and B), got matched_count={job.metadata.get('matched_count')}"
    )
    assert job.metadata["published_count"] == 2, (
        f"2 webhook jobs must be published, got {job.metadata.get('published_count')}"
    )
    assert job.metadata["failed_count"] == 0, (
        f"zero failures expected, got {job.metadata.get('failed_count')}"
    )

    # The two published jobs must point at A and B's URLs, never C.
    published_ids = job.metadata["published_job_ids"]
    published_urls = set(
        Job.objects.filter(id__in=published_ids).values_list("payload__url", flat=True)
    )
    assert published_urls == {a.url, b.url}, (
        f"published jobs must target A and B only, got {published_urls!r} (C={c.url!r})"
    )

    # Each published job must carry sign_group_id — signing is wired through.
    for jid in published_ids:
        pjob = Job.objects.get(id=jid)
        assert pjob.payload.get("sign_group_id") == g.pk, (
            f"per-receiver job {jid} must carry sign_group_id={g.pk}, got {pjob.payload.get('sign_group_id')!r}"
        )

    # Cleanup
    WebhookSubscription.objects.filter(group=g).delete()
    Job.objects.filter(id__in=published_ids).delete()


@th.django_unit_test()
def test_fanout_filters_inactive_rows(opts):
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps.account.services.webhooks import handle_fanout
    from mojo.apps.jobs.models import Job

    Job.objects.filter(channel__in=["webhooks", "default"]).delete()
    g = Group.objects.get(pk=opts.group_id)
    WebhookSubscription.objects.filter(group=g).delete()

    _make_sub(g, "/active", events=["evt.a"], is_active=True)
    _make_sub(g, "/inactive", events=["evt.a"], is_active=False)

    job = _StubJob(payload={
        "group_id": g.pk,
        "event_type": "evt.a",
        "data": {"x": 1},
        "idempotency_key": None,
        "channel": "webhooks",
    })
    handle_fanout(job)

    assert job.metadata["published_count"] == 1, (
        f"only the active sub should fire, got published_count={job.metadata.get('published_count')}"
    )

    WebhookSubscription.objects.filter(group=g).delete()


@th.django_unit_test()
def test_fanout_filters_using_events_contains_not_substring(opts):
    """Sanity check the Postgres `events__contains=[event_type]` semantics —
    array-element containment, not substring or prefix match.
    """
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps.account.services.webhooks import handle_fanout

    g = Group.objects.get(pk=opts.group_id)
    WebhookSubscription.objects.filter(group=g).delete()

    # A near-miss: "evt.x.subevent" must NOT match a fan-out for "evt.x".
    _make_sub(g, "/near", events=["evt.x.subevent"])
    # A real match.
    _make_sub(g, "/real", events=["evt.x"])

    job = _StubJob(payload={
        "group_id": g.pk,
        "event_type": "evt.x",
        "data": {},
        "idempotency_key": None,
        "channel": "webhooks",
    })
    handle_fanout(job)

    assert job.metadata["matched_count"] == 1, (
        f"only the exact 'evt.x' row must match, not 'evt.x.subevent'; "
        f"got matched_count={job.metadata.get('matched_count')}"
    )

    WebhookSubscription.objects.filter(group=g).delete()


@th.django_unit_test()
def test_fanout_idempotency_key_suffixed_per_subscription(opts):
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps.account.services.webhooks import handle_fanout
    from mojo.apps.jobs.models import Job

    Job.objects.filter(channel__in=["webhooks", "default"]).delete()
    g = Group.objects.get(pk=opts.group_id)
    WebhookSubscription.objects.filter(group=g).delete()
    s1 = _make_sub(g, "/s1", events=["evt.k"])
    s2 = _make_sub(g, "/s2", events=["evt.k"])

    job = _StubJob(payload={
        "group_id": g.pk,
        "event_type": "evt.k",
        "data": {"hello": 1},
        "idempotency_key": "abc",
        "channel": "webhooks",
    })
    handle_fanout(job)

    expected_keys = {f"abc_{s1.pk}", f"abc_{s2.pk}"}
    actual = set()
    for jid in job.metadata["published_job_ids"]:
        pjob = Job.objects.get(id=jid)
        # Job.idempotency_key is a field on the Job model.
        actual.add(pjob.idempotency_key)
    assert actual == expected_keys, (
        f"per-receiver idempotency keys must be 'abc_<sub_id>', got {actual!r} vs expected {expected_keys!r}"
    )

    WebhookSubscription.objects.filter(group=g).delete()
    Job.objects.filter(id__in=job.metadata["published_job_ids"]).delete()


@th.django_unit_test()
def test_fanout_zero_matches_returns_success(opts):
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps.account.services.webhooks import handle_fanout

    g = Group.objects.get(pk=opts.group_id)
    WebhookSubscription.objects.filter(group=g).delete()
    _make_sub(g, "/other", events=["evt.other"])

    job = _StubJob(payload={
        "group_id": g.pk,
        "event_type": "evt.does_not_match",
        "data": {},
        "idempotency_key": None,
        "channel": "webhooks",
    })
    result = handle_fanout(job)

    assert result == "success", f"zero-matches must still succeed, got {result!r}"
    assert job.metadata["published_count"] == 0, (
        f"published_count must be 0, got {job.metadata.get('published_count')}"
    )

    WebhookSubscription.objects.filter(group=g).delete()


@th.django_unit_test()
def test_fanout_missing_group_fails_no_retry_and_reports_incident(opts):
    """If the Group has been deleted between dispatch and fan-out execution,
    handle_fanout must return 'failed' (no retry) AND report to incident.
    """
    from mojo.apps.account.services import webhooks as webhook_service
    from mojo.apps.account.services.webhooks import handle_fanout

    job = _StubJob(payload={
        "group_id": 99_999_999,  # very unlikely to exist
        "event_type": "evt.x",
        "data": {},
        "idempotency_key": None,
        "channel": "webhooks",
    })

    incident_calls = []

    def fake_report_event(*args, **kwargs):
        incident_calls.append((args, kwargs))

    # Patch the incident module the service imports. Use mock to avoid touching
    # the real incident pipeline (which would write a DB row + side-effects).
    with mock.patch("mojo.apps.incident.report_event", side_effect=fake_report_event):
        result = handle_fanout(job)

    assert result == "failed", f"missing group must produce 'failed', got {result!r}"
    assert job.metadata.get("error_type") == "webhook_fanout_group_missing", (
        f"error_type must be set, got {job.metadata.get('error_type')!r}"
    )
    assert len(incident_calls) == 1, (
        f"exactly one incident must be reported, got {len(incident_calls)}"
    )
    _, kwargs = incident_calls[0]
    assert kwargs.get("category") == "webhook:fanout:group_missing", (
        f"incident category must be 'webhook:fanout:group_missing', got {kwargs.get('category')!r}"
    )


@th.django_unit_test()
def test_fanout_per_row_failure_reports_incident_and_continues(opts):
    """If publish_webhook raises for one subscription, the fan-out must report
    that failure to incident, then continue with the rest.
    """
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps.account.services.webhooks import handle_fanout
    from mojo.apps.jobs.models import Job

    Job.objects.filter(channel__in=["webhooks", "default"]).delete()
    g = Group.objects.get(pk=opts.group_id)
    WebhookSubscription.objects.filter(group=g).delete()
    s1 = _make_sub(g, "/ok-a", events=["evt.f"])
    s2 = _make_sub(g, "/will-fail", events=["evt.f"])
    s3 = _make_sub(g, "/ok-b", events=["evt.f"])

    incident_calls = []

    def fake_report_event(*args, **kwargs):
        incident_calls.append((args, kwargs))

    real_publish_webhook = None
    # Original publish_webhook reference for the OK rows
    from mojo.apps import jobs as jobs_module
    real_publish_webhook = jobs_module.publish_webhook

    def failing_publish_webhook(*args, **kwargs):
        if kwargs.get("url", "").endswith("/will-fail"):
            raise RuntimeError("forced failure for testing")
        return real_publish_webhook(*args, **kwargs)

    job = _StubJob(payload={
        "group_id": g.pk,
        "event_type": "evt.f",
        "data": {},
        "idempotency_key": None,
        "channel": "webhooks",
    })

    with mock.patch(
        "mojo.apps.account.services.webhooks.jobs.publish_webhook",
        side_effect=failing_publish_webhook,
    ), mock.patch("mojo.apps.incident.report_event", side_effect=fake_report_event):
        result = handle_fanout(job)

    assert result == "success", f"fan-out must succeed (skip-and-continue), got {result!r}"
    assert job.metadata["published_count"] == 2, (
        f"the two OK rows must publish, got published_count={job.metadata.get('published_count')}"
    )
    assert job.metadata["failed_count"] == 1, (
        f"exactly the one failing row must be counted, got failed_count={job.metadata.get('failed_count')}"
    )
    assert len(incident_calls) == 1, (
        f"exactly one incident must be reported for the failing row, got {len(incident_calls)}"
    )
    _, ic_kwargs = incident_calls[0]
    assert ic_kwargs.get("category") == "webhook:fanout:error", (
        f"per-row incident category must be 'webhook:fanout:error', got {ic_kwargs.get('category')!r}"
    )
    assert ic_kwargs.get("subscription_id") == s2.pk, (
        f"incident must carry the failing subscription_id={s2.pk}, got {ic_kwargs.get('subscription_id')!r}"
    )

    WebhookSubscription.objects.filter(group=g).delete()
    Job.objects.filter(id__in=job.metadata["published_job_ids"]).delete()


@th.django_unit_test()
def test_fanout_does_not_publish_for_other_groups(opts):
    """Subscriptions on a different Group must be untouched by this Group's fan-out."""
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps.account.services.webhooks import handle_fanout

    a = Group.objects.get(pk=opts.group_id)
    b = Group.objects.get(pk=opts.other_group_id)
    WebhookSubscription.objects.filter(group__in=[a, b]).delete()
    _make_sub(a, "/a", events=["evt.iso"])
    _make_sub(b, "/b", events=["evt.iso"])

    job = _StubJob(payload={
        "group_id": a.pk,
        "event_type": "evt.iso",
        "data": {},
        "idempotency_key": None,
        "channel": "webhooks",
    })
    handle_fanout(job)

    assert job.metadata["matched_count"] == 1, (
        f"only Group A's subscription must match, got matched_count={job.metadata.get('matched_count')}"
    )

    WebhookSubscription.objects.filter(group__in=[a, b]).delete()
