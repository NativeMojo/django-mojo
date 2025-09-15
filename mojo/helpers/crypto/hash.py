from Crypto.Hash import SHA256
from Crypto.Random import get_random_bytes
import hmac
import hashlib


def hash(value, salt=None):
    """
    Returns a SHA-256 hash of the input value (string, int, or dict), optionally salted.

    :param value: str, int, or dict - the input to be hashed
    :param salt: Optional[str or bytes] - a salt to strengthen the hash
    :return: str - the hex digest of the hash
    """
    if salt is None:
        from django.conf import settings
        salt = settings.SECRET_KEY
    if isinstance(value, dict):
        # Sort the dictionary and prepare a string representation
        value_str = str(sorted(value.items())).encode('utf-8')
    elif isinstance(value, (str, int)):
        value_str = str(value).encode('utf-8')
    else:
        raise TypeError("Only strings, integers, or dictionaries are allowed.")

    # Use provided salt or generate one
    if salt is None:
        salt = get_random_bytes(16)
    elif isinstance(salt, str):
        salt = salt.encode('utf-8')

    # Combine salt and value
    hasher = hashlib.sha256()
    hasher.update(salt + value_str)
    return hasher.hexdigest()


def hash_digits(digits, secret_key):
    """Hashes the digits using a derived salt without storing it."""
    salt = derive_salt(digits, secret_key)
    hash_obj = hashlib.sha256(salt + digits.encode())
    return hash_obj.hexdigest()


def derive_salt(digits, secret_key):
    """Derives a salt from the last 8 digits of the DIGITs using HMAC."""
    last_8_digits = digits[-8:]
    if isinstance(secret_key, str):  # Ensure secret_key is bytes
        secret_key = secret_key.encode()
    return hmac.new(secret_key, last_8_digits.encode(), hashlib.sha256).digest()[:16]  # Use first 16 bytes


def hash_to_hex(input_string):
    if not isinstance(input_string, str):
        raise ValueError("Input must be a string")
    # Create a new SHA-256 hasher
    hasher = hashlib.sha256()
    # Update the hasher with the input string encoded to bytes
    hasher.update(input_string.encode('utf-8'))
    # Return the hexadecimal representation of the hash
    return hasher.hexdigest()
