"""
Unit tests for mojo.helpers.urls.safe_nav_url — the server-side scheme guard
for a caller-supplied navigation target that gets rendered into an href.

Contract this file enforces:
  - A value whose scheme is neither http nor https is refused
  - A scheme-less value (path-relative or scheme-relative) is admitted —
    the host is deliberately NOT restricted
  - A safe value comes back byte-identical; no normalization to absolute
  - Non-string input (list/dict/int/None) is refused, not coerced
  - An unparsable authority raises nothing — it is refused
  - A refused value returns the caller-supplied default
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "core"  # #2792 tier curation


@th.django_unit_test("safe_nav_url: refuses javascript: and every other non-web scheme")
def test_refuses_script_and_non_web_schemes(opts):
    from mojo.helpers import urls

    refused = [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "java\tscript:alert(1)",
        "java\nscript:alert(1)",
        "java\rscript:alert(1)",
        " javascript:alert(1)",
        "\x01javascript:alert(1)",
        "\njavascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox(1)",
        "mailto:someone@example.com",
        "tel:+15555550100",
        "myapp://home",
        "file:///etc/passwd",
    ]
    for value in refused:
        assert_eq(
            urls.safe_nav_url(value), "",
            f"safe_nav_url must refuse {value!r} — a non-http(s) scheme reaching an href is a script-execution sink",
        )


@th.django_unit_test("safe_nav_url: admits http, https, and scheme-less values unchanged")
def test_allows_http_https_and_relative(opts):
    from mojo.helpers import urls

    allowed = [
        "https://example.com/dashboard",
        "https://EXAMPLE.com:8443/x",
        "http://localhost:3000/cb",
        "http://127.0.0.1:5555/done",
        "/dashboard",
        "/a?b=c#d",
        "dashboard/settings",
        # Scheme-relative: admitted on purpose. The acceptance criteria allow a
        # cross-origin https destination, so refusing its equivalent spelling
        # would be incoherent. See the helper docstring.
        "//example.com/x",
        # Percent-encoded colon is a relative path to a browser, not a scheme.
        "javascript%3Aalert(1)",
    ]
    for value in allowed:
        assert_eq(
            urls.safe_nav_url(value), value,
            f"safe_nav_url must admit {value!r} unchanged — the host is deliberately not restricted "
            f"and relative paths must keep working",
        )


@th.django_unit_test("safe_nav_url: refuses non-string input and an unparsable authority")
def test_refuses_non_string_and_unparsable(opts):
    from mojo.helpers import urls

    # request.DATA yields a list for ?redirect=a&redirect=b and a dict for
    # ?redirect.x=1 — where request.GET.get() always yielded a string. Refusing
    # is the fail-closed answer.
    refused = [
        None,
        "",
        [],
        ["/a", "/b"],
        {"x": "1"},
        123,
        True,
        # urlsplit raises ValueError on a malformed IPv6 authority.
        "http://[::1/x",
        "https://[fe80::1",
    ]
    for value in refused:
        assert_eq(
            urls.safe_nav_url(value), "",
            f"safe_nav_url must refuse {value!r} rather than coerce or raise",
        )


@th.django_unit_test("safe_nav_url: returns the value verbatim or the caller's default")
def test_safe_nav_url_returns_value_or_default(opts):
    from mojo.helpers import urls

    # Safe values are returned byte-identically — NOT normalized to absolute.
    # Normalizing "/dashboard" into "https://host/dashboard" would break the
    # "relative paths unchanged" contract.
    same = "/dashboard?next=/a"
    result = urls.safe_nav_url(same)
    assert_true(
        result is same or result == same,
        f"A safe value must come back byte-identical, got {result!r} for {same!r}",
    )

    assert_eq(
        urls.safe_nav_url("javascript:alert(1)", "/home"), "/home",
        "A refused value must return the caller-supplied default",
    )
    assert_eq(
        urls.safe_nav_url(None, "/home"), "/home",
        "Non-string input must return the caller-supplied default",
    )
    assert_eq(
        urls.safe_nav_url("https://example.com/x", "/home"), "https://example.com/x",
        "The default must not be substituted for a value that passes",
    )
