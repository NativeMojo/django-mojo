"""
The resource registry, the two discovery documents, and the challenge header.

Everything here runs in-process against a PRIVATE ``ResourceRegistry``. The
shared default instance is module-level state read by every parallel test
module in the runner's process, so mutating it would make these assertions
order-dependent — the `registry=` seam exists for exactly this.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

RESOURCE_PATH = "/api/testit/oauth-registry-probe"
ORIGIN = "https://oauth.testit.example"


PREFIX_PATH = "/api/testit/oauth-registry-root"


def _registry(enabled=True, path=RESOURCE_PATH, scopes=None):
    from mojo.apps.account.services.oauth_server import resources

    registry = resources.ResourceRegistry()
    registry.register(path, scopes or ["mcp"], lambda: enabled)
    return registry


@th.django_unit_test("register/resolve/unregister, replace-on-reregister")
def test_registry_lifecycle(opts):
    from mojo.apps.account.services.oauth_server import resources

    registry = resources.ResourceRegistry()
    registry.register(RESOURCE_PATH, ["mcp"], lambda: True)
    entry = registry.resolve(RESOURCE_PATH)
    assert_true(entry is not None,
                f"{RESOURCE_PATH} must resolve after registration")
    assert_eq(entry.scopes, ["mcp"],
              f"the registered scopes must be kept verbatim, got {entry.scopes}")

    # Re-registering replaces rather than duplicates, so a repeated ready()
    # is idempotent.
    registry.register(RESOURCE_PATH, ["mcp", "extra"], lambda: True)
    assert_eq(len(registry.paths()), 1,
              f"re-registering a path must replace it, paths={registry.paths()}")
    assert_eq(registry.resolve(RESOURCE_PATH).scopes, ["mcp", "extra"],
              "re-registering must replace the entry's scopes")

    assert_true(registry.unregister(RESOURCE_PATH) is not None,
                "unregister must return the removed entry")
    assert_true(registry.resolve(RESOURCE_PATH) is None,
                f"{RESOURCE_PATH} must not resolve after unregister")


@th.django_unit_test("a prefix resource is a subtree, and it must offer the api scope")
def test_prefix_resources(opts):
    from mojo.apps.account.services.oauth_server import discovery, resources

    registry = resources.ResourceRegistry()
    exact = registry.register(RESOURCE_PATH, ["mcp"], lambda: True)
    root = registry.register(PREFIX_PATH, ["mcp", "api"], lambda: True,
                             prefix=True)

    assert_true(root.prefix is True,
                "a resource registered with prefix=True must record it")
    assert_true(exact.prefix is False,
                "a resource registered without prefix must default to exact")

    # The reach table. `covers` is what bounds an api token; every row here is
    # a path an attacker would try.
    for entry, path, expected, why in (
            (root, PREFIX_PATH, True, "the root itself is covered"),
            (root, PREFIX_PATH + "/account/user/me", True,
             "a path strictly beneath the root is covered"),
            (root, PREFIX_PATH + "/", True,
             "the slashed form of the root is covered"),
            (root, PREFIX_PATH + "x/anything", False,
             "a SIBLING path that merely shares the prefix's characters must "
             "NOT be covered — that is the whole point of comparing with the "
             "separator attached"),
            (root, "/unrelated/path", False, "an unrelated path is not covered"),
            (root, "", False, "an empty request path is never covered"),
            (root, None, False, "a missing request path is never covered"),
            (exact, RESOURCE_PATH, True, "an exact resource covers its own path"),
            (exact, RESOURCE_PATH + "/x", False,
             "an exact resource must NEVER cover anything beneath it")):
        assert_eq(resources.covers(entry, path), expected,
                  f"covers({entry.path!r}, {path!r}) must be {expected} — {why}")
    assert_true(not resources.covers(None, PREFIX_PATH),
                "an absent entry covers nothing")

    # The invariant, enforced at registration in both directions.
    for scopes, prefix, why in (
            (["mcp"], True,
             "a prefix resource without the api scope would be full reach "
             "nobody had to consent to"),
            (["mcp", "api"], False,
             "an exact resource cannot honour the api scope, so it must not "
             "advertise it")):
        raised = False
        try:
            registry.register("/api/testit/oauth-registry-bad", scopes,
                              lambda: True, prefix=prefix)
        except ValueError:
            raised = True
        assert_true(raised,
                    f"register(scopes={scopes}, prefix={prefix}) must raise — {why}")
    assert_true(registry.resolve("/api/testit/oauth-registry-bad") is None,
                "a refused registration must not leave an entry behind")

    # A prefix resource at the SITE ROOT is refused outright. `covers` compares
    # against `path.rstrip("/") + "/"`, so "/" would cover every path this host
    # serves — the hosted sign-in pages included — not merely the REST API.
    for root in ("/", "//"):
        raised = False
        try:
            registry.register(root, ["mcp", "api"], lambda: True, prefix=True)
        except ValueError:
            raised = True
        assert_true(raised,
                    f"register({root!r}, prefix=True) must raise — a prefix "
                    f"resource at the site root is full reach over the whole "
                    f"host, which no consent screen can honestly describe")
        assert_true(registry.resolve(root) is None,
                    f"the refused root registration must leave no entry at {root!r}")
    # An EXACT resource at "/" is still allowed — it is one path, not a subtree.
    exact_root = resources.ResourceRegistry()
    exact_root.register("/", ["mcp"], lambda: True)
    assert_true(exact_root.resolve("/") is not None,
                "the refusal must be about the prefix flag, not the path: an "
                "exact resource at / covers exactly one request path")

    # Discovery advertises the union of what the ENABLED entries offer.
    assert_eq(discovery.authorization_server_metadata(
        ORIGIN, registry=registry)["scopes_supported"], ["mcp", "api"],
        "scopes_supported must be the ordered union of the enabled resources")
    mcp_only = resources.ResourceRegistry()
    mcp_only.register(RESOURCE_PATH, ["mcp"], lambda: True)
    assert_eq(discovery.authorization_server_metadata(
        ORIGIN, registry=mcp_only)["scopes_supported"], ["mcp"],
        "an installation that registers no prefix resource must never "
        "advertise the api scope")
    off = resources.ResourceRegistry()
    off.register(RESOURCE_PATH, ["mcp"], lambda: True)
    off.register(PREFIX_PATH, ["mcp", "api"], lambda: False, prefix=True)
    assert_eq(resources.offered_scopes(off), ["mcp"],
              "a switched-off prefix resource must not contribute its scopes")

    # The PRM lives at the registered path and nowhere beneath it, so a client
    # never gets a document whose `resource` disagrees with what it asked for.
    document = discovery.protected_resource_metadata(
        ORIGIN, PREFIX_PATH, registry=registry)
    assert_eq(document["resource"], f"{ORIGIN}{PREFIX_PATH}",
              f"the prefix resource's PRM must name the root, got {document!r}")
    assert_eq(document["scopes_supported"], ["mcp", "api"],
              f"the prefix resource must publish both scopes, got {document!r}")
    assert_true(discovery.protected_resource_metadata(
        ORIGIN, PREFIX_PATH + "/account", registry=registry) is None,
        "a path BENEATH a prefix resource must have no protected-resource "
        "metadata of its own — resolve stays exact")


@th.django_unit_test("the enable callable is re-read on every check")
def test_enabled_callable_is_live(opts):
    from mojo.apps.account.services.oauth_server import resources

    state = {"on": False}
    registry = resources.ResourceRegistry()
    registry.register(RESOURCE_PATH, ["mcp"], lambda: state["on"])
    entry = registry.resolve(RESOURCE_PATH)

    assert_true(not registry.is_enabled(entry),
                "a resource whose switch reads False must not be enabled")
    state["on"] = True
    assert_true(registry.is_enabled(entry),
                "flipping the switch must take effect with no re-registration")
    assert_eq(len(registry.enabled()), 1,
              "the enabled list must follow the live switch")


@th.django_unit_test("an enable callable that raises counts as disabled")
def test_enabled_callable_fails_closed(opts):
    from mojo.apps.account.services.oauth_server import resources

    def boom():
        raise RuntimeError("the switch is unreadable")

    registry = resources.ResourceRegistry()
    registry.register(RESOURCE_PATH, ["mcp"], boom)
    entry = registry.resolve(RESOURCE_PATH)
    assert_true(not registry.is_enabled(entry),
                "an enable callable that raises must be treated as OFF")
    assert_eq(registry.enabled(), [],
              "an unreadable switch must keep the resource out of enabled()")


@th.django_unit_test("authorization-server metadata: None unless configured, full shape otherwise")
def test_authorization_server_metadata(opts):
    from mojo.apps.account.services.oauth_server import discovery, resources

    live = _registry(enabled=True)
    assert_true(discovery.authorization_server_metadata("", registry=live) is None,
                "no BASE_URL must mean no authorization-server metadata")
    assert_true(
        discovery.authorization_server_metadata(
            ORIGIN, registry=_registry(enabled=False)) is None,
        "no ENABLED resource must mean no authorization-server metadata")

    document = discovery.authorization_server_metadata(ORIGIN, registry=live)
    assert_true(document is not None,
                "a configured origin with a live resource must publish metadata")
    issuer = f"{ORIGIN}{resources.SERVER_PATH}"
    assert_eq(document["issuer"], issuer,
              f"issuer must be the path-suffixed server root, got {document['issuer']}")
    for name, expected in (
            ("authorization_endpoint", f"{issuer}/authorize"),
            ("token_endpoint", f"{issuer}/token"),
            ("registration_endpoint", f"{issuer}/register"),
            ("revocation_endpoint", f"{issuer}/revoke")):
        assert_eq(document[name], expected,
                  f"{name} must be {expected}, got {document[name]}")
    assert_eq(document["code_challenge_methods_supported"], ["S256"],
              "S256 must be the only PKCE method offered")
    assert_eq(document["token_endpoint_auth_methods_supported"], ["none"],
              "the server issues no client secrets, so only `none` is offered")
    assert_eq(document["grant_types_supported"],
              ["authorization_code", "refresh_token"],
              "only the authorization-code and refresh grants are supported")
    assert_true(document["authorization_response_iss_parameter_supported"] is True,
                "RFC 9207 `iss` support must be advertised")
    assert_true(document["client_id_metadata_document_supported"] is True,
                "CIMD support must be advertised")
    assert_eq(document["scopes_supported"], ["mcp"],
              "mcp is the only scope this server offers")


@th.django_unit_test("protected-resource metadata: only for a live, registered path")
def test_protected_resource_metadata(opts):
    from mojo.apps.account.services.oauth_server import discovery, resources

    live = _registry(enabled=True)
    assert_true(
        discovery.protected_resource_metadata("", RESOURCE_PATH, registry=live) is None,
        "no BASE_URL must mean no protected-resource metadata")
    assert_true(
        discovery.protected_resource_metadata(
            ORIGIN, "/api/testit/not-registered", registry=live) is None,
        "an unregistered path must have no protected-resource metadata")
    assert_true(
        discovery.protected_resource_metadata(
            ORIGIN, RESOURCE_PATH, registry=_registry(enabled=False)) is None,
        "a disabled resource must have no protected-resource metadata")

    document = discovery.protected_resource_metadata(
        ORIGIN, RESOURCE_PATH, registry=live)
    assert_eq(document["resource"], f"{ORIGIN}{RESOURCE_PATH}",
              f"resource must be the canonical URL, got {document['resource']}")
    assert_eq(document["authorization_servers"],
              [f"{ORIGIN}{resources.SERVER_PATH}"],
              "the only authorization server is this installation's")
    assert_eq(document["scopes_supported"], ["mcp"],
              "the resource's own scopes must be published")
    assert_eq(document["bearer_methods_supported"], ["header"],
              "only the Authorization header carries the token")


@th.django_unit_test("www_authenticate builds both forms and quotes safely")
def test_www_authenticate(opts):
    from mojo.apps.account.services.oauth_server import discovery, resources

    header = discovery.www_authenticate(RESOURCE_PATH)
    assert_true(header.startswith("Bearer "),
                f"the challenge must use the Bearer scheme, got {header!r}")
    assert_true('error="invalid_token"' in header,
                f"the default challenge must name invalid_token, got {header!r}")

    origin = resources.public_origin()
    if origin:
        assert_true(
            f'resource_metadata="{resources.prm_url(origin, RESOURCE_PATH)}"' in header,
            f"a configured origin must publish resource_metadata, got {header!r}")
    else:
        assert_true("resource_metadata=" not in header,
                    f"an unset origin must omit resource_metadata, got {header!r}")

    scoped = discovery.www_authenticate(
        RESOURCE_PATH, error="insufficient_scope", scope="mcp")
    assert_true('error="insufficient_scope"' in scoped,
                f"the 403 form must name insufficient_scope, got {scoped!r}")
    assert_true('scope="mcp"' in scoped,
                f"the 403 form must name the required scope, got {scoped!r}")

    nasty = discovery.www_authenticate(
        RESOURCE_PATH, description='say "hi"\r\nX-Injected: 1')
    assert_true("\r" not in nasty and "\n" not in nasty,
                f"CR/LF must never survive into a header value, got {nasty!r}")
    assert_true('error_description="say \\"hi\\"X-Injected: 1"' in nasty,
                f"a quote in a description must be escaped, got {nasty!r}")


@th.django_unit_test("the five OAuth settings carry their documented defaults")
def test_settings_defaults(opts):
    from mojo.apps.account.services.oauth_server import resources

    assert_eq(resources.SERVER_PATH, "/api/account/oauth",
              f"OAUTH_SERVER_PATH default must be api/account/oauth, "
              f"got {resources.SERVER_PATH}")
    assert_eq(resources.access_ttl(), 3600,
              f"OAUTH_ACCESS_TTL default must be 3600, got {resources.access_ttl()}")
    assert_eq(resources.refresh_ttl_days(), 30,
              f"OAUTH_REFRESH_TTL_DAYS default must be 30, "
              f"got {resources.refresh_ttl_days()}")
    assert_eq(resources.refresh_grace_seconds(), 30,
              f"OAUTH_REFRESH_GRACE_SECONDS default must be 30, "
              f"got {resources.refresh_grace_seconds()}")
    assert_eq(resources.code_ttl(), 300,
              f"OAUTH_CODE_TTL default must be 300, got {resources.code_ttl()}")


@th.django_unit_test("credential-bearing OAuth paths are labelled for request logging")
def test_sensitive_body_label(opts):
    from objict import objict
    from mojo.helpers import request as request_helpers
    from mojo.apps.account.services.oauth_server import resources

    root = resources.SERVER_PATH
    for path in (f"{root}/token", f"{root}/revoke"):
        label = request_helpers.sensitive_body_label(
            objict(path=path, method="POST"))
        assert_eq(label, "oauth_token",
                  f"POST {path} carries a credential and must be labelled "
                  f"oauth_token, got {label!r}")
    approve = request_helpers.sensitive_body_label(
        objict(path=f"{root}/approve", method="POST"))
    assert_eq(approve, "oauth_approve",
              f"POST {root}/approve carries the session bearer and must be "
              f"labelled oauth_approve, got {approve!r}")
    register = request_helpers.sensitive_body_label(
        objict(path=f"{root}/register", method="POST"))
    assert_true(register is None,
                f"registration carries no secret and must not be labelled, "
                f"got {register!r}")


@th.django_unit_test("the absolute OAuth routes resolve to the server handlers")
def test_routes_are_not_shadowed(opts):
    from django.urls import resolve as url_resolve
    from mojo.decorators.http import URLPATTERN_METHODS
    from mojo.apps.account.rest import oauth_server as rest_oauth
    from mojo.apps.account.services.oauth_server import resources

    def _handler(path, method):
        match = url_resolve(path)
        root_key = match.kwargs.get("__mojo_rest_root_key__")
        assert_true(root_key is not None,
                    f"{path} must route through the mojo dispatcher")
        return URLPATTERN_METHODS.get(f"{root_key}__{method}")

    token = _handler(f"{resources.SERVER_PATH}/token", "POST")
    assert_true(token is rest_oauth.on_token,
                f"{resources.SERVER_PATH}/token must reach the OAuth token "
                f"handler, got {token}")
    metadata = _handler(
        f"/.well-known/oauth-authorization-server{resources.SERVER_PATH}", "GET")
    assert_true(metadata is rest_oauth.on_authorization_server_metadata,
                f"the path-suffixed discovery route must reach the metadata "
                f"handler, got {metadata}")
