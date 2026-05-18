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
                                            • record metadata: matched_count, published_count, failed_count
                                            • return 'success'
                                                            │
                                          (worker)──────────┘
                                                            ▼
                                       jobs.publish_webhook handler signs + sends per receiver
```

Three properties this gives you for free:

- **Signing** — every published webhook job carries `sign_group_id`; the existing webhook handler injects `X-Mojo-Signature` at delivery (see [Webhook Signing](webhook_signing.md)).
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

**Idempotency key suffixing**: if you pass `idempotency_key="x"`, each per-receiver job gets `idempotency_key="x_<sub_id>"`. This is what makes retries safe — the job layer dedupes per receiver, so a retried fan-out cannot deliver twice to the same subscriber.

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

The fan-out uses two job channels. Either add both to your project's `JOBS_CHANNELS` setting so dedicated workers can subscribe, or let them fall back to `"default"`:

```python
# settings.py
JOBS_CHANNELS = ["default", "webhooks", "webhook_fanout", ...]
```

Separating `webhook_fanout` from `webhooks` keeps fan-out work (DB query, per-row enqueue) from competing with HTTP delivery slots when traffic spikes.

## Error reporting

Per-row failures during fan-out are reported to the incident app, never to log files:

| Scenario | `incident.report_event` `category` | `level` |
|---|---|---|
| Group deleted between dispatch and fan-out | `webhook:fanout:group_missing` | 4 |
| `publish_webhook` raises for one subscription | `webhook:fanout:error` | 6 |

The incident events carry `subscription_id` (where applicable), `group_id`, `event_type`, and `error_repr` so post-incident triage has the full context.

The fan-out job itself records `matched_count`, `published_count`, `failed_count`, and a capped list of `published_job_ids` in its `Job.metadata` — useful for debugging "did this event fire for everyone it should have?".

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
- **Signing is automatic, not optional**: deliveries always go through `jobs.publish_webhook(group=...)` which always injects `X-Mojo-Signature`. There is no path through `dispatch()` that delivers unsigned.
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
