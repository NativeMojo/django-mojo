import hmac
import hashlib
import json
from django.conf import settings


def generate_signature(data, secret_key=settings.SECRET_KEY):
    """
    Generate an HMAC-SHA256 signature for the given data using the secret key.

    :param data: str, bytes, or dict - the data to sign
    :param secret_key: str or bytes - the shared secret key
    :return: str - the hex signature
    """
    if isinstance(data, dict):
        data = json.dumps(data, separators=(',', ':'), sort_keys=True)
    if isinstance(data, str):
        data = data.encode('utf-8')
    if isinstance(secret_key, str):
        secret_key = secret_key.encode('utf-8')

    signature = hmac.new(secret_key, data, hashlib.sha256).hexdigest()
    return signature


def verify_signature(data, signature, secret_key=settings.SECRET_KEY):
    """
    Verify an HMAC-SHA256 signature.

    :param data: str, bytes, or dict - the original data
    :param signature: str - the provided hex signature
    :param secret_key: str or bytes - the shared secret key
    :return: bool - True if valid, False otherwise
    """
    expected_signature = generate_signature(data, secret_key)
    return hmac.compare_digest(expected_signature, signature)
