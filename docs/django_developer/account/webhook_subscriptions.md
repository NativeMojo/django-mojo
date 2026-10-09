# Webhook Subscriptions — Django Developer Reference

Django-MOJO ships a generic `WebhookSubscription` model + asynchronous fan-out dispatcher so every downstream SaaS gets a per-Group subscription registry, event fan-out, and signed delivery without re-implementing the same ~150 lines of model + REST + dispatch logic.

> **Pairs with [Webhook Signing](webhook_signing.md).** Subscriptions are the *who, where, when*. Webhook Signing is *how the body is signed on its way out*. Both are framework-owned; downstream services own only their event vocabulary and their domain logic.

## How It Works

```
caller (request thread, sync)
   │
   └─ dispatch(group, event_type, data, ...)
         │
         └─ jobs.publish(handle_fanout, {...}, channel="webhook_fanout")
                                                            │
                                       (worker thread)─────┘
                                                            │
                                                            ▼
                                         handle_fanout(job):
                                            • load Group by id (missing → incident, 'failed', no retry)
                                            • SELECT subs WHERE group=g AND is_active AND events @> [event_type]
                                            • for each sub:
                                                 try jobs.publish_webhook(url=sub.url, data=data, group=g)
                                                 except → incident.report_event(category="webhook:fanout:error"); continue
                                            • record metadata: result, matched_count, published_count, failed_count
                                            • any receiver failed → one incident.report_event(category="webhook:fanout:incomplete")
                                            • return 'success' or 'incomplete' (informational; the job ends `completed`)
                                                            │
                                          (worker)──────────┘
                                                            ▼
                                       jobs.publish_webhook handler signs + sends per receiver
```

Three properties this gives you for free:

- **Signing** — every published webhook job carries `sign_group_id`; the existing webhook handler injects the signature header (`X-Mojo-Signature` by default, configurable via `WEBHOOK_SIGNATURE_HEADER`) at delivery (see [Webhook Signing](webhook_signing.md)).
- **Retries / backoff / dead-letter** — inherited from `publish_webhook` and the jobs system.
- **Skip-and-continue** — one flaky subscription cannot poison the fan-out. Per-row failures land in the incident app for follow-up.

## Model

```python
from mojo.apps.account.models import WebhookSubscription

WebhookSubscription(
    group=group_instance,              # FK to account.Group (CASCADE)
    url="https://hooks.example.com/x", # https only — http rejected at save time
    events=["verification.completed",
            "verification.failed"],    # free-form strings; framework has no opinion
    is_active=True,                    # toggle to pause without losing the URL
    metadata={},                       # JSONField for caller-owned tags / labels
)
```

Validation in `on_rest_pre_save`:

- `url` must start with `https://` and pass Django's `URLValidator`.
- `events` must be a list of non-empty strings (empty list is valid — "draft" state, matches no events).

The framework imposes **no event-name vocabulary**. Strings in, strings out. Each emitting SaaS documents its own event names in its own docs.

## Dispatching events

```python
from mojo.apps.account.services.webhooks import dispatch

def on_verification_complete(verification):
    dispatch(
        group=verification.group,
        event_type="verification.completed",
        data={
            "verification_id": verification.id,
            "customer_id": verification.customer_id,
            "status": "approved",
            "completed_at": verification.completed_at.isoformat(),
            "event_id": str(verification.uuid),  # for receiver-side dedupe
        },
        idempotency_key=f"verify_{verification.id}_completed",
    )
```

`dispatch()` runs in the caller's thread, queues exactly one fan-out job, and returns instantly with the fan-out job id (or `None` if `group is None`). The fan-out runs on the `webhook_fanout` channel; per-receiver delivery happens on the `webhooks` channel.

**Idempotency key suffixing**: if you pass `idempotency_key="x"`, each per-receiver job gets `idempotency_key="x_<sub_id>"`. This is what makes retries safe — the job layer dedupes per receiver, so a retried fan-out cannot deliver twice to the same subscriber. Subscription ids are unique across Groups, so two Groups never share a delivery key.

**Key length**: the job key column holds 64 characters. The suffix is an underscore plus the subscription id, which is a 64-bit number of up to 19 digits, so the suffix is at most 20 characters.

- A key of **44 characters or fewer** always gives the exact form `x_<sub_id>`, whatever the subscription id.
- When `x_<sub_id>` is longer than 64 characters, the delivery job is stored under the SHA-256 hex digest of that same text (64 characters). De-duplication works the same way: the same key and the same subscription always give the same digest. To find the delivery job for a long key, recompute it:

  ```python
  import hashlib
  hashlib.sha256(f"{idempotency_key}_{sub_id}".encode("utf-8")).hexdigest()
  # or: mojo.apps.account.services.webhooks.child_idempotency_key(idempotency_key, sub_id)
  ```

  The caller's own key is on the fan-out job's payload (`payload["idempotency_key"]`).
- A key **longer than 255 characters is refused**: `dispatch()` raises `ValueError` in the caller's thread, before anything is queued. The message names the limit and the length received, not the key.

A publication with **no** `idempotency_key` has no de-duplication: its delivery jobs carry no key, so running the same fan-out again sends again.

## Designing your event vocabulary

Pick names with a stable shape — your subscriptions will store these strings forever. Common conventions:

- `noun.past_tense_verb` — `verification.completed`, `customer.suspended`, `payment.refunded`.
- Lower-case dot-separated. Avoid underscores or camelCase.
- Version in the name if you anticipate schema churn: `verification.completed.v2`.

**Renaming an event is a multi-release flow**:

1. Release N: emit BOTH the old and new event names. Operators can subscribe to the new one.
2. Release N+1: update docs, encourage subscribers to switch.
3. Release N+M (after a deprecation window): drop the old name.

The framework does not enforce this — it's purely operational discipline. There is no registry to manage; just the strings on each subscription row.

## Channel configuration

The fan-out uses two job channels, `webhooks` and `webhook_fanout`. Both ship
in `JOBS_CHANNELS`' default list, so an unconfigured deployment already
consumes them. If you set `JOBS_CHANNELS` explicitly, include both — a job
published to either one stays there rather than falling back to `default`,
and a channel with no live consumer raises a `jobs:unconsumed_channel`
incident:

```python
# settings.py — only needed if you set JOBS_CHANNELS explicitly
JOBS_CHANNELS = ["default", "webhooks", "webhook_fanout", ...]
```

Separating `webhook_fanout` from `webhooks` keeps fan-out work (DB query, per-row enqueue) from competing with HTTP delivery slots when traffic spikes. See [Jobs — Channels](../jobs/publishing.md#channels).

## Error reporting

Per-row failures during fan-out are reported to the incident app, never to log files:

| Scenario | `incident.report_event` `category` | `level` |
|---|---|---|
| Group deleted between dispatch and fan-out | `webhook:fanout:group_missing` | 4 |
| `publish_webhook` raises for one subscription | `webhook:fanout:error` | 6 |
| At least one subscription could not be queued (one per publication) | `webhook:fanout:incomplete` | 6 |

The per-row and group-missing events carry `subscription_id` (where applicable), `group_id`, `event_type`, and `error_repr` so post-incident triage has the full context.

The `webhook:fanout:incomplete` event is the summary of one publication: the Group, `event_type`, `failed_count`, `matched_count`, `published_count` and `fanout_job_id`. It carries no payload and no receiver URL. The per-row events are still filed, one per failed subscription; they are what names the subscription.

### What a publication came to

The fan-out job ends `completed` whenever the handler returns, including when a receiver could not be queued — the job engine ignores the handler's return string, and the fan-out does not raise for a receiver failure, so it is never re-run for one. **An incomplete publication does not show among failed jobs.** The recorded result is in the fan-out job's `Job.metadata`:

| Key | Meaning |
|---|---|
| `result` | `success` — every matching subscription was queued (or none matched). `incomplete` — at least one could not be queued. `failed` — the Group no longer exists. |
| `matched_count` | Subscriptions that matched: `published_count + failed_count`. |
| `published_count` | Delivery jobs queued. The real total, not the sample size. |
| `failed_count` | Subscriptions whose delivery job could not be queued. |
| `published_job_ids` | A sample of at most 50 delivery job ids. |
| `published_job_ids_truncated` | `True` when the sample was cut short, that is when `published_count` is larger than the sample. |

Where an operator looks: the incident list under category `webhook:fanout:incomplete`, then the fan-out job it names (`fanout_job_id`) on the `webhook_fanout` channel for the counts, then the `webhook:fanout:error` events for the subscriptions that failed.

## REST endpoints

See [REST API → Webhook Subscriptions](../../web_developer/account/webhook_subscriptions.md) for the consumer-facing contract. Quick reference:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/group/webhook_subscriptions` | List the Group's subscriptions |
| `POST` | `/api/group/webhook_subscriptions` | Create |
| `GET` | `/api/group/webhook_subscriptions/<id>` | Detail |
| `POST` | `/api/group/webhook_subscriptions/<id>` | Update (RestMeta convention: POST with body updates) |
| `DELETE` | `/api/group/webhook_subscriptions/<id>` | Remove |

Permission: `manage_group` / `manage_groups` / `groups` — same threshold as `ApiKey` CRUD and `POST /api/group/webhook_secret`.

## Security notes

- **URL validation is at the syntactic level only**: `https://`-prefix, valid syntax, no embedded credentials (`user:pass@`). The framework does **not** restrict the target host. An operator with `manage_group` permission can register a URL pointing at `https://169.254.169.254/...` (AWS metadata), `https://10.0.0.1/...` (internal network), `https://localhost/...`, etc. — and the fan-out will dutifully deliver. **This is a deliberate trust model**: subscription writes require `manage_group` (same threshold as ApiKey CRUD), and `manage_group`-holders are considered trusted. If your deployment has a less-trusted operator tier and you need allow-list / deny-list enforcement of subscription URLs, layer that check in your own portal before POSTing to the framework endpoint, or open a follow-up request.
- **Per-row failure reports are bounded**: `error_repr` is truncated to 500 chars before being recorded in incident events. Inner exceptions from `requests` / HTTP libraries can embed response bodies and auth headers in their reprs; the cap bounds that exposure window.
- **Signing is automatic, not optional**: deliveries always go through `jobs.publish_webhook(group=...)` which always injects the signature header. There is no path through `dispatch()` that delivers unsigned.
- **Group hierarchy is not traversed**: `dispatch(group=g, ...)` only matches subscriptions whose `group_id == g.id`. Parent/child groups are not included.

## Out of scope (v1)

- **Per-subscription signing secrets** — the Group's webhook secret signs every delivery, no overrides.
- **Per-subscription retry policy overrides** — use the lower-level `jobs.publish_webhook` path if you need different `max_retries` / `backoff_*` for a specific receiver.
- **Delivery dashboards / per-subscription history UIs** — the `Job` model records every attempt with status, duration, and response; surfacing that is portal work, not framework work.
- **Typed event-payload schemas** — `data` is opaque JSON. Project owns the shape.

## Migration

A new `account.WebhookSubscription` model means a migration. After pulling this code:

```bash
./manage.py makemigrations
./manage.py migrate
```

(In the django-mojo repo itself, `bin/create_testproject` already regenerated the testproject migrations.)
