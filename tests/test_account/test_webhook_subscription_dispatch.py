"""Tests for the two-tier webhook fan-out: dispatch() queues a fan-out job,
handle_fanout() queries subscriptions and publishes per-receiver webhook jobs
with signing inherited from publish_webhook(group=...).

Failure paths inject local publisher/reporter fakes through handle_fanout's
dependency seams (maestro item #1839) — nothing patches the shared jobs or
incident modules, so parallel modules' publishes and events are untouched.
"""
from testit import helpers as th

TESTIT_TIER = "extended"


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

    # Inject the reporter through the seam — the real incident pipeline
    # (DB row + side-effects) is never entered, and no shared module is patched.
    result = handle_fanout(job, reporter=fake_report_event)

    assert result == "failed", f"missing group must produce 'failed', got {result!r}"
    assert job.metadata.get("error_type") == "webhook_fanout_group_missing", (
        f"error_type must be set, got {job.metadata.get('error_type')!r}"
    )
    assert job.metadata.get("result") == "failed", (
        f"the recorded result must be 'failed', got {job.metadata.get('result')!r}"
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

    result = handle_fanout(
        job, publisher=failing_publish_webhook, reporter=fake_report_event)

    assert result == "incomplete", (
        f"fan-out must continue past the failing row and report 'incomplete', got {result!r}"
    )
    assert job.metadata["published_count"] == 2, (
        f"the two OK rows must publish, got published_count={job.metadata.get('published_count')}"
    )
    assert job.metadata["failed_count"] == 1, (
        f"exactly the one failing row must be counted, got failed_count={job.metadata.get('failed_count')}"
    )
    assert len(incident_calls) == 2, (
        f"one incident for the failing row plus one summary must be reported, got {len(incident_calls)}"
    )
    _, summary_kwargs = incident_calls[1]
    assert summary_kwargs.get("category") == "webhook:fanout:incomplete", (
        f"the second incident must be the summary, got {summary_kwargs.get('category')!r}"
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


# ---------------------------------------------------------------------------
# Delivery key length, real counts and the recorded result (maestro #7270)
# ---------------------------------------------------------------------------

JOB_KEY_COLUMN_LEN = 64
MAX_SUBSCRIPTION_ID = 9223372036854775807  # BigAutoField upper bound, 19 digits


def _sha256_hex(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fanout_job(group_id, event_type, idempotency_key=None, data=None):
    return _StubJob(payload={
        "group_id": group_id,
        "event_type": event_type,
        "data": data if data is not None else {},
        "idempotency_key": idempotency_key,
        "channel": "webhooks",
    })


def _key_for_combined_length(sub_id, length, fill="k"):
    """A caller key whose combined '<key>_<sub_id>' text is exactly `length` long."""
    return fill * (length - 1 - len(str(sub_id)))


class _RecordingPublisher:
    """Local stand-in for jobs.publish_webhook: records each call's kwargs and
    returns a made-up job id. `fail_suffix` makes one receiver raise.
    """
    def __init__(self, fail_suffix=None):
        self.calls = []
        self.fail_suffix = fail_suffix

    def __call__(self, **kwargs):
        if self.fail_suffix and kwargs.get("url", "").endswith(self.fail_suffix):
            raise RuntimeError("forced failure for testing")
        self.calls.append(kwargs)
        return f"fake-job-{len(self.calls)}"


@th.django_unit_test()
def test_fanout_counts_are_real_totals_above_sample_cap(opts):
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps.account.services.webhooks import handle_fanout, PUBLISHED_JOB_ID_CAP

    g = Group.objects.get(pk=opts.group_id)
    WebhookSubscription.objects.filter(group=g).delete()
    total = PUBLISHED_JOB_ID_CAP + 10
    for n in range(total):
        _make_sub(g, f"/many-{n}", events=["evt.many"])

    publisher = _RecordingPublisher()
    job = _fanout_job(g.pk, "evt.many")
    handle_fanout(job, publisher=publisher)

    assert len(publisher.calls) == total, (
        f"every one of the {total} subscriptions must be published to, got {len(publisher.calls)}"
    )
    assert job.metadata["published_count"] == total, (
        f"published_count must be the real total {total}, not the sample size; "
        f"got {job.metadata.get('published_count')}"
    )
    assert job.metadata["matched_count"] == total, (
        f"matched_count must be the real total {total}, got {job.metadata.get('matched_count')}"
    )
    assert len(job.metadata["published_job_ids"]) == PUBLISHED_JOB_ID_CAP, (
        f"the id sample must hold {PUBLISHED_JOB_ID_CAP} ids, got {len(job.metadata['published_job_ids'])}"
    )
    assert job.metadata.get("published_job_ids_truncated") is True, (
        f"the sample must be flagged as cut short, got {job.metadata.get('published_job_ids_truncated')!r}"
    )

    WebhookSubscription.objects.filter(group=g).delete()


@th.django_unit_test()
def test_fanout_key_at_64_character_boundary(opts):
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps.account.services.webhooks import handle_fanout
    from mojo.apps.jobs.models import Job

    g = Group.objects.get(pk=opts.group_id)
    WebhookSubscription.objects.filter(group=g).delete()
    sub = _make_sub(g, "/boundary", events=["evt.boundary"])

    exact_key = _key_for_combined_length(sub.pk, JOB_KEY_COLUMN_LEN, fill="e")
    over_key = _key_for_combined_length(sub.pk, JOB_KEY_COLUMN_LEN + 1, fill="o")
    exact_combined = f"{exact_key}_{sub.pk}"
    over_combined = f"{over_key}_{sub.pk}"
    expected_over = _sha256_hex(over_combined)
    Job.objects.filter(idempotency_key__in=[exact_combined, expected_over]).delete()

    created_ids = []
    try:
        job = _fanout_job(g.pk, "evt.boundary", idempotency_key=exact_key)
        handle_fanout(job)
        created_ids += job.metadata["published_job_ids"]
        assert job.metadata["failed_count"] == 0 and job.metadata["published_count"] == 1, (
            f"a combined key of exactly {JOB_KEY_COLUMN_LEN} characters must publish, got {job.metadata}"
        )
        stored = Job.objects.get(id=job.metadata["published_job_ids"][0]).idempotency_key
        assert stored == exact_combined, (
            f"a combined key of exactly {JOB_KEY_COLUMN_LEN} characters must be stored as its "
            f"exact text, got {stored!r}"
        )

        job = _fanout_job(g.pk, "evt.boundary", idempotency_key=over_key)
        handle_fanout(job)
        created_ids += job.metadata["published_job_ids"]
        assert job.metadata["failed_count"] == 0 and job.metadata["published_count"] == 1, (
            f"a combined key of {JOB_KEY_COLUMN_LEN + 1} characters must still publish its "
            f"delivery, got {job.metadata}"
        )
        stored = Job.objects.get(id=job.metadata["published_job_ids"][0]).idempotency_key
        assert stored == expected_over, (
            f"a combined key one character too long must be stored as the SHA-256 hex digest "
            f"of the combined text, got {stored!r}"
        )
        assert len(stored) == JOB_KEY_COLUMN_LEN, (
            f"the stored digest must be {JOB_KEY_COLUMN_LEN} characters, got {len(stored)}"
        )
    finally:
        Job.objects.filter(id__in=created_ids).delete()
        WebhookSubscription.objects.filter(group=g).delete()


@th.django_unit_test()
def test_child_key_fits_at_maximum_subscription_id(opts):
    from mojo.apps.account.services.webhooks import child_idempotency_key

    key_44 = "a" * 44
    exact = child_idempotency_key(key_44, MAX_SUBSCRIPTION_ID)
    assert exact == f"{key_44}_{MAX_SUBSCRIPTION_ID}", (
        f"a 44-character key with the largest subscription id must keep its exact form, got {exact!r}"
    )
    assert len(exact) == JOB_KEY_COLUMN_LEN, (
        f"that exact form is {JOB_KEY_COLUMN_LEN} characters, got {len(exact)}"
    )
    for length in (45, 255):
        key = "b" * length
        child = child_idempotency_key(key, MAX_SUBSCRIPTION_ID)
        assert child == _sha256_hex(f"{key}_{MAX_SUBSCRIPTION_ID}"), (
            f"a {length}-character key with the largest subscription id must become the SHA-256 "
            f"hex digest of the combined text, got {child!r}"
        )
        assert len(child) == JOB_KEY_COLUMN_LEN, (
            f"the digest for a {length}-character key must be {JOB_KEY_COLUMN_LEN} characters, "
            f"got {len(child)}"
        )


@th.django_unit_test()
def test_dispatch_refuses_overlong_key_in_caller_thread(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.account.services.webhooks import dispatch, FANOUT_FUNC
    from mojo.apps.jobs.models import Job

    g = Group.objects.get(pk=opts.group_id)
    long_key = "refuse7270" + "x" * 246
    assert len(long_key) == 256, f"test key must be 256 characters, got {len(long_key)}"
    queued = Job.objects.filter(func=FANOUT_FUNC, payload__idempotency_key=long_key)
    queued.delete()

    raised = None
    try:
        dispatch(g, "evt.refuse", {"v": 1}, idempotency_key=long_key)
    except ValueError as err:
        raised = err
    job_exists = queued.exists()
    queued.delete()

    assert raised is not None, "dispatch must raise ValueError for a 256-character idempotency key"
    assert "255" in str(raised) and "256" in str(raised), (
        f"the error must name the limit and the length received, got {str(raised)!r}"
    )
    assert long_key not in str(raised), "the error must not repeat the caller's key"
    assert not job_exists, "no fan-out job may be queued for a refused key"

    job_id = dispatch(g, "evt.refuse", {"v": 1}, idempotency_key=long_key[:255])
    assert job_id, "a 255-character idempotency key must be accepted"
    Job.objects.filter(id=job_id).delete()


@th.django_unit_test()
def test_child_keys_distinct_per_receiver_and_group(opts):
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps.account.services.webhooks import handle_fanout

    a = Group.objects.get(pk=opts.group_id)
    b = Group.objects.get(pk=opts.other_group_id)
    WebhookSubscription.objects.filter(group__in=[a, b]).delete()
    _make_sub(a, "/a1", events=["evt.distinct"])
    _make_sub(a, "/a2", events=["evt.distinct"])
    _make_sub(b, "/b1", events=["evt.distinct"])

    long_key = "d" * 100
    publisher = _RecordingPublisher()
    handle_fanout(_fanout_job(a.pk, "evt.distinct", idempotency_key=long_key), publisher=publisher)
    handle_fanout(_fanout_job(b.pk, "evt.distinct", idempotency_key=long_key), publisher=publisher)

    keys = [call.get("idempotency_key") for call in publisher.calls]
    assert len(keys) == 3, f"three receivers must be published to, got {len(keys)}"
    assert len(set(keys)) == 3, (
        f"one caller key must give three different delivery keys across receivers and groups, got {keys!r}"
    )
    for key in keys:
        assert key and len(key) <= JOB_KEY_COLUMN_LEN, (
            f"every delivery key must fit the {JOB_KEY_COLUMN_LEN}-character job key column, "
            f"got {len(key or '')} characters"
        )

    WebhookSubscription.objects.filter(group__in=[a, b]).delete()


@th.django_unit_test()
def test_repeat_publication_deduplicates_per_receiver(opts):
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps.account.services.webhooks import handle_fanout
    from mojo.apps.jobs.models import Job

    g = Group.objects.get(pk=opts.group_id)
    WebhookSubscription.objects.filter(group=g).delete()
    s1 = _make_sub(g, "/rep-1", events=["evt.repeat"])
    s2 = _make_sub(g, "/rep-2", events=["evt.repeat"])

    created_ids = set()
    try:
        for label, caller_key in (("short", "repeat7270"), ("overlong", "repeat7270" + "r" * 90)):
            combined = [f"{caller_key}_{s.pk}" for s in (s1, s2)]
            stored_keys = [c if len(c) <= JOB_KEY_COLUMN_LEN else _sha256_hex(c) for c in combined]
            Job.objects.filter(idempotency_key__in=stored_keys).delete()

            first = _fanout_job(g.pk, "evt.repeat", idempotency_key=caller_key)
            handle_fanout(first)
            created_ids.update(first.metadata["published_job_ids"])
            second = _fanout_job(g.pk, "evt.repeat", idempotency_key=caller_key)
            handle_fanout(second)
            created_ids.update(second.metadata["published_job_ids"])

            assert first.metadata["failed_count"] == 0 and first.metadata["published_count"] == 2, (
                f"{label} key: the first publication must reach both receivers, got {first.metadata}"
            )
            assert second.metadata["failed_count"] == 0, (
                f"{label} key: the repeat must not fail, got {second.metadata}"
            )
            assert sorted(first.metadata["published_job_ids"]) == sorted(second.metadata["published_job_ids"]), (
                f"{label} key: the repeat must return the same delivery jobs, got "
                f"{first.metadata['published_job_ids']!r} then {second.metadata['published_job_ids']!r}"
            )
            for stored in stored_keys:
                count = Job.objects.filter(idempotency_key=stored).count()
                assert count == 1, (
                    f"{label} key: exactly one delivery row per receiver must exist, got {count}"
                )
    finally:
        Job.objects.filter(id__in=created_ids).delete()
        WebhookSubscription.objects.filter(group=g).delete()


@th.django_unit_test()
def test_fanout_incomplete_result_and_single_summary_incident(opts):
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps.account.services.webhooks import handle_fanout

    g = Group.objects.get(pk=opts.group_id)
    WebhookSubscription.objects.filter(group=g).delete()
    _make_sub(g, "/inc-ok-a", events=["evt.incomplete"])
    failing = _make_sub(g, "/inc-will-fail", events=["evt.incomplete"])
    _make_sub(g, "/inc-ok-b", events=["evt.incomplete"])

    incident_calls = []

    def fake_report_event(*args, **kwargs):
        incident_calls.append((args, kwargs))

    job = _fanout_job(g.pk, "evt.incomplete", data={"secret_marker": "payload-7270-do-not-report"})
    result = handle_fanout(
        job, publisher=_RecordingPublisher(fail_suffix="/inc-will-fail"), reporter=fake_report_event)

    assert result == "incomplete", f"a failed receiver must make the fan-out 'incomplete', got {result!r}"
    assert job.metadata.get("result") == "incomplete", (
        f"the recorded result must be 'incomplete', got {job.metadata.get('result')!r}"
    )
    assert job.metadata["failed_count"] == 1, f"one receiver failed, got {job.metadata.get('failed_count')}"
    assert job.metadata["published_count"] == 2, (
        f"two receivers were published, got {job.metadata.get('published_count')}"
    )
    assert job.metadata["matched_count"] == 3, (
        f"three receivers matched, got {job.metadata.get('matched_count')}"
    )

    per_row = [kw for _, kw in incident_calls if kw.get("category") == "webhook:fanout:error"]
    summary = [kw for _, kw in incident_calls if kw.get("category") == "webhook:fanout:incomplete"]
    assert len(per_row) == 1, f"the per-receiver incident must stay, one per failed receiver; got {len(per_row)}"
    assert per_row[0].get("subscription_id") == failing.pk, (
        f"the per-receiver incident must name subscription {failing.pk}, got {per_row[0].get('subscription_id')!r}"
    )
    assert len(summary) == 1, (
        f"exactly one summary incident per incomplete publication, got {len(summary)}"
    )
    assert len(incident_calls) == 2, f"no other incident is expected, got {len(incident_calls)}"

    kw = summary[0]
    assert kw.get("level") == 6, f"the summary incident must be level 6, got {kw.get('level')!r}"
    assert kw.get("event_type") == "evt.incomplete", (
        f"the summary must carry the event type, got {kw.get('event_type')!r}"
    )
    assert kw.get("group") == g, f"the summary must carry the group, got {kw.get('group')!r}"
    assert kw.get("failed_count") == 1, f"summary failed_count must be 1, got {kw.get('failed_count')!r}"
    assert kw.get("matched_count") == 3, f"summary matched_count must be 3, got {kw.get('matched_count')!r}"
    assert kw.get("published_count") == 2, (
        f"summary published_count must be 2, got {kw.get('published_count')!r}"
    )
    assert kw.get("fanout_job_id") == job.id, (
        f"the summary must carry the fan-out job id, got {kw.get('fanout_job_id')!r}"
    )
    summary_text = repr(kw)
    assert "payload-7270-do-not-report" not in summary_text, "the summary incident must not carry the payload"
    assert "dispatch.example.test" not in summary_text, (
        "the summary incident must not carry a receiver address"
    )

    WebhookSubscription.objects.filter(group=g).delete()


@th.django_unit_test()
def test_fanout_success_records_result(opts):
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps.account.services.webhooks import handle_fanout

    g = Group.objects.get(pk=opts.group_id)
    WebhookSubscription.objects.filter(group=g).delete()
    _make_sub(g, "/all-ok-a", events=["evt.allok"])
    _make_sub(g, "/all-ok-b", events=["evt.allok"])

    incident_calls = []

    def fake_report_event(*args, **kwargs):
        incident_calls.append((args, kwargs))

    job = _fanout_job(g.pk, "evt.allok")
    result = handle_fanout(job, publisher=_RecordingPublisher(), reporter=fake_report_event)

    assert result == "success", f"full success must return 'success', got {result!r}"
    assert job.metadata.get("result") == "success", (
        f"the recorded result must be 'success', got {job.metadata.get('result')!r}"
    )
    assert job.metadata.get("published_job_ids_truncated") is False, (
        f"a sample that holds every id must not be flagged as cut short, "
        f"got {job.metadata.get('published_job_ids_truncated')!r}"
    )
    assert incident_calls == [], f"a full success must raise no incident, got {incident_calls!r}"

    WebhookSubscription.objects.filter(group=g).delete()
