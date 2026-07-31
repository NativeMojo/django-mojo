"""
SECRET_KEY rotation — bouncer surfaces (tokens + pass cookies).

Rotation is simulated by patching each consumer module's `crypto_keys`
reference in-process — NEVER via th.server_settings: that writes to the live
var/django.conf, and a stranded SECRET_KEY override there would break every
later boot. The package is serial (see __init__.py) because these patches
are process-wide.
"""

import uuid as _uuid
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


class FakeKeys:
    """Stands in for mojo.helpers.crypto.keys with a fixed candidate list."""

    def __init__(self, keys):
        self._keys = list(keys)

    def secret_keys(self):
        return list(self._keys)


@th.django_unit_test("bouncer token round-trips with live settings")
def test_token_live_settings(opts):
    from mojo.apps.account.services.bouncer.token_manager import TokenManager

    ip = "10.9.8.6"
    token = TokenManager.issue("duid-live", "fp-live", ip, 0.0, "login")
    payload = TokenManager.validate(token, ip)
    assert_eq(payload["duid"], "duid-live",
              "token must round-trip under live settings with no fallbacks configured")


@th.django_unit_test("bouncer token verifies across a SECRET_KEY rotation")
def test_token_rotation(opts):
    from mojo.apps.account.services.bouncer import token_manager

    key_a = f"rotation-old-key-{_uuid.uuid4().hex}"
    key_b = f"rotation-new-key-{_uuid.uuid4().hex}"
    ip = "10.9.8.7"

    with mock.patch.object(token_manager, "crypto_keys", FakeKeys([key_a])):
        token = token_manager.TokenManager.issue("duid-1", "fp-1", ip, 0.1, "login")
        payload = token_manager.TokenManager.validate(token, ip)
        assert_eq(payload["duid"], "duid-1",
                  "sanity: token must validate under its own signing key")

    # Rotate: key_b becomes primary, key_a moves to fallbacks.
    with mock.patch.object(token_manager, "crypto_keys", FakeKeys([key_b, key_a])):
        payload = token_manager.TokenManager.validate(token, ip)
        assert_eq(payload["duid"], "duid-1",
                  "token signed under the old key must verify while it is a fallback")
        new_token = token_manager.TokenManager.issue("duid-2", "fp-2", ip, 0.2, "login")

    # A post-rotation token must be signed under the PRIMARY: the old key
    # alone must reject it, the new key alone must accept it.
    with mock.patch.object(token_manager, "crypto_keys", FakeKeys([key_a])):
        try:
            token_manager.TokenManager.validate(new_token, ip)
            assert False, ("token minted after rotation must NOT verify under the "
                           "old key alone — issuance must use the primary")
        except ValueError as err:
            assert_eq(str(err), "invalid_signature",
                      f"expected invalid_signature for a wrong-key token, got {err!r}")

    with mock.patch.object(token_manager, "crypto_keys", FakeKeys([key_b])):
        payload = token_manager.TokenManager.validate(new_token, ip)
        assert_eq(payload["duid"], "duid-2",
                  "post-rotation token must verify under the new primary alone")
        # Fallback removed — pre-rotation tokens die.
        try:
            token_manager.TokenManager.validate(token, ip)
            assert False, "old-key token must stop verifying once the fallback is removed"
        except ValueError as err:
            assert_eq(str(err), "invalid_signature",
                      f"expected invalid_signature once the fallback is removed, got {err!r}")


@th.django_unit_test("bouncer pass cookie round-trips with live settings")
def test_pass_cookie_live(opts):
    import time
    from mojo.helpers.crypto import sign as crypto_sign
    from mojo.apps.account.rest.bouncer import assess

    ip = "10.4.5.9"
    muid = f"muid-{_uuid.uuid4().hex[:12]}"
    issued = str(int(time.time()))
    sig = crypto_sign(f"{muid}:10.4.5:{issued}")[:16]
    cookie = f"{muid}:{issued}:{sig}"
    assert_eq(assess.verify_pass_cookie(cookie, ip), muid,
              "a cookie signed with the live primary key must verify unpatched")


@th.django_unit_test("bouncer pass cookie verifies across a SECRET_KEY rotation")
def test_pass_cookie_rotation(opts):
    import time
    from mojo.helpers.crypto import sign as crypto_sign
    from mojo.apps.account.rest.bouncer import assess

    key_a = f"rotation-cookie-old-{_uuid.uuid4().hex}"
    key_b = f"rotation-cookie-new-{_uuid.uuid4().hex}"
    ip = "10.4.5.6"
    muid = f"muid-{_uuid.uuid4().hex[:12]}"
    issued = str(int(time.time()))
    old_sig = crypto_sign(f"{muid}:10.4.5:{issued}", key_a)[:16]
    old_cookie = f"{muid}:{issued}:{old_sig}"

    with mock.patch.object(assess, "crypto_keys", FakeKeys([key_b, key_a])):
        assert_eq(assess.verify_pass_cookie(old_cookie, ip), muid,
                  "cookie signed under the old key must verify while it is a fallback")

    with mock.patch.object(assess, "crypto_keys", FakeKeys([key_b])):
        assert_eq(assess.verify_pass_cookie(old_cookie, ip), None,
                  "old-key cookie must stop verifying once the fallback is removed")
