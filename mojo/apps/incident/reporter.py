import json
import socket
import time
import uuid


def record_event(details, title=None, category="api_error", level=1, request=None, scope="global", **kwargs):
    event_data = _create_event_dict(details, title, category, level, request, scope, **kwargs)
    return _save_event(event_data)


def report_event(details, title=None, category="api_error", level=1, request=None,
                 scope="global", defer=False, **kwargs):
    """File an incident Event and run the rules engine on it. Returns the Event.

    ``defer=True`` is the request thread saying the write may happen later
    (#6565): the request facts are captured NOW (``_create_event_dict`` reads
    only the request object) and parked in Redis, then the INSERT, the
    geolocation and the rule lookups run in a job on the ``incident_handlers``
    channel, and the call returns ``None``. It stays inline — today's path —
    when the category is in ``sync_categories()`` (the security list, which
    always lands before the response), when the jobs app is not installed, or
    when queueing fails. Callers pass it only for a routine 4xx; a 5xx never
    defers.
    """
    event_data = _create_event_dict(details, title, category, level, request, scope, **kwargs)
    if defer and _may_defer(category) and _queue_event(event_data):
        return None
    event = _save_event(event_data)
    event.publish()
    return event


# The job a deferred report_event publishes. Lives with the other incident
# jobs; it calls write_queued_event below.
QUEUED_EVENT_JOB = "mojo.apps.incident.asyncjobs.record_queued_event"

# How long a queued event waits for its job before it is dropped: a day, not
# the jobs default of 15 minutes, so a backed-up queue delays the row instead
# of losing it. Applies to the job row and to the Redis copy of the facts.
QUEUED_EVENT_TTL = 86400

# Security categories that never defer, whatever a caller asks. These rows are
# evidence the threat-intel tiers (mojo/helpers/geoip/threat_intel.py) and the
# auth-failure counters (Event.AUTH_FAILURE_CATEGORIES) count, so they must be
# in the table before the response goes out. INCIDENT_SYNC_CATEGORIES ADDS to
# this list; it cannot remove from it.
SYNC_CATEGORIES = frozenset({
    "sensitive_field_probe",
    "security:bouncer:honeypot_post",
    "security:bouncer:campaign",
    "invalid_password",
    "login:unknown",
    "reset:unknown",
    "magic:unknown",
    "token:unknown",
    "totp:login_unknown",
    "totp:login_failed",
    "sms:login_unknown",
    "passkey:login_failed",
    "invalid_token",
    "expired_token",
})


def sync_categories():
    """Categories that always write inline: SYNC_CATEGORIES + INCIDENT_SYNC_CATEGORIES.

    Read with get_static (the settings file) — this runs on every deferred
    4xx, and a per-request Redis round-trip is the cost #6565 removes.
    """
    from mojo.helpers.settings import settings
    configured = settings.get_static("INCIDENT_SYNC_CATEGORIES", None) or ()
    if isinstance(configured, str):
        configured = (configured,)
    return SYNC_CATEGORIES.union(configured)


def _may_defer(category):
    if category in sync_categories():
        return False
    from django.apps import apps
    return apps.is_installed("mojo.apps.jobs")


def queued_event_key():
    """A fresh Redis key for one queued event's captured facts."""
    return f"incident:queued:{uuid.uuid4().hex}"


def _queue_event(event_data, redis=None):
    """Hand the captured event to a job. True when queued, never raises.

    The facts go to Redis under a one-off key with a TTL; the job row carries
    only that key plus the category and uid. Job payloads are readable by
    anyone holding view_jobs, and incident metadata (emails, request bodies,
    stack traces) is view_security data — the queue gets a reference, never
    the data, the same rule publish_webhook follows for its secret.

    Any failure — facts not JSON, Redis down, the channel refused, the job
    row not written — returns False and the caller writes inline exactly as
    before. The Redis copy is deleted on a failed publish, so a job row jobs
    committed but could not confirm (its own documented edge) finds nothing
    if an operator requeues it: the inline write is the only one.

    ``redis`` is a keyword test seam; None resolves the shared connection.
    """
    key = None
    try:
        from mojo.apps import jobs
        data = dict(event_data)
        group = data.pop("group", None)
        data["group_id"] = getattr(group, "pk", None)
        raw = json.dumps(data)
        if redis is None:
            from mojo.helpers.redis import get_connection
            redis = get_connection()
        key = queued_event_key()
        redis.set(key, raw, ex=QUEUED_EVENT_TTL)
        jobs.publish(
            QUEUED_EVENT_JOB,
            {"key": key, "category": data.get("category"), "uid": data.get("uid")},
            channel="incident_handlers",
            expires_in=QUEUED_EVENT_TTL)
        return True
    except Exception as exc:
        from mojo.helpers import logit
        if key is not None:
            try:
                redis.delete(key)
            except Exception:
                pass
        logit.warning(
            "incident.report_event",
            f"could not queue {event_data.get('category')!r} event, "
            f"writing inline: {exc}")
        return False


def _claim_queued(key, redis=None):
    """Read and delete one queued event's facts atomically. None when gone.

    MULTI get+delete rather than GETDEL so pre-6.2 Redis works; either way a
    job that runs twice writes the event once.
    """
    if redis is None:
        from mojo.helpers.redis import get_connection
        redis = get_connection()
    pipe = redis.pipeline(transaction=True)
    pipe.get(key)
    pipe.delete(key)
    raw, _deleted = pipe.execute()
    return json.loads(raw) if raw else None


def write_queued_event(payload, redis=None):
    """The job half of a deferred report_event: save + rules, as inline does.

    ``payload`` is the job's ``{"key", "category", "uid"}``; the facts
    ``_create_event_dict`` captured on the request thread are read back from
    Redis. Missing facts (expired, or a second run of the same job) write
    nothing. A group deleted while the job waited is dropped to None — the
    metadata snapshot still names it.
    """
    key = payload.get("key")
    event_data = _claim_queued(key, redis=redis) if key else None
    if event_data is None:
        from mojo.helpers import logit
        logit.warning(
            "incident.write_queued_event",
            f"queued {payload.get('category')!r} event {key} has no stored "
            f"facts (expired or already written); nothing to write")
        return None
    from mojo.apps.account.models import Group
    group_id = event_data.pop("group_id", None)
    event_data["group"] = Group.objects.filter(pk=group_id).first() if group_id else None
    event = _save_event(event_data)
    event.publish()
    return event


def _save_event(event_data):
    from .models import Event
    event = Event(**event_data)
    event.sync_metadata()
    event.save()
    return event


# Set True the first time the Redis suppression path fails while fail_open is on,
# so the "filing without suppression" fallback warning is logged once per process
# instead of once per amplifiable request. Re-armed (set back to False) on the
# next Redis round-trip that answers, so a transient outage warns again.
_REDIS_WARNED = False


def notice_key(category, key):
    """Redis key for the once-per-window suppression flag of a (category, key) pair.

    Rolling TTL: written with ``ex=window`` and set only on the FIRST report of
    each window (``nx=True``), so a pair reported once stays suppressed for up to
    ``window`` seconds after that first report. This is the unit of "have I
    already told the operator about this exact thing recently".

    Exposed (with ``budget_key``) so a test can clear the precise keys it will
    exercise instead of flushing Redis.
    """
    return f"incident:notice:{category}:{key}"


def budget_key(category, window):
    """Redis key for a category's per-window event budget, in a fixed wall-clock bucket.

    Unlike ``notice_key``'s rolling TTL, the budget bucket is pinned to a
    wall-clock boundary (``floor(now/window)*window``) so every distinct key in a
    category shares ONE counter for the bucket — that is what lets a budget cap
    the number of *distinct* keys filed per window, not just repeats of one key.

    The TTL mismatch between the two keys is deliberate and fail-safe: a key that
    passes its own rolling notice check but is then rejected by the fixed-bucket
    budget stays notice-suppressed for up to ``window``. The worst case is FEWER
    events than a naive reading would predict, never more.
    """
    bucket = int(time.time()) // window * window
    return f"incident:budget:{category}:{bucket}"


def report_event_suppressed(details, key, title=None, category="api_error", level=1,
                            request=None, scope="global", window=3600, budget=None,
                            fail_open=True, *, connection=None, **kwargs):
    """File an incident Event at most once per ``(category, key)`` per ``window``. Returns bool.

    The reusable Redis-suppressed reporter. Reach for it — not bare
    ``report_event`` — anywhere a diagnostic is reachable from an
    attacker-amplifiable path (a public endpoint, a tenant-writable list), where
    a raw log line or a raw event is free amplification. It mirrors the
    hand-rolled suppression in ``account.services.redirect_allowlist`` and
    generalizes it.

    Returns ``True`` when an event was filed, ``False`` when it was
    suppressed (already reported this window), dropped (over budget), or the
    report itself failed. It **NEVER raises** — every failure mode is swallowed.

    Two independent limiters, both keyed in Redis:

      * ``notice_key(category, key)`` — the primary suppression. Set with
        ``nx=True, ex=window``; the ``nx`` makes "have I reported this pair this
        window" a single atomic round-trip (no read-then-write race between
        concurrent workers). A pair is filed at most once per ``window``.
      * ``budget_key(category, window)`` — an OPTIONAL ceiling (pass ``budget=``)
        on how many *distinct* keys a category may file in one fixed bucket, so a
        caller that mints unbounded distinct keys (many hosts, many groups)
        cannot turn per-key suppression into an unbounded event flood. When the
        budget is first exceeded a single "budget exhausted" event is filed
        (level 4) so the cap itself is visible; further keys are dropped silently.

    ``fail_open`` decides Redis-outage behavior. Fail-open (the default) files
    WITHOUT suppression and warns once per process — right for a low-rate,
    trusted-provenance diagnostic that must not be lost. Fail-CLOSED
    (``fail_open=False``) drops the event when Redis is unreachable — right for a
    public, attacker-amplifiable category, where a Redis outage must not become
    an open floodgate into the incident table.

    ``key`` is the suppression unit (a host, a source name, a group id).
    ``group=None`` in ``kwargs`` is honored by ``report_event`` to suppress the
    request-group auto-stamp. Extra ``kwargs`` land in the event metadata.

    ``connection`` is a keyword-only test seam (item #2558): a Redis-like
    object exposing ``set``/``incr``/``expire``. Default (None) resolves the
    shared process connection exactly as before. Because it is keyword-only it
    is consumed here and never reaches the event metadata.
    """
    from mojo.helpers import logit

    try:
        redis = connection
        if redis is None:
            from mojo.helpers.redis import get_connection
            redis = get_connection()
        nk = notice_key(category, key)
        # nx=True: atomic "claim this window" — set only if absent, so two
        # concurrent workers can never both pass. First real round-trip.
        fresh = redis.set(nk, "1", nx=True, ex=window)
        _note_redis_ok()
        if not fresh:
            return False
        if budget is not None:
            bk = budget_key(category, window)
            used = redis.incr(bk)
            if used == 1:
                redis.expire(bk, window)
            if used > budget:
                # File the "budget exhausted" marker exactly once (on the first
                # key past the cap), then drop this and every further key.
                if used == budget + 1:
                    _report_budget_exhausted(category, key, budget, window)
                return False
    except Exception as exc:
        if not fail_open:
            # Fail closed: a Redis outage must not let an anonymous caller flood
            # the incident DB on a public, amplifiable category. Drop it.
            return False
        _warn_redis_unavailable(category, exc)
        # fall through and report without suppression

    try:
        from mojo.apps import incident
        incident.report_event(details, title=title, category=category, level=level,
                              request=request, scope=scope, **kwargs)
        return True
    except Exception as exc:
        logit.error(
            "incident.report_event_suppressed",
            f"failed to file {category}: {exc}")
        return False


def _note_redis_ok():
    """Re-arm the fail-open warning after a Redis round-trip answers."""
    global _REDIS_WARNED
    _REDIS_WARNED = False


def _warn_redis_unavailable(category, exc):
    """Log the fail-open fallback once per process (until Redis recovers)."""
    global _REDIS_WARNED
    if _REDIS_WARNED:
        return
    _REDIS_WARNED = True
    from mojo.helpers import logit
    logit.warning(
        "incident.report_event_suppressed",
        f"redis suppression unavailable ({exc}); filing {category} WITHOUT "
        f"suppression until redis recovers")


def _report_budget_exhausted(category, key, budget, window):
    """File ONE event when a category first exhausts its per-window budget.

    Fired exactly once per category per bucket (the caller guards it with
    ``used == budget + 1``) so the operator learns the cap is now dropping rows
    without the flood that tripped it becoming a flood of its own. Calls
    ``report_event`` DIRECTLY — never ``report_event_suppressed`` — so it cannot
    recurse through the budget path. ``request=None`` and ``group=None`` keep it
    deployment-scoped and unstamped. Never raises.
    """
    from mojo.helpers import logit
    try:
        from mojo.apps import incident
        body = (
            f"Incident suppression budget exhausted for category {category!r}: "
            f"more than {budget} distinct keys filed in a {window}s window. "
            f"Further events in this window are being DROPPED (not filed) to "
            f"bound the incident table. Most recent dropped key: {key!r:.200}.")
        incident.report_event(
            body,
            title=f"Incident budget exhausted: {category}",
            category=category,
            level=4,
            request=None,
            group=None)
    except Exception as exc:
        logit.error(
            "incident.report_event_suppressed",
            f"failed to file budget-exhausted marker for {category}: {exc}")


def _resolve_event_group(kwargs, request):
    """
    Resolve the originating group for an event:

      1. Caller-supplied ``group=`` kwarg wins (including explicit
         ``None``, which suppresses the auto-stamp). Higher layers
         (MojoModel.report_incident / class_report_incident_for_user)
         pre-resolve instance.group / request.group and pass it as
         ``group=`` so the reporter respects their decision.
      2. If the caller did not pass ``group``, fall back to
         ``request.group`` so direct callers still get group context.

    Returns a Group instance or None. Pops the ``group`` kwarg from
    ``kwargs`` so it does not leak into ``processed_kwargs``. The
    ``isinstance`` guard rejects non-Group truthy values (e.g. a
    model that uses ``.group`` as a string).
    """
    from mojo.apps.account.models import Group

    if "group" in kwargs:
        candidate = kwargs.pop("group")
        return candidate if isinstance(candidate, Group) else None

    if request is not None:
        candidate = getattr(request, "group", None)
        if isinstance(candidate, Group):
            return candidate

    return None


def _create_event_dict(details, title=None, category="api_error", level=1, request=None, scope="global", **kwargs):
    if title is None:
        title = details[:50]

    group = _resolve_event_group(kwargs, request)

    event_data = {
        "details": details,
        "title": title,
        "scope": scope,
        "category": category,
        "level": level,
        "uid": kwargs.pop("uid", None),
        "hostname": kwargs.pop("hostname", None),
        "model_name": kwargs.pop("model_name", None),
        "model_id": kwargs.pop("model_id", None),
        "source_ip": kwargs.pop("source_ip", None),
        "group": group,
    }

    event_metadata = {
        "server": socket.gethostname()
    }

    if request:
        event_data["source_ip"] = request.ip if event_data["source_ip"] is None else event_data["source_ip"]
        event_metadata.update({
            "request_ip": request.ip,
            "http_path": request.path,
            "http_protocol": request.META.get("SERVER_PROTOCOL", ""),
            "http_method": request.method,
            "http_query_string": request.META.get("QUERY_STRING", ""),
            "http_user_agent": request.META.get("HTTP_USER_AGENT", ""),
            "http_host": request.META.get("HTTP_HOST", "")
        })
        if request.user.is_authenticated:
            event_data["uid"] = request.user.id
            if request.bearer:
                from mojo.helpers.logit import mask_token
                event_metadata["bearer"] = mask_token(request.bearer)
            event_metadata["user_name"] = request.user.display_name
            event_metadata["user_email"] = request.user.email
        # MOJO_SENSITIVE_BODY_PATHS: a host app's listed paths keep no body or
        # query string in any incident, event, ticket or LLM triage payload.
        from mojo.helpers.request import is_host_sensitive, HOST_SENSITIVE_MARKER
        if is_host_sensitive(request):
            if "request_data" in kwargs:
                kwargs["request_data"] = dict(HOST_SENSITIVE_MARKER)
            event_metadata["http_query_string"] = ""
            event_metadata.update(HOST_SENSITIVE_MARKER)

    if group is not None:
        event_metadata["group_id"] = getattr(group, "id", None)
        event_metadata["group_name"] = getattr(group, "name", None)

    from mojo.helpers.logit import sanitize_dict

    processed_kwargs = {}
    # Sanitize the complete mapping first. Sensitive scalar kwargs (for
    # example token="...") used to bypass the dict-only branch below and were
    # persisted verbatim in Event metadata.
    for k, v in sanitize_dict(kwargs).items():
        if k not in event_data:
            if isinstance(v, dict):
                processed_kwargs[k] = sanitize_dict(v)
            elif is_json_serializable(v):
                processed_kwargs[k] = v
            elif hasattr(v, 'id'):
                processed_kwargs[k] = v.id
            else:
                processed_kwargs[k] = str(v)

    event_metadata.update(processed_kwargs)
    event_data['metadata'] = event_metadata
    return event_data

def is_json_serializable(value):
    return isinstance(value, (str, int, float, bool, type(None), list, dict))
