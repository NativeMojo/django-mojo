"""A routine 4xx queues its incident Event; a 5xx still writes inline (#6565).

The REST dispatcher used to INSERT the Event, geolocate the caller and look up
rules before a refused or malformed request got its response. A 4xx now
captures the request facts on the request thread, parks them in Redis and
queues a job on `incident_handlers` that writes the row exactly as the inline
path did. The job row carries only a key, the category and the uid — job
payloads are readable with view_jobs, incident metadata is view_security data.

Every row here is scoped to a per-run user or a per-run UUID category, so the
package stays parallel-safe (see __init__.py).
"""
import uuid

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

PASSWORD = "Defer##6565"
DENIED = "user_permission_denied"


def _queued_jobs(**payload):
    from mojo.apps.jobs.models import Job
    from mojo.apps.incident.reporter import QUEUED_EVENT_JOB
    qs = Job.objects.filter(func=QUEUED_EVENT_JOB, channel="incident_handlers")
    for key, value in payload.items():
        qs = qs.filter(**{f"payload__{key}": value})
    return list(qs)


def _drain(**payload):
    from mojo.apps.incident.reporter import QUEUED_EVENT_JOB
    return th.run_pending_jobs(
        channel="incident_handlers", func=QUEUED_EVENT_JOB, payload=payload)


def _anonymous_request(path):
    import objict
    req = objict.objict()
    req.user = objict.objict(is_authenticated=False, id=None)
    req.DATA = objict.objict()
    req.QUERY_PARAMS = objict.objict()
    req.method = "POST"
    req.group = None
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = path
    req.META = {"QUERY_STRING": ""}
    req.api_key = None
    req._host_sensitive = False
    return req


@th.django_unit_setup()
def setup_deferred_events(opts):
    from mojo.apps.account.models import User
    username = f"defer_{uuid.uuid4().hex[:10]}@defer.test"
    User.objects.filter(username=username).delete()
    user = User.objects.create_user(username=username, email=username, password=PASSWORD)
    user.is_email_verified = True
    user.requires_mfa = False
    user.remove_all_permissions()
    user.save()
    opts.username = username
    opts.user_id = user.pk


@th.django_unit_test("a view 403 writes no Event on the request thread; the queued job writes it")
def test_view_403_queues_event_and_job_writes_it(opts):
    from mojo.apps.incident.models import Event
    from mojo.apps.jobs.models import Job

    Event.objects.filter(uid=opts.user_id).delete()
    Job.objects.filter(pk__in=[j.pk for j in _queued_jobs(uid=opts.user_id)]).delete()
    try:
        assert_true(opts.client.login(opts.username, PASSWORD), "login failed for the no-perm user")
        resp = opts.client.post(
            "/api/incident/ticket", json={"title": "defer-6565", "category": "defer_6565"})
        assert_eq(resp.status_code, 403,
                  f"a no-perm ticket create must be refused, got {resp.status_code}: {resp.response!r}")

        # The response is back. Inline, the row would already be committed.
        written = Event.objects.filter(uid=opts.user_id, category=DENIED).count()
        assert_eq(written, 0,
                  f"a routine 403 must not INSERT its Event on the request thread, found {written}")

        queued = _queued_jobs(uid=opts.user_id)
        assert_eq(len(queued), 1, f"the 403 must queue exactly one event job, got {len(queued)}")
        payload = queued[0].payload
        assert_eq(sorted(payload), ["category", "key", "uid"],
                  f"the job row must carry a reference only — no request facts — got {payload!r}")
        assert_eq(payload["category"], DENIED,
                  f"the queued category must be the dispatcher's, got {payload['category']!r}")

        assert_eq(_drain(uid=opts.user_id), 1, "the queued event job must run once")
        events = list(Event.objects.filter(uid=opts.user_id, category=DENIED))
        assert_eq(len(events), 1, f"the job must write exactly one {DENIED} Event, got {len(events)}")
        event = events[0]
        meta = event.metadata
        assert_eq(event.category, DENIED, f"category must round-trip, got {event.category!r}")
        assert_eq(event.level, 4, f"level must be the dispatcher's 4, got {event.level}")
        assert_eq(event.source_ip, meta.get("request_ip"),
                  f"source_ip must be the captured request ip, got {event.source_ip!r} vs {meta.get('request_ip')!r}")
        assert_true(event.source_ip, "the captured request ip must be recorded")
        assert_eq(meta.get("http_path"), "/api/incident/ticket",
                  f"the request path captured on the request thread must land, got {meta.get('http_path')!r}")
        assert_eq(meta.get("http_method"), "POST", f"method must land, got {meta.get('http_method')!r}")
        assert_eq(meta.get("model_name"), "Ticket", f"model_name must land, got {meta.get('model_name')!r}")
        assert_eq(meta.get("branch"), "user.has_permission",
                  f"the denial branch must land, got {meta.get('branch')!r}")
        assert_eq(meta.get("user_email"), opts.username,
                  f"the acting user captured at request time must land, got {meta.get('user_email')!r}")

        # A second run of the same job (operator requeue, double delivery)
        # finds its facts already claimed and writes nothing.
        reruns = th.run_pending_jobs(
            channel="incident_handlers", status="completed",
            func=queued[0].func, payload={"key": payload["key"]})
        assert_eq(reruns, 1, "the completed job must be re-runnable for this check")
        count = Event.objects.filter(uid=opts.user_id, category=DENIED).count()
        assert_eq(count, 1, f"a re-run of the same job must not write a second Event, got {count}")
    finally:
        Event.objects.filter(uid=opts.user_id).delete()
        Job.objects.filter(pk__in=[j.pk for j in _queued_jobs(uid=opts.user_id)]).delete()


@th.django_unit_test("report_event(defer=True) queues; the job writes the same fields")
def test_report_event_defer_round_trips_fields(opts):
    from mojo.apps.incident import report_event
    from mojo.apps.incident.models import Event

    category = f"testit:defer:{uuid.uuid4().hex}"
    try:
        result = report_event("deferred contract", title="deferred title", category=category,
                              level=3, scope="testit", defer=True, note="kept")
        assert_true(result is None, f"a deferred report returns None, got {result!r}")
        assert_eq(Event.objects.filter(category=category).count(), 0,
                  "a deferred report must not write the row itself")
        assert_eq(_drain(category=category), 1, "exactly one queued job for this category")
        event = Event.objects.filter(category=category).first()
        assert_true(event is not None, "the job must write the Event")
        assert_eq((event.details, event.title, event.level, event.scope),
                  ("deferred contract", "deferred title", 3, "testit"),
                  f"fields must round-trip, got {(event.details, event.title, event.level, event.scope)!r}")
        assert_eq(event.metadata.get("note"), "kept",
                  f"extra kwargs must land in metadata, got {event.metadata!r}")
    finally:
        Event.objects.filter(category=category).delete()


@th.django_unit_test("a failed publish falls back to the inline write")
def test_publish_failure_writes_inline(opts):
    from mojo.apps.incident import report_event
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.reporter import QUEUED_EVENT_JOB

    category = f"testit:defer_fail:{uuid.uuid4().hex}"

    def mine(call):
        return call.get("func") == QUEUED_EVENT_JOB and (call.get("payload") or {}).get("category") == category

    try:
        with th.capture_publishes(mine, side_effect=RuntimeError("queue down")) as calls:
            event = report_event("fallback contract", category=category, level=3, defer=True)
        assert_eq(len(calls), 1, f"the deferral must have tried to publish once, got {len(calls)}")
        assert_true(event is not None and event.pk,
                    "when the publish fails report_event must write inline and return the Event")
        assert_eq(Event.objects.filter(category=category).count(), 1,
                  "the inline fallback must write exactly one row")
        assert_eq(len(_queued_jobs(category=category)), 0,
                  "a failed publish must leave no queued job behind")
    finally:
        Event.objects.filter(category=category).delete()


@th.django_unit_test("a 500 from a view still writes its Event inline")
def test_5xx_still_writes_inline(opts):
    import mojo.errors as merrors
    from mojo.apps.incident.models import Event
    from mojo.decorators import http as http_decorators

    path = f"/api/testit_defer/{uuid.uuid4().hex[:10]}/server_error"

    def failing_view(request):
        raise merrors.MojoException("boom", 500, 500)

    try:
        http_decorators.dispatch_error_handler(failing_view)(_anonymous_request(path))
        rows = list(Event.objects.filter(metadata__http_path=path).values_list("category", flat=True))
        assert_eq(rows, ["mojo_rest_error"],
                  f"a 5xx must write its Event before the response, got {rows}")
    finally:
        Event.objects.filter(metadata__http_path=path).delete()


@th.django_unit_test("security categories never defer")
def test_security_categories_stay_sync(opts):
    from mojo.apps.incident import reporter

    for category in ("invalid_password", "login:unknown", "sensitive_field_probe",
                     "security:bouncer:honeypot_post", "invalid_token"):
        assert_true(category in reporter.sync_categories(),
                    f"{category} is security evidence and must be in the sync set")
        assert_true(not reporter._may_defer(category), f"{category} must never defer")
    assert_true(reporter._may_defer("user_permission_denied"),
                "a routine permission denial may defer when the jobs app is installed")
