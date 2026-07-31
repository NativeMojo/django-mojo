# crypto — Django Developer Reference

## Import

```python
from mojo.helpers import crypto
```

The `crypto` module contains sub-modules for different cryptographic operations.

## AES Symmetric Encryption

```python
from mojo.helpers.crypto import aes

# Encrypt
encrypted = aes.encrypt("sensitive data", password="my-secret-key")

# Decrypt
plaintext = aes.decrypt(encrypted, password="my-secret-key")
```

Use for encrypting data at rest when you need to retrieve it later (two-way). For passwords, use hashing instead.

## Hashing

```python
from mojo.helpers.crypto import hash

hashed = hash.hash("password123", salt="optional-salt")
```

SHA-256 one-way hash. Use for passwords, tokens, and verification codes.

When no `salt` is passed, the salt defaults to `SECRET_KEY`. These hashes are
**stored lookup keys** (e.g. an `ssn_hash` column queried by equality), so
`SECRET_KEY_FALLBACKS` cannot help them across a key rotation — see
[SECRET_KEY rotation](#secret_key-rotation-secret_key_fallbacks) below.

## HMAC Signing

```python
from mojo.helpers.crypto import sign

# Generate signature
signature = sign.generate_signature(data, secret_key="my-key")

# Verify signature (constant-time compare)
is_valid = sign.verify_signature(data, signature, secret_key="my-key")
```

Use for webhook payloads, API request signing, and tamper detection.

### Webhook signing helpers

For signing outbound webhooks keyed on a Group secret, use the higher-level helpers instead of calling `generate_signature` directly:

```python
from mojo.helpers.crypto.sign import sign_for_group, get_signature_header

# sign_for_group auto-mints the Group's webhook secret on first use
body_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
sig = sign_for_group(group, body_bytes)
response.headers[get_signature_header()] = sig   # "X-Mojo-Signature" by default
```

`get_signature_header()` returns the effective signature header name —
`"X-Mojo-Signature"` by default, or the value of the `WEBHOOK_SIGNATURE_HEADER`
Django setting when an operator overrides it (e.g. to avoid advertising the
framework to receivers). Use the accessor, not the `WEBHOOK_SIGNATURE_HEADER`
module constant, when emitting or verifying so both sides honor the setting; the
constant remains the default string for back-compat.

Most webhook emission should go through `jobs.publish_webhook(group=...)`, which calls these helpers automatically. See [Webhook Signing](../account/webhook_signing.md).

## SECRET_KEY Rotation (`SECRET_KEY_FALLBACKS`)

Django's `SECRET_KEY_FALLBACKS` only covers Django's own signing (sessions,
signed cookies, password-reset tokens). Mojo's crypto derives from
`SECRET_KEY` directly, so mojo honors the same setting through its own
accessor:

```python
from mojo.helpers.crypto import keys as crypto_keys

crypto_keys.secret_keys()   # [SECRET_KEY, *SECRET_KEY_FALLBACKS] — primary first
```

Verify/unwrap paths iterate the list; sign/wrap paths always use
`secret_keys()[0]`. **A fallback is never used to sign, wrap, or issue** —
otherwise a rotation would never complete, because new material would keep
being produced under the old key. Both values are read **file-based only**
(`settings.get_static`): a DB-settable fallback list would be a
runtime-injectable key-acceptance list.

### The rotation procedure

1. Set the new `SECRET_KEY` in your settings file and move the old value into
   `SECRET_KEY_FALLBACKS`:

   ```python
   SECRET_KEY = "<new key>"
   SECRET_KEY_FALLBACKS = ["<old key>"]
   ```

2. Deploy. Everything issued under the old key keeps verifying; everything new
   is produced under the new key.
3. Once material signed under the old key has expired (bouncer tokens and pass
   cookies age out on their TTLs; filevault files re-wrap only when
   re-uploaded), remove the fallback entry.

### What the fallbacks cover

- **Bouncer auth tokens** — verification tries each candidate key; issuance
  uses the primary.
- **Bouncer pass cookies** (`mbp`) — same.
- **filevault** — `unwrap_ekey` and download-token validation try each
  candidate; wrapping and token minting use the primary. Files wrapped under
  the old key stay downloadable indefinitely while the old key remains in
  `SECRET_KEY_FALLBACKS` (they are **not** re-wrapped automatically — a
  re-encryption pass is the consumer project's job if it wants to retire the
  fallback).
- **Django's own signing** — sessions, signed cookies, password-reset tokens
  (Django handles these itself).

### What they cannot cover

- **`crypto.hash.hash()` with the default salt** — its output is a stored
  lookup key queried by equality; you cannot look a value up N ways and call
  it a match. Rotating `SECRET_KEY` changes what the same input hashes to, so
  any column storing these digests needs a **re-hash data migration** in the
  consumer repo. The fallback mechanism deliberately does not pretend to help
  here.
- **Write-only fingerprints salted with `SECRET_KEY`** (e.g. dnsman's
  registrant-contact fingerprint on purchase ledger rows) — markers recorded
  before the rotation will not match markers computed after it. Nothing
  breaks, but cross-rotation equality comparisons are void.
- **Per-Group/per-User secrets** (webhook secrets, user auth keys) — these are
  DB values independent of `SECRET_KEY`; a rotation does not touch them.

## Asymmetric (Public/Private Key) Encryption

```python
from mojo.helpers.crypto.privpub import hybrid

enc = hybrid.PrivatePublicEncryption()

# Encrypt with public key
encrypted = enc.encrypt(plaintext, public_key)

# Decrypt with private key
plaintext = enc.decrypt(encrypted, private_key)
```

Use for end-to-end encryption and secure key exchange.

## MojoSecrets (Model-Level Encryption)

For storing encrypted data on model instances, use `MojoSecrets` rather than calling `crypto` directly. See [MojoModel](../core/mojo_model.md#mojosecrets).

```python
# Preferred — use MojoSecrets on your model
integration.set_secret("api_key", "sk-abc123")
key = integration.get_secret("api_key")
```

## KMS Secrets (AWS KMS)

For AWS KMS-backed encryption:

```python
from mojo.models import KSMSecrets
```

`KSMSecrets` uses AWS Key Management Service for envelope encryption. Requires AWS credentials and KMS key configuration in settings. See [AWS docs](../email/README.md) for AWS setup.
