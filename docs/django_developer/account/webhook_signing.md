# Webhook Signing — Django Developer Reference

Django-MOJO ships a uniform primitive for signing outbound webhooks and verifying inbound ones, keyed on a **per-Group HMAC-SHA256 secret**. Every SaaS built on the framework can stop rolling its own signing scheme — there is one helper to sign, one to verify, and one standard header.

> **Why per-Group?** Webhooks are almost always tied to a specific Group's data. Keying the HMAC on a Group secret lets you scope signatures by tenant without coordinating extra credentials.

## How It Works

1. The framework's `Group` model owns a webhook secret, stored inside its existing `MojoSecrets` blob (no migration). The secret is minted lazily on first use.
2. Outbound jobs published with `jobs.publish_webhook(..., group=g)` are signed at **delivery time**: the handler looks up `g`, canonicalizes the body, computes the HMAC, sets `X-Mojo-Signature`, and sends those exact bytes. Retries re-sign with whatever secret is current — rotation is safe.
3. Inbound receivers verify with `verify_signed_request(request, group.get_webhook_secret())`.

The secret never enters the job queue payload — only the Group id is stored.

## Group Secret API

```python
from mojo.apps.account.models import Group

g = Group.objects.get(pk=42)

# Read (default — never auto-mints; safe for verify paths)
secret = g.get_webhook_secret()              # → "wsec_..." or None

# Read with auto-mint (emit-side semantics)
secret = g.get_webhook_secret(auto_create=True)

# Full record with timestamps
info = g.get_webhook_secret_info(auto_create=True)
# objict(value="wsec_…", created_at="2026-05-17T…", last_rotated_at="2026-05-17T…")

# Rotate — new value, preserves created_at, advances last_rotated_at
info = g.rotate_webhook_secret()
```

**Default is `auto_create=False`** so a verify path can never accidentally mint a secret to make a tampered request "valid." The two places that pass `True` are the REST endpoint (operator workflow) and the `sign_for_group` helper (emit-time).

The secret is `"wsec_"` + 48 alphanumeric characters (53 chars total), generated with `crypto.random_string(48, allow_special=False)` — same generator as `ApiKey` tokens.

## REST Endpoint

```
POST /api/group/webhook_secret
```

| Body | Behavior |
|---|---|
| `{}` (or `{"group": <id>}`) | Return current secret; auto-mint on first call |
| `{"rotate": true}` | Generate a new secret, return it, invalidate the prior |

Response:

```json
{
  "status": true,
  "data": {
    "secret": "wsec_<48 chars>",
    "created_at": "2026-05-17T01:23:45.000+00:00",
    "last_rotated_at": "2026-05-17T01:23:45.000+00:00"
  }
}
```

**Permission:** `manage_group`, `manage_groups`, or `groups` — same threshold as `ApiKey` CRUD. If you can mint API keys for a Group, you can read its signing secret.

The Group is resolved by the framework dispatcher from `request.group` — either set by API-key auth (`Authorization: apikey <token>`) or by a `group=<id>` parameter on a session/JWT request.

## Recommended Path: `jobs.publish_webhook(group=...)`

Almost all webhook emission should flow through the jobs queue (retries, backoff, dead-letter). Pass `group=` and signing happens automatically:

```python
from mojo.apps import jobs

jobs.publish_webhook(
    url=receiver_url,
    data={"event": "verification_complete", "customer_id": 42},
    group=customer.group,        # ← this is the whole API
)
```

What happens at delivery time:

1. The handler reads `payload['sign_group_id']` and loads the Group.
2. Canonicalizes the body: `json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")`.
3. Computes `X-Mojo-Signature: <hex HMAC-SHA256>` keyed on the Group's secret.
4. Sends the exact bytes it hashed via `requests.post(..., data=body_bytes)` — guarantees signature and wire bytes match.

If the Group has been deleted between publish and delivery, the handler returns `'failed'` with `error_type='sign_group_missing'` — it does **not** retry and does **not** silently send unsigned.

## Verifying Inbound Webhooks (Consumer Side)

Consumer endpoints look up the Group their own way (URL param, body field, header — whatever fits) and call the helper:

```python
from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.models import Group
from mojo.helpers.request import verify_signed_request


@md.URL('webhook/<str:group_uuid>')
@md.public_endpoint("signature is the authentication")
def on_webhook(request, group_uuid=None):
    group = Group.objects.filter(uuid=group_uuid).first()
    if not verify_signed_request(request, group.get_webhook_secret() if group else None):
        raise merrors.PermissionDeniedException("invalid signature", 401, 401)
    # process the verified body...
```

`verify_signed_request`:

- Pulls raw `request.body` and the `X-Mojo-Signature` header.
- Returns `False` (never raises) when the secret is missing, the header is missing, or the signature does not match.
- Uses `hmac.compare_digest` (via `verify_signature`) for constant-time comparison.

**Important for receivers:** hash the **raw request body bytes**. Do not re-serialize the parsed JSON — `requests`/your framework may use different separators or key ordering, and the HMAC will fail.

## Escape Hatch: Sign Outside the Jobs Queue

For synchronous webhooks or custom transports, call the helper directly:

```python
from mojo.helpers.crypto.sign import sign_for_group, WEBHOOK_SIGNATURE_HEADER

body_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
sig = sign_for_group(group, body_bytes)
response.headers[WEBHOOK_SIGNATURE_HEADER] = sig
```

`sign_for_group` auto-mints on first use (same as the REST endpoint).

## Security Notes

- **Constant-time compare**: `verify_signature` uses `hmac.compare_digest`. Don't roll your own.
- **No auto-mint on verify**: `get_webhook_secret()` defaults to `auto_create=False` so a tampered request cannot trigger a mint.
- **Secret never in the queue**: `publish_webhook(group=...)` stores only `sign_group_id` in the job payload. Anyone reading the queue store sees no signing material.
- **Rotation is immediate**: no overlap window. Receivers should accept transient signature-mismatch errors during operator-driven rotation, same as ApiKey rotation.
- **Group hierarchy is not auto-resolved**: the signing secret is on the Group you pass, not its parent or descendants. Pass the exact Group whose data the webhook represents.
- **Header is masked in logs**: the jobs handler runs `X-Mojo-Signature` through its `_sanitize_headers` masking — full signatures don't end up in job metadata or log lines.

## Downstream Adoption Recipe

For services migrating off bespoke webhook secrets:

1. **Emitter side** — replace your old `requests.post(..., headers={"X-MyService-Signature": ...})` call with `jobs.publish_webhook(url, data, group=...)`. Delete the old config field that stored the per-consumer secret.
2. **Consumer side** — drop your local `webhook_secret` model field and bespoke `_validate_signature`. Replace the validation with `verify_signed_request(request, remote_group.get_webhook_secret())`.
3. Aliases: keep your old service-specific signature header working for one release cycle (read both headers, prefer `X-Mojo-Signature`), then remove the legacy path.
