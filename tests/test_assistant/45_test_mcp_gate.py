"""The MCP door's acceptance checks, its invisibility while off, and its wiring.

Two halves, both default-tier:

* the token-kind gate, exercised in process against ``mcp/auth.refusal`` — the
  MCP door accepts MCP grants only, and each refusal carries exactly the header
  a spec client needs (or, for a permission miss, deliberately none);
* the switch. The generated test project leaves ``ASSISTANT_MCP_ENABLED``
  unset, which is the state this module asserts against: while the door is off
  it must be indistinguishable from a route that was never registered, and it
  must not touch its rate-limit bucket. Nothing here WRITES the switch — the
  enabled path is the serial wire module's job.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true


ADMIN_EMAIL = "mcp-gate-admin@example.com"
NOPERM_EMAIL = "mcp-gate-noperm@example.com"
TEST_PASSWORD = "TestPass1!"

CLIENT_ID = "testit-mcp-gate-client"
CLIENT_NAME = "Testit MCP Gate Client"
RESOURCE = "https://oauth.testit.example/api/assistant/mcp"
# The API-root resource an `api` grant is bound to. The door sits beneath it,
# so a root-bound token reaches this path — and the door's own scope check is
# what decides whether it may act here.
API_RESOURCE = "https://oauth.testit.example/api"

MCP_PATH = "/api/assistant/mcp"
API_KEY_NAME = "testit mcp gate key"
API_KEY_GROUP = "testit mcp gate group"
RATE_BUCKET = "assistant_mcp"


def _grant(user, client, scopes, resource=RESOURCE):
    from mojo.apps.account.services.oauth_server import tokens

    return tokens.create_grant(user, client, scopes, resource, 1700000000)


def _rate_keys():
    from mojo.helpers.redis import get_connection

    return list(get_connection().scan_iter(f"rl:{RATE_BUCKET}:*"))


def _clear_rate_keys():
    from mojo.helpers.redis import get_connection

    conn = get_connection()
    for key in list(conn.scan_iter(f"rl:{RATE_BUCKET}:*")):
        conn.delete(key)


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
@th.requires_app("mojo.apps.account")
def setup_mcp_gate(opts):
    from mojo.apps.account.models import ApiKey, Group, OAuthClient, User

    User.objects.filter(email__in=[ADMIN_EMAIL, NOPERM_EMAIL]).delete()
    OAuthClient.objects.filter(client_id=CLIENT_ID).delete()
    ApiKey.objects.filter(name=API_KEY_NAME).delete()
    Group.objects.filter(name=API_KEY_GROUP).delete()

    opts.admin = User.objects.create_user(
        username=ADMIN_EMAIL, email=ADMIN_EMAIL, password=TEST_PASSWORD)
    opts.admin.is_email_verified = True
    opts.admin.save()
    opts.admin.save_password(TEST_PASSWORD)
    for perm in ("view_admin", "assistant"):
        opts.admin.add_permission(perm)

    opts.noperm = User.objects.create_user(
        username=NOPERM_EMAIL, email=NOPERM_EMAIL, password=TEST_PASSWORD)
    opts.noperm.is_email_verified = True
    opts.noperm.save()
    opts.noperm.remove_all_permissions()

    opts.client_row = OAuthClient.objects.create(
        client_id=CLIENT_ID, kind="dcr", client_name=CLIENT_NAME,
        redirect_uris=["http://127.0.0.1:8500/cb"])
    opts.grant = _grant(opts.admin, opts.client_row, ["mcp"])
    opts.grant_noscope = _grant(opts.admin, opts.client_row, [])
    opts.grant_noperm = _grant(opts.noperm, opts.client_row, ["mcp"])
    # Root-bound grants: full API access alone does not open the tool door,
    # and asking for both does.
    opts.grant_api_only = _grant(
        opts.admin, opts.client_row, ["api"], API_RESOURCE)
    opts.grant_api_and_tools = _grant(
        opts.admin, opts.client_row, ["mcp", "api"], API_RESOURCE)

    group = Group.objects.create(name=API_KEY_GROUP, kind="organization")
    opts.api_key, _raw = ApiKey.create_for_group(
        group=group, name=API_KEY_NAME, permissions={"view_admin": True})


def _request(opts, user=None, **attrs):
    request = th.get_mock_request(
        user=user or opts.admin, path=MCP_PATH, method="POST")
    for key, value in attrs.items():
        setattr(request, key, value)
    return request


# ---------------------------------------------------------------------------
# The door's own checks
# ---------------------------------------------------------------------------

@th.django_unit_test("the MCP door accepts MCP grants only, and challenges the rest")
def test_refusal_token_kinds(opts):
    from mojo.apps.assistant.mcp import auth as mcp_auth

    for attrs, why in (
            ({}, "no credential at all"),
            ({"bearer": "bearer"}, "a browser session JWT"),
            ({"bearer": "apikey", "api_key": opts.api_key},
             "an ApiKey-backed session"),
            ({"bearer": "grouptoken", "group_token": opts.api_key},
             "a group-scoped token"),
            ({"bearer": "apikey", "api_key": opts.api_key,
              "oauth_grant": opts.grant},
             "a key-backed session that also carries a grant")):
        response = mcp_auth.refusal(_request(opts, **attrs))
        assert_true(response is not None, f"{why} must be refused at the MCP door")
        assert_eq(response.status_code, 401,
                  f"{why} must answer 401, got {response.status_code}")
        challenge = response.get("WWW-Authenticate", "")
        assert_true(challenge.startswith('Bearer error="invalid_token"'),
                    f"{why} must carry the RFC 9728 challenge, got {challenge!r}")

    response = mcp_auth.refusal(
        _request(opts, bearer="bearer", oauth_grant=opts.grant_noscope))
    assert_true(response is not None, "a grant without the mcp scope must be refused")
    assert_eq(response.status_code, 403,
              f"a missing scope is a 403, not a 401, got {response.status_code}")
    challenge = response.get("WWW-Authenticate", "")
    assert_true('error="insufficient_scope"' in challenge,
                f"the scope refusal must name the error, got {challenge!r}")
    assert_true('scope="mcp"' in challenge,
                f"the scope refusal must name the scope needed, got {challenge!r}")

    # `scopes` is a JSONField: a string value must not satisfy the membership
    # test by substring. Mutated on the in-memory object only — nothing saved.
    opts.grant_noscope.scopes = "mcpx"
    try:
        response = mcp_auth.refusal(
            _request(opts, bearer="bearer", oauth_grant=opts.grant_noscope))
        assert_true(response is not None and response.status_code == 403,
                    f"a non-list scopes value must never grant the mcp scope, "
                    f"got {response}")
    finally:
        opts.grant_noscope.scopes = []

    response = mcp_auth.refusal(
        _request(opts, user=opts.noperm, bearer="bearer",
                 oauth_grant=opts.grant_noperm))
    assert_true(response is not None,
                "an operator with neither view_admin nor assistant must be refused")
    assert_eq(response.status_code, 403,
              f"a permission miss is a 403, got {response.status_code}")
    assert_true(not response.has_header("WWW-Authenticate"),
                "a permission miss must carry NO challenge — re-authenticating "
                "cannot fix it")

    # Full API access is not tool-door access. The chokepoint lets a root-bound
    # token through to this path (the door is beneath the root), so this 403 is
    # the only thing keeping an `api`-only grant out of the tools.
    response = mcp_auth.refusal(
        _request(opts, bearer="bearer", oauth_grant=opts.grant_api_only))
    assert_true(response is not None,
                "an api-only grant must not open the Assistant's tool door")
    assert_eq(response.status_code, 403,
              f"an api-only grant is a scope miss, not a bad token, got "
              f"{response.status_code}")
    challenge = response.get("WWW-Authenticate", "")
    assert_true('error="insufficient_scope"' in challenge and 'scope="mcp"' in challenge,
                f"the refusal must tell the client to re-authorize with the mcp "
                f"scope, got {challenge!r}")

    accepted = mcp_auth.refusal(
        _request(opts, bearer="bearer", oauth_grant=opts.grant))
    assert_true(accepted is None,
                f"an mcp grant held by a permitted operator must pass, got "
                f"{accepted}")

    accepted = mcp_auth.refusal(
        _request(opts, bearer="bearer", oauth_grant=opts.grant_api_and_tools))
    assert_true(accepted is None,
                f"a grant consented to for BOTH tools and full API must open the "
                f"door, got {accepted}")


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

@th.django_unit_test("while the switch is off the door is the project's plain 404")
def test_disabled_is_invisible_over_the_wire(opts):
    from mojo.apps.assistant.mcp import auth as mcp_auth

    assert_eq(mcp_auth.is_enabled(), False,
              "this module asserts the DISABLED contract — the generated test "
              "project must leave ASSISTANT_MCP_ENABLED unset")

    _clear_rate_keys()

    for path in ("/api/assistant/no-such-route", "/no-such-route"):
        unknown = opts.client.post(path, {})
        assert_eq(unknown.status_code, 404,
                  f"the comparison route {path} must itself be a 404, got "
                  f"{unknown.status_code}")

    for method, call in (("POST", lambda: opts.client.post(MCP_PATH, {})),
                         ("GET", lambda: opts.client.get(MCP_PATH)),
                         ("DELETE", lambda: opts.client.delete(MCP_PATH))):
        resp = call()
        assert_eq(resp.status_code, 404,
                  f"{method} on a disabled MCP door must 404, got "
                  f"{resp.status_code}")
        headers = {k.lower(): v for k, v in
                   opts.client.last_response.headers.items()}
        assert_true("www-authenticate" not in headers,
                    f"{method} on a disabled door must not advertise a resource")
        assert_true("mcp-session-id" not in headers,
                    f"{method} must never issue a session id — this server is "
                    f"stateless")

    # The door answers the PROJECT's own handler404, which is what a genuinely
    # unresolved path gets in any deployment. The byte-identical comparison
    # against an unresolved path needs DEBUG off (Django serves its technical
    # page instead while DEBUG is on) and therefore lives in the serial wire
    # module; here the envelope itself is the assertion.
    disabled = opts.client.post(MCP_PATH, {})
    body = disabled.response
    assert_eq(body.get("error"), "Not found",
              f"a disabled door must answer the project's own handler404, got "
              f"{body}")
    assert_eq(body.get("code"), 404,
              f"the handler404 envelope must be unchanged, got {body}")
    assert_eq(body.get("status"), False,
              f"the handler404 envelope must be unchanged, got {body}")
    assert_true("Endpoint not found" not in str(body),
                "a disabled door must never answer the dispatcher's "
                "registered-route-wrong-method 404 — that body only exists for "
                "routes that DO exist")

    assert_true(opts.client.login(ADMIN_EMAIL, TEST_PASSWORD),
                "the gate admin must be able to sign in")
    try:
        signed_in = opts.client.post(MCP_PATH, {})
        assert_eq(signed_in.status_code, 404,
                  f"an authenticated operator must not be able to tell the door "
                  f"exists either, got {signed_in.status_code}")
    finally:
        opts.client.logout()

    assert_eq(_rate_keys(), [],
              f"a disabled door must never touch its rate-limit bucket — an "
              f"incident-filing limiter in front of the gate would be an "
              f"existence oracle, got {_rate_keys()}")


@th.django_unit_test("the MCP body is labelled sensitive so no tool argument is logged")
def test_sensitive_body_label(opts):
    from mojo.helpers import request as request_helpers

    for path in (MCP_PATH, MCP_PATH + "/"):
        label = request_helpers.sensitive_body_label(
            _request(opts, path=path, method="POST"))
        assert_eq(label, "assistant_mcp",
                  f"POST {path} must be labelled assistant_mcp, got {label!r}")

    label = request_helpers.sensitive_body_label(
        _request(opts, path=MCP_PATH, method="GET"))
    assert_true(label is None,
                f"only the POST body carries tool arguments, got {label!r}")

    label = request_helpers.sensitive_body_label(
        _request(opts, path="/api/assistant", method="POST"))
    assert_true(label is None,
                f"the chat endpoint's labelling must be unchanged, got {label!r}")


@th.django_unit_test("the MCP settings, their protection and the route are all wired")
def test_descriptors_protection_and_registration(opts):
    from django.urls import resolve
    from mojo.apps.account.services import admin_settings
    from mojo.apps.account.services import oauth_server
    from mojo.apps.assistant.rest.mcp import on_assistant_mcp
    from mojo.decorators.http import URLPATTERN_METHODS

    catalog = {row.key: row for row in admin_settings.descriptors()}

    enabled = catalog.get("ASSISTANT_MCP_ENABLED")
    assert_true(enabled is not None,
                "the MCP switch must be advertised in the Admin catalog")
    for attribute, expected in (
            ("label", "Remote agent access (MCP)"),
            ("section", "Security & operations"),
            ("value_type", "boolean"),
            ("default", False),
            ("resolver", "dynamic"),
            ("writable", "assistant_setup"),
            ("owner", "Assistant setup"),
            ("change_behavior", "immediate"),
            ("storage", "database")):
        assert_eq(getattr(enabled, attribute), expected,
                  f"ASSISTANT_MCP_ENABLED.{attribute} must be {expected!r}, got "
                  f"{getattr(enabled, attribute)!r}")

    path = catalog.get("ASSISTANT_MCP_PATH")
    assert_true(path is not None,
                "the MCP endpoint path must be advertised in the Admin catalog")
    for attribute, expected in (
            ("label", "MCP endpoint path"),
            ("section", "Security & operations"),
            ("value_type", "string"),
            ("default", "api/assistant/mcp"),
            ("resolver", "static"),
            ("writable", "none"),
            ("owner", "Deployment settings"),
            ("change_behavior", "restart")):
        assert_eq(getattr(path, attribute), expected,
                  f"ASSISTANT_MCP_PATH.{attribute} must be {expected!r}, got "
                  f"{getattr(path, attribute)!r}")

    # Protection is what stops a manage_settings holder writing a global row
    # that outranks the deployment file and opens the door on every node.
    # Predicate only — the test writes nothing.
    assert_true(admin_settings.is_catalog_protected("ASSISTANT_MCP_ENABLED"),
                "ASSISTANT_MCP_ENABLED can still be written through the generic "
                "settings API")

    entry = oauth_server.resolve(MCP_PATH)
    assert_true(entry is not None,
                "the MCP path must be registered as an OAuth resource")
    assert_eq(list(entry.scopes), ["mcp"],
              f"the resource must require the mcp scope, got {entry.scopes}")
    assert_true(entry.prefix is False,
                "the MCP door is ONE endpoint — registering it as a prefix "
                "would hand every tool-door token the whole API")

    # The second registration, behind the same switch: the REST API root.
    from mojo.helpers.request import API_ROOT

    root = oauth_server.resolve(API_ROOT)
    assert_true(root is not None,
                f"the REST API root {API_ROOT} must be registered as an OAuth "
                f"resource so an `api` grant has something to bind to")
    assert_true(root.prefix is True,
                "the API root must be a PREFIX resource, or an api token would "
                "authenticate at the root path alone")
    assert_eq(list(root.scopes), ["mcp", "api"],
              f"the API root must offer both scopes, got {root.scopes}")
    assert_true(oauth_server.covers(root, MCP_PATH),
                f"the shipped layout puts the MCP door beneath {API_ROOT}, so "
                f"one `mcp api` grant serves both")

    match = resolve(MCP_PATH)
    assert_eq(match.kwargs.get("__mojo_rest_root_key__"),
              "__absolute__api/assistant/mcp",
              f"the MCP route must resolve as an absolute mojo route, got "
              f"{match.kwargs}")
    assert_true(
        URLPATTERN_METHODS.get("__absolute__api/assistant/mcp__ALL")
        is on_assistant_mcp,
        "the route must be registered for EVERY method — a POST-only route "
        "would answer GET with the dispatcher's 404 instead of a 405")


@th.django_unit_test("the MCP envelope never becomes request.DATA")
def test_envelope_is_not_parsed_into_request_data(opts):
    import ujson
    from django.http import HttpResponse
    from django.test import RequestFactory
    from mojo.middleware.mojo import MojoMiddleware

    envelope = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "block_ip",
                   "arguments": {"ip": "203.0.113.9", "reason": "secret"}},
        # A client-supplied tenant, sitting where the dispatcher looks for one.
        "group": 999999,
    }
    middleware = MojoMiddleware(lambda request: HttpResponse(status=200))

    request = RequestFactory().post(
        MCP_PATH, data=ujson.dumps(envelope),
        content_type="application/json")
    middleware(request)

    assert_eq(request._sensitive_body_label, "assistant_mcp",
              f"the MCP path must be labelled before the body is touched, got "
              f"{request._sensitive_body_label!r}")
    assert_eq(dict(request.DATA), {},
              f"the JSON-RPC envelope must NEVER become request.DATA: a parsed "
              f"params.arguments rides into the incident an unhandled 500 files, "
              f"and a top-level 'group' lets the dispatcher rebind the tenant. "
              f"Got {dict(request.DATA)}")
    assert_true(request._raw_body is None,
                f"a labelled body must not be captured for request logging, got "
                f"{request._raw_body!r}")

    # The chat endpoint is unchanged — this is a targeted exemption, not a
    # weakening of request parsing.
    chat = RequestFactory().post(
        "/api/assistant", data=ujson.dumps({"message": "hi", "group": 999999}),
        content_type="application/json")
    middleware(chat)
    assert_eq(chat.DATA.get("message"), "hi",
              f"the chat endpoint must still parse its body, got {dict(chat.DATA)}")


@th.django_unit_test("one helper derives the MCP path for the route, the resource and the challenge")
def test_configured_path_is_the_single_source(opts):
    from mojo.apps.account.services import oauth_server
    from mojo.apps.assistant.mcp import auth as mcp_auth

    assert_eq(mcp_auth.configured_path("api/assistant/mcp", append_slash=False),
              "/api/assistant/mcp",
              "the ordinary path must be absolute and unslashed")
    assert_eq(mcp_auth.configured_path("api/assistant/mcp", append_slash=True),
              "/api/assistant/mcp/",
              "a MOJO_APPEND_SLASH deployment must serve the slashed path — the "
              "route, the OAuth resource and the challenge must agree on it")
    assert_eq(mcp_auth.configured_path("/api/assistant/mcp/", append_slash=True),
              "/api/assistant/mcp/",
              "an already-slashed setting must not grow a second slash")

    for raw, why in (("", "an empty"), ("/", "a '/'-only"),
                     ("   ", "a whitespace-only"), (None, "a null")):
        if raw is None:
            continue
        assert_eq(mcp_auth.configured_path(raw, append_slash=False),
                  "/" + mcp_auth.DEFAULT_PATH,
                  f"{why} ASSISTANT_MCP_PATH must fall back to the default — "
                  f"honouring it would mount the MCP server at the site root")

    live = mcp_auth.configured_path()
    assert_eq(mcp_auth.resource_path(), live,
              "the challenge's resource path must be the configured one")
    assert_true(oauth_server.resolve(live) is not None,
                f"the OAuth resource must be registered at exactly the "
                f"configured path — validate_access resolves it from the token "
                f"audience exactly — got no entry for {live!r}")

    # The Admin surface is the FOURTH consumer of this path, and it must not
    # re-derive it: under MOJO_APPEND_SLASH the helper appends a trailing slash,
    # so a re-derived unslashed path would make `resource__endswith` match none
    # of the grants actually stored, and every connection would go unlisted and
    # unswept by Disconnect all.
    from mojo.apps.account.services import assistant_setup

    assert_eq(assistant_setup.mcp_path(), live,
              f"the Admin's connect address and grant scoping must be the SAME "
              f"path the resource is registered at, got "
              f"{assistant_setup.mcp_path()!r} vs {live!r}")
    assert_true(assistant_setup.api_root() in assistant_setup.grant_paths()
                and live in assistant_setup.grant_paths(),
                f"both remote-agent resources must be in the Admin's scope, "
                f"got {assistant_setup.grant_paths()!r}")


@th.django_unit_test("a REST API root of / is refused, and the tool door still registers")
def test_root_prefix_registration_is_refused(opts):
    from mojo.apps.account.services.oauth_server import resources
    from mojo.apps.assistant.apps import register_oauth_resources

    # MOJO_PREFIX="" resolves API_ROOT to "/". A prefix resource there would
    # cover every path this host serves — the hosted sign-in pages and anything
    # else sharing the host — so it is refused and `api` is simply not offered.
    registry = resources.ResourceRegistry()
    result = register_oauth_resources(MCP_PATH, "/", lambda: True,
                                      registry=registry)
    assert_true(result is None,
                f"registering the API root at / must be refused, got {result!r}")
    assert_eq(registry.paths(), [MCP_PATH],
              f"the MCP door must still register when the root is refused — "
              f"the tool door is unaffected, got {registry.paths()}")
    assert_eq(resources.offered_scopes(registry), ["mcp"],
              "an installation with no usable API root must not advertise the "
              "api scope at all")

    # A real root registers both, and the door sits beneath it.
    ordinary = resources.ResourceRegistry()
    entry = register_oauth_resources(MCP_PATH, "/api", lambda: True,
                                     registry=ordinary)
    assert_true(entry is not None and entry.prefix is True,
                f"an ordinary API root must register as a prefix resource, "
                f"got {entry!r}")
    assert_eq(sorted(ordinary.paths()), sorted([MCP_PATH, "/api"]),
              f"both resources must register, got {ordinary.paths()}")
    assert_true(resources.covers(entry, MCP_PATH),
                "the tool door must sit beneath the registered root")
