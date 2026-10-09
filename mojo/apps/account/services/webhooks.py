"""Group-scoped webhook fan-out dispatcher.

The public API is `dispatch(group, event_type, data, ...)`. It runs in the
caller's thread, queues a single fan-out job, and returns instantly.

The fan-out (`handle_fanout`) runs in a worker on the `webhook_fanout` channel.
It queries the Group's active `WebhookSubscription` rows whose `events` list
contains `event_type`, then publishes one signed `jobs.publish_webhook(group=...)`
per match. Per-row failures are reported to the incident app and skipped —
one flaky row cannot poison the fan-out.

Signing, retries, backoff, dead-letter, and `X-Mojo-Signature` injection are
all inherited from the existing `publish_webhook(group=...)` path. This module
adds only storage + fan-out + per-receiver idempotency.

The fan-out job always ends `completed` unless the handler raises; the job
engine discards the handler's return string. What a publication came to is
recorded in the fan-out job's metadata (`result`, the counts) and, when a
receiver could not be queued, in one `webhook:fanout:incomplete` incident.
"""
import hashlib

from mojo.apps import jobs
from mojo.helpers import logit


FANOUT_CHANNEL = "webhook_fanout"
FANOUT_FUNC = "mojo.apps.account.services.webhooks.handle_fanout"

# How many job_ids to record in fan-out job metadata. Capped to keep the
# metadata blob bounded for Groups with many subscribers.
PUBLISHED_JOB_ID_CAP = 50

# Hard cap on exception repr length when reporting to the incident app.
# Exception __repr__ values can embed HTTP response bodies, auth headers, or
# other sensitive content from inner libraries; bound the surface area.
ERROR_REPR_MAX_LEN = 500

# Job.idempotency_key is varchar(64) and unique. A per-receiver key longer than
# this is stored as its SHA-256 hex digest, which is exactly this long.
JOB_KEY_MAX_LEN = 64

# Longest idempotency_key dispatch() accepts. A sanity bound, not a fit bound:
# with the digest any length fits the job key column.
IDEMPOTENCY_KEY_MAX_LEN = 255


def _safe_error_repr(err):
    """Bounded repr() of an exception for incident reporting. Truncates to
    ERROR_REPR_MAX_LEN with an explicit marker so triage can tell something
    was elided.
    """
    text = repr(err)
    if len(text) > ERROR_REPR_MAX_LEN:
        return text[: ERROR_REPR_MAX_LEN - 11] + "...truncated"
    return text


def child_idempotency_key(idempotency_key, subscription_id):
    """The job key for one receiver's delivery: `<idempotency_key>_<subscription_id>`.

    Returned unchanged when it fits the 64-character job key column, so a key
    that produced a delivery before still names the same row. When it is
    longer, the SHA-256 hex digest of that same text is returned instead
    (64 characters). A caller key of 44 characters or fewer always keeps the
    exact form: a subscription id is at most 19 digits.
    """
    combined = f"{idempotency_key}_{subscription_id}"
    if len(combined) <= JOB_KEY_MAX_LEN:
        return combined
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def dispatch(group, event_type, data, *, idempotency_key=None, channel="webhooks"):
    """Queue a fan-out job for `event_type` against `group`'s active subscriptions.

    Returns the fan-out job_id, or None if `group is None` (treated as a no-op
    so callers don't need to guard the call site).

    The fan-out itself happens asynchronously on the `webhook_fanout` channel —
    `handle_fanout` does the actual queryset + per-receiver publish loop.

    Raises ValueError, before anything is queued, when `idempotency_key` is
    longer than IDEMPOTENCY_KEY_MAX_LEN characters.
    """
    if group is None:
        return None
    if idempotency_key is not None and len(idempotency_key) > IDEMPOTENCY_KEY_MAX_LEN:
        raise ValueError(
            f"idempotency_key must be at most {IDEMPOTENCY_KEY_MAX_LEN} characters, "
            f"got {len(idempotency_key)}"
        )
    return jobs.publish(
        FANOUT_FUNC,
        {
            "group_id": group.id,
            "event_type": event_type,
            "data": data,
            "idempotency_key": idempotency_key,
            "channel": channel,
        },
        channel=FANOUT_CHANNEL,
    )


def handle_fanout(job, *, publisher=None, reporter=None):
    """Worker handler: load the Group, query matching active subscriptions,
    publish one signed webhook job per row. Per-row failures are reported to
    the incident app and skipped.

    Returns 'success', 'incomplete' (at least one receiver could not be
    queued) or 'failed' (the group is missing). The return string is
    informational: the job engine discards it and marks the job `completed`.
    The recorded result is `job.metadata["result"]`, with the counts beside
    it. The handler does not raise for a receiver failure, so the engine never
    re-runs the fan-out for one.

    `publisher` and `reporter` default to the production callables
    (jobs.publish_webhook and incident.report_event). They exist so a test
    can inject local fakes instead of patching the shared module attributes,
    which every parallel test thread observes.
    """
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps import incident

    if publisher is None:
        publisher = jobs.publish_webhook
    if reporter is None:
        reporter = incident.report_event

    payload = job.payload
    group_id = payload.get("group_id")
    event_type = payload.get("event_type")
    data = payload.get("data")
    idempotency_key = payload.get("idempotency_key")
    channel = payload.get("channel", "webhooks")

    group = Group.objects.filter(pk=group_id).first()
    if group is None:
        reporter(
            details=f"webhook fan-out skipped: group_id={group_id} not found (event_type={event_type})",
            category="webhook:fanout:group_missing",
            scope="account",
            level=4,
            group_id=group_id,
            event_type=event_type,
        )
        job.metadata["error_type"] = "webhook_fanout_group_missing"
        job.metadata["result"] = "failed"
        return "failed"

    # Postgres-native JSONField containment: pushes the "events contains
    # event_type" check into the DB so we don't iterate inactive rows in Python.
    rows = WebhookSubscription.objects.filter(
        group=group,
        is_active=True,
        events__contains=[event_type],
    )

    published_job_ids = []
    published_count = 0
    failed_count = 0
    for sub in rows:
        try:
            kwargs = dict(
                url=sub.url,
                data=data,
                group=group,
                channel=channel,
            )
            if idempotency_key:
                kwargs["idempotency_key"] = child_idempotency_key(idempotency_key, sub.id)
            jid = publisher(**kwargs)
            published_count += 1
            if len(published_job_ids) < PUBLISHED_JOB_ID_CAP:
                published_job_ids.append(jid)
        except Exception as e:
            failed_count += 1
            safe_repr = _safe_error_repr(e)
            try:
                reporter(
                    details=(
                        f"webhook fan-out failed to publish for subscription "
                        f"{sub.id} (group={group.id}, event_type={event_type}): {safe_repr}"
                    ),
                    category="webhook:fanout:error",
                    scope="account",
                    level=6,
                    group=group,
                    subscription_id=sub.id,
                    event_type=event_type,
                    error_repr=safe_repr,
                )
            except Exception as ie:
                # Never let incident reporting crash the fan-out itself.
                logit.error(
                    f"incident.report_event failed inside webhook fan-out: {ie!r} (original error: {e!r})"
                )

    matched_count = published_count + failed_count
    result = "incomplete" if failed_count else "success"
    job.metadata["event_type"] = event_type
    job.metadata["group_id"] = group.id
    job.metadata["matched_count"] = matched_count
    job.metadata["published_count"] = published_count
    job.metadata["failed_count"] = failed_count
    # A sample of at most PUBLISHED_JOB_ID_CAP ids; the counts above are the totals.
    job.metadata["published_job_ids"] = published_job_ids
    job.metadata["published_job_ids_truncated"] = published_count > len(published_job_ids)
    job.metadata["result"] = result

    if failed_count:
        # One summary per incomplete publication, beside the per-receiver
        # incidents above. No payload and no receiver address.
        try:
            reporter(
                details=(
                    f"webhook fan-out incomplete: {failed_count} of {matched_count} receivers "
                    f"could not be queued (group={group.id}, event_type={event_type}, "
                    f"published={published_count}, fanout_job={job.id})"
                ),
                category="webhook:fanout:incomplete",
                scope="account",
                level=6,
                group=group,
                event_type=event_type,
                failed_count=failed_count,
                matched_count=matched_count,
                published_count=published_count,
                fanout_job_id=job.id,
            )
        except Exception as ie:
            # Never let incident reporting crash the fan-out itself.
            logit.error(
                f"incident.report_event failed for the webhook fan-out summary: {ie!r}"
            )
    return result
