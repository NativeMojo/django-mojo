import json
from base64 import b64encode, b64decode
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Random import get_random_bytes
from objict import objict
import mojo.errors
import hashlib

PBKDF2_ITERATIONS = 100_000
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
    key = derive_key(password, salt)
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


def derive_key(password, salt, key_length=32):
    return PBKDF2(password, salt, dkLen=key_length, count=PBKDF2_ITERATIONS)


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
