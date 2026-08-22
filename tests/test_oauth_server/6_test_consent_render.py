"""
The consent page and the approve handler, in-process.

Two behaviours carry the security weight and are asserted directly:

  * a bad client or an unregistered redirect RENDERS an error and never sends a
    Location header — redirecting there IS the open redirect the check exists
    to prevent;
  * the page is `X-Frame-Options: DENY` unconditionally, because AUTH_CSP_ENABLED
    ships false and a one-click credential-minting page must not be frameable on
    the shipped default.

The script-nonce check mirrors the contract in tests/test_auth/csp.py: every
`<script>` in the body is either nonce-stamped or a JSON data block.
"""
import base64
import hashlib
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TEST_USER = "oauth_consent_user"
CLIENT_ID = "testit-oauth-consent-client"
REDIRECT = "http://127.0.0.1:8400/cb"
RESOURCE_PATH = "/api/testit/oauth-consent-probe"
ORIGIN = "https://oauth.testit.example"
RESOURCE = ORIGIN + RESOURCE_PATH

SCRIPT_TAG_RE = re.compile(r"<script([^>]*)>", re.IGNORECASE)


def _verifier():
    return secrets.token_urlsafe(48)[:64]


def _challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _registry(enabled=True, extra=None):
    from mojo.apps.account.services.oauth_server import resources

    registry = resources.ResourceRegistry()
    registry.register(RESOURCE_PATH, ["mcp"], lambda: enabled)
    if extra:
        registry.register(extra, ["mcp"], lambda: enabled)
    return registry


def _authorize_request(**overrides):
    from django.test import RequestFactory
    from objict import objict
    from mojo.apps.account.services.oauth_server import resources

    verifier = _verifier()
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "state": "state-value",
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "resource": RESOURCE,
    }
    params.update(overrides)
    params = {k: v for k, v in params.items() if v is not None}
    request = RequestFactory().get(f"{resources.SERVER_PATH}/authorize", params)
    request.DATA = objict.fromdict(params)
    request.ip = "127.0.0.1"
    request.user_agent = "testit"
    request.bearer = None
    return request, verifier


def _unguarded_scripts(html, nonce):
    bad = []
    for attrs in SCRIPT_TAG_RE.findall(html):
        if nonce and f'nonce="{nonce}"' in attrs:
            continue
        if 'type="application/json"' in attrs:
            continue
        bad.append(attrs)
    return bad


@th.django_unit_setup()
def setup_consent(opts):
    from mojo.apps.account.models import OAuthClient, User

    User.objects.filter(username=TEST_USER).delete()
    OAuthClient.objects.filter(client_id=CLIENT_ID).delete()

    user = User(username=TEST_USER, display_name=TEST_USER,
                email=f"{TEST_USER}@example.com")
    user.save()
    OAuthClient(client_id=CLIENT_ID, kind="dcr", client_name="Consent Client",
                redirect_uris=[REDIRECT]).save()


@th.django_unit_test("the consent page renders the request in plain words")
def test_consent_page_renders(opts):
    from mojo.apps.account.services.oauth_server import consent

    request, _verifier_value = _authorize_request()
    response = consent.handle_authorize(request, ORIGIN, registry=_registry())
    assert_eq(response.status_code, 200,
              f"a well-formed authorize request must render the page, "
              f"got {response.status_code}")
    assert_true(response.get("Location") is None,
                "the consent page must never redirect")
    assert_eq(response.get("X-Frame-Options"), "DENY",
              "the consent page must be un-frameable on the shipped default "
              "configuration, where the CSP is off")

    html = response.content.decode("utf-8")
    assert_true("Consent Client" in html,
                "the page must name the client asking for access")
    assert_true("the same permissions as your account" in html,
                "the page must say what the access means in plain words")
    assert_true('id="oauth-approve-payload"' in html,
                "the validated parameters must ride as a json_script data block")
    assert_true("access_denied" in html,
                "the page must carry a server-built deny URL")
    assert_true(CLIENT_ID in html, "the page must carry the client_id it validated")


@th.django_unit_test("every script on the consent page is nonce-stamped")
def test_consent_page_scripts_are_nonced(opts):
    from mojo.apps.account.services.oauth_server import consent

    request, _verifier_value = _authorize_request()
    response = consent.handle_authorize(request, ORIGIN, registry=_registry())
    html = response.content.decode("utf-8")

    nonces = re.findall(r'nonce="([0-9a-f]{32})"', html)
    assert_true(bool(nonces),
                "the consent page must carry a per-request script nonce")
    nonce = nonces[0]
    assert_eq(len(set(nonces)), 1,
              f"every script must share ONE nonce, found {set(nonces)}")
    assert_eq(_unguarded_scripts(html, nonce), [],
              "every <script> must be nonce-stamped or a JSON data block, so "
              "anything injected after render cannot execute")

    from mojo.helpers.settings import settings
    if settings.get_static("AUTH_CSP_ENABLED", False, kind="bool"):
        policy = response.get("Content-Security-Policy") or \
            response.get("Content-Security-Policy-Report-Only") or ""
        assert_true("frame-ancestors 'none'" in policy,
                    f"the consent page's CSP must forbid framing, got {policy!r}")
        assert_true(f"'nonce-{nonce}'" in policy,
                    "the header nonce and the markup nonce must be the same value")


@th.django_unit_test("a bad client or redirect renders an error and never redirects")
def test_client_failures_never_redirect(opts):
    from mojo.apps.account.services.oauth_server import consent

    for overrides, why in (
            ({"client_id": "testit-oauth-unknown"}, "an unknown client"),
            ({"redirect_uri": "https://evil.example/cb"},
             "a redirect the client never registered"),
            ({"redirect_uri": None}, "a missing redirect_uri")):
        request, _v = _authorize_request(**overrides)
        response = consent.handle_authorize(request, ORIGIN, registry=_registry())
        assert_eq(response.status_code, 400,
                  f"{why} must render an error page, got {response.status_code}")
        assert_true(response.get("Location") is None,
                    f"{why} must NEVER produce a redirect — that is the open "
                    f"redirect this check exists to prevent")
        assert_eq(response.get("X-Frame-Options"), "DENY",
                  f"the error page for {why} must also be un-frameable")


@th.django_unit_test("a parameter error redirects with the RFC error, state and iss")
def test_parameter_errors_redirect(opts):
    from mojo.apps.account.services.oauth_server import consent, resources

    cases = [
        ({"code_challenge_method": "plain"}, "invalid_request", "the plain PKCE method"),
        ({"code_challenge": None, "code_challenge_method": None},
         "invalid_request", "a missing PKCE challenge"),
        ({"response_type": "token"}, "unsupported_response_type", "an implicit flow"),
        ({"scope": "admin"}, "invalid_scope", "a scope this server does not offer"),
        ({"resource": "https://elsewhere.example/api/x"}, "invalid_target",
         "an unregistered resource"),
    ]
    for overrides, expected, why in cases:
        request, _v = _authorize_request(**overrides)
        response = consent.handle_authorize(request, ORIGIN, registry=_registry())
        assert_eq(response.status_code, 302,
                  f"{why} must redirect the error back to the client, "
                  f"got {response.status_code}")
        query = parse_qs(urlsplit(response["Location"]).query)
        assert_eq(query.get("error"), [expected],
                  f"{why} must answer {expected}, got {query.get('error')}")
        assert_eq(query.get("state"), ["state-value"],
                  f"{why} must echo the client's state, got {query.get('state')}")
        assert_eq(query.get("iss"), [f"{ORIGIN}{resources.SERVER_PATH}"],
                  f"{why} must carry the RFC 9207 issuer, got {query.get('iss')}")


@th.django_unit_test("an absent resource defaults only when it is unambiguous")
def test_resource_defaulting(opts):
    from mojo.apps.account.services.oauth_server import consent

    request, _v = _authorize_request(resource=None)
    response = consent.handle_authorize(request, ORIGIN, registry=_registry())
    assert_eq(response.status_code, 200,
              "a pre-2025-06-18 client that omits `resource` must still be served "
              "when exactly one resource is enabled")
    assert_true(RESOURCE in response.content.decode("utf-8"),
                "the sole enabled resource must be the one bound into the request")

    request, _v = _authorize_request(resource=None)
    ambiguous = _registry(extra="/api/testit/oauth-consent-second")
    response = consent.handle_authorize(request, ORIGIN, registry=ambiguous)
    assert_eq(response.status_code, 302,
              "an omitted `resource` with several enabled must not be guessed")
    query = parse_qs(urlsplit(response["Location"]).query)
    assert_eq(query.get("error"), ["invalid_target"],
              f"an ambiguous resource must answer invalid_target, "
              f"got {query.get('error')}")


@th.django_unit_test("an unconfigured installation serves a 404 page, still un-frameable")
def test_not_available_page(opts):
    from mojo.apps.account.services.oauth_server import consent

    request, _v = _authorize_request()
    response = consent.handle_authorize(request, "", registry=_registry())
    assert_eq(response.status_code, 404,
              f"no BASE_URL means no authorization server, got {response.status_code}")
    assert_eq(response.get("X-Frame-Options"), "DENY",
              "even the not-available page must be un-frameable")

    request, _v = _authorize_request()
    response = consent.handle_authorize(
        request, ORIGIN, registry=_registry(enabled=False))
    assert_eq(response.status_code, 404,
              "no ENABLED resource means no authorization server either")


def _approve_request(token=None, bearer="bearer", **overrides):
    from django.test import RequestFactory
    from objict import objict
    from mojo.apps.account.models import User
    from mojo.apps.account.services.oauth_server import resources

    verifier = _verifier()
    payload = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "state": "state-value",
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "scope": "mcp",
        "resource": RESOURCE,
    }
    payload.update(overrides)
    request = RequestFactory().post(f"{resources.SERVER_PATH}/approve", payload)
    request.DATA = objict.fromdict(payload)
    request.ip = "127.0.0.1"
    request.user_agent = "testit"
    request.user = User.objects.get(username=TEST_USER)
    request.bearer = bearer
    request.auth_token = objict(prefix="bearer", token=token) if token else None
    return request, verifier


def _session_token(token_type="access", with_auth_time=True):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils.jwtoken import JWToken

    user = User.objects.get(username=TEST_USER)
    claims = dict(uid=user.pk)
    if with_auth_time:
        claims["auth_time"] = int(time.time())
    return JWToken(user.get_auth_key()).create_access_token(
        token_type=token_type, **claims)


@th.django_unit_test("approve mints a code and returns the client's redirect")
def test_approve_mints_a_code(opts):
    from mojo.apps.account.models import OAuthClient
    from mojo.apps.account.services.oauth_server import codes, consent, resources

    request, verifier = _approve_request(token=_session_token())
    result = consent.handle_approve(request, ORIGIN, registry=_registry())
    url = result["redirect_url"]
    query = parse_qs(urlsplit(url).query)

    assert_true(url.startswith(REDIRECT),
                f"approve must return the client's own redirect URI, got {url}")
    assert_eq(query.get("state"), ["state-value"],
              f"the client's state must be echoed, got {query.get('state')}")
    assert_eq(query.get("iss"), [f"{ORIGIN}{resources.SERVER_PATH}"],
              f"the RFC 9207 issuer must ride along, got {query.get('iss')}")
    raw_code = query.get("code", [None])[0]
    assert_true(bool(raw_code), f"approve must hand back a code, got {url}")

    client = OAuthClient.objects.get(client_id=CLIENT_ID)
    row = codes.consume_code(raw_code, client, REDIRECT, verifier)
    assert_eq(row.resource, RESOURCE,
              f"the minted code must be bound to the requested resource, "
              f"got {row.resource}")
    assert_true(row.auth_time > 0,
                "the approving session's auth_time must be copied onto the code")


@th.django_unit_test("approve refuses anything that is not an interactive session")
def test_approve_requires_an_interactive_session(opts):
    from mojo import errors as merrors
    from mojo.apps.account.services.oauth_server import consent

    cases = [
        (dict(token=_session_token(), bearer="apikey"), "an API key"),
        (dict(token=_session_token(), bearer="grouptoken"), "a group token"),
        (dict(token=_session_token(), bearer=None), "no credential at all"),
        (dict(token=_session_token(token_type="user_api_key")),
         "a user_api_key JWT"),
        (dict(token=_session_token(with_auth_time=False)),
         "a legacy token with no auth_time"),
    ]
    for kwargs, why in cases:
        request, _verifier_value = _approve_request(**kwargs)
        refused = False
        try:
            consent.handle_approve(request, ORIGIN, registry=_registry())
        except merrors.MojoException:
            refused = True
        assert_true(refused, f"approve must refuse {why}")


@th.django_unit_test("approve re-validates the posted values from scratch")
def test_approve_does_not_trust_the_page(opts):
    from mojo import errors as merrors
    from mojo.apps.account.services.oauth_server import consent

    cases = [
        (dict(redirect_uri="https://evil.example/cb"), "an unregistered redirect"),
        (dict(client_id="testit-oauth-unknown"), "an unknown client"),
        (dict(code_challenge_method="plain"), "a plain PKCE challenge"),
        (dict(resource="https://elsewhere.example/api/x"), "an unknown resource"),
        (dict(scope="admin"), "a scope this server does not offer"),
    ]
    for overrides, why in cases:
        request, _verifier_value = _approve_request(
            token=_session_token(), **overrides)
        refused = False
        try:
            consent.handle_approve(request, ORIGIN, registry=_registry())
        except merrors.MojoException:
            refused = True
        assert_true(refused,
                    f"approve must re-check every value and refuse {why}, even "
                    f"though the page it came from was well-formed")


CIMD_CLIENT_URL = "https://consent.testit.example/oauth-client.json"


def _seed_cimd_client():
    """A CIMD client whose document is already in the cache, so no fetch runs."""
    from mojo.apps.account.models import OAuthClient
    from mojo.apps.account.services.oauth_server import clients

    canonical = clients.canonical_cimd_url(CIMD_CLIENT_URL)
    OAuthClient.objects.filter(client_id=canonical).delete()
    clients._cache_set(canonical, {"ok": True, "document": {
        "client_id": canonical,
        "client_name": "Published Client",
        "redirect_uris": [REDIRECT],
    }})
    return canonical


@th.django_unit_test("the consent page shows what a client name cannot forge")
def test_consent_page_shows_verifiable_facts(opts):
    from mojo.apps.account.services.oauth_server import consent

    request, _v = _authorize_request()
    html = consent.handle_authorize(
        request, ORIGIN, registry=_registry()).content.decode("utf-8")

    # The DCR client picked its own name and nothing vouched for it.
    assert_true("unverified name" in html,
                "a self-registered client's name must be marked unverified — the "
                "name is the whole phishing surface")
    # Asserted as the rendered SENTENCE, not as a bare substring: the redirect
    # URI also appears inside the deny URL and the approve payload, so a
    # substring test would pass with the line absent.
    assert_true("You will be sent back to 127.0.0.1:8400" in html,
                "the page must show where the credential is actually delivered")
    assert_true(f"Access to: {RESOURCE}" in html,
                "the page must name the exact resource the token will open")

    canonical = _seed_cimd_client()
    request, _v = _authorize_request(client_id=canonical)
    html = consent.handle_authorize(
        request, ORIGIN, registry=_registry()).content.decode("utf-8")
    assert_true(f"Verified from {canonical}" in html,
                "a CIMD client's name is vouched for by a document at a URL — "
                "show the URL so the reader can judge it")
    assert_true("unverified name" not in html,
                "a CIMD client must not be marked unverified")

    from mojo.apps.account.models import OAuthClient
    OAuthClient.objects.filter(client_id=canonical).delete()


@th.django_unit_test("every consent response is Cache-Control: no-store")
def test_consent_pages_are_not_cached(opts):
    from mojo.apps.account.services.oauth_server import consent

    request, _v = _authorize_request()
    ok = consent.handle_authorize(request, ORIGIN, registry=_registry())
    assert_eq(ok.get("Cache-Control"), "no-store",
              f"the consent page names the signed-in person and is one click "
              f"from a credential, got {ok.get('Cache-Control')!r}")

    request, _v = _authorize_request()
    unavailable = consent.handle_authorize(request, "", registry=_registry())
    assert_eq(unavailable.get("Cache-Control"), "no-store",
              "the not-available page must not be cached either")


@th.django_unit_test("a hostile redirect_uri is refused, not 500'd")
def test_hostile_redirect_uri_is_refused(opts):
    from mojo.apps.account.services.oauth_server import consent

    # 100 KB is well past OAuthCode.redirect_uri (varchar 2048). Before the
    # presented value was re-validated, the loopback branch of
    # redirect_uri_matches let it through to the database.
    oversize = "http://127.0.0.1:8400/cb#" + "A" * 100000
    for presented, why in ((oversize, "a 100 KB fragment"),
                           ("http://user:pw@127.0.0.1:8400/cb", "userinfo"),
                           ("http://127.0.0.1:8400/cb#frag", "a fragment")):
        request, _v = _authorize_request(redirect_uri=presented)
        response = consent.handle_authorize(request, ORIGIN, registry=_registry())
        assert_eq(response.status_code, 400,
                  f"a redirect_uri carrying {why} must render a 400, never reach "
                  f"the database — got {response.status_code}")
        assert_true(response.get("Location") is None,
                    f"a redirect_uri carrying {why} must not be redirected to")


@th.django_unit_test("a repeated scope token grants the scope once")
def test_scope_is_de_duplicated(opts):
    from mojo.apps.account.models import OAuthClient
    from mojo.apps.account.services.oauth_server import codes, consent

    request, verifier = _approve_request(
        token=_session_token(), scope="mcp mcp mcp mcp")
    result = consent.handle_approve(request, ORIGIN, registry=_registry())
    raw_code = parse_qs(urlsplit(result["redirect_url"]).query)["code"][0]

    client = OAuthClient.objects.get(client_id=CLIENT_ID)
    row = codes.consume_code(raw_code, client, REDIRECT, verifier)
    assert_eq(row.scope, "mcp",
              f"a repeated scope token must collapse to one — the string is "
              f"stored on the grant and rides in every token, got {row.scope!r}")
