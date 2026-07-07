"""Tests for the webhook signing helpers in mojo.helpers.crypto.sign and
mojo.helpers.request — both are exercised against a real Group instance to
catch any integration drift.

Note on imports: the `mojo.helpers.crypto` package `__init__` aliases
`generate_signature as sign`, so the *attribute* `mojo.helpers.crypto.sign`
resolves to a function — never use `import mojo.helpers.crypto.sign as foo`.
Always import the names directly from the submodule:
`from mojo.helpers.crypto.sign import sign_for_group, ...`.
"""
from contextlib import contextmanager
from testit import helpers as th


GROUP_NAME = "wsign_group"
GROUP_NAME_OTHER = "wsign_group_other"


@th.django_unit_setup()
def setup_webhook_signer(opts):
    from mojo.apps.account.models import Group

    Group.objects.filter(name__in=[GROUP_NAME, GROUP_NAME_OTHER]).delete()
    g = Group.objects.create(name=GROUP_NAME, kind="organization")
    other = Group.objects.create(name=GROUP_NAME_OTHER, kind="organization")
    opts.group_id = g.pk
    opts.other_group_id = other.pk


@th.django_unit_test()
def test_sign_for_group_deterministic(opts):
    """sign_for_group produces the same hex for the same (group, body)."""
    from mojo.apps.account.models import Group
    from mojo.helpers.crypto.sign import sign_for_group

    g = Group.objects.get(pk=opts.group_id)
    body = b'{"event":"signup","user":42}'
    sig1 = sign_for_group(g, body)
    g.refresh_from_db()
    sig2 = sign_for_group(g, body)
    assert sig1 == sig2, (
        f"sign_for_group must be deterministic on the same body+secret, got {sig1!r} vs {sig2!r}"
    )
    assert len(sig1) == 64, f"HMAC-SHA256 hex length must be 64, got {len(sig1)}"


@th.django_unit_test()
def test_sign_for_group_auto_creates_secret_on_first_use(opts):
    """First call to sign_for_group mints the Group's secret transparently."""
    from mojo.apps.account.models import Group
    from mojo.helpers.crypto.sign import sign_for_group

    g = Group.objects.get(pk=opts.group_id)
    g.set_secret("webhook_secret", None); g.save(); g.refresh_from_db()
    assert g.get_webhook_secret() is None, "precondition: group must have no secret"

    sig = sign_for_group(g, b"hello")
    assert sig and len(sig) == 64, "sign_for_group must return a 64-char hex digest"

    g.refresh_from_db()
    assert g.get_webhook_secret() is not None, (
        "sign_for_group must auto-mint the secret (emit-side semantics)"
    )


@th.django_unit_test()
def test_verify_signed_request_accepts_valid(opts):
    """verify_signed_request returns True for a properly-signed request."""
    from mojo.apps.account.models import Group
    from mojo.helpers.crypto.sign import sign_for_group
    from mojo.helpers.request import verify_signed_request

    g = Group.objects.get(pk=opts.group_id)
    body = b'{"event":"order_paid","id":99}'
    sig_hex = sign_for_group(g, body)

    fake_request = _make_request(body=body, headers={
        "HTTP_X_MOJO_SIGNATURE": sig_hex,
    })
    g.refresh_from_db()
    secret = g.get_webhook_secret()
    assert secret is not None, "after sign_for_group, secret must be present"

    ok = verify_signed_request(fake_request, secret)
    assert ok is True, "verify_signed_request must accept a valid signature"


@th.django_unit_test()
def test_verify_signed_request_rejects_tampered_body(opts):
    """Tampering with the body after signing must fail verification."""
    from mojo.apps.account.models import Group
    from mojo.helpers.crypto.sign import sign_for_group
    from mojo.helpers.request import verify_signed_request

    g = Group.objects.get(pk=opts.group_id)
    body = b'{"amount":100}'
    sig_hex = sign_for_group(g, body)

    tampered = b'{"amount":1000}'
    fake_request = _make_request(body=tampered, headers={
        "HTTP_X_MOJO_SIGNATURE": sig_hex,
    })
    g.refresh_from_db()
    ok = verify_signed_request(fake_request, g.get_webhook_secret())
    assert ok is False, (
        "tampered body must fail verification — got True (signature accepted incorrectly)"
    )


@th.django_unit_test()
def test_verify_signed_request_returns_false_when_no_secret(opts):
    """No secret means no auto-mint on verify — must return False, not raise."""
    from mojo.helpers.request import verify_signed_request

    fake_request = _make_request(body=b"anything", headers={
        "HTTP_X_MOJO_SIGNATURE": "deadbeef" * 8,
    })
    ok = verify_signed_request(fake_request, None)
    assert ok is False, "verify_signed_request(None secret) must return False, not raise"
    ok2 = verify_signed_request(fake_request, "")
    assert ok2 is False, "verify_signed_request(empty secret) must return False"


@th.django_unit_test()
def test_verify_signed_request_returns_false_when_no_header(opts):
    """Missing signature header must short-circuit to False."""
    from mojo.apps.account.models import Group
    from mojo.helpers.crypto.sign import sign_for_group
    from mojo.helpers.request import verify_signed_request

    g = Group.objects.get(pk=opts.group_id)
    sign_for_group(g, b"prime")  # ensure a secret exists
    g.refresh_from_db()

    fake_request = _make_request(body=b"anything", headers={})
    ok = verify_signed_request(fake_request, g.get_webhook_secret())
    assert ok is False, "missing signature header must return False"


@th.django_unit_test()
def test_verify_signed_request_rejects_other_groups_signature(opts):
    """Signature from group A must not validate against group B's secret."""
    from mojo.apps.account.models import Group
    from mojo.helpers.crypto.sign import sign_for_group
    from mojo.helpers.request import verify_signed_request

    a = Group.objects.get(pk=opts.group_id)
    b = Group.objects.get(pk=opts.other_group_id)
    body = b'{"x":1}'
    sig_from_a = sign_for_group(a, body)
    sign_for_group(b, body)  # ensure b has its own secret
    b.refresh_from_db()

    fake_request = _make_request(body=body, headers={
        "HTTP_X_MOJO_SIGNATURE": sig_from_a,
    })
    ok = verify_signed_request(fake_request, b.get_webhook_secret())
    assert ok is False, (
        "signature from another Group's secret must not pass — cross-group leakage"
    )


@th.django_unit_test()
def test_verify_signed_request_uses_constant_time_compare(opts):
    """Smoke: the verify path imports hmac.compare_digest via verify_signature."""
    import inspect
    from mojo.helpers.crypto.sign import verify_signature

    source = inspect.getsource(verify_signature)
    assert "hmac.compare_digest" in source, (
        "verify_signature must use hmac.compare_digest for constant-time compare"
    )


@th.django_unit_test()
def test_verify_uses_configured_header(opts):
    """verify_signed_request's default header tracks the WEBHOOK_SIGNATURE_HEADER
    setting, so send and verify agree without callers passing header=.
    """
    from mojo.apps.account.models import Group
    from mojo.helpers.crypto.sign import sign_for_group
    from mojo.helpers.request import verify_signed_request

    g = Group.objects.get(pk=opts.group_id)
    body = b'{"event":"order_paid","id":99}'
    sig_hex = sign_for_group(g, body)
    g.refresh_from_db()
    secret = g.get_webhook_secret()
    assert secret is not None, "after sign_for_group, secret must be present"

    with _override_setting("WEBHOOK_SIGNATURE_HEADER", "X-Acme-Signature"):
        # Signature under the configured header name verifies with no header= arg.
        req_ok = _make_request(body=body, headers={"HTTP_X_ACME_SIGNATURE": sig_hex})
        assert verify_signed_request(req_ok, secret) is True, (
            "verify must read the configured header name (X-Acme-Signature) by default"
        )
        # The old default name must no longer be what the resolver looks for.
        req_old = _make_request(body=body, headers={"HTTP_X_MOJO_SIGNATURE": sig_hex})
        assert verify_signed_request(req_old, secret) is False, (
            "with the setting overridden, the X-Mojo-Signature default must not be consulted"
        )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

@contextmanager
def _override_setting(name, value):
    """Temporarily set a Django setting for these in-process verify tests.

    verify_signed_request resolves its default header via settings.get_static
    against this process's own django.conf.settings, and the test calls it
    directly (no test server), so setting the attribute here is visible to the
    code under test. override_settings is banned by the testing rules;
    th.server_settings only affects the separate server process, which these
    in-process tests never touch.
    """
    from django.conf import settings as dj
    missing = object()
    original = getattr(dj, name, missing)
    setattr(dj, name, value)
    try:
        yield
    finally:
        if original is missing:
            try:
                delattr(dj, name)
            except AttributeError:
                pass
        else:
            setattr(dj, name, original)

class _FakeRequest:
    """Minimal stand-in for a Django HttpRequest — body + META suffice for the
    verify helper. We avoid spinning up a real request through the test client
    because verify_signed_request only consumes those two attributes.
    """
    def __init__(self, body, meta):
        self.body = body
        self.META = meta
        self.headers = {}


def _make_request(body, headers):
    return _FakeRequest(body=body, meta=headers)
