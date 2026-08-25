"""AES encryption helpers.

Decrypt key derivations are memoized in a 512-entry, per-process LRU keyed by
the normalized PBKDF2 inputs. The cache retains password bytes, salts, and
derived keys until eviction or process exit; it never receives ciphertext
payloads or decrypted application data. Encryption derives uncached because
its fresh salt makes every write key a one-use value.

The LRU is thread-safe, but simultaneous first misses may compute the same
pure PBKDF2 result more than once before one result is cached.
"""

import json
from functools import lru_cache
from base64 import b64encode, b64decode
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Random import get_random_bytes
from Crypto.Util.py3compat import tobytes
from objict import objict
import mojo.errors
import hashlib

PBKDF2_ITERATIONS = 100_000
DERIVED_KEY_CACHE_SIZE = 512
SALT_LENGTH = 16
NONCE_LENGTH = 12
TAG_LENGTH = 16


def encrypt(data, password):
    if isinstance(data, dict):
        data = json.dumps(data)
    if not isinstance(data, str):
        raise mojo.errors.ValueException("Data must be a string or dictionary")

    data_bytes = data.encode('utf-8')
    salt = get_random_bytes(SALT_LENGTH)
    key = _derive_key_uncached(tobytes(password), tobytes(salt), 32)
    cipher = AES.new(key, AES.MODE_GCM, nonce=get_random_bytes(NONCE_LENGTH))

    ciphertext, tag = cipher.encrypt_and_digest(data_bytes)

    # Final payload: [salt | nonce | tag | ciphertext]
    payload = salt + cipher.nonce + tag + ciphertext
    return b64encode(payload).decode('utf-8')

def decrypt(enc_data_b64, password, ignore_errors=True):
    raw = b64decode(enc_data_b64)

    salt = raw[:SALT_LENGTH]
    nonce = raw[SALT_LENGTH:SALT_LENGTH + NONCE_LENGTH]
    tag = raw[SALT_LENGTH + NONCE_LENGTH:SALT_LENGTH + NONCE_LENGTH + TAG_LENGTH]
    ciphertext = raw[SALT_LENGTH + NONCE_LENGTH + TAG_LENGTH:]

    key = derive_key(password, salt)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)

    if ignore_errors:
        try:
            decrypted = cipher.decrypt_and_verify(ciphertext, tag)
        except ValueError:
            return None
    else:
        decrypted = cipher.decrypt_and_verify(ciphertext, tag)

    decrypted_str = decrypted.decode('utf-8')

    try:
        return objict.from_json(decrypted_str)
    except Exception:
        return decrypted_str


def _derive_key_uncached(password_bytes, salt_bytes, key_length):
    return PBKDF2(
        password_bytes,
        salt_bytes,
        dkLen=key_length,
        count=PBKDF2_ITERATIONS,
    )


@lru_cache(maxsize=DERIVED_KEY_CACHE_SIZE, typed=True)
def _derive_key_cached(password_bytes, salt_bytes, key_length):
    return _derive_key_uncached(password_bytes, salt_bytes, key_length)


def derive_key(password, salt, key_length=32):
    return _derive_key_cached(tobytes(password), tobytes(salt), key_length)


def decrypt_ecb(edata, key_str):
    key = hashlib.sha256(key_str.encode("utf-8")).digest()  # 32 bytes
    cipher = AES.new(key, AES.MODE_ECB)
    pt = cipher.decrypt(b64decode(edata))
    pad_len = pt[-1]
    return pt[:-pad_len].decode("utf-8")

def encrypt_ecb(data, key_str):
    key = hashlib.sha256(key_str.encode("utf-8")).digest()  # 32 bytes
    cipher = AES.new(key, AES.MODE_ECB)
    # PKCS7 pad
    pad_len = 16 - (len(data.encode("utf-8")) % 16)
    padded = data.encode("utf-8") + bytes([pad_len]) * pad_len
    ct = cipher.encrypt(padded)
    return b64encode(ct).decode("utf-8")

def calculate_kcv(key):
    """
    Calculate Key Check Value (KCV) using AES encryption.

    KCV is the first 3 bytes of AES-encrypting a zero block with the key.
    This matches the firmware's PSA Crypto implementation.
    """
    # Convert hex string to bytes
    key_bytes = bytes.fromhex(key)

    # Create AES cipher in ECB mode
    cipher = AES.new(key_bytes, AES.MODE_ECB)

    # Encrypt a zero block (16 bytes of 0x00)
    zero_block = b'\x00' * 16
    encrypted = cipher.encrypt(zero_block)

    # KCV is first 3 bytes
    return encrypted[:3].hex().upper()
