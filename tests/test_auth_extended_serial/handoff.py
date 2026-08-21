"""
Cross-origin auth handoff — the in-process allowlist tests.

Moved out of tests/test_auth/handoff.py into this opt-in serial package
(maestro item #1839): every test here reconfigures the allowlist by
assigning/deleting AUTH_HANDOFF_ALLOWED_URLS / AUTH_HANDOFF_RESOLVER on
django.conf.settings (via the _allowlist/_unconfigured contextmanagers or
directly), which is a process-global mutation racing parallel test threads
even with a try/finally restore.

The security posture under test is unchanged: destination allowlisting,
confusable-host refusal, dot-segment escapes, scheme rules, fail-closed
resolver behavior. The service-layer, endpoint, and template-regression
tests stay in tests/test_auth/handoff.py.
"""
import contextlib

from testit import helpers as th
from testit.helpers import assert_true, assert_eq


def assert_false(value, msg):
    """Local mirror of assert_true — this module is mostly "must be refused"
    assertions, and `assert_true(not x, ...)` reads backwards for every one.
    testit.helpers has no assert_false today."""
    assert not value, msg


# A destination the pinned test-project allowlist admits (used to prove an
# empty in-process allowlist refuses even a "good" destination).
ALLOWED_DEST = "https://example.com/app"


# ---------------------------------------------------------------------------
# Resolver fixtures — addressed by the name testit loads this module under
# (tests/ is on sys.path), so load_function() resolves the SAME module object.
# ---------------------------------------------------------------------------

def _resolver_allow_all(url, request=None):
    return True


def _resolver_deny_all(url, request=None):
    return False


def _resolver_raises(url, request=None):
    raise RuntimeError("resolver blew up")


@contextlib.contextmanager
def _allowlist(urls=None, resolver=None):
    """Point the in-process allowlist at a specific configuration.

    In-process only: opts.client talks to a separate server process which keeps
    the pinned test-project settings, so this never leaks into endpoint tests.
    """
    from django.conf import settings as django_settings
    from mojo.apps.account.services import redirect_allowlist

    missing = object()
    prev_urls = getattr(django_settings, "AUTH_HANDOFF_ALLOWED_URLS", missing)
    prev_resolver = getattr(django_settings, "AUTH_HANDOFF_RESOLVER", missing)
    django_settings.AUTH_HANDOFF_ALLOWED_URLS = list(urls or [])
    django_settings.AUTH_HANDOFF_RESOLVER = resolver or ""
    redirect_allowlist._reset_cache_for_tests()
    try:
        yield
    finally:
        if prev_urls is missing:
            delattr(django_settings, "AUTH_HANDOFF_ALLOWED_URLS")
        else:
            django_settings.AUTH_HANDOFF_ALLOWED_URLS = prev_urls
        if prev_resolver is missing:
            delattr(django_settings, "AUTH_HANDOFF_RESOLVER")
        else:
            django_settings.AUTH_HANDOFF_RESOLVER = prev_resolver
        redirect_allowlist._reset_cache_for_tests()


@contextlib.contextmanager
def _unconfigured():
    """Remove BOTH handoff settings in-process — the shipped default state."""
    from django.conf import settings as django_settings
    from mojo.apps.account.services import redirect_allowlist

    missing = object()
    prev_urls = getattr(django_settings, "AUTH_HANDOFF_ALLOWED_URLS", missing)
    prev_resolver = getattr(django_settings, "AUTH_HANDOFF_RESOLVER", missing)
    for key in ("AUTH_HANDOFF_ALLOWED_URLS", "AUTH_HANDOFF_RESOLVER"):
        if hasattr(django_settings, key):
            delattr(django_settings, key)
    redirect_allowlist._reset_cache_for_tests()
    try:
        yield
    finally:
        if prev_urls is not missing:
            django_settings.AUTH_HANDOFF_ALLOWED_URLS = prev_urls
        if prev_resolver is not missing:
            django_settings.AUTH_HANDOFF_RESOLVER = prev_resolver
        redirect_allowlist._reset_cache_for_tests()


# ---------------------------------------------------------------------------
# Enforcement is opt-in — the setting is the switch (no flag day on upgrade)
# ---------------------------------------------------------------------------

@th.django_unit_test("opt-in: nothing configured is monitor mode, not enforcement")
def test_enforcement_is_off_when_unconfigured(opts):
    from mojo.apps.account.services import redirect_allowlist as ra
    with _unconfigured():
        assert_true(not ra.is_enforced(),
                    "with neither setting present the handoff must NOT be enforced — "
                    "an upgrading deployment has to keep working unchanged")


@th.django_unit_test("opt-in: an empty allowlist is a deliberate enforce-and-allow-nothing")
def test_empty_allowlist_is_enforcement(opts):
    from mojo.apps.account.services import redirect_allowlist as ra
    with _allowlist(urls=[]):
        assert_true(ra.is_enforced(),
                    "AUTH_HANDOFF_ALLOWED_URLS = [] is SET, so enforcement must be on")
        assert_true(not ra.is_allowed_destination(ALLOWED_DEST),
                    "an empty allowlist must then admit nothing")


@th.django_unit_test("opt-in: a resolver alone turns enforcement on")
def test_resolver_alone_is_enforcement(opts):
    from django.conf import settings as django_settings
    from mojo.apps.account.services import redirect_allowlist as ra
    with _unconfigured():
        django_settings.AUTH_HANDOFF_RESOLVER = f"{__name__}._resolver_allow_all"
        ra._reset_cache_for_tests()
        try:
            assert_true(ra.is_enforced(),
                        "a configured resolver must turn enforcement on without a list")
        finally:
            delattr(django_settings, "AUTH_HANDOFF_RESOLVER")
            ra._reset_cache_for_tests()


# ---------------------------------------------------------------------------
# Destination allowlist — the mint-time gate
# ---------------------------------------------------------------------------

@th.django_unit_test("allowlist: allowed host + path prefix is admitted")
def test_allowlist_admits_allowed_destination(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    with _allowlist(["https://app.example.com/"]):
        assert_true(ra.is_allowed_destination("https://app.example.com/"),
                    "the allowlisted root must be admitted")
        assert_true(ra.is_allowed_destination("https://app.example.com/dash?next=1"),
                    "a path under the allowlisted prefix must be admitted")
        assert_true(ra.is_allowed_destination("https://APP.Example.COM/dash"),
                    "host comparison must be case-folded")


@th.django_unit_test("allowlist: a foreign host is refused")
def test_allowlist_refuses_foreign_host(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    with _allowlist(["https://app.example.com/"]):
        assert_false(ra.is_allowed_destination("https://evil.tld/steal"),
                     "an unlisted host must never be admitted")
        assert_false(ra.is_allowed_destination("https://other.example.com/"),
                     "a sibling host that is not listed must not be admitted")


@th.django_unit_test("allowlist: confusable hosts are refused")
def test_allowlist_refuses_confusable_hosts(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    with _allowlist(["https://example.com/"]):
        assert_false(ra.is_allowed_destination("https://example.com.evil.tld/"),
                     "a suffix-extended host must not be admitted (example.com.evil.tld)")
        assert_false(ra.is_allowed_destination("https://example.com@evil.tld/"),
                     "userinfo confusable must not be admitted (real host is evil.tld)")
        # The one-sided parser differential: `_HOST_CHARS` is applied to
        # `parts.hostname`, which has ALREADY discarded the userinfo, so a
        # backslash hidden before the `@` never reaches it. Python reads the
        # host as example.com; a WHATWG browser terminates the authority at the
        # backslash and navigates to evil.tld. The mirror form below (backslash
        # inside the host) was always refused — this one was not.
        assert_false(ra.is_allowed_destination("https://evil.tld\\@example.com/"),
                     "a backslash before the @ must not be admitted — Python "
                     "parses the host as example.com, a browser as evil.tld")
        assert_false(ra.is_allowed_destination("https://example.com\\.evil.tld/"),
                     "a backslash inside the authority must not be admitted")
        assert_false(ra.is_allowed_destination("https://evil.tld/?to=https://example.com/"),
                     "an allowed URL in the query string must not admit a foreign host")
        assert_false(ra.is_allowed_destination("https://notexample.com/"),
                     "a host merely containing the allowed one must not be admitted")


@th.django_unit_test("allowlist: wildcard admits one label, refuses two")
def test_allowlist_wildcard_label_depth(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    with _allowlist(["https://*.example.com/"]):
        assert_true(ra.is_allowed_destination("https://a.example.com/x"),
                    "*.example.com must admit a single label")
        assert_true(ra.is_allowed_destination("https://example.com/x"),
                    "*.example.com must admit the base host itself")
        assert_false(ra.is_allowed_destination("https://a.b.example.com/x"),
                     "*.example.com must NOT admit two labels")
        assert_false(ra.is_allowed_destination("https://example.com.evil.tld/x"),
                     "*.example.com must NOT admit a suffix-extended host")
        assert_false(ra.is_allowed_destination("https://xexample.com/x"),
                     "*.example.com must NOT admit a host missing the dot boundary")


@th.django_unit_test("allowlist: path prefix stops at a segment boundary")
def test_allowlist_path_prefix_is_segment_bounded(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    with _allowlist(["https://example.com/app"]):
        assert_true(ra.is_allowed_destination("https://example.com/app"),
                    "the exact allowlisted path must be admitted")
        assert_true(ra.is_allowed_destination("https://example.com/app/inner"),
                    "a path under the allowlisted prefix must be admitted")
        assert_false(ra.is_allowed_destination("https://example.com/application"),
                     "a sibling path sharing a string prefix must NOT be admitted")
        assert_false(ra.is_allowed_destination("https://example.com/other"),
                     "a path outside the prefix must not be admitted")


@th.django_unit_test("allowlist: a dot-segment path escape is refused")
def test_allowlist_refuses_a_dot_segment_escape(opts):
    """A `..` (or `%2e%2e`) segment sits UNDER the prefix as a string but the
    browser resolves it OUT of the prefix before the request — so it must be
    refused, not admitted. Refusal, not normalization (see
    redirect_allowlist._has_dot_segment)."""
    from mojo.apps.account.services import redirect_allowlist as ra

    with _allowlist(["https://example.com/app"]):
        assert_false(ra.is_allowed_destination("https://example.com/app/../../steal"),
                     "a `..` traversal under the prefix resolves out of it and must be refused")
        assert_false(ra.is_allowed_destination("https://example.com/app/%2e%2e/steal"),
                     "the `%2e%2e` spelling of the same traversal must be refused")
        assert_true(ra.is_allowed_destination("https://example.com/app/inner"),
                    "a real path under the prefix must still be admitted")


@th.django_unit_test("allowlist: scheme must be http/https and must not downgrade")
def test_allowlist_scheme_rules(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    with _allowlist(["https://example.com/"]):
        assert_false(ra.is_allowed_destination("http://example.com/"),
                     "an https entry must not admit an http destination")
        assert_false(ra.is_allowed_destination("javascript:alert(1)"),
                     "javascript: must never be a handoff destination")
        assert_false(ra.is_allowed_destination("data:text/html,<script>x</script>"),
                     "data: must never be a handoff destination")
        assert_false(ra.is_allowed_destination("//example.com/"),
                     "a protocol-relative URL has no scheme and must be refused")
        assert_false(ra.is_allowed_destination("/relative/path"),
                     "a relative path is not a cross-origin destination and must be refused")
        assert_false(ra.is_allowed_destination(""),
                     "an empty destination must be refused")
        assert_false(ra.is_allowed_destination(None),
                     "a missing destination must be refused")


@th.django_unit_test("allowlist: nothing configured refuses every destination")
def test_allowlist_fails_closed_when_unconfigured(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    with _allowlist([], resolver=""):
        assert_false(ra.is_allowed_destination("https://example.com/"),
                     "with no allowlist and no resolver, every destination must be refused")
        assert_false(ra.is_allowed_destination("https://app.example.com/dash"),
                     "fail-closed must not depend on the shape of the URL")


@th.django_unit_test("allowlist: resolver decides when configured")
def test_allowlist_resolver_decides(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    with _allowlist([], resolver=f"{__name__}._resolver_allow_all"):
        assert_true(ra.is_allowed_destination("https://anything.tld/x"),
                    "a resolver returning True must admit a host no static entry covers")

    with _allowlist(["https://example.com/"], resolver=f"{__name__}._resolver_deny_all"):
        assert_false(ra.is_allowed_destination("https://example.com/"),
                     "a resolver returning False must refuse even a statically-listed host")


@th.django_unit_test("allowlist: a resolver that raises refuses (never fails open)")
def test_allowlist_resolver_exception_is_closed(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    with _allowlist(["https://example.com/"], resolver=f"{__name__}._resolver_raises"):
        assert_false(ra.is_allowed_destination("https://example.com/"),
                     "a raising resolver must be treated as REFUSED, not fall through to the list")
        assert_false(ra.is_allowed_destination("https://evil.tld/"),
                     "a raising resolver must refuse everything")


@th.django_unit_test("allowlist: an unimportable resolver path refuses")
def test_allowlist_broken_resolver_path_is_closed(opts):
    from mojo.apps.account.services import redirect_allowlist as ra

    with _allowlist(["https://example.com/"], resolver="no.such.module.resolver_fn"):
        assert_false(ra.is_allowed_destination("https://example.com/"),
                     "a resolver dotted path that fails to import must refuse everything")
