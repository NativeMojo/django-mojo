"""
Tests for GitHub App service — JWT, token caching, webhook signature verification.

These tests use test RSA keys and do not hit real GitHub APIs.
"""

TESTIT_TIER = "extended"
import time

from testit import helpers as th


# RSA key pair for testing (2048-bit, generated for tests only)
TEST_RSA_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0AHB7MhgHcTz6sE2I2yPB
aFDrBz9vFqU4jK5Z0JqFV7g0CJq6EMg3cS9QLZY0TiNX3CpR3GSv1MhKPfHaQH6F
z0bH+3MKC0TVr1I7L4lQaJj2HCT9mFVNRWKHPfN3oC7ESUlF5kFF5MzV5N6hTEjBQ
r/WZL3GxBOlAyR8E8kf7u1Bgo0HRzLUJ5c0wuVcn8CsxKlQtK5N3SBFn0SAOQHJ2v
K5f1WS7GIMvekCBM0AvZi1VKxdVDAi1JN9JxOIi0oWPT1DAf8qr/cH3aEEBjKqFE5P
IflVp19tdEH1ePZauR4eJTIxWLOP2L3JEvHYrwIDAQABAoIBAFnSq/3OhJnHF7J2kM
EFXGaSYGeWv9s3mD/K9YMeP2s5q+2A6m6OZQFTYQ+r+3E4La1lQxMb8VEn0S5OPYMU
5aeNmGVvD8AAxC+cADT+HRKEC0EMwL2k0JWTJ1FJFfsSCGDJKPSsNNEb2RGCBL91J4
lqB1SnN0NZwZP2LPrfXbD8AxVCPnIoj11RWNHm7yNqBLM3EGqanMOxnXlA9dwkN4MN
+4VPHQDkxAQN2bKUXemDnFmOCRbp6DVfm1sJcLJ4xXLYKqK/i8kp2c8saN7fRbxjnD
xFnM8TGPvkWDCiAhLsI4xnupMFbkb5LPHmHVYP3v7eSdG4qdvRO8ECgYEA7aPLkfS
zU3J+k4u+kDGy7G3XBlzYxpGqx2s3F8gL/edrkviJjG4bHDn5fDR3FfIq2AaSZfR7s
3e+2s3U0CIAJ5FBVm3+6K7LTJbRiJUmKsA8kVNEPP5Y7FQKPFnNT3R5ZU5gFJ4DX0k
B3S5JHkHDhNTNGLJMrr3G4i3/xClP1cCgYEA4rG2IM/sYzz3UjrfxRAyiEU+0Ckek1
f1W3tSL0kMX3DKVMQNV8pL4iyJ2xTk4T6qT6URKLqaIqLFiK3EL0BQ6alJU/K5+q/L
NeBLi7x/m5j02vkmgA4pKv/6RW0CQpEkKSLxl/uBLGxkVJROnAsh9bZ3rTiHRCX+0P
G5u2hMsCkCgYEAglqCchyIQPFQoIR6LSrZc2s3fR9LcLV6W7Lg/HFMSmKMD3/7X2FE
+NsLLFHnTJ2I0t8F4i3E6aSG0pPC3dFVjcVDlF7vfN0REXR3vMiVQP3ENMpmHvA8B4
J0ljgH6ht1s+JlxQk5OKN7fHKOoRY8A4GFHqJFVsAFn+PjJhp70CgYBbv31K8lLCR0
CEJFw+pn0VYBnqYjPHxfNJA/8syZdfgKC8PT2q5E1FRZWX2y3cGdR3g1kWHdnij/ke
G3e+Sc5sTEnGONpVWKsMpV7p7V2U5SVN6TA5F1pCvP9B3E+C0OYfWFKvA8SdP2DLVK
j3zJlIFbWOEBLdOPmxkXWM38g6QKBgAKxfJJnVr1fqFNqNcFOEJnkLxGBJD3c0FHkP
JoC9H/bxO+DEnFLU2+dWnKGHPMm6i7zB0RRUFQ4MuPBXIIAKi6V6hP0vKz7M/LwWj
Gc7FeAf3je6Ua8xmMEJ2VZ/DqJGUOVH4Ww5bHAiSnRddlB2Lj/ODAl3a/X/Jx7HjI7
-----END RSA PRIVATE KEY-----"""


@th.django_unit_test("github app: is_configured returns False when settings missing")
def test_is_configured_false(opts):
    from mojo.apps.github.services.github_app import is_configured

    # Without settings, should return False
    result = is_configured()
    assert result is False, "is_configured should be False without GITHUB_APP_ID"


@th.django_unit_test("github app: is_token_valid with None returns False")
def test_is_token_valid_none(opts):
    from mojo.apps.github.services.github_app import is_token_valid

    assert is_token_valid(None) is False, "is_token_valid(None) should be False"


@th.django_unit_test("github app: is_token_valid with future expiry returns True")
def test_is_token_valid_future(opts):
    from django.utils import timezone
    from mojo.apps.github.services.github_app import is_token_valid

    future = timezone.now() + timezone.timedelta(hours=1)
    assert is_token_valid(future) is True, "Token expiring in 1 hour should be valid"


@th.django_unit_test("github app: is_token_valid with past expiry returns False")
def test_is_token_valid_past(opts):
    from django.utils import timezone
    from mojo.apps.github.services.github_app import is_token_valid

    past = timezone.now() - timezone.timedelta(hours=1)
    assert is_token_valid(past) is False, "Expired token should not be valid"


@th.django_unit_test("github app: is_token_valid within buffer returns False")
def test_is_token_valid_within_buffer(opts):
    from django.utils import timezone
    from mojo.apps.github.services.github_app import is_token_valid

    # Expires in 2 minutes — inside the 5-minute buffer
    near_future = timezone.now() + timezone.timedelta(minutes=2)
    assert is_token_valid(near_future, buffer_seconds=300) is False, (
        "Token expiring within buffer should not be valid"
    )


@th.django_unit_test("github app: generate_jwt raises when not configured")
def test_generate_jwt_not_configured(opts):
    from mojo.apps.github.services.github_app import generate_jwt

    raised = False
    try:
        generate_jwt()
    except ValueError as e:
        raised = True
        assert "not configured" in str(e).lower(), f"Error should mention configuration: {e}"

    assert raised, "generate_jwt should raise ValueError when not configured"


@th.django_unit_test("github app: verify_webhook_signature rejects when secret not set")
def test_verify_webhook_no_secret(opts):
    from mojo.apps.github.services.github_app import verify_webhook_signature

    result = verify_webhook_signature(b"payload", "sha256=abc123")
    assert result is False, "Should reject when GITHUB_WEBHOOK_SECRET is not set"


# test_verify_webhook_no_signature, test_verify_webhook_valid and
# test_verify_webhook_invalid moved to
# tests/test_github_extended_serial/github_app.py — they mutate
# django.conf.settings (GITHUB_WEBHOOK_SECRET) in-process (maestro item #1839).


# --- GITHUB_APP_PRIVATE_KEY holding the PEM itself (maestro #10955) ---------
# _normalize_private_key is pure (no settings), so these run in the default
# package. Keys are generated per test and never committed.

_FIXED_CLAIMS = {"iat": 1700000000, "exp": 1700000540, "iss": "12345"}


def _make_pem(pkcs8=False):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fmt = (serialization.PrivateFormat.PKCS8 if pkcs8
           else serialization.PrivateFormat.TraditionalOpenSSL)
    pem = key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption()).decode()
    return key, pem


def _stripped(pem):
    return "".join(pem.split("\n"))


@th.django_unit_test("github app: a PEM value loads and signs a JWT the public key verifies (#10955)")
def test_private_key_pem_value(opts):
    import jwt
    from mojo.apps.github.services.github_app import _normalize_private_key

    key, pem = _make_pem()
    normalized = _normalize_private_key(pem)
    assert normalized == pem, "a well-formed PEM must come back unchanged"
    token = jwt.encode(_FIXED_CLAIMS, normalized, algorithm="RS256")
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "12345", f"the signed JWT must verify with the public key, got {claims}"


@th.django_unit_test("github app: a one-line PEM with literal \\n escapes normalises to the original (#10955)")
def test_private_key_escaped_newlines(opts):
    from mojo.apps.github.services.github_app import _normalize_private_key

    _, pem = _make_pem()
    escaped = pem.replace("\n", "\\n")
    assert "\n" not in escaped, "test setup: the escaped value must be one line"
    assert _normalize_private_key(escaped) == pem, "\\n escapes must turn back into the original PEM"


@th.django_unit_test("github app: a PEM with every line break stripped is re-wrapped and signs identically (#10955)")
def test_private_key_stripped_line_breaks(opts):
    import jwt
    from mojo.apps.github.services.github_app import _normalize_private_key

    _, pem = _make_pem()
    normalized = _normalize_private_key(_stripped(pem))
    assert normalized == pem, "a stripped PEM must be re-wrapped at 64 columns into the original text"
    assert jwt.encode(_FIXED_CLAIMS, normalized, algorithm="RS256") == \
        jwt.encode(_FIXED_CLAIMS, pem, algorithm="RS256"), \
        "the re-wrapped key must sign exactly like the original (RS256 is deterministic)"


@th.django_unit_test("github app: PKCS#8, CRLF, space-joined and quoted PEM values all normalise (#10955)")
def test_private_key_variants(opts):
    from mojo.apps.github.services.github_app import _normalize_private_key

    _, pem = _make_pem()
    _, pkcs8 = _make_pem(pkcs8=True)
    cases = {
        "pkcs8": (pkcs8, pkcs8),
        "pkcs8 stripped": (_stripped(pkcs8), pkcs8),
        "crlf": (pem.replace("\n", "\r\n"), pem),
        "space-joined": (" ".join(pem.strip().split("\n")), pem),
        "double-quoted": ('"' + pem + '"', pem),
        "single-quoted escaped": ("'" + pem.replace("\n", "\\n") + "'", pem),
        "surrounding whitespace": ("  \n" + pem + "\n  ", pem),
    }
    for name, (value, expected) in cases.items():
        assert _normalize_private_key(value) == expected, f"{name}: must normalise to the original PEM"


@th.django_unit_test("github app: a PEM-shaped value that is not a key normalises to None (#10955)")
def test_private_key_garbage_is_none(opts):
    from mojo.apps.github.services.github_app import _normalize_private_key

    _, pem = _make_pem()
    lines = pem.strip().split("\n")
    cases = {
        "garbage body": "-----BEGIN RSA PRIVATE KEY-----\nnot-a-key\n-----END RSA PRIVATE KEY-----\n",
        "garbage one line": "-----BEGIN RSA PRIVATE KEY-----notakey-----END RSA PRIVATE KEY-----",
        "mismatched labels": _stripped(pem).replace("END RSA PRIVATE KEY", "END PRIVATE KEY"),
        "truncated body": "\n".join(lines[:5] + lines[-1:]) + "\n",
        "no footer": _stripped(pem).split("-----END")[0],
        "empty": "   ",
        "not a string": 12345,
    }
    for name, value in cases.items():
        assert _normalize_private_key(value) is None, f"{name}: a value that is not a loadable key must give None"


@th.django_unit_test("github app: a loadable non-RSA private key normalises to None (#10955)")
def test_private_key_non_rsa_is_none(opts):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from mojo.apps.github.services.github_app import _normalize_private_key

    ec_pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    assert _normalize_private_key(ec_pem) is None, "an EC key cannot sign RS256, so it must not count as configured"
