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
# The same identity, spelled three ways an attacker would reach for.
CIMD_URL_VARIANTS = (
    "https://CIMD.TESTIT.example/oauth-client.json",
    "https://cimd.testit.example:443/oauth-client.json",
    "https://cimd.testit.example//oauth-%63lient.json",
)
CIMD_VARIANT_URL = "https://cimd.testit.example/variant-probe.json"
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


def _fetcher(document, status=200, content_type="application/json", body=None,
             echo_url=False):
    """A fetcher that records its calls, so a cache hit is provable.

    `echo_url` makes the served document name whatever URL it was asked for,
    which is how the canonicalisation tests prove the SERVER reduced the
    variants rather than the fixture doing it for them.
    """
    calls = []

    def fetch(url):
        calls.append(url)
        if body is not None:
            return status, content_type, body
        served = dict(document or {})
        if echo_url:
            served["client_id"] = url
        return status, content_type, json.dumps(served).encode()

    fetch.calls = calls
    return fetch


@th.django_unit_setup()
def setup_clients(opts):
    from mojo.helpers.redis import get_connection
    from mojo.apps.account.models import OAuthClient
    from mojo.apps.account.services.oauth_server import clients

    OAuthClient.objects.filter(client_id__in=[
        DB_CLIENT_ID, INACTIVE_CLIENT_ID, CIMD_URL, CIMD_INACTIVE_URL,
        CIMD_VARIANT_URL]).delete()
    OAuthClient.objects.filter(client_name=DCR_NAME).delete()
    for url in (CIMD_URL, CIMD_INACTIVE_URL, CIMD_VARIANT_URL):
        try:
            get_connection().delete(
                clients._cimd_cache_key(clients.canonical_cimd_url(url)))
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
        # Clear by the CANONICAL key — that is what the cache is keyed on, and
        # failures are now negatively cached, so a stale entry would make the
        # next case pass without exercising anything.
        try:
            get_connection().delete(
                clients._cimd_cache_key(clients.canonical_cimd_url(url)))
        except Exception:
            pass
        code = None
        try:
            clients.resolve_client(url, fetcher=fetch)
        except clients.ClientError as err:
            code = err.code
        assert_eq(code, "invalid_client",
                  f"{why} must be refused as invalid_client, got {code!r}")


@th.django_unit_test("every spelling of a CIMD URL is one client identity")
def test_cimd_url_canonicalisation(opts):
    from mojo.helpers.redis import get_connection
    from mojo.apps.account.models import OAuthClient
    from mojo.apps.account.services.oauth_server import clients

    canonical = clients.canonical_cimd_url(CIMD_URL)
    assert_eq(canonical, CIMD_URL,
              f"an already-canonical URL must be returned unchanged, got {canonical}")
    for variant in CIMD_URL_VARIANTS:
        assert_eq(clients.canonical_cimd_url(variant), canonical,
                  f"{variant} must reduce to the same identity as {CIMD_URL} — "
                  f"got {clients.canonical_cimd_url(variant)}")

    for bad, why in (
            ("http://cimd.testit.example/c.json", "a non-https URL"),
            ("https://cimd.testit.example/", "a root-path URL"),
            ("https://cimd.testit.example/c.json?x=1", "a URL carrying a query"),
            ("https://cimd.testit.example/c.json#f", "a URL carrying a fragment"),
            ("https://u:p@cimd.testit.example/c.json", "a URL carrying userinfo"),
            ("", "an empty value"),
            (None, "a non-string")):
        refused = False
        try:
            clients.canonical_cimd_url(bad)
        except clients.ClientError:
            refused = True
        assert_true(refused, f"{why} must not be a usable client identity: {bad!r}")

    # Resolving through any variant reaches ONE row, and the document may spell
    # its own client_id however it likes as long as it reduces to the same URL.
    OAuthClient.objects.filter(client_id=canonical).delete()
    for variant in (CIMD_URL,) + CIMD_URL_VARIANTS:
        try:
            get_connection().delete(clients._cimd_cache_key(canonical))
        except Exception:
            pass
        client = clients.resolve_client(
            variant, fetcher=_fetcher(_document(), echo_url=True))
        assert_eq(client.client_id, canonical,
                  f"resolving {variant} must land on the canonical row, "
                  f"got {client.client_id}")
    assert_eq(OAuthClient.objects.filter(client_id__contains="oauth-client.json").count(), 1,
              "every spelling of one CIMD URL must share a single row")


@th.django_unit_test("a deactivated CIMD client stays dead under every URL variant")
def test_cimd_deactivation_covers_variants(opts):
    from mojo.helpers.redis import get_connection
    from mojo.apps.account.models import OAuthClient
    from mojo.apps.account.services.oauth_server import clients

    canonical = clients.canonical_cimd_url(CIMD_VARIANT_URL)
    OAuthClient.objects.filter(client_id=canonical).delete()
    OAuthClient(client_id=canonical, kind="cimd", client_name="Gone",
                redirect_uris=[CIMD_REDIRECT], is_active=False).save()

    variants = (
        CIMD_VARIANT_URL,
        "https://CIMD.testit.EXAMPLE/variant-probe.json",
        "https://cimd.testit.example:443/variant-probe.json",
        "https://cimd.testit.example//variant-%70robe.json",
    )
    for variant in variants:
        try:
            get_connection().delete(clients._cimd_cache_key(canonical))
        except Exception:
            pass
        fetch = _fetcher(_document(client_id=canonical), echo_url=True)
        refused = False
        try:
            clients.resolve_client(variant, fetcher=fetch)
        except clients.ClientError:
            refused = True
        assert_true(refused,
                    f"a deactivated client must stay refused when reached as "
                    f"{variant} — the kill switch is not a spelling contest")
        assert_eq(fetch.calls, [],
                  f"{variant} must be refused BEFORE any outbound fetch, "
                  f"got {fetch.calls}")

    assert_eq(OAuthClient.objects.filter(
        client_id__contains="variant-probe.json").count(), 1,
        "a refused variant must never create a second, active row")
    assert_true(not OAuthClient.objects.get(client_id=canonical).is_active,
                "the deactivated row must still be deactivated afterwards")


@th.django_unit_test("unknown and deactivated clients are indistinguishable")
def test_client_state_is_not_an_oracle(opts):
    from mojo.apps.account.services.oauth_server import clients

    messages = set()
    for client_id in (INACTIVE_CLIENT_ID, "testit-oauth-never-existed"):
        try:
            clients.resolve_client(client_id)
        except clients.ClientError as err:
            messages.add((err.code, err.description))
    assert_eq(len(messages), 1,
              f"a deactivated client and an unknown one must answer identically, "
              f"or the endpoint enumerates which identities exist — got {messages}")


@th.django_unit_test("a failing CIMD document is fetched once per cache window")
def test_cimd_failures_are_negatively_cached(opts):
    from mojo.helpers.redis import get_connection
    from mojo.apps.account.services.oauth_server import clients

    url = "https://cimd.testit.example/never-works.json"
    canonical = clients.canonical_cimd_url(url)
    try:
        get_connection().delete(clients._cimd_cache_key(canonical))
    except Exception:
        pass

    calls = []

    def stalling(target):
        calls.append(target)
        raise ValueError("Request timed out after 5s")

    for attempt in range(2):
        refused = False
        try:
            clients.resolve_client(url, fetcher=stalling)
        except clients.ClientError:
            refused = True
        assert_true(refused, f"attempt {attempt + 1} must be refused")
    assert_eq(len(calls), 1,
              f"a failing client metadata URL must cost ONE outbound fetch per "
              f"window, not one per attempt — got {len(calls)}")

    try:
        get_connection().delete(clients._cimd_cache_key(canonical))
    except Exception:
        pass


@th.django_unit_test("a client cannot forge a log line through its own name")
def test_client_name_is_sanitised(opts):
    from mojo.apps.account.models import OAuthClient
    from mojo.helpers.redis import get_connection
    from mojo.apps.account.services.oauth_server import clients

    forged = "Nice App" + chr(10) + "2026-01-01 ERROR forged   entry" + chr(7)
    result = clients.register_client({
        "redirect_uris": ["https://sanitise.example/cb"],
        "client_name": forged,
    })
    row = OAuthClient.objects.get(client_id=result["client_id"])
    assert_true("\n" not in row.client_name and "\r" not in row.client_name,
                f"a newline in client_name would forge a log record, "
                f"got {row.client_name!r}")
    assert_true(chr(7) not in row.client_name,
                f"control characters must be stripped, got {row.client_name!r}")
    assert_eq(row.client_name, "Nice App 2026-01-01 ERROR forged entry",
              f"whitespace must collapse to single spaces, got {row.client_name!r}")
    OAuthClient.objects.filter(client_id=result["client_id"]).delete()

    url = "https://cimd.testit.example/noisy.json"
    canonical = clients.canonical_cimd_url(url)
    OAuthClient.objects.filter(client_id=canonical).delete()
    try:
        get_connection().delete(clients._cimd_cache_key(canonical))
    except Exception:
        pass
    client = clients.resolve_client(url, fetcher=_fetcher(
        _document(client_id=canonical, client_name=forged)))
    assert_true("\n" not in client.client_name,
                f"a CIMD document must not be able to forge a log line either, "
                f"got {client.client_name!r}")
    OAuthClient.objects.filter(client_id=canonical).delete()
