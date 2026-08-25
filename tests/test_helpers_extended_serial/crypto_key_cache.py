from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import threading
from unittest import mock

from testit import helpers as th


@contextmanager
def _cleared_cache(aes):
    aes._derive_key_cached.cache_clear()
    try:
        yield
    finally:
        aes._derive_key_cached.cache_clear()


def _fake_pbkdf2(password, salt, dkLen, count):
    seed = hashlib.sha512(
        password + b"\x00" + salt + b"\x00" + str(count).encode("ascii")
    ).digest()
    repeats = (dkLen + len(seed) - 1) // len(seed)
    return (seed * repeats)[:dkLen]


@th.django_unit_test()
def test_cache_hits_normalized_inputs_and_separates_identity(opts):
    from mojo.helpers.crypto import aes

    calls = []

    def recording_pbkdf2(password, salt, dkLen, count):
        calls.append((password, salt, dkLen, count))
        if not isinstance(dkLen, int):
            raise TypeError("dkLen must be an integer")
        return _fake_pbkdf2(password, salt, dkLen, count)

    with _cleared_cache(aes), mock.patch.object(
            aes, "PBKDF2", side_effect=recording_pbkdf2):
        expected = aes.derive_key("password", b"salt")
        assert aes.derive_key(b"password", bytearray(b"salt")) == expected, \
            "Equivalent string/bytes/bytearray inputs must share a cache entry"
        assert aes.derive_key(memoryview(b"password"), memoryview(b"salt")) == expected, \
            "Equivalent memoryview inputs must share a cache entry"
        assert len(calls) == 1, \
            f"Expected one PBKDF2 call for normalized inputs, got {len(calls)}"

        fallback = aes.derive_key(65, 66)
        assert aes.derive_key(b"A", b"B") == fallback, \
            "PyCryptodome tobytes fallback inputs must share their normalized entry"
        assert len(calls) == 2, \
            f"Expected one additional fallback derivation, got {len(calls)} calls"

        aes.derive_key(b"other-password", b"salt")
        aes.derive_key(b"password", b"other-salt")
        aes.derive_key(b"password", b"salt", key_length=16)
        assert len(calls) == 5, \
            "Password, salt, and key length changes must each miss independently"

        try:
            aes.derive_key(b"password", b"salt", key_length=32.0)
        except TypeError:
            pass
        else:
            assert False, \
                "A float key length must miss instead of reusing the integer entry"
        assert len(calls) == 6, \
            "An invalidly typed key length must reach PBKDF2 on its own cache miss"


@th.django_unit_test()
def test_encryption_bypasses_cache_and_rotation_misses_once(opts):
    from mojo.helpers.crypto import aes

    calls = []

    def recording_pbkdf2(password, salt, dkLen, count):
        calls.append((password, salt, dkLen, count))
        return _fake_pbkdf2(password, salt, dkLen, count)

    with _cleared_cache(aes), mock.patch.object(
            aes, "PBKDF2", side_effect=recording_pbkdf2):
        payloads = [aes.encrypt("secret", "password") for _ in range(3)]
        salts = [aes.b64decode(payload)[:aes.SALT_LENGTH] for payload in payloads]

        assert len(set(salts)) == 3, \
            "Every encryption must produce a distinct random salt"
        assert aes._derive_key_cached.cache_info().currsize == 0, \
            "Fresh-salt encryption must not populate the decrypt LRU"
        assert len(calls) == 3, \
            f"Expected one uncached derivation per encryption, got {len(calls)}"

        assert aes.decrypt(payloads[0], "password") == "secret", \
            "The first decrypt after encryption must remain compatible"
        assert aes.decrypt(payloads[0], "password") == "secret", \
            "The repeated decrypt must return the original plaintext"
        info = aes._derive_key_cached.cache_info()
        assert len(calls) == 4, \
            f"Expected one cached decrypt derivation, got {len(calls)} total calls"
        assert info.misses == 1 and info.hits == 1, \
            f"Expected one decrypt miss and hit, got {info}"

        assert aes.decrypt(payloads[1], "password") == "secret", \
            "A rotated payload with a fresh salt must decrypt correctly"
        rotated_info = aes._derive_key_cached.cache_info()
        assert len(calls) == 5, \
            "The first decrypt of fresh ciphertext must derive a new key"
        assert rotated_info.misses == 2, \
            f"Expected the rotated ciphertext to miss, got {rotated_info}"


@th.django_unit_test()
def test_cache_evicts_least_recently_used_entry_at_bound(opts):
    from mojo.helpers.crypto import aes

    calls = []

    def recording_pbkdf2(password, salt, dkLen, count):
        calls.append((password, salt, dkLen, count))
        return _fake_pbkdf2(password, salt, dkLen, count)

    with _cleared_cache(aes), mock.patch.object(
            aes, "PBKDF2", side_effect=recording_pbkdf2):
        for index in range(aes.DERIVED_KEY_CACHE_SIZE + 1):
            salt = index.to_bytes(4, "big")
            aes.derive_key(b"password", salt)

        info = aes._derive_key_cached.cache_info()
        assert info.currsize == aes.DERIVED_KEY_CACHE_SIZE, \
            f"Expected a {aes.DERIVED_KEY_CACHE_SIZE}-entry cache, got {info}"
        assert len(calls) == aes.DERIVED_KEY_CACHE_SIZE + 1, \
            "Each unique key must derive once while filling and overflowing the cache"

        aes.derive_key(b"password", (0).to_bytes(4, "big"))
        assert len(calls) == aes.DERIVED_KEY_CACHE_SIZE + 2, \
            "The least-recently-used entry must derive again after eviction"


@th.django_unit_test()
def test_concurrent_cold_misses_remain_coherent(opts):
    from mojo.helpers.crypto import aes

    workers = 4
    start = threading.Barrier(workers + 1, timeout=5)
    all_misses_entered = threading.Event()
    release_misses = threading.Event()
    call_lock = threading.Lock()
    calls = []

    def blocking_pbkdf2(password, salt, dkLen, count):
        with call_lock:
            calls.append((password, salt, dkLen, count))
            if len(calls) == workers:
                all_misses_entered.set()
        if not release_misses.wait(timeout=5):
            raise TimeoutError("concurrent PBKDF2 misses were not released")
        return _fake_pbkdf2(password, salt, dkLen, count)

    def derive_shared_key():
        start.wait()
        return aes.derive_key(b"password", b"shared-salt")

    with _cleared_cache(aes), mock.patch.object(
            aes, "PBKDF2", side_effect=blocking_pbkdf2):
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(derive_shared_key) for _ in range(workers)]
            start.wait()
            entered = all_misses_entered.wait(timeout=5)
            release_misses.set()
            results = [future.result(timeout=5) for future in futures]

        assert entered, \
            "All concurrent callers must overlap inside the cold PBKDF2 miss"
        assert len(calls) == workers, \
            f"Expected duplicate safe cold computations, got {len(calls)}"
        assert all(result == results[0] for result in results), \
            "Concurrent same-key misses must never cross-wire derived results"
        assert aes._derive_key_cached.cache_info().currsize == 1, \
            "Concurrent same-key misses must converge on one cache entry"

    with _cleared_cache(aes), mock.patch.object(
            aes, "PBKDF2", side_effect=_fake_pbkdf2):
        salts = [f"different-{index}".encode("ascii") for index in range(workers)]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(
                lambda salt: aes.derive_key(b"password", salt), salts
            ))

        expected = [
            _fake_pbkdf2(b"password", salt, 32, aes.PBKDF2_ITERATIONS)
            for salt in salts
        ]
        assert results == expected, \
            "Concurrent different-key derivations must remain correctly separated"
        assert aes._derive_key_cached.cache_info().currsize == workers, \
            "Concurrent different-key derivations must populate distinct entries"
