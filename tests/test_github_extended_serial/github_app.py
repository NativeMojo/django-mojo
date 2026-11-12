"""
GitHub App webhook-signature tests that mutate django.conf.settings in the
test process — moved out of tests/test_github/github_app.py into this opt-in
serial package (maestro item #1839). Each installs/deletes
GITHUB_WEBHOOK_SECRET process-wide, which races parallel test threads even
with a try/finally restore.

These tests use test secrets and do not hit real GitHub APIs.
"""
import hashlib
import hmac

from testit import helpers as th


@th.django_unit_test("github app: verify_webhook_signature rejects missing signature")
def test_verify_webhook_no_signature(opts):
    from django.conf import settings as django_settings
    from mojo.apps.github.services.github_app import verify_webhook_signature

    django_settings.GITHUB_WEBHOOK_SECRET = "test-secret"
    try:
        result = verify_webhook_signature(b"payload", None)
    finally:
        del django_settings.GITHUB_WEBHOOK_SECRET
    assert result is False, "Should reject when signature header is missing"


@th.django_unit_test("github app: verify_webhook_signature accepts valid signature")
def test_verify_webhook_valid(opts):
    from django.conf import settings as django_settings
    from mojo.apps.github.services.github_app import verify_webhook_signature

    secret = "test-webhook-secret"
    payload = b'{"action": "created"}'

    # Compute the expected signature
    expected_sig = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    django_settings.GITHUB_WEBHOOK_SECRET = secret
    try:
        result = verify_webhook_signature(payload, expected_sig)
    finally:
        del django_settings.GITHUB_WEBHOOK_SECRET
    assert result is True, "Should accept valid HMAC signature"


@th.django_unit_test("github app: verify_webhook_signature rejects invalid signature")
def test_verify_webhook_invalid(opts):
    from django.conf import settings as django_settings
    from mojo.apps.github.services.github_app import verify_webhook_signature

    django_settings.GITHUB_WEBHOOK_SECRET = "test-secret"
    try:
        result = verify_webhook_signature(b"payload", "sha256=definitely_wrong")
    finally:
        del django_settings.GITHUB_WEBHOOK_SECRET
    assert result is False, "Should reject invalid HMAC signature"


# --- GITHUB_APP_PRIVATE_KEY as the PEM itself or a path (maestro #10955) -----
# These set GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY in django.conf.settings,
# hence the serial package. Keys are generated per test, never committed.

_MISSING = object()
_KEYS = ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY")


def _make_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    return key, pem


def _no_global_rows():
    from mojo.apps.account.models.setting import Setting
    assert not Setting.objects.filter(key__in=_KEYS, group__isnull=True).exists(), \
        "a global GITHUB_APP_* Setting row would override the file values these tests set"


class _github_settings:
    """Set GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY on django.conf.settings, restoring on exit."""

    def __init__(self, private_key):
        self.values = {"GITHUB_APP_ID": "12345", "GITHUB_APP_PRIVATE_KEY": private_key}

    def __enter__(self):
        from django.conf import settings as django_settings
        self.saved = {k: getattr(django_settings, k, _MISSING) for k in self.values}
        for k, v in self.values.items():
            setattr(django_settings, k, v)

    def __exit__(self, *exc):
        from django.conf import settings as django_settings
        for k, v in self.saved.items():
            if v is _MISSING:
                delattr(django_settings, k)
            else:
                setattr(django_settings, k, v)


def _assert_signs(key, label):
    import jwt
    from mojo.apps.github.services.github_app import generate_jwt, is_configured
    assert is_configured() is True, f"{label}: is_configured() must be True"
    claims = jwt.decode(generate_jwt(), key.public_key(), algorithms=["RS256"])
    assert claims["iss"] == "12345", f"{label}: the JWT must verify with the public key and carry iss, got {claims}"


@th.django_unit_test("github app: a PEM in GITHUB_APP_PRIVATE_KEY configures the app and signs (#10955)")
def test_private_key_setting_holds_pem(opts):
    _no_global_rows()
    key, pem = _make_key()
    with _github_settings(pem):
        _assert_signs(key, "PEM value")
    with _github_settings("".join(pem.split("\n"))):
        _assert_signs(key, "stripped PEM value")


@th.django_unit_test("github app: a path in GITHUB_APP_PRIVATE_KEY still works (#10955)")
def test_private_key_setting_holds_path(opts):
    import os
    import tempfile
    _no_global_rows()
    key, pem = _make_key()
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(pem)
        with _github_settings(path):
            _assert_signs(key, "path value")
    finally:
        os.unlink(path)


@th.django_unit_test("github app: a bad path or bad PEM gives None and the key is never logged (#10955)")
def test_private_key_bad_values_not_logged(opts):
    import logging
    from mojo.apps.github.services.github_app import _get_private_key, is_configured

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    _no_global_rows()
    key, pem = _make_key()
    body = pem.split("\n")[1]
    bad_path = "/nonexistent/milo-10955/key.pem"
    handler = _Capture()
    stdlib_logger = logging.getLogger("github_app")
    stdlib_logger.addHandler(handler)
    try:
        with _github_settings(bad_path):
            assert _get_private_key() is None, "an unreadable path must give None"
            assert is_configured() is False, "an unreadable path must not count as configured"
        assert any(bad_path in line for line in handler.lines), \
            f"the unreadable-path error must name the path, got {handler.lines}"
        broken = pem.replace(body, "A" * len(body))
        with _github_settings(broken):
            assert _get_private_key() is None, "a PEM that does not load must give None"
        with _github_settings("x\x00y"):
            assert _get_private_key() is None, "a value with a NUL byte must give None, not raise"
        with _github_settings(pem):
            assert _get_private_key() == pem, "a good PEM must load"
        logged = "\n".join(handler.lines)
        assert "BEGIN" not in logged, "no log record may contain a PEM header"
        assert body not in logged and "A" * 64 not in logged, "no log record may contain key body text"
    finally:
        stdlib_logger.removeHandler(handler)
