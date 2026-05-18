# Webhook Signing — REST API Reference

Django-MOJO emits and consumes webhooks signed with HMAC-SHA256, keyed on a **per-Group secret**. As a consumer you fetch the secret once with your `ApiKey`, cache it, and verify the signature on every incoming webhook. As an operator you can rotate the secret at any time without coordinating shared strings between sender and receiver.

See [Django Developer — Webhook Signing](../../django_developer/account/webhook_signing.md) for the framework-side helpers.

## Endpoint

```
POST /api/group/webhook_secret
```

### Auth

Either:
- `Authorization: apikey <token>` where the API key has `manage_group` permission on the target Group. `request.group` is taken from the API key automatically — no `group` body field is needed.
- A session/JWT user with `manage_group`, `manage_groups`, or `groups` permission on the target Group. Pass `{"group": <id>}` in the body to select the target.

Returns `403` (or `401`) without the required permission.

### Read the current secret (auto-mints on first call)

Request:

```http
POST /api/group/webhook_secret HTTP/1.1
Authorization: apikey wsec_NOT_THE_KEY_just_an_example
Content-Type: application/json

{}
```

Response:

```json
{
  "status": true,
  "data": {
    "secret": "wsec_4kE3J9p2…",
    "created_at": "2026-05-17T01:23:45.000+00:00",
    "last_rotated_at": "2026-05-17T01:23:45.000+00:00"
  }
}
```

Subsequent calls with `{}` return the **same** secret and the same timestamps.

### Rotate

```http
POST /api/group/webhook_secret HTTP/1.1
Authorization: apikey ...
Content-Type: application/json

{"rotate": true}
```

Response shape is identical, but `data.secret` is new and `data.last_rotated_at` is advanced. `data.created_at` is preserved from the original mint.

Rotation invalidates the prior secret **immediately** — there is no overlap window. Receivers should refresh their cached secret on signature mismatch and retry once before alerting.

## Signature Header

Sender adds:

```
X-Mojo-Signature: <hex hmac-sha256 of the raw body>
```

The signature is computed over the **raw request body bytes**. The sender uses a canonical JSON encoding (sorted keys, compact separators) — but receivers do not need to know this. **Hash the bytes you actually receive on the wire** — do not re-serialize the parsed JSON.

## Verifying an Incoming Webhook

```python
# Pseudo-code on the receiver side (any django-mojo project)
from mojo.helpers.request import verify_signed_request
from mojo.apps.account.models import Group

group = Group.objects.get(uuid=request_group_uuid)
if not verify_signed_request(request, group.get_webhook_secret()):
    return 401
# process the verified body
```

Non-django consumers: compute `hmac.new(secret.encode(), request.body, sha256).hexdigest()` and compare against the `X-Mojo-Signature` header using a constant-time comparison (Python: `hmac.compare_digest`).

## Caching the Secret

Fetch once at startup (or on first incoming webhook), cache in memory, refresh on signature failure. Do not look up the secret per request — it is unchanged between rotations and the REST endpoint is not designed for high read volume.

A typical cache invalidation loop:

```python
expected = cached_secret_for(group_id)
if not verify(request.body, request.headers["X-Mojo-Signature"], expected):
    expected = refetch_secret_for(group_id)   # one extra REST call
    cached_secret_for(group_id, set=expected)
    if not verify(request.body, request.headers["X-Mojo-Signature"], expected):
        return 401
```

## Replay Protection — Your Responsibility

The HMAC covers the request body only. There is **no nonce and no timestamp** baked into the signature. A captured `(body, X-Mojo-Signature)` pair will validate indefinitely until the Group secret is rotated.

If you need replay protection (most production webhook receivers do):

- **Dedupe on an event id**: have the sender include a stable id (e.g. `event_id`, `webhook_id`) in the payload, and short-circuit your handler on already-seen ids. Storing the ids in Redis/SQL for 24-48 hours is usually enough.
- **Reject stale requests**: include an ISO-8601 `timestamp` field in the payload and reject anything older than N seconds (e.g. 5 minutes). The signature covers the timestamp because it covers the whole body — an attacker cannot bump the timestamp without invalidating the signature.

The framework does not enforce either pattern — it is application-layer policy. A future revision may add a `X-Mojo-Webhook-Timestamp` header to the signature input; for now the primitive is intentionally minimal.

## Errors

| Status | Reason |
|---|---|
| `200` | Success (secret returned) |
| `401` | Missing or invalid `Authorization` |
| `403` | Authenticated, but lacks `manage_group` on the target Group |
| `400` | Group could not be resolved from the request |
