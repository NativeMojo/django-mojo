import socket
import time


def report_event(details, title=None, category="api_error", level=1, request=None, scope="global", **kwargs):
    from .models import Event
    event_data = _create_event_dict(details, title, category, level, request, scope, **kwargs)
    event = Event(**event_data)
    event.sync_metadata()
    event.save()
    event.publish()


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
                            fail_open=True, **kwargs):
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
    """
    from mojo.helpers import logit

    try:
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

    if group is not None:
        event_metadata["group_id"] = getattr(group, "id", None)
        event_metadata["group_name"] = getattr(group, "name", None)

    from mojo.helpers.logit import sanitize_dict

    processed_kwargs = {}
    for k, v in kwargs.items():
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
