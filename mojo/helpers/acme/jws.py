"""JOSE/JWS primitives for ACMEv2 (RFC 8555), ES256 only.

The flattened JSON JWS is assembled by hand rather than through PyJWT's
``PyJWS.encode`` on purpose: PyJWT injects a ``typ`` member into the protected
header, and RFC 8555 section 6.2 is strict about what a request's protected
header may carry -- ``alg``, ``nonce``, ``url`` and exactly one of ``jwk`` /
``kid``. A CA that validates the header rejects the extra member.

Only ES256 over NIST P-256 is supported. It is accepted by every public ACME
CA and is the only key type dnsman ever generates, so there is no reason to
carry an algorithm-negotiation layer.

This module deliberately imports nothing from Django (or from any mojo module
that needs a configured project), so it can be exercised standalone.
"""
import hashlib
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.utils import base64url_decode, base64url_encode, der_to_raw_signature


ALG = "ES256"
CRV = "P-256"
# P-256 field size: x and y are always encoded as fixed-width 32-byte integers
# (RFC 7518 section 6.2.1.2) -- left-padded, never trimmed.
COORD_BYTES = 32


def b64(data):
    """base64url-encode bytes (or utf-8 text) with the padding stripped."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64url_encode(data).decode("ascii")


def b64_decode(value):
    """Decode a padding-less base64url string back to bytes."""
    if isinstance(value, str):
        value = value.encode("ascii")
    return base64url_decode(value)


def generate_key():
    """Generate a fresh P-256 private key (account keys and cert keys alike)."""
    return ec.generate_private_key(ec.SECP256R1())


def load_key(pem):
    """Load an unencrypted PEM private key, refusing anything that is not P-256."""
    if isinstance(pem, str):
        pem = pem.encode("utf-8")
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("ACME keys must be EC private keys")
    if key.curve.name != "secp256r1":
        raise ValueError(f"ACME keys must be P-256 (secp256r1), got {key.curve.name}")
    return key


def dump_key(key):
    """Serialize a private key to an unencrypted PKCS8 PEM string.

    Callers are expected to hand the result straight to KMS-backed secrets --
    it is plaintext key material and must never be logged or graphed.
    """
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode("utf-8")


def public_key(key):
    """Accept either a private or a public key and return the public half."""
    if isinstance(key, ec.EllipticCurvePublicKey):
        return key
    return key.public_key()


def jwk(key):
    """Return the public JWK for a P-256 key (RFC 7518 section 6.2.1)."""
    numbers = public_key(key).public_numbers()
    return {
        "kty": "EC",
        "crv": CRV,
        "x": b64(numbers.x.to_bytes(COORD_BYTES, "big")),
        "y": b64(numbers.y.to_bytes(COORD_BYTES, "big"))
    }


def thumbprint_json(key):
    """The exact JSON string hashed for the RFC 7638 thumbprint.

    Required members only, lexicographic order, no whitespace. Exposed so the
    canonical form is directly assertable in tests -- getting the ordering or
    the separators wrong silently produces a thumbprint the CA disagrees with,
    and the only symptom is every DNS-01 validation failing.
    """
    members = jwk(key)
    required = {
        "crv": members["crv"],
        "kty": members["kty"],
        "x": members["x"],
        "y": members["y"]
    }
    return json.dumps(required, separators=(",", ":"), sort_keys=True)


def thumbprint(key):
    """RFC 7638 JWK thumbprint: base64url(SHA-256(canonical JWK JSON))."""
    digest = hashlib.sha256(thumbprint_json(key).encode("utf-8")).digest()
    return b64(digest)


def key_authorization(token, key):
    """RFC 8555 section 8.1 key authorization: ``token || '.' || thumbprint``."""
    return f"{token}.{thumbprint(key)}"


def dns_txt_value(token, key):
    """The DNS-01 TXT value: base64url(SHA-256(key authorization))."""
    digest = hashlib.sha256(key_authorization(token, key).encode("utf-8")).digest()
    return b64(digest)


def encode_payload(payload):
    """base64url the JWS payload.

    ``None`` and ``""`` both encode to the empty payload, which is how RFC 8555
    section 6.3 POST-as-GET requests are formed. An empty JSON object must be
    passed as ``{}`` -- it is a real payload and is NOT the same request.
    """
    if payload is None or payload == "":
        return ""
    if isinstance(payload, bytes):
        return b64(payload)
    if isinstance(payload, str):
        return b64(payload)
    return b64(json.dumps(payload, separators=(",", ":")))


def sign(payload, key, protected_header):
    """Build the flattened JSON JWS for an ACME request.

    Returns ``{"protected": ..., "payload": ..., "signature": ...}`` --
    the exact body POSTed as ``application/jose+json``.
    """
    protected_b64 = b64(json.dumps(protected_header, separators=(",", ":")))
    payload_b64 = encode_payload(payload)
    signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
    der_signature = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    raw_signature = der_to_raw_signature(der_signature, key.curve)
    return {
        "protected": protected_b64,
        "payload": payload_b64,
        "signature": b64(raw_signature)
    }
