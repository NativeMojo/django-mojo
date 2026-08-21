"""In-process `_vetted_bounce_scheme` coverage for the OAuth deep-link bounce.

Moved out of tests/test_oauth/deep_link_bounce.py (maestro item #1839): this
test overrides django.conf.settings (OAUTH_REDIRECT_URI) in-process via
setattr/delattr, which is unsafe under the parallel default tier. The endpoint
round-trips, fail-closed negatives, and `matchable_scheme` unit coverage stay
in the source module.
"""
import contextlib

from testit import helpers as th


@th.django_unit_test("oauth: _vetted_bounce_scheme trusts only allowlisted or OAUTH_REDIRECT_URI schemes")
def test_vetted_bounce_scheme_ignores_the_origin_branch(opts):
    """Only two provenances make a custom scheme trustworthy: the allowlist
    (`allowlisted=True`) or a byte-equal `OAUTH_REDIRECT_URI`. The origin-derived
    branch (`allowlisted=False`, no match) never is. http(s) always returns ''."""
    from mojo.apps.account.rest import oauth

    @contextlib.contextmanager
    def _oauth_redirect_uri(value):
        """Point the IN-PROCESS OAUTH_REDIRECT_URI at `value`.

        In-process only, mirroring `_entries` in redirect_uri.py: `opts.client`
        talks to a separate server process that keeps the pinned settings, so
        this never leaks out of the test process.
        """
        from django.conf import settings as django_settings

        missing = object()
        prev = getattr(django_settings, "OAUTH_REDIRECT_URI", missing)
        setattr(django_settings, "OAUTH_REDIRECT_URI", value)
        try:
            yield
        finally:
            if prev is missing:
                delattr(django_settings, "OAUTH_REDIRECT_URI")
            else:
                setattr(django_settings, "OAUTH_REDIRECT_URI", prev)

    assert oauth._vetted_bounce_scheme("myapp://callback", True) == "myapp", (
        "an allowlisted deep link must yield its scheme")
    assert oauth._vetted_bounce_scheme("evilapp://x", False) == "", (
        "a non-allowlisted, non-OAUTH_REDIRECT_URI scheme (the origin branch) "
        "must never be trusted")
    assert oauth._vetted_bounce_scheme("https://example.com/", True) == "", (
        "an http(s) scheme never needs widening, so it returns ''")

    with _oauth_redirect_uri("myapp://home"):
        assert oauth._vetted_bounce_scheme("myapp://home", False) == "myapp", (
            "a frontend_uri byte-equal to OAUTH_REDIRECT_URI is trusted even "
            "without the allowlist")
    with _oauth_redirect_uri("myapp://elsewhere"):
        assert oauth._vetted_bounce_scheme("myapp://home", False) == "", (
            "a frontend_uri NOT byte-equal to OAUTH_REDIRECT_URI stays untrusted")
