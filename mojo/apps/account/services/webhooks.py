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
"""
from mojo.apps import jobs
from mojo.helpers import logit


FANOUT_CHANNEL = "webhook_fanout"
FANOUT_FUNC = "mojo.apps.account.services.webhooks.handle_fanout"

# How many job_ids to record in fan-out job metadata. Capped to keep the
# metadata blob bounded for Groups with many subscribers.
PUBLISHED_JOB_ID_CAP = 50


def dispatch(group, event_type, data, *, idempotency_key=None, channel="webhooks"):
    """Queue a fan-out job for `event_type` against `group`'s active subscriptions.

    Returns the fan-out job_id, or None if `group is None` (treated as a no-op
    so callers don't need to guard the call site).

    The fan-out itself happens asynchronously on the `webhook_fanout` channel —
    `handle_fanout` does the actual queryset + per-receiver publish loop.
    """
    if group is None:
        return None
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


def handle_fanout(job):
    """Worker handler: load the Group, query matching active subscriptions,
    publish one signed webhook job per row. Per-row failures are reported to
    the incident app and skipped. Returns 'success' or 'failed' (no retry on
    'failed' — group_missing is not recoverable).
    """
    from mojo.apps.account.models import Group, WebhookSubscription
    from mojo.apps import incident

    payload = job.payload
    group_id = payload.get("group_id")
    event_type = payload.get("event_type")
    data = payload.get("data")
    idempotency_key = payload.get("idempotency_key")
    channel = payload.get("channel", "webhooks")

    group = Group.objects.filter(pk=group_id).first()
    if group is None:
        incident.report_event(
            details=f"webhook fan-out skipped: group_id={group_id} not found (event_type={event_type})",
            category="webhook:fanout:group_missing",
            scope="account",
            level=4,
            group_id=group_id,
            event_type=event_type,
        )
        job.metadata["error_type"] = "webhook_fanout_group_missing"
        return "failed"

    # Postgres-native JSONField containment: pushes the "events contains
    # event_type" check into the DB so we don't iterate inactive rows in Python.
    rows = WebhookSubscription.objects.filter(
        group=group,
        is_active=True,
        events__contains=[event_type],
    )

    published_job_ids = []
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
                kwargs["idempotency_key"] = f"{idempotency_key}_{sub.id}"
            jid = jobs.publish_webhook(**kwargs)
            if len(published_job_ids) < PUBLISHED_JOB_ID_CAP:
                published_job_ids.append(jid)
        except Exception as e:
            failed_count += 1
            try:
                incident.report_event(
                    details=(
                        f"webhook fan-out failed to publish for subscription "
                        f"{sub.id} (group={group.id}, event_type={event_type}): {e!r}"
                    ),
                    category="webhook:fanout:error",
                    scope="account",
                    level=6,
                    group=group,
                    subscription_id=sub.id,
                    event_type=event_type,
                    error_repr=repr(e),
                )
            except Exception as ie:
                # Never let incident reporting crash the fan-out itself.
                logit.error(
                    f"incident.report_event failed inside webhook fan-out: {ie!r} (original error: {e!r})"
                )

    job.metadata["event_type"] = event_type
    job.metadata["group_id"] = group.id
    job.metadata["matched_count"] = len(published_job_ids) + failed_count
    job.metadata["published_count"] = len(published_job_ids)
    job.metadata["failed_count"] = failed_count
    job.metadata["published_job_ids"] = published_job_ids  # capped at PUBLISHED_JOB_ID_CAP
    return "success"
