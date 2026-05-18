# Webhook Subscriptions — REST API Reference

A `WebhookSubscription` is the registry row that says "Group G should POST event-type T (and a few more) to URL U." Once a row is active, every emitted event of that type is signed and delivered via the framework's webhook pipeline.

See also: [Webhook Signing](webhook_signing.md) for the signature header and verification recipes.

## Endpoint

```
GET    /api/group/webhook_subscriptions          # list
POST   /api/group/webhook_subscriptions          # create
GET    /api/group/webhook_subscriptions/<id>     # detail
POST   /api/group/webhook_subscriptions/<id>     # update
DELETE /api/group/webhook_subscriptions/<id>     # remove
```

## Auth

Either:

- `Authorization: apikey <token>` where the API key has `manage_group` permission on the target Group. `request.group` is taken from the key automatically — no `group` body field needed.
- A session/JWT user with `manage_group`, `manage_groups`, or `groups` permission on the target Group. Pass `{"group": <id>}` in the body (or as a query parameter for `GET`).

Without the required permission: `403`. Without any auth: `401`.

## Create

```http
POST /api/group/webhook_subscriptions HTTP/1.1
Authorization: apikey wsec_…
Content-Type: application/json

{
  "url": "https://hooks.acme.com/mojo/verify",
  "events": ["verification.completed", "verification.failed"]
}
```

Response:

```json
{
  "status": true,
  "data": {
    "id": 17,
    "created": "2026-05-17T01:23:45.000+00:00",
    "modified": "2026-05-17T01:23:45.000+00:00",
    "url": "https://hooks.acme.com/mojo/verify",
    "events": ["verification.completed", "verification.failed"],
    "is_active": true,
    "group": {"id": 42, "name": "Acme Co", ...}
  }
}
```

### URL rules

- **Must start with `https://`**. HTTP is rejected with a clear `400`. There is no override.
- Must be a syntactically valid URL (`URLValidator`).

### Events

Free-form list of strings. The framework imposes no vocabulary. Each emitting SaaS publishes the event names it supports in its own docs (e.g., MojoVerify documents `verification.completed`, etc.). Subscribe to whichever you want; unknown names will simply never fire and are tolerated.

An **empty list is valid** — the subscription stores the URL but matches no events. Useful as a "draft" state when configuring a receiver before flipping events on.

## List

```http
GET /api/group/webhook_subscriptions?group=42 HTTP/1.1
Authorization: apikey ...
```

Returns the Group's subscriptions in `data[]`. Standard MOJO list filters (search, pagination) apply.

## Detail

```http
GET /api/group/webhook_subscriptions/17?group=42 HTTP/1.1
```

The `detail` graph adds the `metadata` JSON field for caller-owned tags or annotations. Request it via `?graph=detail`.

## Update

```http
POST /api/group/webhook_subscriptions/17 HTTP/1.1
Content-Type: application/json

{"group": 42, "is_active": false}
```

Common updates:

- `{"is_active": false}` — pause delivery without losing the URL. Events that fire while paused are not buffered.
- `{"events": [...]}` — replace the events list (this is a SET, not append; pass the full list you want).
- `{"url": "https://..."}` — same https-only validation as create.

## Delete

```http
DELETE /api/group/webhook_subscriptions/17?group=42 HTTP/1.1
```

Permanent. The row is removed from the database. There is no undo.

## How rotation interacts

Rotating the Group's webhook secret (`POST /api/group/webhook_secret {"rotate": true}` — see [Webhook Signing](webhook_signing.md)) **immediately invalidates** the old secret. The next delivery to every subscription signs with the new key. Receivers should refresh their cached secret on signature mismatch and retry once before alerting.

There is no overlap window. Plan rotations during low-traffic windows.

## Errors

| Status | Reason |
|---|---|
| `200` | Success |
| `400` | URL not https / not valid; events not a list / not strings |
| `401` | Missing or invalid `Authorization` |
| `403` | Authenticated but lacks `manage_group` on the target Group |
| `404` | Subscription id does not exist (or belongs to another Group) |

## Operational notes

- **Delivery is async.** Creating or updating a subscription does not retroactively fire events. Only events emitted *after* the row is active will be delivered.
- **Many subscriptions per Group is fine.** The fan-out is a single SQL query plus a per-row enqueue. Tens to hundreds of subscriptions is well within bounds.
- **Receivers must implement replay protection.** See the [Webhook Signing — Replay Protection](webhook_signing.md#replay-protection--your-responsibility) section. The framework signs the body only — no nonce, no timestamp.
- **HTTP delivery has its own retry policy** inherited from `jobs.publish_webhook` — exponential backoff up to ~1 hour, configurable per-publish if needed. Returns `2xx` to acknowledge; any 5xx/timeout retries; 4xx fails fast.
