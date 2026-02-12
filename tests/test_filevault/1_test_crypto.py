"""
Pure crypto unit tests for mojo.helpers.crypto.vault.

No Django required — exercises ekey generation, key wrapping,
chunk encryption, file format, password hashing, and access tokens.
"""

import time
from testit import helpers as th
from testit.helpers import assert_eq, assert_true


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@th.unit_setup()
def setup_crypto(opts):
    from mojo.helpers.crypto import vault as cv
    opts.cv = cv
    opts.secret_key = "test-secret-key-for-filevault-unit-tests"
    opts.file_uuid = "abc123def456"


# ---------------------------------------------------------------------------
# ekey generation
# ---------------------------------------------------------------------------

@th.unit_test("generate_ekey returns 32 alphanumeric chars")
def test_generate_ekey(opts):
    ekey = opts.cv.generate_ekey()
    assert_eq(len(ekey), 32, "ekey should be 32 characters")
    assert_true(ekey.isalnum(), "ekey should be alphanumeric")


@th.unit_test("generate_ekey produces unique keys")
def test_generate_ekey_unique(opts):
    keys = {opts.cv.generate_ekey() for _ in range(100)}
    assert_eq(len(keys), 100, "100 generated ekeys should all be unique")


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

@th.unit_test("derive_aes_key produces 32 bytes")
def test_derive_aes_key(opts):
    salt = opts.cv.get_random_bytes(32)
    key = opts.cv.derive_aes_key("test-passphrase", salt)
    assert_eq(len(key), 32, "AES key should be 32 bytes")


@th.unit_test("derive_aes_key is deterministic for same inputs")
def test_derive_aes_key_deterministic(opts):
    salt = b"fixed-salt-for-determinism-test!"  # 32 bytes
    k1 = opts.cv.derive_aes_key("passphrase", salt)
    k2 = opts.cv.derive_aes_key("passphrase", salt)
    assert_eq(k1, k2, "same passphrase + salt should produce same key")


@th.unit_test("derive_aes_key differs with different passphrase")
def test_derive_aes_key_differs(opts):
    salt = opts.cv.get_random_bytes(32)
    k1 = opts.cv.derive_aes_key("passphrase-a", salt)
    k2 = opts.cv.derive_aes_key("passphrase-b", salt)
    assert_true(k1 != k2, "different passphrases should produce different keys")


# ---------------------------------------------------------------------------
# Chunk nonce derivation
# ---------------------------------------------------------------------------

@th.unit_test("derive_chunk_nonce returns 12 bytes")
def test_derive_chunk_nonce(opts):
    key = opts.cv.get_random_bytes(32)
    nonce = opts.cv.derive_chunk_nonce(key, 0)
    assert_eq(len(nonce), 12, "chunk nonce should be 12 bytes")


@th.unit_test("derive_chunk_nonce differs per chunk index")
def test_derive_chunk_nonce_differs(opts):
    key = opts.cv.get_random_bytes(32)
    n0 = opts.cv.derive_chunk_nonce(key, 0)
    n1 = opts.cv.derive_chunk_nonce(key, 1)
    assert_true(n0 != n1, "different chunk indices should produce different nonces")


# ---------------------------------------------------------------------------
# Chunk encrypt / decrypt
# ---------------------------------------------------------------------------

@th.unit_test("encrypt_chunk then decrypt_chunk round-trips")
def test_chunk_roundtrip(opts):
    key = opts.cv.get_random_bytes(32)
    plaintext = b"Hello, FileVault chunk encryption!"
    encrypted = opts.cv.encrypt_chunk(key, 0, plaintext)
    decrypted = opts.cv.decrypt_chunk(key, 0, encrypted)
    assert_eq(decrypted, plaintext, "decrypted chunk should match original")


@th.unit_test("decrypt_chunk fails with wrong key")
def test_chunk_wrong_key(opts):
    key1 = opts.cv.get_random_bytes(32)
    key2 = opts.cv.get_random_bytes(32)
    encrypted = opts.cv.encrypt_chunk(key1, 0, b"secret data")
    try:
        opts.cv.decrypt_chunk(key2, 0, encrypted)
        assert False, "should have raised ValueError"
    except ValueError:
        pass  # expected


@th.unit_test("decrypt_chunk fails with wrong chunk index")
def test_chunk_wrong_index(opts):
    key = opts.cv.get_random_bytes(32)
    encrypted = opts.cv.encrypt_chunk(key, 0, b"secret data")
    try:
        opts.cv.decrypt_chunk(key, 1, encrypted)
        assert False, "should have raised ValueError for wrong chunk index"
    except ValueError:
        pass  # expected — nonce mismatch


# ---------------------------------------------------------------------------
# File header
# ---------------------------------------------------------------------------

@th.unit_test("build_header produces 64 bytes")
def test_build_header_size(opts):
    salt = opts.cv.get_random_bytes(32)
    header = opts.cv.build_header(5242880, salt, 10)
    assert_eq(len(header), 64, "header should be 64 bytes")


@th.unit_test("build_header / parse_header round-trips")
def test_header_roundtrip(opts):
    salt = opts.cv.get_random_bytes(32)
    header = opts.cv.build_header(5242880, salt, 42)
    parsed = opts.cv.parse_header(header)
    assert_eq(parsed["chunk_size"], 5242880, "chunk_size should survive round-trip")
    assert_eq(parsed["kdf_salt"], salt, "kdf_salt should survive round-trip")
    assert_eq(parsed["total_chunks"], 42, "total_chunks should survive round-trip")


@th.unit_test("parse_header rejects bad magic")
def test_header_bad_magic(opts):
    bad_header = b"XXXX" + b"\x00" * 60
    try:
        opts.cv.parse_header(bad_header)
        assert False, "should have raised ValueError for bad magic"
    except ValueError as e:
        assert_true("Invalid magic" in str(e), "error should mention invalid magic")


# ---------------------------------------------------------------------------
# ekey wrapping
# ---------------------------------------------------------------------------

@th.unit_test("wrap_ekey / unwrap_ekey round-trips")
def test_ekey_wrap_roundtrip(opts):
    ekey = opts.cv.generate_ekey()
    wrapped = opts.cv.wrap_ekey(ekey, opts.secret_key, opts.file_uuid)
    unwrapped = opts.cv.unwrap_ekey(wrapped, opts.secret_key, opts.file_uuid)
    assert_eq(unwrapped, ekey, "unwrapped ekey should match original")


@th.unit_test("unwrap_ekey fails with wrong secret_key")
def test_ekey_wrong_secret(opts):
    ekey = opts.cv.generate_ekey()
    wrapped = opts.cv.wrap_ekey(ekey, opts.secret_key, opts.file_uuid)
    try:
        opts.cv.unwrap_ekey(wrapped, "wrong-secret-key", opts.file_uuid)
        assert False, "should have raised ValueError"
    except ValueError:
        pass  # expected


@th.unit_test("unwrap_ekey fails with wrong uuid")
def test_ekey_wrong_uuid(opts):
    ekey = opts.cv.generate_ekey()
    wrapped = opts.cv.wrap_ekey(ekey, opts.secret_key, opts.file_uuid)
    try:
        opts.cv.unwrap_ekey(wrapped, opts.secret_key, "wrong-uuid")
        assert False, "should have raised ValueError"
    except ValueError:
        pass  # expected


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

@th.unit_test("hash_password / verify_password round-trips")
def test_password_roundtrip(opts):
    hashed = opts.cv.hash_password("my-secret-password")
    assert_true(opts.cv.verify_password("my-secret-password", hashed),
                "correct password should verify")


@th.unit_test("verify_password rejects wrong password")
def test_password_wrong(opts):
    hashed = opts.cv.hash_password("correct-password")
    assert_true(not opts.cv.verify_password("wrong-password", hashed),
                "wrong password should not verify")


@th.unit_test("hash_password produces different hashes for same password")
def test_password_unique_salts(opts):
    h1 = opts.cv.hash_password("same-password")
    h2 = opts.cv.hash_password("same-password")
    assert_true(h1 != h2, "different salts should produce different hashes")


# ---------------------------------------------------------------------------
# Full file encrypt / decrypt
# ---------------------------------------------------------------------------

@th.unit_test("encrypt_file / decrypt_file round-trips small file")
def test_file_roundtrip_small(opts):
    data = b"Hello, this is a small test file."
    ekey = opts.cv.generate_ekey()
    encrypted = opts.cv.encrypt_file(data, ekey)
    decrypted = opts.cv.decrypt_file(encrypted, ekey)
    assert_eq(decrypted, data, "decrypted file should match original")


@th.unit_test("encrypt_file / decrypt_file round-trips with password")
def test_file_roundtrip_password(opts):
    data = b"Password-protected content"
    ekey = opts.cv.generate_ekey()
    encrypted = opts.cv.encrypt_file(data, ekey, password="secret123")
    decrypted = opts.cv.decrypt_file(encrypted, ekey, password="secret123")
    assert_eq(decrypted, data, "decrypted file with password should match original")


@th.unit_test("decrypt_file fails with wrong password")
def test_file_wrong_password(opts):
    data = b"Protected content"
    ekey = opts.cv.generate_ekey()
    encrypted = opts.cv.encrypt_file(data, ekey, password="correct")
    try:
        opts.cv.decrypt_file(encrypted, ekey, password="wrong")
        assert False, "should have raised ValueError for wrong password"
    except ValueError:
        pass  # expected


@th.unit_test("encrypt_file / decrypt_file round-trips multi-chunk file")
def test_file_roundtrip_multichunk(opts):
    # Use small chunk size to force multiple chunks
    chunk_size = 100
    data = b"A" * 350  # 4 chunks: 100 + 100 + 100 + 50
    ekey = opts.cv.generate_ekey()
    encrypted = opts.cv.encrypt_file(data, ekey, chunk_size=chunk_size)
    decrypted = opts.cv.decrypt_file(encrypted, ekey)
    assert_eq(decrypted, data, "multi-chunk file should round-trip correctly")
    # verify header reports correct chunk count
    header = opts.cv.parse_header(encrypted[:64])
    assert_eq(header["total_chunks"], 4, "350 bytes / 100 chunk_size = 4 chunks")


@th.unit_test("encrypt_file handles empty file")
def test_file_empty(opts):
    ekey = opts.cv.generate_ekey()
    encrypted = opts.cv.encrypt_file(b"", ekey)
    decrypted = opts.cv.decrypt_file(encrypted, ekey)
    assert_eq(decrypted, b"", "empty file should round-trip to empty bytes")


@th.unit_test("encrypt_file handles 2KB file")
def test_file_2kb(opts):
    data = b"x" * 2048
    ekey = opts.cv.generate_ekey()
    encrypted = opts.cv.encrypt_file(data, ekey)
    decrypted = opts.cv.decrypt_file(encrypted, ekey)
    assert_eq(len(decrypted), 2048, "2KB file should round-trip correctly")
    assert_eq(decrypted, data, "2KB content should match")


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------

@th.unit_test("generate / validate access token round-trips")
def test_token_roundtrip(opts):
    token = opts.cv.generate_access_token(42, "1.2.3.4", opts.secret_key, ttl=60)
    fid = opts.cv.validate_access_token(token, "1.2.3.4", opts.secret_key)
    assert_eq(fid, 42, "validated token should return correct file_id")


@th.unit_test("validate_access_token rejects wrong IP")
def test_token_wrong_ip(opts):
    token = opts.cv.generate_access_token(42, "1.2.3.4", opts.secret_key, ttl=60)
    fid = opts.cv.validate_access_token(token, "5.6.7.8", opts.secret_key)
    assert_eq(fid, None, "wrong IP should return None")


@th.unit_test("validate_access_token rejects expired token")
def test_token_expired(opts):
    token = opts.cv.generate_access_token(42, "1.2.3.4", opts.secret_key, ttl=0)
    time.sleep(1)
    fid = opts.cv.validate_access_token(token, "1.2.3.4", opts.secret_key)
    assert_eq(fid, None, "expired token should return None")


@th.unit_test("validate_access_token rejects tampered token")
def test_token_tampered(opts):
    token = opts.cv.generate_access_token(42, "1.2.3.4", opts.secret_key, ttl=60)
    tampered = token[:-4] + "XXXX"
    fid = opts.cv.validate_access_token(tampered, "1.2.3.4", opts.secret_key)
    assert_eq(fid, None, "tampered token should return None")


@th.unit_test("validate_access_token rejects wrong secret_key")
def test_token_wrong_secret(opts):
    token = opts.cv.generate_access_token(42, "1.2.3.4", opts.secret_key, ttl=60)
    fid = opts.cv.validate_access_token(token, "1.2.3.4", "wrong-key")
    assert_eq(fid, None, "wrong secret_key should return None")
