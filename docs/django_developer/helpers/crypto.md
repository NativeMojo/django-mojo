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

## HMAC Signing

```python
from mojo.helpers.crypto import sign

# Generate signature
signature = sign.generate_signature(data, secret_key="my-key")

# Verify signature
is_valid = sign.verify_signature(data, signature, secret_key="my-key")
```

Use for webhook payloads, API request signing, and tamper detection.

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
