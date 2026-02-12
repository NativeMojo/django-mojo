"""
FileVault crypto utilities.

Pure functions for chunked AES-256-GCM encryption, key wrapping,
password hashing, and signed access tokens. No Django models or I/O.
"""

import hmac
import json
import hashlib
import struct
import time
from base64 import b64encode, b64decode, urlsafe_b64encode, urlsafe_b64decode

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Random import get_random_bytes

from mojo.helpers.crypto.utils import random_string

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VAULT_MAGIC = b"VF02"
VAULT_HEADER_SIZE = 64
VAULT_CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB
VAULT_KDF_ITERATIONS = 600_000
VAULT_SALT_LENGTH = 32
VAULT_NONCE_LENGTH = 12
VAULT_TAG_LENGTH = 16
VAULT_TOKEN_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# ekey generation
# ---------------------------------------------------------------------------

def generate_ekey():
    """Generate a 32-char random alphanumeric encryption key (~190 bits)."""
    return random_string(32, allow_special=False)


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def derive_aes_key(passphrase, salt):
    """Derive a 256-bit AES key from passphrase + salt via PBKDF2."""
    if isinstance(passphrase, str):
        passphrase = passphrase.encode("utf-8")
    return PBKDF2(passphrase, salt, dkLen=32, count=VAULT_KDF_ITERATIONS)


def derive_chunk_nonce(aes_key, chunk_index):
    """Derive a deterministic 12-byte nonce for a chunk index."""
    index_bytes = struct.pack(">I", chunk_index)
    digest = hmac.new(aes_key, index_bytes, hashlib.sha256).digest()
    return digest[:VAULT_NONCE_LENGTH]


# ---------------------------------------------------------------------------
# Chunk encrypt / decrypt
# ---------------------------------------------------------------------------

def encrypt_chunk(aes_key, chunk_index, plaintext):
    """
    Encrypt a single chunk with AES-256-GCM.

    Returns: nonce(12) + ciphertext + tag(16)
    """
    nonce = derive_chunk_nonce(aes_key, chunk_index)
    cipher = AES.new(aes_key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return nonce + ciphertext + tag


def decrypt_chunk(aes_key, chunk_index, chunk_data):
    """
    Decrypt a single chunk. Verifies nonce derivation and GCM auth tag.

    Raises ValueError on auth failure or nonce mismatch.
    """
    expected_nonce = derive_chunk_nonce(aes_key, chunk_index)
    nonce = chunk_data[:VAULT_NONCE_LENGTH]
    if nonce != expected_nonce:
        raise ValueError(f"Nonce mismatch for chunk {chunk_index}")
    tag = chunk_data[-VAULT_TAG_LENGTH:]
    ciphertext = chunk_data[VAULT_NONCE_LENGTH:-VAULT_TAG_LENGTH]
    cipher = AES.new(aes_key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag)


# ---------------------------------------------------------------------------
# File header
# ---------------------------------------------------------------------------

def build_header(chunk_size, kdf_salt, total_chunks):
    """
    Build a 64-byte file header.

    Layout: magic(4) + chunk_size(4) + salt(32) + total_chunks(4) + reserved(20)
    """
    header = bytearray(VAULT_HEADER_SIZE)
    header[0:4] = VAULT_MAGIC
    struct.pack_into(">I", header, 4, chunk_size)
    header[8:40] = kdf_salt
    struct.pack_into(">I", header, 40, total_chunks)
    # bytes 44-63 reserved (already zeroed)
    return bytes(header)


def parse_header(header_bytes):
    """
    Parse a 64-byte file header.

    Returns dict with chunk_size, kdf_salt, total_chunks.
    Raises ValueError on invalid magic or size.
    """
    if len(header_bytes) < VAULT_HEADER_SIZE:
        raise ValueError("Header too short")
    magic = header_bytes[0:4]
    if magic != VAULT_MAGIC:
        raise ValueError(f"Invalid magic: {magic!r}")
    chunk_size = struct.unpack_from(">I", header_bytes, 4)[0]
    kdf_salt = header_bytes[8:40]
    total_chunks = struct.unpack_from(">I", header_bytes, 40)[0]
    return {
        "chunk_size": chunk_size,
        "kdf_salt": kdf_salt,
        "total_chunks": total_chunks,
    }


# ---------------------------------------------------------------------------
# ekey wrapping (per-file, using SECRET_KEY + uuid)
# ---------------------------------------------------------------------------

def wrap_ekey(ekey, secret_key, file_uuid):
    """
    Wrap (encrypt) an ekey using a per-file passphrase derived from
    secret_key + file_uuid.

    Returns base64 string: [wrap_salt(32)][wrap_nonce(12)][ciphertext][tag(16)]
    """
    passphrase = f"{secret_key}{file_uuid}"
    wrap_salt = get_random_bytes(VAULT_SALT_LENGTH)
    wrap_key = derive_aes_key(passphrase, wrap_salt)
    wrap_nonce = get_random_bytes(VAULT_NONCE_LENGTH)
    cipher = AES.new(wrap_key, AES.MODE_GCM, nonce=wrap_nonce)
    ciphertext, tag = cipher.encrypt_and_digest(ekey.encode("utf-8"))
    payload = wrap_salt + wrap_nonce + ciphertext + tag
    return b64encode(payload).decode("utf-8")


def unwrap_ekey(wrapped_b64, secret_key, file_uuid):
    """
    Unwrap (decrypt) an ekey. Returns the plaintext ekey string.

    Raises ValueError on auth failure.
    """
    passphrase = f"{secret_key}{file_uuid}"
    raw = b64decode(wrapped_b64)
    wrap_salt = raw[:VAULT_SALT_LENGTH]
    wrap_nonce = raw[VAULT_SALT_LENGTH:VAULT_SALT_LENGTH + VAULT_NONCE_LENGTH]
    tag = raw[-VAULT_TAG_LENGTH:]
    ciphertext = raw[VAULT_SALT_LENGTH + VAULT_NONCE_LENGTH:-VAULT_TAG_LENGTH]
    wrap_key = derive_aes_key(passphrase, wrap_salt)
    cipher = AES.new(wrap_key, AES.MODE_GCM, nonce=wrap_nonce)
    plaintext = cipher.decrypt_and_verify(ciphertext, tag)
    return plaintext.decode("utf-8")


# ---------------------------------------------------------------------------
# Password hashing / verification
# ---------------------------------------------------------------------------

def hash_password(password):
    """
    Hash a password with PBKDF2-HMAC-SHA256.

    Returns base64 string: [salt(32)][hash(32)]
    """
    salt = get_random_bytes(VAULT_SALT_LENGTH)
    if isinstance(password, str):
        password = password.encode("utf-8")
    hashed = hashlib.pbkdf2_hmac(
        "sha256", password, salt, VAULT_KDF_ITERATIONS, dklen=32
    )
    return b64encode(salt + hashed).decode("utf-8")


def verify_password(password, stored_hash):
    """
    Verify a password against a stored PBKDF2 hash.
    Uses constant-time comparison.
    """
    if isinstance(password, str):
        password = password.encode("utf-8")
    raw = b64decode(stored_hash)
    salt = raw[:VAULT_SALT_LENGTH]
    expected = raw[VAULT_SALT_LENGTH:]
    computed = hashlib.pbkdf2_hmac(
        "sha256", password, salt, VAULT_KDF_ITERATIONS, dklen=32
    )
    return hmac.compare_digest(computed, expected)


# ---------------------------------------------------------------------------
# Access tokens (signed, IP-bound, stateless)
# ---------------------------------------------------------------------------

def generate_access_token(file_id, client_ip, secret_key, ttl=None):
    """
    Generate a signed, IP-bound download token.

    Returns: base64url(payload).base64url(signature)
    """
    if ttl is None:
        ttl = VAULT_TOKEN_TTL
    now = int(time.time())
    payload = json.dumps({
        "fid": file_id,
        "ip": client_ip,
        "exp": now + ttl,
        "iat": now,
    }, separators=(",", ":"), sort_keys=True)
    payload_b64 = urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8")
    if isinstance(secret_key, str):
        secret_key = secret_key.encode("utf-8")
    sig = hmac.new(secret_key, payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = urlsafe_b64encode(sig).decode("utf-8")
    return f"{payload_b64}.{sig_b64}"


def validate_access_token(token, client_ip, secret_key):
    """
    Validate a signed access token.

    Returns file_id on success, None on failure.
    """
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig_b64 = parts
        if isinstance(secret_key, str):
            secret_key = secret_key.encode("utf-8")
        expected_sig = hmac.new(
            secret_key, payload_b64.encode("utf-8"), hashlib.sha256
        ).digest()
        actual_sig = urlsafe_b64decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(urlsafe_b64decode(payload_b64).decode("utf-8"))
        if payload.get("ip") != client_ip:
            return None
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload.get("fid")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Encrypt / decrypt a full file (in-memory, returns bytes)
# ---------------------------------------------------------------------------

def encrypt_file(file_bytes, ekey, password=None, chunk_size=None):
    """
    Encrypt file bytes into the vault file format.

    Returns the complete encrypted blob (header + chunks).
    """
    if chunk_size is None:
        chunk_size = VAULT_CHUNK_SIZE
    kdf_salt = get_random_bytes(VAULT_SALT_LENGTH)
    passphrase = (password + ekey) if password else ekey
    aes_key = derive_aes_key(passphrase, kdf_salt)

    # split into chunks
    chunks = []
    offset = 0
    while offset < len(file_bytes):
        chunks.append(file_bytes[offset:offset + chunk_size])
        offset += chunk_size
    if not chunks:
        chunks = [b""]

    header = build_header(chunk_size, kdf_salt, len(chunks))
    parts = [header]
    for i, chunk in enumerate(chunks):
        parts.append(encrypt_chunk(aes_key, i, chunk))

    return b"".join(parts)


def decrypt_file(encrypted_blob, ekey, password=None):
    """
    Decrypt a vault file format blob.

    Returns the original plaintext bytes.
    """
    header_info = parse_header(encrypted_blob[:VAULT_HEADER_SIZE])
    chunk_size = header_info["chunk_size"]
    kdf_salt = header_info["kdf_salt"]
    total_chunks = header_info["total_chunks"]

    passphrase = (password + ekey) if password else ekey
    aes_key = derive_aes_key(passphrase, kdf_salt)

    # each encrypted chunk = nonce(12) + ciphertext(<=chunk_size) + tag(16)
    parts = []
    pos = VAULT_HEADER_SIZE
    for i in range(total_chunks):
        # determine chunk data length
        if i < total_chunks - 1:
            chunk_data_len = VAULT_NONCE_LENGTH + chunk_size + VAULT_TAG_LENGTH
        else:
            chunk_data_len = len(encrypted_blob) - pos
        chunk_data = encrypted_blob[pos:pos + chunk_data_len]
        parts.append(decrypt_chunk(aes_key, i, chunk_data))
        pos += chunk_data_len

    return b"".join(parts)
