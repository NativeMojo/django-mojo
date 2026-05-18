# Feature: Group-Scoped Webhook Subscriptions

**Type**: feature
**Status**: resolved
**Date**: 2026-05-17
**Requested by**: downstream consumer integration

## Background

Django-MOJO already ships the **emit-side signing primitive**: `jobs.publish_webhook(url, data, group=g)` signs at delivery time with the Group's secret. What's missing is the **subscription registry** — the persisted state that answers "given this Group fired event X, where do I POST it?"

Every SaaS built on django-mojo will reimplement the same three pieces of state to bridge that gap:

1. A model that stores `(group, url, events[], is_active)` per subscriber.
2. CRUD REST endpoints to manage subscriptions.
3. A dispatch loop that, given a Group + event_type + payload, fans out via `publish_webhook(group=g, url=row.url, data=payload)` for each matching active row.

These have zero project-specific shape. Putting them in the framework lets every downstream service drop ~150 lines of model + REST + service code and inherit retries, signing, fan-out, and event filtering for free.

## Proposal

Add a generic `WebhookSubscription` model + dispatch helper + event-type registry to `mojo.apps.account`. The framework owns the storage, the CRUD, and the fan-out loop. Projects declare their event vocabulary at startup and write thin convenience dispatchers for their domain events.

### Model — `WebhookSubscription`

`mojo/apps/account/models/webhook_subscription.py` (new):

```python
class WebhookSubscription(MojoModel, models.Model):
    group       = ForeignKey("account.Group", related_name="webhook_subscriptions", on_delete=CASCADE)
    url         = URLField()                       # https only at validation time
    events      = JSONField(default=list)          # list[str], validated against the registry
    is_active   = BooleanField(default=True, db_index=True)
    metadata    = JSONField(default=dict, blank=True)
    created     = DateTimeField(auto_now_add=True)
    modified    = DateTimeField(auto_now=True)
```

No per-subscription secret. Signing uses the Group's existing `webhook_secret` via `publish_webhook(group=group, ...)`. One Group = one secret = N subscriptions; rotation rolls every subscription at once.

`RestMeta`:
- `VIEW_PERMS = SAVE_PERMS = DELETE_PERMS = ["manage_group", "manage_groups", "groups"]` (mirror `ApiKey`)
- `CAN_DELETE = True`
- `GRAPHS["default"]` includes everything but `metadata`; `GRAPHS["detail"]` adds `metadata`.
- `on_rest_save` validates each entry in `events` against the registered set; rejects unknown event names with a clear error (`"unknown event 'foo.bar' — known: [...]"`).

### Event Registry

Projects declare their vocabulary at app load via a single call:

`mojo/apps/account/webhooks.py` (new):

```python
_REGISTRY: dict[str, set[str]] = {}   # namespace -> {event names}

def register_events(namespace: str, events: list[str]) -> None:
    """Register webhook event names for a project namespace. Idempotent."""
    _REGISTRY.setdefault(namespace, set()).update(events)

def all_events() -> list[dict]:
    """Return [{"namespace": "verify", "event": "verification.completed"}, ...]
    sorted by (namespace, event). Used by the REST events endpoint and by
    WebhookSubscription validation."""

def is_registered(event: str) -> bool:
    """O(1) check that an event name has been registered by some namespace."""
```

Project usage in `apps.py`:

```python
class VerifyConfig(AppConfig):
    def ready(self):
        from mojo.apps.account.webhooks import register_events
        register_events("verify", [
            "verification.completed",
            "verification.failed",
            "kyc.status_changed",
            "customer.suspended",
            "profile.updated",
            "terms.published",
        ])
```

Validation happens at subscription-save time, not at dispatch time — operators get immediate feedback on a typo, and dispatch stays fast.

### Dispatch helper

`mojo/apps/account/services/webhooks.py` (new):

```python
def dispatch(group, event_type, data, *, idempotency_key=None, channel="webhooks"):
    """Fan a single event out to all active subscriptions for the Group.

    Filters WebhookSubscription rows by (group, is_active, event_type in events)
    and publishes one signed job per match via jobs.publish_webhook(group=group).
    Per-subscription idempotency_key is suffixed with the subscription id so
    the same logical event publishes once per receiver.

    Returns the list of published job_ids (empty if no subscribers match).
    """
```

Implementation outline:

```python
from mojo.apps import jobs
from mojo.apps.account.models import WebhookSubscription

def dispatch(group, event_type, data, *, idempotency_key=None, channel="webhooks"):
    if group is None:
        return []
    rows = WebhookSubscription.objects.filter(group=group, is_active=True)
    job_ids = []
    for sub in rows:
        if event_type not in (sub.events or []):
            continue
        kwargs = dict(
            url=sub.url,
            data=data,
            group=group,
            channel=channel,
        )
        if idempotency_key:
            kwargs["idempotency_key"] = f"{idempotency_key}_{sub.id}"
        job_ids.append(jobs.publish_webhook(**kwargs))
    return job_ids
```

Three properties this gives projects for free:
- **Signing** — every published job carries `sign_group_id`; handler injects `X-Mojo-Signature` at delivery.
- **Retries** — `publish_webhook` already inherits jobs' retry/backoff/dead-letter.
- **Idempotency** — caller passes one logical id; framework makes it unique per receiver so retry semantics work at the job layer.

### REST endpoints

`mojo/apps/account/rest/webhook_subscription.py` (new):

```python
@md.URL("group/webhook_subscriptions")
@md.URL("group/webhook_subscriptions/<int:pk>")
@md.uses_model_security(WebhookSubscription)
def on_webhook_subscriptions(request, pk=None):
    return WebhookSubscription.on_rest_request(request, pk)


@md.GET("group/webhook_subscriptions/events")
@md.requires_perms("manage_group", "manage_groups", "groups")
def on_webhook_subscription_events(request):
    """List all registered event types so portal UIs can render checkboxes."""
    from mojo.apps.account.webhooks import all_events
    return {"status": True, "data": all_events()}
```

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/group/webhook_subscriptions` | List the Group's subscriptions |
| `POST` | `/api/group/webhook_subscriptions` | Create a subscription |
| `GET` | `/api/group/webhook_subscriptions/<id>` | Detail |
| `PUT` | `/api/group/webhook_subscriptions/<id>` | Update url / events / is_active / metadata |
| `DELETE` | `/api/group/webhook_subscriptions/<id>` | Remove |
| `GET` | `/api/group/webhook_subscriptions/events` | List all registered event types |

Permissions: `manage_group`/`manage_groups`/`groups` — same threshold as ApiKey and the existing `POST /api/group/webhook_secret`. If you can mint API keys for the Group, you can manage its webhook subscriptions.

Group resolution follows the standard pattern: API-key auth sets `request.group` from the key; user auth uses the `group` parameter resolved by `@md.requires_perms`.

## Acceptance Criteria

### Framework

- [ ] `mojo/apps/account/models/webhook_subscription.py` — `WebhookSubscription` model as above, with `RestMeta` and event-name validation in `on_rest_save`.
- [ ] `mojo/apps/account/models/__init__.py` — export `WebhookSubscription`.
- [ ] `mojo/apps/account/webhooks.py` — `register_events`, `all_events`, `is_registered`. Module-level dict, no DB.
- [ ] `mojo/apps/account/services/webhooks.py` — `dispatch(group, event_type, data, *, idempotency_key=None, channel="webhooks")`.
- [ ] `mojo/apps/account/rest/webhook_subscription.py` — CRUD + `events` endpoint as above.
- [ ] `mojo/apps/account/rest/__init__.py` — import the new module so routes register.
- [ ] **Ask the user to run `makemigrations` and `migrate`** after the model lands.

### Tests

- [ ] `tests/test_account/test_webhook_subscription_model.py`:
  - Create with registered event names succeeds.
  - Create with an unregistered event name returns the model error from `on_rest_save`.
  - `is_active=False` rows are excluded by `dispatch`.
  - Permission gating: user without `manage_group` cannot read/write subscriptions for that Group.
- [ ] `tests/test_account/test_webhook_subscription_dispatch.py`:
  - `dispatch(group, "evt.a", data)` publishes one job per active subscription whose `events` contains `"evt.a"`, and **none** for subscriptions that don't include the event.
  - Each published job has `payload['sign_group_id'] == group.id` (signing wired through).
  - `idempotency_key="x"` produces per-subscription keys `"x_<sub_id>"`.
  - `dispatch(None, ...)` returns `[]` and publishes nothing.
  - `dispatch` does not raise when there are zero matching subscriptions (returns `[]`).
- [ ] `tests/test_account/test_webhook_registry.py`:
  - `register_events("ns", ["a", "b"])` then `register_events("ns", ["b", "c"])` results in `{a, b, c}` (idempotent merge).
  - `all_events()` returns sorted, namespaced entries.
  - `is_registered("ns.unknown")` returns `False`; `is_registered("a")` returns `True` after registration.
- [ ] `tests/test_account/test_webhook_subscription_rest.py`:
  - `GET /api/group/webhook_subscriptions/events` returns the registry contents.
  - Full CRUD round-trip (POST → GET list → PUT → DELETE) under API-key auth.
  - 403 without `manage_group`.

### Docs

- [ ] New: `docs/django_developer/account/webhook_subscriptions.md` — model overview, `register_events` API, `dispatch` helper, link to [webhook_signing.md](../../docs/django_developer/account/webhook_signing.md) for the emit-side guarantees that come bundled.
- [ ] New: `docs/web_developer/account/webhook_subscriptions.md` — REST contract, auth, the events list endpoint, example registration and update bodies, link to the consumer-side [webhook_signing.md](../../docs/web_developer/account/webhook_signing.md).
- [ ] Update: `docs/django_developer/account/README.md` and `docs/web_developer/account/README.md` to link the new pages.
- [ ] Update: `mkdocs.yml` — two new nav entries under Account in both books.
- [ ] Update: `CHANGELOG.md` — note the new model, helpers, and endpoints.

## Design Notes

- **Why `WebhookSubscription` and not `WebhookEndpoint`**: "endpoint" is overloaded in REST contexts (already means "a URL the framework serves"). "Subscription" reads correctly — the row is the receiver subscribing to a stream of events.
- **Why no per-subscription secret**: the Group's `webhook_secret` is already the framework's signing key. Per-subscription overrides would re-introduce the bespoke-secret coordination the signing primitive was designed to eliminate. If someone genuinely needs per-receiver isolation later, that's a sibling feature, not a v1 requirement.
- **Why a registry instead of free-form strings**: catches typos at registration time instead of at dispatch (silent no-ops are the worst webhook bug). It's also what the operator portal needs — a checklist UI can't render unless someone tells it the valid options.
- **Why `dispatch` lives in `services/webhooks.py` and not on the model**: dispatch is an action that crosses model + jobs; service modules are the framework's convention for that shape (matches `services/auth_handoff.py`, `services/disable.py`, etc.).
- **Synchronous dispatch helper, async delivery**: `dispatch()` runs in-process (does one query, queues N jobs) — fast. Actual HTTP delivery is owned by `publish_webhook` and runs in the jobs worker. Projects can call `dispatch()` from REST handlers without worrying about response latency.
- **`is_active` instead of soft-delete**: matches `ApiKey.is_active`. Operators commonly toggle a receiver off during maintenance without wanting to lose its URL.

## Out of Scope

- Per-subscription signing keys.
- Per-subscription retry/backoff overrides (the jobs system already exposes these on `publish_webhook`; if a project needs them they can use the low-level path).
- Delivery history UI / per-subscription delivery logs (the `Job` model already records every attempt with status, duration, response — surfacing that is a portal concern, not a framework one).
- Event payload schemas / typed payloads. The framework treats `data` as opaque JSON; projects own the shape.
- Subscription import/export, bulk operations.
- Webhook delivery dashboards or operator monitoring (build on top of `Job` model in a separate effort).

## Downstream Adoption (out of scope, follow-up work)

Once this lands, downstream services replace their local `WebhookEndpoint`-equivalents:

- Delete the project's webhook subscription model and CRUD endpoints; clients move to `/api/group/webhook_subscriptions`.
- Replace the project's local `dispatch(group, event_type, payload, ...)` with `from mojo.apps.account.services.webhooks import dispatch`.
- Keep the project's domain convenience dispatchers (e.g. `verification_completed(pvr)`) — they just call into the framework `dispatch()` now.
- Register the project's event vocabulary in `apps.py::ready()`.

---

## Plan

**Status**: planned
**Planned**: 2026-05-17

### Diverges from the original proposal

The original proposal section above is the request as filed. The plan below drops two pieces and changes one for KISS:

1. **No event-name registry.** No `mojo/apps/account/webhooks.py`, no `register_events`, no `apps.py::ready()` ceremony, no `GET /api/group/webhook_subscriptions/events` endpoint. Event names are free-form strings; each emitting SaaS documents its own vocabulary in its own docs. Saves one module, one endpoint, and the cross-process registry-state asymmetry.
2. **Dispatch is async via a fan-out job.** `dispatch(group, event_type, data)` queues a single fan-out job and returns its job_id. A worker-side `handle_fanout` then queries subscriptions and calls `publish_webhook` per row. Caller request stays sub-ms regardless of subscription count; the fan-out is itself retriable/observable.
3. **Per-row failures report to the incident app**, not logit — persistent record, not fire-and-forget.

### Objective
Add a generic `WebhookSubscription` model with CRUD + an async two-tier dispatch (`dispatch` → fan-out job → per-receiver signed `publish_webhook`) to `mojo.apps.account`, so every downstream service inherits subscription storage + event fan-out + signing + retries for free.

### Design Decisions

- **Two-tier dispatch (fan-out job → per-receiver webhook jobs)**: `dispatch()` returns in ~1 query of latency regardless of subscription count. The fan-out is observable as its own Job. Failure of the worker mid-fan-out retries the whole fan-out; `idempotency_key` suffixing prevents duplicate deliveries to receivers already hit. Separate channel `webhook_fanout` keeps fan-out work from competing with HTTP delivery slots.
- **No registry / no event vocabulary in the framework**: strings in, strings out. Save-time event-name validation is application-layer concern (each portal can validate against its own docs). The framework has no opinion.
- **`events__contains=[event_type]` in SQL**: Postgres-native JSONField containment filter, executed in the DB. No Python predicate loop over inactive-for-this-event rows. The testproject is on Postgres so this works in CI; production deployments are Postgres-only by convention.
- **URL validation in `on_rest_pre_save`**: reject anything not starting with `https://` and run Django's `URLValidator` for syntax. Fail-closed.
- **Permission threshold**: `manage_group` / `manage_groups` / `groups` — same as ApiKey CRUD and the existing `POST /api/group/webhook_secret`. If you can mint API keys for the Group, you can manage its webhook subscriptions.
- **Group resolution**: standard pattern — `request.group` set by API-key auth, or `group=<id>` parameter resolved by `@md.uses_model_security` for user auth.
- **URL plural** (`/api/group/webhook_subscriptions`): more REST-idiomatic than the existing singular pattern (`/api/group/apikey`, `/api/group/webhook_secret`). Establishes a slightly different convention going forward; documented.
- **No per-subscription secret**: signing keys on Group (already shipped). Rotating the Group's webhook secret rolls every subscription at once. Per-receiver isolation is a future sibling feature, not v1.
- **Skip-and-continue + incident report** on per-row publish failure in `handle_fanout`: one flaky row cannot poison the rest of the fan-out. Failures land in incident events with `event_type='webhook_fanout_error'`, including `subscription_id`, `group_id`, `event_type`, and the exception repr.
- **`is_active` toggle, not soft-delete**: matches `ApiKey.is_active`. Operators commonly disable a receiver during maintenance; URL is preserved.
- **Empty `events: []` is a valid "draft" state**: subscription saves fine, matches no events, never fires. Operator can fill events later.

### Steps

1. `mojo/apps/account/models/webhook_subscription.py` (new) —
   - `WebhookSubscription(MojoModel, models.Model)` with FK `group → account.Group` (CASCADE), `url = URLField()`, `events = JSONField(default=list)`, `is_active = BooleanField(default=True, db_index=True)`, `metadata = JSONField(default=dict, blank=True)`, `created`, `modified`.
   - `Meta: ordering = ["-created"]`.
   - `RestMeta`: `VIEW_PERMS = SAVE_PERMS = DELETE_PERMS = ["manage_group", "manage_groups", "groups"]`, `CAN_DELETE = True`, `GRAPHS = {"default": {fields: [id, created, modified, url, events, is_active]}, "detail": {fields: default + [metadata], graphs: {group: "basic"}}}`.
   - `on_rest_pre_save(changed_fields, created)`: validate `self.url` with Django `URLValidator` and require `https://`-prefix; raise `merrors.ValueException` on failure. Validate `self.events` is a list of strings; raise on shape error.

2. `mojo/apps/account/models/__init__.py` — append `from .webhook_subscription import WebhookSubscription`.

3. `mojo/apps/account/services/webhooks.py` (new) —
   - `dispatch(group, event_type, data, *, idempotency_key=None, channel="webhooks")`: validates `group` is not None, calls `jobs.publish("mojo.apps.account.services.webhooks.handle_fanout", {"group_id": group.id, "event_type": event_type, "data": data, "idempotency_key": idempotency_key, "channel": channel}, channel="webhook_fanout")`. Returns the fanout job_id (or `None` if `group is None`).
   - `handle_fanout(job)`: reads `payload`, loads `Group` by `group_id` (if missing → incident report `event_type='webhook_fanout_group_missing'`, return `'failed'` no retry), filters `WebhookSubscription.objects.filter(group=group, is_active=True, events__contains=[event_type])`, iterates rows in a `try/except`: per row, build `publish_webhook` kwargs (suffix idempotency_key with `_{sub.id}`), call `jobs.publish_webhook(...)`. On exception per row, call `incident.report_event(details=..., event_type='webhook_fanout_error', level=..., extra={subscription_id, group_id, event_type, error})` and continue. Records `job.metadata['published_count']`, `job.metadata['failed_count']`, `job.metadata['published_job_ids']` (capped at first 50 for size). Returns `'success'`.

4. `mojo/apps/account/rest/webhook_subscription.py` (new) —
   ```python
   import mojo.decorators as md
   from mojo.apps.account.models import WebhookSubscription

   @md.URL('group/webhook_subscriptions')
   @md.URL('group/webhook_subscriptions/<int:pk>')
   @md.uses_model_security(WebhookSubscription)
   def on_group_webhook_subscriptions(request, pk=None):
       return WebhookSubscription.on_rest_request(request, pk)
   ```

5. `mojo/apps/account/rest/__init__.py` — append `from .webhook_subscription import *`.

6. Run `bin/create_testproject` (regenerates the testproject with new migrations for the WebhookSubscription model). **Ask the user to run the equivalent `makemigrations` + `migrate` in their Django project after this lands.**

7. `tests/test_account/test_webhook_subscription_model.py` (new) — model save behavior, URL validation, RestMeta permission gating.

8. `tests/test_account/test_webhook_subscription_dispatch.py` (new) — `dispatch` queues fanout, fanout queues per-receiver webhook jobs with `sign_group_id`, idempotency suffixing, incident report on per-row failure, missing group fails without retry, no-match returns success with zero published, event_type filtering via `events__contains`.

9. `tests/test_account/test_webhook_subscription_rest.py` (new) — CRUD round-trip under API-key auth + 403 without `manage_group`.

10. `docs/django_developer/account/webhook_subscriptions.md` (new) — model overview, `dispatch()` semantics + two-tier flow diagram, link to [webhook_signing.md](webhook_signing.md) for the signing guarantees that come bundled, "designing your event vocabulary" subsection (renaming = register-and-deprecate, etc.).

11. `docs/django_developer/account/README.md` — add link to the new doc.

12. `docs/web_developer/account/webhook_subscriptions.md` (new) — REST contract for CRUD, auth, example request/response bodies, behavior under `is_active=false`, rotation interaction, link to web-side [webhook_signing.md](webhook_signing.md).

13. `docs/web_developer/account/README.md` — add link.

14. `mkdocs.yml` — two new nav entries under Account in both books.

15. `CHANGELOG.md` — note the new model, dispatch helper, fan-out job, and CRUD endpoints under the upcoming version.

### User Cases

- **Sender registers and emits**: SaaS app calls `dispatch(group, "verification.completed", {"verification_id": …})` from domain code. Returns instantly (fanout job_id). Worker picks up the fanout job, queries the Group's active subscriptions whose `events` contains `"verification.completed"`, publishes one signed webhook job per match.
- **Operator creates subscription via portal**: `POST /api/group/webhook_subscriptions {"url": "https://hooks.example.com/...", "events": ["verification.completed", "verification.failed"]}` → 200 with new row, `is_active=True` by default.
- **Operator pauses delivery**: `PUT /api/group/webhook_subscriptions/<id> {"is_active": false}`. Next fan-out skips the row. Toggling back resumes deliveries; events fired while paused are not buffered.
- **Operator rotates webhook secret**: hits the existing `POST /api/group/webhook_secret {"rotate": true}` — all subscriptions immediately sign with the new key on next delivery. Receivers refresh on signature-mismatch (already documented in webhook_signing.md).
- **Receiver verifies inbound**: `if not verify_signed_request(request, group.get_webhook_secret()): return 401` — same one-liner as for any signed webhook.
- **Same logical event published twice**: caller passes the same `idempotency_key` both times; each subscription gets a stable per-receiver key (`{key}_{sub.id}`); job-level dedupe prevents duplicate delivery.
- **Group deleted between dispatch and fan-out**: fan-out job marks itself `'failed'` with `event_type='webhook_fanout_group_missing'` in the incident record. No retry — won't recover.
- **One subscription's URL is unreachable mid-fanout**: per-row `try/except` reports the failure to incident with `event_type='webhook_fanout_error'`, continues. The HTTP delivery itself is still subject to `publish_webhook`'s retry/backoff/dead-letter.
- **No subscriptions match**: fanout job succeeds with `published_count=0`. No webhook jobs queued. Common case (most events have no listeners).

### Edge Cases

- **URL not https** → `on_rest_pre_save` raises `ValueException`. Operators get immediate 400 with a clear message.
- **URL syntactically invalid** → `URLValidator` raises; converted to `ValueException`.
- **`events` not a list** → `on_rest_pre_save` raises; UI should always send a list.
- **Subscription deleted between fan-out enqueue and execution** → query just returns one fewer row; behavior matches expectation.
- **Subscription updated mid-fan-out** → the queryset evaluates once at the start of `handle_fanout`; the row's URL/events/is_active are read from that snapshot. Updates during the fan-out are picked up on the next event.
- **Two events fire near-simultaneously for the same Group** → two independent fanout jobs; per-job iteration. Subscriptions may receive them in any order — receivers should not assume ordering. Documented.
- **Fan-out job retries after partial success** → without `idempotency_key`, retries publish duplicate webhook jobs to subscribers already hit. With `idempotency_key`, per-receiver keys (`{key}_{sub.id}`) keep job-layer dedupe correct. Documented.
- **Massive `data` payload** → goes through `publish_webhook`'s existing `json.dumps` validation; the fanout job payload itself stores `data` once, then each per-receiver job duplicates it. v1 doesn't deduplicate the body across fan-out. Acceptable for typical payload sizes; oversized payloads should be solved at the application layer (push a ref instead of the blob).
- **No `manage_group` on the calling user** → 403 from `@md.uses_model_security(WebhookSubscription)` before any handler runs.
- **Cross-group access via `request.DATA.group` override** → same model as ApiKey CRUD and `webhook_secret` (intentional: a cross-group admin with `manage_group` on the target can read/edit). Documented.

### Testing

`tests/test_account/test_webhook_subscription_model.py`:
- `test_create_valid` — POST → 200, row created with `is_active=True`. → same file
- `test_reject_non_https_url` — POST with `http://` URL → 400 / `ValueException`. → same file
- `test_reject_malformed_url` — POST with `"not a url"` → 400. → same file
- `test_reject_events_not_list` — POST with `events: "string"` → 400. → same file
- `test_inactive_excluded_from_dispatch` — create + flip `is_active=False`, dispatch, no per-receiver job is queued for it. → same file
- `test_permission_denied_without_manage_group` — non-admin user → 403 on every CRUD verb. → same file

`tests/test_account/test_webhook_subscription_dispatch.py`:
- `test_dispatch_queues_fanout_job` — `dispatch(group, "evt.a", {...})` returns a job_id; `Job.objects.get(id=job_id)` exists in `webhook_fanout` channel with the right payload shape (group_id, event_type, data). No webhook jobs yet. → same file
- `test_fanout_publishes_one_signed_job_per_match` — pre-create 3 subscriptions for the Group: A (`events=["evt.a"]`), B (`events=["evt.a","evt.b"]`), C (`events=["evt.b"]`). Run `handle_fanout` on a stub job for `"evt.a"`. Assert exactly 2 webhook jobs published (A and B), each with `payload['sign_group_id'] == group.id`. → same file
- `test_fanout_filters_via_events_contains_in_sql` — assert the SQL filter used (`events__contains=[event_type]`) actually excludes rows in the queryset, not just post-filter in Python (read the queryset's SQL or smoke via creating a "containing 'evt.x' substring but not array-contains 'evt.x'" row — e.g. `events=["evt.x.subevent"]` must not match `evt.x`). → same file
- `test_fanout_inactive_skipped` — `is_active=False` row never produces a webhook job. → same file
- `test_idempotency_key_suffixed_per_subscription` — pass `idempotency_key="abc"`; each per-receiver job's payload has `idempotency_key="abc_{sub.id}"`. → same file
- `test_dispatch_none_group_returns_none` — `dispatch(None, "x", {})` returns `None`, no job queued. → same file
- `test_fanout_missing_group_reports_incident` — fanout job references a deleted group → return `'failed'`, no retry, `incident.report_event` called once with `event_type='webhook_fanout_group_missing'`. → same file
- `test_fanout_per_row_failure_reports_incident_and_continues` — patch `jobs.publish_webhook` to raise on subscription #2 of 3; expect: 2 webhook jobs published (sub #1, #3), one incident reported for sub #2 with `event_type='webhook_fanout_error'`. → same file
- `test_fanout_zero_matches_returns_success` — no subscriptions for event → fanout completes with `published_count=0`, no incidents. → same file

`tests/test_account/test_webhook_subscription_rest.py`:
- `test_rest_crud_round_trip` — POST creates, GET list shows it, GET detail returns it, PUT updates `is_active=False`, GET reflects update, DELETE removes; subsequent GET 404s. → same file
- `test_rest_create_requires_manage_group` — user without permission → 403. → same file
- `test_rest_under_api_key_auth` — API-key with `manage_group` on the Group can CRUD without passing `group` in body (uses `request.group` from key). → same file

Run: `bin/run_tests --agent -t test_account.test_webhook_subscription_model -t test_account.test_webhook_subscription_dispatch -t test_account.test_webhook_subscription_rest`. Migration needs `bin/create_testproject` to run first to regenerate the testproject schema.

### Docs

- `docs/django_developer/account/webhook_subscriptions.md` — new. Sections: Overview, Model API + `RestMeta`, `dispatch()` two-tier flow (with diagram), `handle_fanout` behavior + incident events emitted, idempotency-key suffix semantics, designing your event vocabulary (free-form strings; register-and-deprecate flow for renames), interaction with `webhook_signing.md` (signing is automatic).
- `docs/django_developer/account/README.md` — add line: `- [Webhook Subscriptions](webhook_subscriptions.md) — Per-Group subscription registry, fan-out dispatcher, signed delivery via the existing webhook primitive`.
- `docs/web_developer/account/webhook_subscriptions.md` — new. REST contract for CRUD (`POST /api/group/webhook_subscriptions`, list, detail, update, delete), auth, request/response examples, what `is_active=false` does, how rotation of the Group's webhook secret affects all subscriptions, cross-link to web-side [webhook_signing.md](webhook_signing.md).
- `docs/web_developer/account/README.md` — add link.
- `mkdocs.yml` — two new nav entries under Account in both books.
- `CHANGELOG.md` — entry under the upcoming version: "Added `WebhookSubscription` model, `dispatch()` + async fan-out helper (`mojo.apps.account.services.webhooks`), and `/api/group/webhook_subscriptions` CRUD. Outbound deliveries inherit signing + retries from `jobs.publish_webhook(group=...)`."

---

## Resolution

**Status**: resolved
**Date**: 2026-05-17
**Commit**: 52f2e74

### What Was Built

Generic `WebhookSubscription` storage + async two-tier fan-out dispatcher in `mojo.apps.account`. One model + CRUD endpoints + sync `dispatch()` entry that queues a fan-out job, and a `handle_fanout` worker that queries matching active subscriptions and publishes one signed `jobs.publish_webhook(group=...)` per receiver. No registry — strings in, strings out. Per-row failures + missing-group conditions report to the incident app, not logit.

### Files Changed

- `mojo/apps/account/models/webhook_subscription.py` (new) — `WebhookSubscription` model with `RestMeta` (`manage_group`/`manage_groups`/`groups`) and `on_rest_pre_save` validation (https-only URL via `URLValidator(schemes=["https"])`, events list-of-strings).
- `mojo/apps/account/services/webhooks.py` (new) — `dispatch(group, event_type, data, *, idempotency_key=None, channel="webhooks")` returns fan-out job_id; `handle_fanout(job)` does the queryset + per-row publish + incident-on-failure.
- `mojo/apps/account/rest/webhook_subscription.py` (new) — `@md.URL('group/webhook_subscriptions')` CRUD via `@md.uses_model_security`.
- `mojo/apps/account/migrations/0044_webhooksubscription.py` (new — generated by `bin/create_testproject`).
- `mojo/apps/account/models/__init__.py` — exports `WebhookSubscription`.
- `mojo/apps/account/rest/__init__.py` — registers the new URL routes.

### Tests

- `tests/test_account/test_webhook_subscription_model.py` — URL validation (https-only, malformed, etc.), events shape, empty-events draft state, REST 403 for non-admin, admin happy-path, REST rejects http with 400. 9 cases.
- `tests/test_account/test_webhook_subscription_dispatch.py` — dispatch queues correct fan-out payload, handle_fanout publishes one signed job per matching active subscription, Postgres `events__contains` semantics (no substring/prefix false-matches), inactive rows skipped, idempotency key suffixing, zero-match success, missing-group + per-row failure → incident reporting, cross-group isolation. 10 cases.
- `tests/test_account/test_webhook_subscription_rest.py` — full CRUD round-trip under password auth + ApiKey auth path. 2 cases.
- Run: `bin/run_tests --agent -t test_account.test_webhook_subscription_model -t test_account.test_webhook_subscription_dispatch -t test_account.test_webhook_subscription_rest` — all 21 pass.

### Docs Updated

- `docs/django_developer/account/webhook_subscriptions.md` (new) — model, dispatch + fan-out flow, vocabulary design, channel config, incident categories, REST quick-reference.
- `docs/web_developer/account/webhook_subscriptions.md` (new) — REST contract, request/response shapes, rotation interaction, error codes.
- `docs/django_developer/account/README.md`, `docs/web_developer/account/README.md` — link rows.
- `mkdocs.yml` — two new nav entries under Account in both books.
- `CHANGELOG.md` — under v1.1.0.

### Design Notes (deltas from the original proposal)

- **No event-name registry**: dropped during design (KISS). Free-form strings; each emitting SaaS documents its own vocabulary; renaming is a register-and-deprecate operational flow.
- **Async fan-out**: original proposal had `dispatch()` run synchronously and queue N jobs. Changed to: `dispatch()` queues a single fan-out job on `webhook_fanout` channel; worker-side `handle_fanout` does the per-row publish. Caller request stays sub-ms regardless of subscription count; fan-out itself is retriable/observable.
- **Per-row failures → incident**: rather than `logit`. Persistent record with category `webhook:fanout:error` (per row) or `webhook:fanout:group_missing` (group deleted between dispatch and fan-out).
- **Postgres-native filter**: `events__contains=[event_type]` instead of a Python predicate loop.

### Channel configuration to add downstream

Add to your project's `JOBS_CHANNELS` setting (or accept the fallback to `"default"`):

```python
JOBS_CHANNELS = ["default", "webhooks", "webhook_fanout", ...]
```

### Migration

After pulling this change, run `./manage.py makemigrations && ./manage.py migrate` in your Django project. The django-mojo repo's testproject was regenerated via `bin/create_testproject` and includes `account.0044_webhooksubscription`.

### Follow-up (out of scope)

- Downstream services migrate their local `WebhookEndpoint`-equivalents to `/api/group/webhook_subscriptions` and swap their dispatch loops for `from mojo.apps.account.services.webhooks import dispatch` — separate requests per service.
- Per-subscription signing secrets (rejected in v1; revisit if a real need emerges).
- Operator dashboard for per-subscription delivery history (build on `Job` model, separate effort).
- Replay protection: receivers responsibility — documented in webhook_signing.md.
