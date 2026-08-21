"""Webhook-signer test that overrides django.conf.settings in-process.

Moved out of tests/test_account/test_webhook_signer.py (maestro item #1839):
this test mutates process-global Django settings via setattr/delattr, which is
unsafe under the parallel default tier. The rest of the signing/verification
coverage stays in the source module.

Note on imports: the `mojo.helpers.crypto` package `__init__` aliases
`generate_signature as sign`, so the *attribute* `mojo.helpers.crypto.sign`
resolves to a function — never use `import mojo.helpers.crypto.sign as foo`.
Always import the names directly from the submodule:
`from mojo.helpers.crypto.sign import sign_for_group, ...`.
"""
from contextlib import contextmanager
from testit import helpers as th


GROUP_NAME = "wsign_group_serial"


@th.django_unit_setup()
def setup_webhook_signer_serial(opts):
    from mojo.apps.account.models import Group

    Group.objects.filter(name=GROUP_NAME).delete()
    g = Group.objects.create(name=GROUP_NAME, kind="organization")
    opts.group_id = g.pk


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
