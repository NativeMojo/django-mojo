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

The recipe is the same in any language:

1. Take the **raw request body bytes** (never the parsed JSON — re-serialization will change separators or key order, and the signature will fail).
2. Compute `HMAC-SHA256(secret, raw_body)` and hex-encode.
3. **Constant-time compare** against the `X-Mojo-Signature` header. Never use `==`.
4. Reject if the header is missing.

### Python (django-mojo consumer)

```python
from mojo.helpers.request import verify_signed_request

if not verify_signed_request(request, group.get_webhook_secret()):
    raise merrors.PermissionDeniedException("invalid signature", 401, 401)
# process the verified body
```

`verify_signed_request` handles the raw-body pull, the header lookup, the constant-time compare, and the `None`/missing-secret cases.

### Python (stdlib — any framework)

```python
import hmac, hashlib

def verify(raw_body: bytes, signature_header: str, secret: str) -> bool:
    if not signature_header:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

In Django views, use `request.body` (bytes) — not `request.POST` or any parsed form. In FastAPI, use `await request.body()`.

### Node.js

```js
const crypto = require('crypto');

function verify(rawBody, signatureHeader, secret) {
  if (!signatureHeader) return false;
  const expected = crypto.createHmac('sha256', secret).update(rawBody).digest('hex');
  const a = Buffer.from(expected, 'hex');
  const b = Buffer.from(signatureHeader, 'hex');
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

**Express gotcha**: mount `express.raw({type: 'application/json'})` (or `bodyParser.raw`) on the webhook route, **not** `express.json()` — the latter parses and discards the raw bytes you need for hashing.

### Go

```go
import (
    "crypto/hmac"
    "crypto/sha256"
    "encoding/hex"
)

func Verify(rawBody []byte, signatureHeader, secret string) bool {
    if signatureHeader == "" {
        return false
    }
    mac := hmac.New(sha256.New, []byte(secret))
    mac.Write(rawBody)
    expected := hex.EncodeToString(mac.Sum(nil))
    return hmac.Equal([]byte(expected), []byte(signatureHeader))
}
```

Read the body with `io.ReadAll(r.Body)` once and reuse — don't read it twice or it will be empty the second time.

### Ruby

```ruby
require 'openssl'
require 'rack/utils'

def verify(raw_body, signature_header, secret)
  return false if signature_header.nil? || signature_header.empty?
  expected = OpenSSL::HMAC.hexdigest('sha256', secret, raw_body)
  Rack::Utils.secure_compare(expected, signature_header)
end
```

In Rails, `request.raw_post` gives the body bytes. Don't use `params` — that's the parsed form.

### Debug with curl + openssl

To reproduce a signature on the command line (sanity-check before pointing a live receiver at the sender):

```bash
SECRET="wsec_…"
echo -n "$(cat raw_body.json)" | openssl dgst -sha256 -hmac "$SECRET" -hex
# → SHA2-256(stdin)= a1b2c3…   ← compare against the X-Mojo-Signature you received
```

`echo -n` is critical — without it, a trailing newline changes the hash.

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
