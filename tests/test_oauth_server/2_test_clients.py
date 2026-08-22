"""
Redirect-URI rules, dynamic client registration, and CIMD resolution.

The CIMD tests inject a `fetcher` and never touch the network. The Redis cache
entry for the probe document is cleared in setup, because these tests run
against a long-lived database and a long-lived Redis.
"""
import json

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

DCR_NAME = "testit oauth client"
DB_CLIENT_ID = "testit-oauth-db-client"
INACTIVE_CLIENT_ID = "testit-oauth-inactive-client"
CIMD_URL = "https://cimd.testit.example/oauth-client.json"
CIMD_INACTIVE_URL = "https://cimd.testit.example/deactivated.json"
CIMD_REDIRECT = "https://cimd.testit.example/callback"


def _document(client_id=CIMD_URL, **overrides):
    document = {
        "client_id": client_id,
        "client_name": "Probe Client",
        "redirect_uris": [CIMD_REDIRECT],
    }
    document.update(overrides)
    return document


def _fetcher(document, status=200, content_type="application/json", body=None):
    """A fetcher that records its calls, so a cache hit is provable."""
    calls = []

    def fetch(url):
        calls.append(url)
        payload = body if body is not None else json.dumps(document).encode()
        return status, content_type, payload

    fetch.calls = calls
    return fetch


@th.django_unit_setup()
def setup_clients(opts):
    from mojo.helpers.redis import get_connection
    from mojo.apps.account.models import OAuthClient
    from mojo.apps.account.services.oauth_server import clients

    OAuthClient.objects.filter(client_id__in=[
        DB_CLIENT_ID, INACTIVE_CLIENT_ID, CIMD_URL, CIMD_INACTIVE_URL]).delete()
    OAuthClient.objects.filter(client_name=DCR_NAME).delete()
    for url in (CIMD_URL, CIMD_INACTIVE_URL):
        try:
            get_connection().delete(clients._cimd_cache_key(url))
        except Exception:
            pass

    OAuthClient(client_id=DB_CLIENT_ID, kind="dcr", client_name="DB Client",
                redirect_uris=["https://db.testit.example/cb"]).save()
    OAuthClient(client_id=INACTIVE_CLIENT_ID, kind="dcr", client_name="Gone",
                redirect_uris=["https://db.testit.example/cb"],
                is_active=False).save()


@th.django_unit_test("validate_redirect_uri accepts https and loopback http only")
def test_validate_redirect_uri(opts):
    from mojo.apps.account.services.oauth_server import clients

    good = [
        "https://app.example.com/callback",
        "https://app.example.com/callback?next=1",
        "http://localhost:3000/cb",
        "http://127.0.0.1/cb",
        "http://[::1]:5000/cb",
    ]
    for uri in good:
        assert_eq(clients.validate_redirect_uri(uri), uri,
                  f"{uri} must be accepted verbatim as a redirect URI")

    bad = [
        ("http://example.com/cb", "remote http is not confidential"),
        ("myapp://cb", "a custom scheme cannot be attributed to anyone"),
        ("https://app.example.com/cb#frag", "a fragment is never sent to the server"),
        ("https://user:pw@app.example.com/cb", "userinfo must be refused"),
        ("https://app.example.com/cb\r\nX: 1", "control characters must be refused"),
        ("https://" + "a" * 3000 + ".example.com/cb", "an over-long URI must be refused"),
        ("https:///cb", "a host-less URI must be refused"),
        ("", "an empty URI must be refused"),
        (None, "a non-string must be refused"),
    ]
    for uri, why in bad:
        refused = False
        try:
            clients.validate_redirect_uri(uri)
        except ValueError:
            refused = True
        assert_true(refused, f"{uri!r} must be refused as a redirect URI: {why}")


@th.django_unit_test("redirect matching is exact, except loopback ignores the port")
def test_redirect_uri_matches(opts):
    from mojo.apps.account.services.oauth_server import clients

    assert_true(
        clients.redirect_uri_matches("http://127.0.0.1:1234/cb",
                                     "http://127.0.0.1:59999/cb"),
        "RFC 8252 §7.3 requires a loopback redirect to match on any port")
    assert_true(
        clients.redirect_uri_matches("http://localhost/cb", "http://localhost:8080/cb"),
        "a loopback redirect with no port must match one with a port")
    assert_true(
        not clients.redirect_uri_matches("http://127.0.0.1:1/cb",
                                         "http://127.0.0.1:1/other"),
        "the loopback exception covers the PORT only — never the path")
    assert_true(
        not clients.redirect_uri_matches("https://app.example.com/cb",
                                         "https://app.example.com:8443/cb"),
        "a non-loopback redirect must match exactly, port included")
    assert_true(
        not clients.redirect_uri_matches("https://app.example.com/cb",
                                         "https://app.example.com/cb2"),
        "a different path must never match")
    assert_true(
        clients.redirect_uri_matches("https://app.example.com/cb",
                                     "https://app.example.com/cb"),
        "an identical https redirect must match")


@th.django_unit_test("DCR registers a client and echoes what the server supports")
def test_register_client(opts):
    from mojo.apps.account.models import OAuthClient
    from mojo.apps.account.services.oauth_server import clients

    OAuthClient.objects.filter(client_name=DCR_NAME).delete()
    result = clients.register_client({
        "redirect_uris": ["http://127.0.0.1:7777/cb"],
        "client_name": DCR_NAME,
        # Asked for a method the server does not offer, and for one grant type
        # it does not support. RFC 7591 §3.2.1: substitute, never refuse.
        "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code", "client_credentials"],
        "response_types": ["code", "token"],
        "client_uri": "https://app.example.com",
    })
    assert_eq(result["token_endpoint_auth_method"], "none",
              f"an unsupported auth method must be substituted with none, "
              f"got {result['token_endpoint_auth_method']}")
    assert_eq(result["grant_types"], ["authorization_code"],
              f"grant_types must be intersected with what the server does, "
              f"got {result['grant_types']}")
    assert_eq(result["response_types"], ["code"],
              f"response_types must be intersected, got {result['response_types']}")
    assert_eq(result["redirect_uris"], ["http://127.0.0.1:7777/cb"],
              "the registered redirect URIs must be echoed back")
    assert_true(result["client_id_issued_at"] > 0,
                "client_id_issued_at must be a real timestamp")

    row = OAuthClient.objects.filter(client_id=result["client_id"]).first()
    assert_true(row is not None, "registration must persist an OAuthClient row")
    assert_eq(row.kind, "dcr", f"a registered client must be kind=dcr, got {row.kind}")
    assert_eq(row.metadata.get("client_uri"), "https://app.example.com",
              "an https client_uri must be kept in the row's metadata")
    OAuthClient.objects.filter(client_id=result["client_id"]).delete()


@th.django_unit_test("DCR refuses a bad redirect set and a non-https metadata URL")
def test_register_client_refusals(opts):
    from mojo.apps.account.services.oauth_server import clients

    with th.assert_raises(clients.ClientError):
        clients.register_client({"client_name": "no redirects"})
    with th.assert_raises(clients.ClientError):
        clients.register_client({
            "redirect_uris": [f"https://a{n}.example.com/cb" for n in range(11)]})
    with th.assert_raises(clients.ClientError):
        clients.register_client({"redirect_uris": ["myapp://cb"]})
    with th.assert_raises(clients.ClientError):
        clients.register_client({
            "redirect_uris": ["https://a.example.com/cb"],
            "client_uri": "http://a.example.com"})


@th.django_unit_test("resolve_client reads the DB and refuses a deactivated row")
def test_resolve_client_db(opts):
    from mojo.apps.account.services.oauth_server import clients

    client = clients.resolve_client(DB_CLIENT_ID)
    assert_eq(client.client_id, DB_CLIENT_ID,
              f"a registered client_id must resolve to its row, got {client.client_id}")
    for bad in (INACTIVE_CLIENT_ID, "testit-oauth-nonexistent", "", None):
        with th.assert_raises(clients.ClientError):
            clients.resolve_client(bad)


@th.django_unit_test("a valid CIMD document upserts a client and is cached")
def test_resolve_client_cimd(opts):
    from mojo.apps.account.models import OAuthClient
    from mojo.apps.account.services.oauth_server import clients

    fetch = _fetcher(_document())
    client = clients.resolve_client(CIMD_URL, fetcher=fetch)
    assert_eq(client.client_id, CIMD_URL,
              "a CIMD client's client_id IS its document URL")
    assert_eq(client.kind, "cimd", f"the row must be kind=cimd, got {client.kind}")
    assert_eq(client.client_name, "Probe Client",
              f"the document's client_name must be stored, got {client.client_name}")
    assert_eq(client.redirect_uris, [CIMD_REDIRECT],
              f"the document's redirect URIs must be stored, got {client.redirect_uris}")
    assert_eq(len(fetch.calls), 1,
              f"the first resolve must fetch the document once, got {fetch.calls}")

    again = clients.resolve_client(CIMD_URL, fetcher=fetch)
    assert_eq(len(fetch.calls), 1,
              f"a cached document must not be re-fetched within the window, "
              f"got {len(fetch.calls)} fetches")
    assert_eq(again.pk, client.pk, "resolving twice must not create a second row")
    assert_eq(OAuthClient.objects.filter(client_id=CIMD_URL).count(), 1,
              "CIMD resolution must upsert exactly one row")


@th.django_unit_test("a deactivated CIMD row is refused before any fetch or write")
def test_cimd_deactivation_sticks(opts):
    from mojo.apps.account.models import OAuthClient
    from mojo.apps.account.services.oauth_server import clients

    OAuthClient(client_id=CIMD_INACTIVE_URL, kind="cimd", client_name="Gone",
                redirect_uris=[CIMD_REDIRECT], is_active=False).save()
    fetch = _fetcher(_document(client_id=CIMD_INACTIVE_URL))
    with th.assert_raises(clients.ClientError):
        clients.resolve_client(CIMD_INACTIVE_URL, fetcher=fetch)
    assert_eq(fetch.calls, [],
              f"a deactivated client must be refused BEFORE any fetch, "
              f"got {fetch.calls}")
    row = OAuthClient.objects.filter(client_id=CIMD_INACTIVE_URL).first()
    assert_true(not row.is_active,
                "re-resolving a deactivated CIMD client must never re-activate it")


@th.django_unit_test("every CIMD failure mode answers invalid_client")
def test_cimd_refusals(opts):
    from mojo.helpers.redis import get_connection
    from mojo.apps.account.services.oauth_server import clients

    def boom(url):
        raise ValueError("Cannot fetch private or internal addresses")

    cases = [
        ("http://cimd.testit.example/doc.json", _fetcher(_document()),
         "a non-https client_id URL"),
        ("https://cimd.testit.example/", _fetcher(_document()),
         "a root-path client_id URL"),
        ("https://cimd.testit.example/doc.json?x=1", _fetcher(_document()),
         "a client_id URL carrying a query"),
        (CIMD_URL, _fetcher(_document(client_id="https://other.example/doc.json")),
         "a document that names a different client_id"),
        (CIMD_URL, _fetcher(None, body=b"<html>nope</html>",
                            content_type="text/html"),
         "a document that is not JSON"),
        (CIMD_URL, _fetcher(None, body=b"x" * 70000),
         "a document over the byte cap"),
        (CIMD_URL, _fetcher(_document(), status=404), "a document that 404s"),
        (CIMD_URL, _fetcher(_document(redirect_uris=["myapp://cb"])),
         "a document whose redirect URIs are not acceptable"),
        (CIMD_URL, boom, "a fetcher refusal"),
    ]
    for url, fetch, why in cases:
        try:
            get_connection().delete(clients._cimd_cache_key(url))
        except Exception:
            pass
        code = None
        try:
            clients.resolve_client(url, fetcher=fetch)
        except clients.ClientError as err:
            code = err.code
        assert_eq(code, "invalid_client",
                  f"{why} must be refused as invalid_client, got {code!r}")
