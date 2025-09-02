### KMSHelper – Field-level Encryption with AWS KMS

### Overview

KMSHelper provides a simple interface for encrypting and decrypting sensitive fields before storing them in a database such as AWS RDS. It uses AWS KMS (backed by HSMs) for secure key generation and wrapping, and AES-256-GCM for local encryption with integrity protection.

This design follows AWS’s envelope encryption pattern:
	•	A data key is generated via AWS KMS.
	•	The data key is used locally to encrypt the field with AES-GCM.
	•	The data key itself is stored only as a KMS CiphertextBlob (cannot be unwrapped without KMS).
	•	On decryption, the helper calls KMS to unwrap the data key, then uses it to decrypt the field.

By auto-deriving Additional Authenticated Data (AAD) from the field identifier ("account.User.22.email"), ciphertexts are cryptographically bound to their exact table/row/field. This prevents copy-and-paste attacks and enforces contextual integrity.



### Key Features

​	•	Per-field encryption → each value encrypted with its own unique AES-256 data key.
​	•	FIPS 140-2/3 alignment → keys are created and protected by AWS KMS/HSMs.
​	•	AES-GCM → provides both confidentiality and integrity.
​	•	Encryption context binding → field ciphertext is only valid for the original row/column.
​	•	Audit logging → every decrypt call is recorded in AWS CloudTrail.
​	•	Base64(JSON) text storage → ciphertext is returned as a base64-encoded JSON string suitable for TEXT/VARCHAR columns.
​	•	Key hygiene → plaintext data keys are zeroized in memory after use.



### API

KMSHelper(kms_key_id: str, region_name: str, encryption_context_key: str = "ctx")

Initialize the helper with:
	•	kms_key_id: ARN or alias of your KMS key (e.g., "alias/app-prod").
	•	region_name: AWS region (e.g., "us-east-1").
	•	encryption_context_key: field name used inside KMS EncryptionContext (default "ctx").

**IMPORTANT**: If the kms_key_id does not exist it will automatically **creates a symmetric KMS key**, **creates/updates the alias**, **enables annual rotation**.

⸻

encrypt_field(key: str, value: str|bytes|dict) -> str

Encrypt a field value.
	•	Parameters:
	•	key: logical identifier (also used as AAD), e.g. "account.User.22.email".
	•	value: the plaintext value (string or bytes).
	•	Returns: base64 with ciphertext, iv, tag, wrapped data key, and metadata.
	•	Store: Directly into the database.

⸻

decrypt_field(key: str, blob: str) -> str

Decrypt a previously encrypted field.
	•	Parameters:
	•	key: must be the same logical identifier used during encryption.
	•	blob: base64 returned by encrypt_field.
	•	Returns: plaintext as string

-----

decrypt_dict_field(key: str, blob: str) -> dict

Decrypt a previously encrypted field.
	•	Parameters:
	•	key: must be the same logical identifier used during encryption.
	•	blob: base64 returned by encrypt_field.
	•	Returns: dict

⸻

rewrap_data_key(blob: str, target_kms_key_id: str | None = None) -> str

Re-encrypt (rewrap) the stored data key under a new KMS key without touching field plaintext.
	•	blob: base64(JSON) produced by encrypt_field.
	•	target_kms_key_id: destination CMK (alias/ARN/KeyId). Defaults to the helper’s current key.
	•	Returns: base64(JSON) with updated dk (wrapped data key) and kek (key metadata).

⸻

#### Example Usage



```python	
from kms_helper import KMSHelper
kms = KMSHelper(kms_key_id="alias/app-prod", region_name="us-east-1")

# Encrypt and store as TEXT/VARCHAR
blob = kms.encrypt_field("account.User.22.email", "ian@example.com")
plaintext = kms.decrypt_field("account.User.22.email", blob)
print(plaintext)
"ian@example.com"

# Dict round-trip
blob = kms.encrypt_field("account.User.22.email", {"email":"ian@example.com"})
mydict = kms.decrypt_dict_field("account.User.22.email", blob)
print(mydict)
{"email":"ian@example.com"}

# Rotate KEK without plaintext (rewrap only)
blob_rotated = kms.rewrap_data_key(blob, target_kms_key_id="alias/app-rotated")
```

### Model Integration: KSMSecrets

Use the KMS-backed secrets model to store a single encrypted JSON blob per row using envelope encryption.

Requirements:
	•	settings.KMS_KEY_ID = "alias/your-key" (or KeyId/ARN)
	•	settings.AWS_REGION (or settings.AWS_DEFAULT_REGION)

Properties:
	•	Storage: base64(JSON) text stored in mojo_secrets
	•	Context binding: "app_label.ModelName.<pk>.mojo_secrets"
	•	Same API as MojoSecrets (set_secret/get_secret/set_secrets/clear_secrets)
	•	Same save semantics (first save to get pk, then persist secrets)

Example:

```python
from django.db import models
from mojo.models.secrets import KSMSecrets
from mojo.models import MojoModel

class CustomerSecret(KSMSecrets, MojoModel):
    name = models.CharField(max_length=200)

obj = CustomerSecret.objects.create(name="x")
obj.set_secret("api_token", "secret-token")
obj.save()
token = obj.get_secret("api_token")
```

### FAQ (quick answers for auditors)

•	Why AES-GCM server-side if devices use CBC/CTR? Field encryption is independent of device constraints; GCM gives integrity at rest.
•	What prevents row-swap attacks? The encryption context (ctx = "account.User.22.email") must match to decrypt.
•	What’s the blast radius of a DB leak? Without KMS Decrypt + correct context + IAM role, ciphertext is useless.
•	How do you rotate keys? Use KMSHelper.rewrap_data_key (KMS ReEncrypt) to rotate the KEK without touching plaintext; for full rotation, decrypt and re-encrypt with a new data key. Enable annual CMK rotation in AWS for the managing key.
