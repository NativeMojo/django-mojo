import base64
import hashlib
import hmac as _hmac
import json
import uuid
from datetime import timedelta

from mojo.helpers import dates
from mojo.helpers.redis import get_connection
from mojo.helpers.settings import settings

_NONCE_PREFIX = 'bouncer:nonce:'


def _redis():
    return get_connection()


def _get_signing_key():
    """Derive a signing key from SECRET_KEY + label."""
    secret = settings.SECRET_KEY.encode('utf-8')
    label = settings.get_static('BOUNCER_TOKEN_SIGNING_KEY_LABEL', 'bouncer-token-signing')
    if isinstance(label, str):
        label = label.encode('utf-8')
    return _hmac.new(secret, label, hashlib.sha256).digest()


def _b64url_encode(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(s):
    padding = 4 - len(s) % 4
    if padding != 4:
        s += '=' * padding
    return base64.urlsafe_b64decode(s.encode('ascii'))


class TokenManager:
    """
    Signs, validates, and consumes single-use bouncer tokens.

    Token format: base64url(JSON payload) + "." + base64url(HMAC-SHA256 signature)

    Payload: duid, fingerprint_id, ip, risk_score, page_type,
             issued_at, expires_at, nonce.

    Nonces stored in Redis for single-use enforcement.
    """

    @classmethod
    def issue(cls, duid, fingerprint_id, ip, risk_score, page_type):
        """Generate a signed bouncer token and store its nonce in Redis."""
        ttl = settings.get_static('BOUNCER_TOKEN_TTL', 900)
        now = dates.utcnow()
        nonce = uuid.uuid4().hex

        payload = {
            'duid': str(duid),
            'fingerprint_id': fingerprint_id or '',
            'ip': ip,
            'risk_score': risk_score,
            'page_type': page_type,
            'issued_at': int(now.timestamp()),
            'expires_at': int((now + timedelta(seconds=ttl)).timestamp()),
            'nonce': nonce,
        }

        payload_b64 = _b64url_encode(json.dumps(payload, separators=(',', ':')))
        sig = _hmac.new(_get_signing_key(), payload_b64.encode('ascii'), hashlib.sha256).digest()
        _redis().setex(f"{_NONCE_PREFIX}{nonce}", ttl + 30, '1')
        return f"{payload_b64}.{_b64url_encode(sig)}"

    @classmethod
    def validate(cls, token_str, request_ip, request_duid=None):
        """
        Verify signature, expiry, IP, and duid. Does NOT consume the token.
        Returns decoded payload dict on success. Raises ValueError on failure.
        """
        if not token_str or '.' not in token_str:
            raise ValueError('invalid_format')

        payload_b64, sig_b64 = token_str.split('.', 1)

        expected_sig = _hmac.new(
            _get_signing_key(), payload_b64.encode('ascii'), hashlib.sha256
        ).digest()
        try:
            provided_sig = _b64url_decode(sig_b64)
        except Exception:
            raise ValueError('invalid_format')

        if not _hmac.compare_digest(expected_sig, provided_sig):
            raise ValueError('invalid_signature')

        try:
            payload = json.loads(_b64url_decode(payload_b64))
        except Exception:
            raise ValueError('invalid_format')

        if int(dates.utcnow().timestamp()) > payload.get('expires_at', 0):
            raise ValueError('expired')

        if payload.get('ip') != request_ip:
            raise ValueError('ip_mismatch')

        if request_duid and payload.get('duid') and payload.get('duid') != str(request_duid):
            raise ValueError('duid_mismatch')

        return payload

    @classmethod
    def consume(cls, nonce):
        """Mark nonce as used. Returns False if already consumed or expired."""
        return bool(_redis().delete(f"{_NONCE_PREFIX}{nonce}"))

    @classmethod
    def validate_and_consume(cls, token_str, request_ip, request_duid=None):
        """Atomic validate + consume. Use this in production endpoints."""
        payload = cls.validate(token_str, request_ip, request_duid)
        nonce = payload.get('nonce')
        if not nonce or not cls.consume(nonce):
            raise ValueError('nonce_consumed')
        return payload
