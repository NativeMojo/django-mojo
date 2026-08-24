"""
Severity of ``security:bouncer:token_invalid`` events, per failure cause.

The default "Auth - Bouncer Token Abuse" ruleset firewalls an IP fleet-wide off
these events once they reach level 7. A bouncer token that expired while the
user read the page, a double-submitted form (the nonce is single-use), or a
cellular/CGNAT handoff that changed the egress IP mid-session are all things a
legitimate user produces without doing anything wrong — they must stay below
that level.

And a deployment still in log-only mode (``BOUNCER_REQUIRE_TOKEN`` False) has
not enabled enforcement at all, so nothing it observes may get anybody
firewalled, whatever the cause.

Runs the decorator in-process against a stand-in request. ``TokenManager`` is
patched to raise each real error string it can raise, and the incident reporter
is patched so the assertion is on the level the decorator chose.
"""
from testit import helpers as th

TESTIT_TIER = "extended"


# Every ValueError string raised by TokenManager.validate() /
# validate_and_consume(), plus page_type_mismatch which the decorator itself
# raises after a successful validation.
BENIGN_CAUSES = ("expired", "nonce_consumed", "ip_mismatch")
HOSTILE_CAUSES = ("invalid_format", "invalid_signature", "page_type_mismatch", "duid_mismatch")

BENIGN_LEVEL = 4
HOSTILE_LEVEL = 7


class _FakeRequest:
    """Minimal stand-in — the decorator reads only these attributes."""

    def __init__(self, require, token="not-a-real-token"):
        self.DATA = {"bouncer_token": token}
        # Loopback + no proxy chain lets the gated X-Mojo-Test-* header drive
        # BOUNCER_REQUIRE_TOKEN per request (see mojo/helpers/test_mode.py), so
        # neither mode depends on what the deployment conf happens to say.
        self.META = {
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_X_MOJO_TEST_BOUNCER_REQUIRE_TOKEN": "1" if require else "0",
        }
        self.group = None
        self.ip = "203.0.113.9"
        self.muid = "token-level-test-muid"
        self.duid = "token-level-test-duid"


def _reported_event(cause, require):
    """Run the guarded view with a token that fails for ``cause``.

    Returns the kwargs the decorator would have handed the incident reporter.
    """
    from unittest import mock
    import mojo.errors
    from mojo.decorators import bouncer as bouncer_decorator

    captured = {}

    def _capture(request, page_type, category, details, level, **kwargs):
        captured.update(category=category, level=level, details=details, **kwargs)

    with mock.patch.object(bouncer_decorator, "_report_token_event", _capture), \
            mock.patch(
                "mojo.apps.account.services.bouncer.token_manager"
                ".TokenManager.validate_and_consume",
                side_effect=ValueError(cause)):
        guarded = bouncer_decorator.requires_bouncer_token("login")(lambda request: "ok")
        try:
            guarded(_FakeRequest(require=require))
        except mojo.errors.PermissionDeniedException:
            # Enforcement rejects the request AFTER reporting; the level is
            # what this test is about, so the rejection itself is expected.
            pass

    return captured


@th.django_unit_test()
def test_benign_token_failures_stay_below_block_level(opts):
    """Expiry, replay and IP handoff are lifecycle events, not attacks."""
    for cause in BENIGN_CAUSES:
        reported = _reported_event(cause, require=True)
        assert reported.get("category") == "security:bouncer:token_invalid", (
            f"'{cause}' should report a token_invalid event, got "
            f"{reported.get('category')!r}"
        )
        assert reported.get("level") == BENIGN_LEVEL, (
            f"'{cause}' is produced by legitimate users (token TTL expiry, double submit, "
            f"cellular IP handoff) — expected level {BENIGN_LEVEL}, got {reported.get('level')}"
        )


@th.django_unit_test()
def test_tampered_tokens_report_at_block_level(opts):
    """Forged, malformed or out-of-scope tokens keep their higher severity."""
    for cause in HOSTILE_CAUSES:
        reported = _reported_event(cause, require=True)
        assert reported.get("level") == HOSTILE_LEVEL, (
            f"'{cause}' means the token was tampered with or replayed out of scope — "
            f"expected level {HOSTILE_LEVEL}, got {reported.get('level')}"
        )


@th.django_unit_test()
def test_log_only_mode_never_reports_at_block_level(opts):
    """A deployment that has not enabled enforcement must never firewall anyone."""
    for cause in BENIGN_CAUSES + HOSTILE_CAUSES:
        reported = _reported_event(cause, require=False)
        assert reported.get("level") == BENIGN_LEVEL, (
            f"log-only mode must cap '{cause}' at level {BENIGN_LEVEL} so no blocking "
            f"ruleset can act on it, got {reported.get('level')}"
        )
