"""
edge validators — the layer that makes nginx injection unreachable.

The workspec for item #1433 asks that operator-controllable values be
**rejected**, not escaped. These tests assert exactly that: every metacharacter
case below raises, and none of them reaches a rendered file with a backslash in
front of it.

Note what is NOT tested here, deliberately: there is no "escaping" helper to
test, because there isn't one. A value is either derived from a foreign key or
matched against a whitelist regex.
"""

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    API_HOSTNAME, clear_reserved_names, declare_reserved_names, raises,
)


# Every one of these is a documented nginx-injection attempt. `\n` and `\r\n`
# matter most: a newline is what turns one directive into two.
INJECTION_STRINGS = [
    "example.com; } server { listen 443; root /etc; #",
    "www;",
    "www}",
    "www{",
    "www#comment",
    "www\nserver_name evil.com",
    "www\r\nroot /etc",
    "www$host",
    'www"',
    "www'",
    "www ",
    " www",
    "www\x00",
    "../../etc",
    "/opt/api/var",
    "..%2f..",
    "www.example.com",       # a dot climbs into another zone
    "-www",                  # a label may not start with a hyphen
    "www-",
    "WWW",                   # uppercase is not normalised, it is refused
]


@th.django_unit_setup()
def setup_validators(opts):
    declare_reserved_names()
    declare_pools()


@th.django_unit_test("label rejects every nginx metacharacter and path escape")
def test_label_rejects_injection(opts):
    from mojo.apps.edge import validators

    for candidate in INJECTION_STRINGS:
        err = raises(validators.validate_label, candidate)
        assert err is not None, \
            f"validate_label accepted {candidate!r} — that value can reach nginx"


@th.django_unit_test("a TRAILING newline is refused by every validator")
def test_trailing_newline_everywhere(opts):
    """Python's `$` matches before a trailing newline. `\\Z` does not.

    REGRESSION. Every regex in `validators.py` was written with `$`, and every
    one of them accepted a trailing "\\n" — `LABEL_RE.match("www\\n")` was True,
    which puts a newline directly into an nginx config file. A test on the
    release-version field caught it; the same bug was present in all six
    patterns, so all six are pinned here.
    """
    from mojo.apps.edge import validators

    cases = [
        (validators.validate_label, "www\n"),
        (validators.validate_label, "www\r\n"),
        (validators.validate_upstream_host, "example.com\n"),
        (validators.validate_pool, "default\n"),
        (validators.validate_release_version, "v1\n"),
        (validators.validate_server_name, "www.example.com\n"),
    ]
    for func, candidate in cases:
        err = raises(func, candidate)
        assert err is not None, (
            f"{func.__name__} accepted {candidate!r} — a trailing newline "
            f"reaches the config file")

    err = raises(validators.validate_manifest,
                 [dict(path="index.html\n", sha256="a" * 64, size=1)])
    assert err is not None, "validate_manifest accepted a path with a newline"

    err = raises(validators.validate_manifest,
                 [dict(path="index.html", sha256="a" * 64 + "\n", size=1)])
    assert err is not None, "validate_manifest accepted a sha256 with a newline"


@th.django_unit_test("label accepts apex, wildcard and ordinary labels")
def test_label_accepts_legitimate(opts):
    from mojo.apps.edge import validators

    for candidate in ["", "*", "www", "a", "api-v2", "x9", "a" * 63]:
        err = raises(validators.validate_label, candidate)
        assert err is None, \
            f"validate_label rejected the legitimate label {candidate!r}: {err}"


@th.django_unit_test("a label longer than 63 characters is refused")
def test_label_length(opts):
    from mojo.apps.edge import validators

    err = raises(validators.validate_label, "a" * 64)
    assert err is not None, "validate_label accepted a 64-character label"


@th.django_unit_test("server_name is derived from the domain, never concatenated free text")
def test_server_name_derivation(opts):
    from mojo.apps.edge import validators

    assert validators.server_name_for("example.com", "") == "example.com", \
        "an empty label must serve the apex"
    assert validators.server_name_for("example.com", "www") == "www.example.com", \
        "a label must prefix the domain"
    assert validators.server_name_for("example.com", "*") == "*.example.com", \
        "a wildcard label must render as the wildcard name"


@th.django_unit_test("reserved names refuse the API's own hostname")
def test_reserved_names(opts):
    from mojo.apps.edge import validators

    err = raises(validators.validate_not_reserved, API_HOSTNAME)
    assert err is not None, \
        "a vhost was allowed to claim the API's own hostname — that is the shadowing attack"
    assert raises(validators.validate_not_reserved, API_HOSTNAME.upper()) is not None, \
        "the reserved check must be case-insensitive"
    assert raises(validators.validate_not_reserved, "tenant.example.com") is None, \
        "an unrelated name must be allowed"


@th.django_unit_test("with no declared hostnames the validator FAILS CLOSED")
def test_reserved_names_fail_closed(opts):
    """A deployment that cannot name itself cannot protect itself.

    The test project ships ALLOWED_HOSTS = ["*"], so clearing the setting is
    genuinely the misconfigured state — not a contrived one.
    """
    from mojo.apps.edge import validators

    clear_reserved_names()
    try:
        assert validators.reserved_server_names() is None, \
            "with no concrete ALLOWED_HOSTS and no setting there is nothing to reserve"
        err = raises(validators.validate_not_reserved, "anything.example.com")
        assert err is not None, \
            "an undeclared deployment must refuse every vhost, not allow every name"
    finally:
        declare_reserved_names()
    declare_pools()


@th.django_unit_test("the instance metadata service can never be an upstream")
def test_upstream_rejects_metadata(opts):
    from mojo.apps.edge import validators

    for candidate in ["169.254.169.254", "169.254.0.1"]:
        err = raises(validators.validate_upstream_host, candidate)
        assert err is not None, \
            f"{candidate} was accepted as an upstream — that is the IMDS attack"


@th.django_unit_test("an upstream host is a host, never a URL or a path")
def test_upstream_host_shape(opts):
    from mojo.apps.edge import validators

    for candidate in [
        "http://169.254.169.254/", "http://example.com", "example.com/path",
        "example.com:8080", "example.com;", "example.com\nroot /etc",
        "exa mple.com", "", "-example.com",
    ]:
        err = raises(validators.validate_upstream_host, candidate)
        assert err is not None, \
            f"validate_upstream_host accepted {candidate!r}"


@th.django_unit_test("loopback and RFC1918 ARE valid upstreams for a platform admin")
def test_upstream_allows_private(opts):
    """The real upstream on every node is 127.0.0.1:<asgi port>.

    The workspec asked for RFC1918 to be rejected; that would forbid the
    primary use case. What actually stops a tenant reaching a private address
    is that only a platform admin may declare an Upstream at all — asserted in
    `2_rest_permissions.py`, not here.
    """
    from mojo.apps.edge import validators

    for candidate in ["127.0.0.1", "10.0.1.5", "192.168.1.10", "app.internal"]:
        err = raises(validators.validate_upstream_host, candidate)
        assert err is None, \
            f"validate_upstream_host rejected the legitimate upstream {candidate!r}: {err}"


@th.django_unit_test("a socket path outside the declared base is refused")
def test_socket_path_containment(opts):
    from mojo.apps.edge import validators

    base = validators.socket_base()
    for candidate in [
        "/etc/passwd", "/opt/api/var/django.conf", f"{base}/../../etc/passwd",
        f"{base}/..", "relative/path", f"{base}/ok;rm -rf /", "",
    ]:
        err = raises(validators.validate_socket_path, candidate)
        assert err is not None, \
            f"validate_socket_path accepted {candidate!r}"

    assert raises(validators.validate_socket_path, f"{base}/app.sock") is None, \
        "a socket directly under the base must be allowed"


@th.django_unit_test("containment uses normalisation, not string prefixes")
def test_containment_is_not_startswith(opts):
    """`/opt/wwwevil` starts with `/opt/www` and is NOT under it."""
    from mojo.apps.edge import validators

    assert not validators.contained_under("/opt/www", "/opt/wwwevil/x"), \
        "prefix matching accepted a sibling directory as contained"
    assert validators.contained_under("/opt/www", "/opt/www/1/current"), \
        "a genuine child was reported as not contained"


@th.django_unit_test("a port outside 1..65535 is refused")
def test_upstream_port(opts):
    from mojo.apps.edge import validators

    for candidate in [0, -1, 65536, "8000", None, True]:
        err = raises(validators.validate_upstream_port, candidate)
        assert err is not None, f"validate_upstream_port accepted {candidate!r}"
    assert raises(validators.validate_upstream_port, 8000) is None, \
        "8000 must be a valid port"


@th.django_unit_test("body_size_mb is bounded and typed")
def test_body_size_mb(opts):
    from mojo.apps.edge import validators

    for candidate in [0, -1, 4097, "50", None, True, 5.5]:
        err = raises(validators.validate_body_size_mb, candidate)
        assert err is not None, \
            f"validate_body_size_mb accepted {candidate!r}"
    for candidate in [1, 50, 4096]:
        err = raises(validators.validate_body_size_mb, candidate)
        assert err is None, \
            f"validate_body_size_mb rejected the legitimate {candidate!r}: {err}"


@th.django_unit_test("quiet paths reject nginx metacharacters and traversal")
def test_quiet_path_rejects_injection(opts):
    from mojo.apps.edge import validators

    hostile = [
        "health",                       # no leading slash
        "/health;",
        "/health{",
        "/health}",
        "/health#x",
        "/health$host",
        '/health"',
        "/health'",
        "/health x",
        "/health\n",
        "/health\\n",                   # charset refuses backslash outright
        "/health\x00",
        "/../etc/passwd",
        "/a/../b",
        "//double",
        "/a//b",
        "/" + "a" * 128,                # longer than the 128-char field
        "",
        None,
    ]
    for candidate in hostile:
        err = raises(validators.validate_quiet_path, candidate)
        assert err is not None, \
            f"validate_quiet_path accepted {candidate!r} — that value can reach nginx"

    for candidate in ["/", "/health", "/api/v1/health-check", "/ping.txt",
                      "/deep/path/ok/"]:
        err = raises(validators.validate_quiet_path, candidate)
        assert err is None, \
            f"validate_quiet_path rejected the legitimate {candidate!r}: {err}"


@th.django_unit_test("route prefixes follow the same whitelist and refuse bare '/'")
def test_route_prefix_shape(opts):
    from mojo.apps.edge import validators

    err = raises(validators.validate_route_prefix, "/")
    assert err is not None, \
        "a bare '/' route prefix was accepted — that is kind=api in disguise"

    for candidate in ["/api;", "/api\n", "api", "/a/../b", "//api", None]:
        err = raises(validators.validate_route_prefix, candidate)
        assert err is not None, \
            f"validate_route_prefix accepted {candidate!r}"

    for candidate in ["/api", "/api/", "/api/v2", "/ws"]:
        err = raises(validators.validate_route_prefix, candidate)
        assert err is None, \
            f"validate_route_prefix rejected the legitimate {candidate!r}: {err}"


@th.django_unit_test("a redirect target is a host, never a URL, wildcard or free text")
def test_redirect_target_shape(opts):
    from mojo.apps.edge import validators

    hostile = [
        "https://example.com",
        "example.com/path",
        "example.com;",
        "example.com\n",
        "example.com; } server { root /etc; #",
        "*.example.com",
        "localhost",                    # single label — not an FQDN
        "",
        None,
    ]
    for candidate in hostile:
        err = raises(validators.validate_redirect_target, candidate)
        assert err is not None, \
            f"validate_redirect_target accepted {candidate!r}"

    err = raises(validators.validate_redirect_target, "www.example.com")
    assert err is None, \
        f"validate_redirect_target rejected a legitimate host: {err}"


@th.django_unit_test("wildcard certificate coverage follows the one-label TLS rule")
def test_certificate_coverage(opts):
    from mojo.apps.edge import validators

    class FakeCert:
        def __init__(self, cn, sans):
            self.common_name = cn
            self.sans = sans

    wildcard = FakeCert("*.example.com", ["*.example.com"])
    assert validators.certificate_covers(wildcard, "www.example.com"), \
        "a wildcard must cover one label"
    assert not validators.certificate_covers(wildcard, "example.com"), \
        "a wildcard must NOT cover the apex"
    assert not validators.certificate_covers(wildcard, "a.b.example.com"), \
        "a wildcard must NOT cover two labels"
    assert validators.certificate_covers(wildcard, "*.example.com"), \
        "a wildcard vhost is covered by the identical wildcard certificate"

    apex = FakeCert("example.com", ["example.com", "www.example.com"])
    assert validators.certificate_covers(apex, "example.com"), \
        "an exact SAN must match"
    assert validators.certificate_covers(apex, "WWW.example.com"), \
        "coverage must be case-insensitive"
    assert not validators.certificate_covers(apex, "other.example.com"), \
        "an unlisted name must not be covered"
