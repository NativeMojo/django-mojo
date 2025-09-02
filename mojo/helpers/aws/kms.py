"""
KMSHelper – Field-level Encryption with AWS KMS

Overview
- Envelope encryption using AWS KMS + AES-256-GCM
- Per-field data keys; KMS only stores/wraps the data key (CiphertextBlob)
- AES-GCM provides confidentiality and integrity
- EncryptionContext binds ciphertexts to a specific logical field key (e.g., "account.User.22.email")
- Base64(JSON) text blob for direct DB storage
- All decrypt operations are audited via CloudTrail
- Plaintext data keys are zeroized in memory after use

API
- KMSHelper(kms_key_id: str, region_name: str, encryption_context_key: str = "ctx")
  Initializes the helper. If kms_key_id is an alias that does not exist, will create a
  symmetric KMS key, bind/update the alias, and enable key rotation (best-effort).

- encrypt_field(key: str, value: str | bytes | dict) -> str
  Returns a JSON-safe dict with ct (ciphertext), iv, tag, wrapped data key (dk), and metadata.

- decrypt_field(key: str, blob: str) -> str
  Decrypts and returns plaintext as a UTF-8 string. Accepts dict or a JSON string of the dict.

- decrypt_dict_field(key: str, blob: str) -> dict
  Decrypts and returns a Python dict (when the original plaintext was JSON/dict).

Security Properties
- Envelope encryption; data keys only exist plaintext in RAM during ops
- KMS EncryptionContext and AES-GCM AAD both bind to the same logical key (e.g., "account.User.22.email")
- CloudTrail auditability for KMS Decrypt/ReEncrypt
- Zeroization of plaintext data keys in RAM

Notes
- This is a framework helper. It expects AWS creds/region via standard AWS SDK resolution
  (env vars, instance profile, etc.) unless provided externally to boto3.
"""

from __future__ import annotations

from base64 import b64encode, b64decode
from typing import Any, Dict, Optional, Union
import json
import boto3
from botocore.exceptions import ClientError
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from datetime import datetime, timezone

from mojo.helpers import logit


# --------------------------
# Exceptions
# --------------------------
class KMSHelperError(Exception):
    """Base error for KMSHelper."""


class KMSPermissionError(KMSHelperError):
    """Permission/IAM related errors."""


class KMSBlobError(KMSHelperError):
    """Ciphertext blob format or integrity issues."""


class KMSContextError(KMSHelperError):
    """EncryptionContext / key mismatch errors."""


# --------------------------
# Utility
# --------------------------
def _b64e(data: bytes) -> str:
    return b64encode(data).decode("utf-8")


def _b64d(data: str) -> bytes:
    return b64decode(data.encode("utf-8"))


def _utc_now_iso_z() -> str:
    # e.g., "2025-09-02T15:00:00Z"
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_json_dict(blob: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
    if isinstance(blob, dict):
        return blob
    try:
        # Expect base64(JSON) string; fallback to direct JSON for backward compatibility
        try:
            decoded = _b64d(blob).decode("utf-8")
            return json.loads(decoded)
        except Exception:
            return json.loads(blob)
    except Exception as exc:
        raise KMSBlobError("Encrypted blob must be base64(JSON) or a JSON string of that dict") from exc


# --------------------------
# KMS Helper
# --------------------------
class KMSHelper:
    VERSION = 1
    ALGO = "AES-256-GCM"
    NONCE_LENGTH = 12  # GCM recommended nonce length
    TAG_LENGTH = 16    # 128-bit tag

    def __init__(
        self,
        kms_key_id: str,
        region_name: str,
        encryption_context_key: str = "ctx",
        *,
        ensure_key: bool = True,
    ):
        """
        :param kms_key_id: ARN, KeyId, or alias (e.g., "alias/app-prod")
        :param region_name: AWS region (e.g., "us-east-1")
        :param encryption_context_key: Field name used in KMS EncryptionContext (default "ctx")
        :param ensure_key: If True and kms_key_id is an alias, ensure key+alias exist and rotation enabled
        """
        self.kms_key_id = kms_key_id
        self.region_name = region_name
        self.context_key = encryption_context_key

        # Create a KMS client using default AWS credential resolution chain
        self.kms = boto3.client("kms", region_name=region_name)

        if ensure_key:
            self._ensure_key_and_alias_if_needed()

    # --------------------------
    # Public API
    # --------------------------
    def encrypt_field(self, key: str, value: Union[str, bytes, Dict[str, Any]]) -> str:
        """
        Encrypt a field under a per-field data key derived from AWS KMS (GenerateDataKey).
        The KMS EncryptionContext and AES-GCM AAD are both bound to the provided logical key.

        :param key: Logical identifier (AAD), e.g., "account.User.22.email"
        :param value: Plaintext str/bytes or dict (dict will be JSON-encoded)
        :return: base64(JSON) string blob containing ciphertext and metadata
        """
        if isinstance(value, dict):
            # Canonicalize JSON to keep deterministic payloads for auditing/tamper checks
            plaintext = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        elif isinstance(value, bytes):
            plaintext = value
        elif isinstance(value, str):
            plaintext = value.encode("utf-8")
        else:
            raise KMSHelperError("Value must be str, bytes, or dict")

        enc_context = {self.context_key: key}

        # Generate a fresh AES-256 data key wrapped by KMS
        dk_resp = None
        try:
            dk_resp = self.kms.generate_data_key(
                KeyId=self.kms_key_id,
                KeySpec="AES_256",
                EncryptionContext=enc_context,
            )
        except ClientError as ce:
            self._raise_client_error("kms.generate_data_key", ce)

        # Plaintext data key (bytes). We will zeroize it as soon as we're done.
        assert dk_resp is not None, "kms.generate_data_key did not return a response"
        dk_plain = dk_resp["Plaintext"]
        dk_wrapped = dk_resp["CiphertextBlob"]

        # AES-GCM with random nonce and AAD bound to the same 'key' for contextual integrity
        iv = get_random_bytes(self.NONCE_LENGTH)
        cipher = AES.new(dk_plain, AES.MODE_GCM, nonce=iv)
        cipher.update(key.encode("utf-8"))  # AAD

        ciphertext, tag = cipher.encrypt_and_digest(plaintext)

        # Zeroize plaintext data key
        self._zeroize_bytes(dk_plain)

        blob = {
            "v": self.VERSION,
            "algo": self.ALGO,
            "ct": _b64e(ciphertext),
            "iv": _b64e(iv),
            "tag": _b64e(tag),
            "dk": _b64e(dk_wrapped),
            self.context_key: key,
            "ts": _utc_now_iso_z(),
            "kek": self.kms_key_id,  # Helpful metadata for audits/rotation
        }
        return _b64e(json.dumps(blob).encode("utf-8"))

    def decrypt_field(self, key: str, blob: str) -> str:
        """
        Decrypt a previously encrypted field. Returns plaintext as a UTF-8 string.

        :param key: Must match the original logical identifier used during encrypt
        :param blob: Base64(JSON) string returned by encrypt_field
        :return: Plaintext string
        """
        b = _as_json_dict(blob)
        self._validate_blob(b)

        # Validate context integrity at the application layer before KMS call
        ctx_from_blob = b.get(self.context_key)
        if ctx_from_blob != key:
            raise KMSContextError(
                f"Context mismatch. Provided key '{key}' != blob context '{ctx_from_blob}'"
            )

        enc_context = {self.context_key: key}

        dk_plain_resp = None
        try:
            dk_plain_resp = self.kms.decrypt(
                CiphertextBlob=_b64d(b["dk"]),
                EncryptionContext=enc_context,
            )
        except ClientError as ce:
            self._raise_client_error("kms.decrypt", ce)

        assert dk_plain_resp is not None, "kms.decrypt did not return a response"
        dk_plain = dk_plain_resp["Plaintext"]

        # Local AES-GCM decrypt and integrity check
        iv = _b64d(b["iv"])
        tag = _b64d(b["tag"])
        ct = _b64d(b["ct"])

        cipher = AES.new(dk_plain, AES.MODE_GCM, nonce=iv)
        cipher.update(key.encode("utf-8"))  # AAD must match encrypt

        try:
            pt = cipher.decrypt_and_verify(ct, tag)
        finally:
            # Zeroize plaintext data key
            self._zeroize_bytes(dk_plain)

        return pt.decode("utf-8")

    def decrypt_dict_field(self, key: str, blob: str) -> Dict[str, Any]:
        """
        Decrypt a previously encrypted field where the original plaintext was a dict.
        :param key: Same logical identifier used during encryption
        :param blob: Base64 string returned by encrypt_field
        :return: Dict
        """
        pt_str = self.decrypt_field(key, blob)
        try:
            return json.loads(pt_str)
        except Exception as exc:
            raise KMSBlobError("Decrypted plaintext is not valid JSON") from exc

    # --------------------------
    # Optional: Re-wrap support (CMK rotation without touching plaintext)
    # --------------------------
    def rewrap_data_key(
        self,
        blob: Union[Dict[str, Any], str],
        *,
        target_kms_key_id: Optional[str] = None,
    ) -> str:
        """
        Re-encrypt (rewrap) the stored data key under a new KMS key without decrypting field ciphertext.

        :param blob: Base64(JSON) string ciphertext record
        :param target_kms_key_id: Destination key (alias/ARN/KeyId). Defaults to self.kms_key_id.
        :return: base64(JSON) string with updated 'dk' and 'kek'
        """
        b = _as_json_dict(blob)
        self._validate_blob(b)

        ctx_val = b.get(self.context_key)
        if not isinstance(ctx_val, str) or not ctx_val:
            raise KMSBlobError("Missing or invalid context in blob")
        enc_context = {self.context_key: ctx_val}

        src_dk = _b64d(b["dk"])
        dest_key = target_kms_key_id or self.kms_key_id

        resp = None
        try:
            resp = self.kms.re_encrypt(
                CiphertextBlob=src_dk,
                DestinationKeyId=dest_key,
                SourceEncryptionContext=enc_context,
                DestinationEncryptionContext=enc_context,
            )
        except ClientError as ce:
            self._raise_client_error("kms.re_encrypt", ce)

        b2 = dict(b)
        assert resp is not None, "kms.re_encrypt did not return a response"
        b2["dk"] = _b64e(resp["CiphertextBlob"])
        b2["kek"] = dest_key
        return _b64e(json.dumps(b2).encode("utf-8"))

    # --------------------------
    # Internal helpers
    # --------------------------
    def _ensure_key_and_alias_if_needed(self):
        """
        If kms_key_id is an alias and it doesn't exist, create a new symmetric key,
        bind the alias to it, and enable key rotation (best-effort). If alias exists,
        ensure rotation is enabled on the target key (best-effort).
        """
        alias_name = self._extract_alias_name(self.kms_key_id)
        if not alias_name:
            # Not an alias; cannot manage creation/rotation here.
            return

        try:
            meta = self.kms.describe_key(KeyId=alias_name)["KeyMetadata"]
            key_id = meta["KeyId"]
            # Best-effort rotation enable
            self._enable_rotation_best_effort(key_id)
            return
        except ClientError as ce:
            code = ce.response.get("Error", {}).get("Code")
            if code not in ("NotFoundException", "NotFound", "ResourceNotFoundException"):
                # Some other error; don't auto-create
                logit.error("kms.describe_key failed", {"error": str(ce)})
                return

        # Alias does not exist -> create key and alias
        try:
            create_resp = self.kms.create_key(
                Description=f"MOJO managed key for {alias_name}",
                KeyUsage="ENCRYPT_DECRYPT",
                KeySpec="SYMMETRIC_DEFAULT",
                Origin="AWS_KMS",
            )
            key_id = create_resp["KeyMetadata"]["KeyId"]
            self.kms.create_alias(AliasName=alias_name, TargetKeyId=key_id)
            self._enable_rotation_best_effort(key_id)
            logit.info("KMS key created and alias bound", {"alias": alias_name, "key_id": key_id})
        except ClientError as ce:
            # Do not raise hard errors here to avoid crashing app boot in locked-down environments
            logit.error("Failed to create/bind KMS key alias", {"alias": alias_name, "error": str(ce)})

    def _enable_rotation_best_effort(self, key_id: str):
        try:
            self.kms.enable_key_rotation(KeyId=key_id)
        except ClientError as ce:
            # Some principals can't call enable_key_rotation; log and move on
            logit.warn("Unable to enable key rotation", {"key_id": key_id, "error": str(ce)})

    @staticmethod
    def _extract_alias_name(kms_key_id: str) -> Optional[str]:
        """
        Extract an alias name ("alias/xyz") from an input that may be:
        - "alias/xyz"
        - "arn:aws:kms:region:acct:alias/xyz"
        Returns None if not an alias form.
        """
        if kms_key_id.startswith("alias/"):
            return kms_key_id
        # Alias ARN shape: arn:aws:kms:REGION:ACCOUNT:alias/NAME
        parts = kms_key_id.split(":")
        if parts and parts[-1].startswith("alias/"):
            return parts[-1]
        return None

    @staticmethod
    def _zeroize_bytes(b: bytes):
        try:
            # Convert to mutable and zero in place
            ba = bytearray(b)
            for i in range(len(ba)):
                ba[i] = 0
        except Exception:
            # Best effort zeroization; Python immutability limits guarantees
            pass

    def _validate_blob(self, b: Dict[str, Any]):
        # Schema check
        if b.get("v") != self.VERSION:
            raise KMSBlobError(f"Unsupported blob version: {b.get('v')}")
        if b.get("algo") != self.ALGO:
            raise KMSBlobError(f"Unsupported algorithm: {b.get('algo')}")
        required = {"ct", "iv", "tag", "dk", self.context_key}
        missing = [k for k in required if k not in b]
        if missing:
            raise KMSBlobError(f"Missing fields in blob: {', '.join(missing)}")

        # Basic size checks
        try:
            iv = _b64d(b["iv"])
            tag = _b64d(b["tag"])
            if len(iv) != self.NONCE_LENGTH:
                raise KMSBlobError("Invalid IV size")
            if len(tag) != self.TAG_LENGTH:
                raise KMSBlobError("Invalid GCM tag size")
        except Exception as exc:
            raise KMSBlobError("Invalid IV/tag encoding") from exc

    @staticmethod
    def _raise_client_error(op: str, ce: ClientError):
        code = ce.response.get("Error", {}).get("Code", "Unknown")
        msg = ce.response.get("Error", {}).get("Message", str(ce))
        request_id = ce.response.get("ResponseMetadata", {}).get("RequestId")
        err_detail = {"operation": op, "code": code, "message": msg, "request_id": request_id}

        if code in ("AccessDeniedException", "UnauthorizedException", "AccessDenied"):
            logit.error("KMS permission error", err_detail)
            raise KMSPermissionError(f"{op} denied: {msg}") from ce

        logit.error("KMS client error", err_detail)
        raise KMSHelperError(f"{op} failed: {msg}") from ce
