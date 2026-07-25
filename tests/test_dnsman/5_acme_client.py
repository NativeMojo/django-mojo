"""ACMEv2 client tests -- protocol shape, nonce handling, and the DNS-01 flow.

Everything here runs in-process against a fully scripted stand-in for
``requests``: no socket is ever opened and no CA is ever contacted. The
certificate *service* (dnsman) is tested separately; this file is only about
the protocol helpers in ``mojo/helpers/acme/``.
"""
import hashlib
import json
from unittest.mock import patch

from requests.structures import CaseInsensitiveDict
from testit import helpers as th


DIRECTORY_URL = "https://acme.test/directory"
NEW_NONCE_URL = "https://acme.test/new-nonce"
NEW_ACCOUNT_URL = "https://acme.test/new-acct"
NEW_ORDER_URL = "https://acme.test/new-order"
REVOKE_CERT_URL = "https://acme.test/revoke-cert"
ACCOUNT_URL = "https://acme.test/acct/42"
ORDER_URL = "https://acme.test/order/7"
AUTHZ_URL = "https://acme.test/authz/7-1"
CHALLENGE_URL = "https://acme.test/chall/7-1/dns"
FINALIZE_URL = "https://acme.test/order/7/finalize"
CERT_URL = "https://acme.test/cert/7"

BAD_NONCE = "urn:ietf:params:acme:error:badNonce"
RATE_LIMITED = "urn:ietf:params:acme:error:rateLimited"

DIRECTORY_BODY = {
    "newNonce": NEW_NONCE_URL,
    "newAccount": NEW_ACCOUNT_URL,
    "newOrder": NEW_ORDER_URL,
    "revokeCert": REVOKE_CERT_URL
}

# A fixed P-256 scalar so the JWK, its RFC 7638 thumbprint, and the DNS-01
# digest are stable, checkable vectors rather than "whatever we just generated".
FIXED_SCALAR = 0x4f3b1c2d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809
FIXED_JWK_X = "AvTHIScF_4GbPWpfgz0J8FABfCtgGQlhWZYOKCnVQXM"
FIXED_JWK_Y = "Q3tpsYY3EmblBjUrMYTweS6tMAoa-9McUb43JLkHVkI"
FIXED_THUMBPRINT = "uJ6b6CLjbNcloKNg9xcf1suGtIVU6sY05dedR0ypnlo"

CHALLENGE_TOKEN = "evaGxfADs6pSRb2LAv9IZf17Dt3juxGJ-PCt92wr-oA"

PEM_CHAIN = (
    "-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----\n"
    "-----BEGIN CERTIFICATE-----\nissuer\n-----END CERTIFICATE-----\n")


def _fixed_key():
    """A deterministic P-256 key, so vectors in this file never drift."""
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.derive_private_key(FIXED_SCALAR, ec.SECP256R1())


class FakeResponse(object):
    """A stand-in for requests.Response with just what the client reads."""

    def __init__(self, status_code=200, body=None, headers=None, text=None):
        self.status_code = status_code
        self._body = body
        self.headers = CaseInsensitiveDict(headers or {})
        if text is not None:
            self.text = text
        elif body is not None:
            self.text = json.dumps(body)
        else:
            self.text = ""

    def json(self):
        if self._body is None:
            raise ValueError("response has no JSON body")
        return self._body


class FakeAcme(object):
    """Scripted stand-in for the ``requests`` module used by the ACME client.

    POST responses are queued per URL. The last queued response for a URL is
    reused if the client posts again (which is what makes poll loops easy to
    script); every request is recorded with its decoded protected header and
    payload so tests can assert on the wire format.
    """

    def __init__(self, directory=None):
        self.directory_body = dict(directory or DIRECTORY_BODY)
        self.post_queue = {}
        self.posts = []
        self.get_calls = []
        self.head_calls = []
        self.nonce_counter = 0

    def next_nonce(self):
        self.nonce_counter += 1
        return f"nonce-{self.nonce_counter}"

    def queue(self, url, *responses):
        self.post_queue.setdefault(url, []).extend(responses)
        return self

    def _with_nonce(self, resp):
        # A response that explicitly sets the header (even to "") keeps it, so
        # a test can model a CA that failed to hand back a usable nonce.
        if "Replay-Nonce" not in resp.headers:
            resp.headers["Replay-Nonce"] = self.next_nonce()
        return resp

    def get(self, url, **kwargs):
        # Real CAs do not put a Replay-Nonce on the directory document, so the
        # first POST always has to go and fetch one.
        self.get_calls.append(url)
        return FakeResponse(200, self.directory_body)

    def head(self, url, **kwargs):
        self.head_calls.append(url)
        return self._with_nonce(FakeResponse(200))

    def post(self, url, data=None, headers=None, **kwargs):
        from jwt.utils import base64url_decode
        from objict import objict

        body = json.loads(data)
        protected = json.loads(base64url_decode(body["protected"]).decode("utf-8"))
        payload_raw = body["payload"]
        payload = None
        if payload_raw:
            payload = json.loads(base64url_decode(payload_raw).decode("utf-8"))
        self.posts.append(objict(
            url=url, protected=protected, payload=payload,
            payload_raw=payload_raw, signature=body["signature"],
            headers=dict(headers or {})))

        queue = self.post_queue.get(url)
        assert queue, f"no scripted ACME response was queued for POST {url}"
        resp = queue.pop(0) if len(queue) > 1 else queue[0]
        return self._with_nonce(resp)


def _client(transport, key=None, **kwargs):
    from mojo.helpers.acme.client import AcmeClient
    return AcmeClient(DIRECTORY_URL, key or _fixed_key(), **kwargs)


# ---------------------------------------------------------------------------
# JWS shape
# ---------------------------------------------------------------------------

@th.django_unit_test("acme jws: protected header carries only the RFC 8555 members")
def test_acme_jws_protected_header_shape(opts):
    from jwt.utils import base64url_decode
    from mojo.helpers.acme import jws

    key = _fixed_key()
    header = {"alg": "ES256", "nonce": "abc123", "url": NEW_ACCOUNT_URL,
              "jwk": jws.jwk(key)}
    signed = jws.sign({"termsOfServiceAgreed": True}, key, header)

    assert set(signed.keys()) == {"protected", "payload", "signature"}, \
        f"a flattened JSON JWS has exactly protected/payload/signature, got {sorted(signed.keys())}"

    protected = json.loads(base64url_decode(signed["protected"]).decode("utf-8"))
    assert set(protected.keys()) == {"alg", "nonce", "url", "jwk"}, \
        f"protected header must be exactly alg/nonce/url/jwk, got {sorted(protected.keys())}"
    # The whole reason the JWS is hand-rolled: PyJWS.encode injects typ, and
    # RFC 8555 servers reject a protected header carrying members they did not
    # ask for.
    assert "typ" not in protected, \
        f"protected header must NOT contain typ (PyJWT injects it), got {protected}"
    assert protected["alg"] == "ES256", \
        f"expected alg ES256, got {protected.get('alg')}"


@th.django_unit_test("acme jws: signature verifies over protected.payload")
def test_acme_jws_signature_verifies(opts):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from jwt.utils import raw_to_der_signature
    from mojo.helpers.acme import jws

    key = _fixed_key()
    signed = jws.sign({"a": 1}, key, {"alg": "ES256", "nonce": "n", "url": "https://acme.test/x"})
    signing_input = f"{signed['protected']}.{signed['payload']}".encode("ascii")
    der = raw_to_der_signature(jws.b64_decode(signed["signature"]), key.curve)

    verified = True
    try:
        key.public_key().verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        verified = False
    assert verified, "the ES256 signature must verify over ASCII(protected + '.' + payload)"

    tampered = f"{signed['protected']}.{signed['payload']}x".encode("ascii")
    still_valid = True
    try:
        key.public_key().verify(der, tampered, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        still_valid = False
    assert not still_valid, "a modified signing input must NOT verify against the signature"


@th.django_unit_test("acme jws: POST-as-GET signs an empty payload, not '{}'")
def test_acme_jws_post_as_get_payload(opts):
    from mojo.helpers.acme import jws

    key = _fixed_key()
    header = {"alg": "ES256", "nonce": "n", "url": AUTHZ_URL, "kid": ACCOUNT_URL}

    empty = jws.sign("", key, header)
    assert empty["payload"] == "", \
        f"POST-as-GET must sign an empty base64url payload, got {empty['payload']!r}"
    assert jws.sign(None, key, header)["payload"] == "", \
        "a None payload must also encode to the empty POST-as-GET payload"

    obj = jws.sign({}, key, header)
    assert obj["payload"] == jws.b64("{}"), \
        f"an empty JSON object payload must encode '{{}}', got {obj['payload']!r}"
    assert obj["payload"] != "", \
        "'{}' and POST-as-GET are different requests and must not encode identically"


# ---------------------------------------------------------------------------
# JWK / thumbprint / key authorization
# ---------------------------------------------------------------------------

@th.django_unit_test("acme jws: public JWK is a fixed-width P-256 EC key")
def test_acme_jwk_members(opts):
    from mojo.helpers.acme import jws

    key = _fixed_key()
    members = jws.jwk(key)
    assert members["kty"] == "EC", f"expected kty EC, got {members.get('kty')}"
    assert members["crv"] == "P-256", f"expected crv P-256, got {members.get('crv')}"
    assert members["x"] == FIXED_JWK_X, \
        f"JWK x drifted for the fixed key: expected {FIXED_JWK_X}, got {members['x']}"
    assert members["y"] == FIXED_JWK_Y, \
        f"JWK y drifted for the fixed key: expected {FIXED_JWK_Y}, got {members['y']}"
    assert len(jws.b64_decode(members["x"])) == 32, \
        "JWK x must be a fixed-width 32-byte coordinate (RFC 7518 6.2.1.2)"
    assert len(jws.b64_decode(members["y"])) == 32, \
        "JWK y must be a fixed-width 32-byte coordinate (RFC 7518 6.2.1.2)"
    assert "=" not in members["x"] and "=" not in members["y"], \
        "base64url members must have their padding stripped"


@th.django_unit_test("acme jws: RFC 7638 thumbprint hashes the canonical JWK JSON")
def test_acme_thumbprint_canonical_form(opts):
    from mojo.helpers.acme import jws

    key = _fixed_key()
    canonical = jws.thumbprint_json(key)
    expected_json = (
        '{"crv":"P-256","kty":"EC","x":"%s","y":"%s"}' % (FIXED_JWK_X, FIXED_JWK_Y))
    # RFC 7638 section 3: required members only, lexicographic order, no
    # whitespace. Any deviation yields a thumbprint the CA disagrees with, and
    # the only symptom is every DNS-01 validation failing.
    assert canonical == expected_json, \
        f"thumbprint input must be exactly {expected_json}, got {canonical}"
    assert " " not in canonical, f"thumbprint input must contain no whitespace, got {canonical}"

    expected = jws.b64(hashlib.sha256(expected_json.encode("utf-8")).digest())
    assert jws.thumbprint(key) == expected, \
        f"thumbprint must be base64url(SHA-256(canonical JSON)), got {jws.thumbprint(key)}"
    assert jws.thumbprint(key) == FIXED_THUMBPRINT, \
        f"thumbprint drifted for the fixed key: expected {FIXED_THUMBPRINT}, got {jws.thumbprint(key)}"


@th.django_unit_test("acme jws: DNS-01 TXT value is base64url(sha256(token.thumbprint))")
def test_acme_dns01_txt_value(opts):
    from mojo.helpers.acme import jws

    key = _fixed_key()
    key_auth = jws.key_authorization(CHALLENGE_TOKEN, key)
    assert key_auth == f"{CHALLENGE_TOKEN}.{FIXED_THUMBPRINT}", \
        f"key authorization must be token + '.' + thumbprint, got {key_auth}"

    expected = jws.b64(hashlib.sha256(key_auth.encode("utf-8")).digest())
    assert jws.dns_txt_value(CHALLENGE_TOKEN, key) == expected, \
        f"DNS-01 TXT value must be base64url(sha256(key authorization)), got {jws.dns_txt_value(CHALLENGE_TOKEN, key)}"
    assert "=" not in expected, "the TXT value must be base64url with padding stripped"

    transport = FakeAcme()
    with patch("mojo.helpers.acme.client.requests", transport):
        client = _client(transport, key=key)
        assert client.key_authorization_digest(CHALLENGE_TOKEN) == expected, \
            "the client digest helper must match jws.dns_txt_value"
    assert not transport.posts, "computing a TXT digest must not touch the network"


# ---------------------------------------------------------------------------
# Transport: nonces, retries, problem documents
# ---------------------------------------------------------------------------

@th.django_unit_test("acme client: newAccount signs with jwk and never with kid")
def test_acme_new_account_uses_jwk(opts):
    transport = FakeAcme()
    transport.queue(NEW_ACCOUNT_URL,
                    FakeResponse(201, {"status": "valid"}, {"Location": ACCOUNT_URL}))

    with patch("mojo.helpers.acme.client.requests", transport):
        client = _client(transport, contact_email="ops@example.com")
        account_url = client.new_account()

    assert account_url == ACCOUNT_URL, \
        f"new_account must return the Location header, got {account_url}"
    assert client.kid == ACCOUNT_URL, "the account URL must be cached as the kid"

    post = transport.posts[0]
    assert set(post.protected.keys()) == {"alg", "nonce", "url", "jwk"}, \
        f"newAccount protected header must be alg/nonce/url/jwk, got {sorted(post.protected.keys())}"
    assert "kid" not in post.protected, \
        "a newAccount request must never carry kid alongside jwk"
    assert post.protected["url"] == NEW_ACCOUNT_URL, \
        f"the protected url must be the request URL, got {post.protected['url']}"
    assert post.payload.get("termsOfServiceAgreed") is True, \
        f"newAccount must agree to the terms of service, got {post.payload}"
    assert post.payload.get("contact") == ["mailto:ops@example.com"], \
        f"the contact must be normalized to a mailto: URI, got {post.payload.get('contact')}"
    assert post.headers.get("Content-Type") == "application/jose+json", \
        f"ACME POSTs must be application/jose+json, got {post.headers.get('Content-Type')}"
    assert transport.head_calls == [NEW_NONCE_URL], \
        f"the first POST must be preceded by exactly one newNonce HEAD, got {transport.head_calls}"


@th.django_unit_test("acme client: a badNonce problem is retried once with the fresh nonce")
def test_acme_bad_nonce_retry(opts):
    transport = FakeAcme()
    transport.queue(
        NEW_ACCOUNT_URL,
        FakeResponse(400,
                     {"type": BAD_NONCE, "detail": "JWS has an invalid anti-replay nonce"},
                     {"Content-Type": "application/problem+json",
                      "Replay-Nonce": "fresh-nonce"}),
        FakeResponse(201, {"status": "valid"}, {"Location": ACCOUNT_URL}))

    with patch("mojo.helpers.acme.client.requests", transport):
        client = _client(transport)
        account_url = client.new_account()

    assert account_url == ACCOUNT_URL, \
        "a badNonce retry must succeed transparently and return the account URL"
    assert len(transport.posts) == 2, \
        f"badNonce must trigger exactly one retry (2 POSTs total), got {len(transport.posts)}"
    assert transport.posts[1].protected["nonce"] == "fresh-nonce", \
        f"the retry must use the nonce from the rejecting response, got {transport.posts[1].protected['nonce']}"
    assert transport.posts[0].protected["nonce"] != transport.posts[1].protected["nonce"], \
        "the retry must not replay the rejected nonce"


@th.django_unit_test("acme client: badNonce without a fresh nonce refetches one")
def test_acme_bad_nonce_without_replacement(opts):
    transport = FakeAcme()
    # No Replay-Nonce on the rejection: the client has nothing usable and must
    # go back to newNonce rather than replaying the nonce the CA just refused.
    rejection = FakeResponse(400, {"type": BAD_NONCE, "detail": "bad nonce"},
                             {"Content-Type": "application/problem+json"})
    rejection.headers["Replay-Nonce"] = ""
    transport.queue(NEW_ACCOUNT_URL, rejection,
                    FakeResponse(201, {"status": "valid"}, {"Location": ACCOUNT_URL}))

    with patch("mojo.helpers.acme.client.requests", transport):
        client = _client(transport)
        client.new_account()

    assert len(transport.head_calls) == 2, \
        f"a badNonce with no replacement nonce must refetch from newNonce, got {transport.head_calls}"
    assert transport.posts[0].protected["nonce"] != transport.posts[1].protected["nonce"], \
        "the retry must carry a different nonce than the rejected request"


@th.django_unit_test("acme client: a second badNonce is not retried again")
def test_acme_bad_nonce_retries_only_once(opts):
    from mojo import errors as me

    transport = FakeAcme()
    transport.queue(
        NEW_ACCOUNT_URL,
        FakeResponse(400, {"type": BAD_NONCE, "detail": "first"},
                     {"Content-Type": "application/problem+json"}),
        FakeResponse(400, {"type": BAD_NONCE, "detail": "second"},
                     {"Content-Type": "application/problem+json"}))

    raised = None
    with patch("mojo.helpers.acme.client.requests", transport):
        client = _client(transport)
        try:
            client.new_account()
        except me.ValueException as err:
            raised = err

    assert raised is not None, "a repeated badNonce must eventually raise, not loop"
    assert len(transport.posts) == 2, \
        f"badNonce must be retried exactly once, got {len(transport.posts)} POSTs"
    assert "second" in raised.reason, \
        f"the surfaced error must be the second rejection's detail, got {raised.reason}"


@th.django_unit_test("acme client: a problem document surfaces the CA's type and detail")
def test_acme_problem_document_mapping(opts):
    from mojo import errors as me

    detail = ("Error creating new order :: too many certificates already issued "
              "for example.com, retry after 2026-08-01T00:00:00Z")
    transport = FakeAcme()
    transport.queue(NEW_ACCOUNT_URL,
                    FakeResponse(201, {"status": "valid"}, {"Location": ACCOUNT_URL}))
    transport.queue(NEW_ORDER_URL,
                    FakeResponse(429, {"type": RATE_LIMITED, "detail": detail},
                                 {"Content-Type": "application/problem+json"}))

    raised = None
    with patch("mojo.helpers.acme.client.requests", transport):
        client = _client(transport)
        client.new_account()
        try:
            client.new_order(["example.com"])
        except me.ValueException as err:
            raised = err

    assert raised is not None, "an ACME problem document must raise a ValueException"
    assert detail in raised.reason, \
        f"the CA's detail must be preserved verbatim, got {raised.reason}"
    assert RATE_LIMITED in raised.reason, \
        f"the CA's problem type must be preserved verbatim, got {raised.reason}"
    assert raised.problem_type == RATE_LIMITED, \
        f"the exception must carry the problem type, got {raised.problem_type}"
    assert raised.detail == detail, \
        f"the exception must carry the problem detail, got {raised.detail}"
    assert raised.status == 400, \
        f"an ACME problem must surface as a 400-class error, not a 500, got {raised.status}"


@th.django_unit_test("acme client: a request without an account URL refuses before posting")
def test_acme_requires_kid(opts):
    from mojo import errors as me

    transport = FakeAcme()
    raised = None
    with patch("mojo.helpers.acme.client.requests", transport):
        client = _client(transport)
        try:
            client.new_order(["example.com"])
        except me.ValueException as err:
            raised = err

    assert raised is not None, "posting without a kid must raise instead of signing with jwk"
    assert "kid" in raised.reason, \
        f"the error must name the missing account URL (kid), got {raised.reason}"
    assert not transport.posts, "no JWS may be posted when the client has no account URL"


# ---------------------------------------------------------------------------
# The DNS-01 issuance flow
# ---------------------------------------------------------------------------

def _script_happy_path(transport):
    transport.queue(NEW_ACCOUNT_URL,
                    FakeResponse(201, {"status": "valid"}, {"Location": ACCOUNT_URL}))
    transport.queue(NEW_ORDER_URL, FakeResponse(201, {
        "status": "pending",
        "identifiers": [{"type": "dns", "value": "example.com"},
                        {"type": "dns", "value": "*.example.com"}],
        "authorizations": [AUTHZ_URL],
        "finalize": FINALIZE_URL
    }, {"Location": ORDER_URL}))
    transport.queue(AUTHZ_URL, FakeResponse(200, {
        "status": "pending",
        "identifier": {"type": "dns", "value": "example.com"},
        "wildcard": True,
        "challenges": [
            {"type": "http-01", "url": "https://acme.test/chall/7-1/http", "token": "http-token"},
            {"type": "dns-01", "url": CHALLENGE_URL, "token": CHALLENGE_TOKEN}
        ]
    }))
    transport.queue(CHALLENGE_URL, FakeResponse(200, {"type": "dns-01", "status": "processing"}))
    transport.queue(ORDER_URL,
                    FakeResponse(200, {"status": "pending", "finalize": FINALIZE_URL}),
                    FakeResponse(200, {"status": "ready", "finalize": FINALIZE_URL}))
    transport.queue(FINALIZE_URL, FakeResponse(200, {
        "status": "valid", "certificate": CERT_URL, "finalize": FINALIZE_URL}))
    transport.queue(CERT_URL, FakeResponse(200, None, {}, text=PEM_CHAIN))


@th.django_unit_test("acme client: newOrder -> authz -> challenge -> finalize -> download")
def test_acme_happy_path(opts):
    from mojo.helpers.acme import client as acme_client
    from mojo.helpers.acme import jws

    key = _fixed_key()
    transport = FakeAcme()
    _script_happy_path(transport)

    with patch("mojo.helpers.acme.client.requests", transport):
        client = _client(transport, key=key)
        client.new_account()

        order = client.new_order(["example.com", "*.example.com"])
        assert order.url == ORDER_URL, \
            f"the order URL must come from the Location header, got {order.url}"
        assert order.authorizations == [AUTHZ_URL], \
            f"the order must expose its authorizations, got {order.get('authorizations')}"

        authz = client.get_authorization(AUTHZ_URL)
        challenge_url, token = client.dns01_challenge(authz)
        assert challenge_url == CHALLENGE_URL, \
            f"dns01_challenge must select the dns-01 challenge URL, got {challenge_url}"
        assert token == CHALLENGE_TOKEN, \
            f"dns01_challenge must return the dns-01 token, got {token}"

        digest = client.key_authorization_digest(token)
        assert digest == jws.dns_txt_value(token, key), \
            "the TXT digest must be derived from the account key"

        client.answer_challenge(challenge_url)
        ready = client.poll_order(ORDER_URL, until=acme_client.ORDER_READY,
                                  timeout=5, interval=0)
        assert ready.status == "ready", \
            f"the order must be polled until it is ready to finalize, got {ready.get('status')}"

        csr_der = acme_client.make_csr(jws.generate_key(), ["example.com", "*.example.com"])
        finalized = client.finalize(ready, csr_der)
        assert finalized.certificate == CERT_URL, \
            f"finalize must return the order carrying the certificate URL, got {finalized.get('certificate')}"

        chain = client.download_certificate(finalized.certificate)

    assert chain == PEM_CHAIN, \
        "download_certificate must return the full PEM chain body verbatim"
    assert chain.count("BEGIN CERTIFICATE") == 2, \
        f"the downloaded chain must include the issuer, got {chain.count('BEGIN CERTIFICATE')} certificates"

    by_url = {}
    for post in transport.posts:
        by_url.setdefault(post.url, []).append(post)

    for post in transport.posts[1:]:
        assert post.protected.get("kid") == ACCOUNT_URL, \
            f"every post after newAccount must be signed with kid, got {post.protected}"
        assert "jwk" not in post.protected, \
            f"kid and jwk must never both appear in a protected header, got {sorted(post.protected.keys())}"
        assert set(post.protected.keys()) == {"alg", "nonce", "url", "kid"}, \
            f"protected header must be exactly alg/nonce/url/kid, got {sorted(post.protected.keys())}"
        assert "typ" not in post.protected, \
            "no request may carry a typ member in its protected header"

    for url in (AUTHZ_URL, ORDER_URL, CERT_URL):
        for post in by_url[url]:
            assert post.payload_raw == "", \
                f"{url} must be fetched POST-as-GET with an empty payload, got {post.payload_raw!r}"

    assert by_url[CHALLENGE_URL][0].payload == {}, \
        f"answering a challenge must post an empty JSON object, got {by_url[CHALLENGE_URL][0].payload}"
    assert len(by_url[ORDER_URL]) == 2, \
        f"the order must be polled until it left 'pending', got {len(by_url[ORDER_URL])} polls"

    submitted = jws.b64_decode(by_url[FINALIZE_URL][0].payload["csr"])
    assert submitted == csr_der, \
        "finalize must submit the base64url-encoded DER CSR unchanged"

    nonces = [post.protected["nonce"] for post in transport.posts]
    assert len(set(nonces)) == len(nonces), \
        f"every request must use a distinct nonce, got {nonces}"


@th.django_unit_test("acme client: an authorization with no dns-01 challenge fails loudly")
def test_acme_missing_dns01_challenge(opts):
    from mojo import errors as me
    from objict import objict

    transport = FakeAcme()
    raised = None
    with patch("mojo.helpers.acme.client.requests", transport):
        client = _client(transport)
        authz = objict.from_dict({
            "identifier": {"type": "dns", "value": "example.com"},
            "challenges": [{"type": "http-01", "url": "https://acme.test/h", "token": "t"}]})
        try:
            client.dns01_challenge(authz)
        except me.ValueException as err:
            raised = err

    assert raised is not None, "an authorization without dns-01 must raise, not return None"
    assert "example.com" in raised.reason, \
        f"the error must name the identifier that cannot be validated, got {raised.reason}"


@th.django_unit_test("acme client: polling gives up cleanly at the timeout")
def test_acme_poll_timeout(opts):
    from mojo import errors as me

    transport = FakeAcme()
    transport.queue(NEW_ACCOUNT_URL,
                    FakeResponse(201, {"status": "valid"}, {"Location": ACCOUNT_URL}))
    transport.queue(ORDER_URL, FakeResponse(200, {"status": "pending"}))

    raised = None
    with patch("mojo.helpers.acme.client.requests", transport):
        client = _client(transport)
        client.new_account()
        try:
            client.poll_order(ORDER_URL, timeout=0, interval=0)
        except me.ValueException as err:
            raised = err

    assert raised is not None, "a stuck order must time out with an error, not hang"
    assert "pending" in raised.reason, \
        f"the timeout error must report the last status seen, got {raised.reason}"


# ---------------------------------------------------------------------------
# CSR
# ---------------------------------------------------------------------------

@th.django_unit_test("acme csr: every requested name lands in the SAN extension")
def test_acme_make_csr_sans(opts):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from mojo.helpers.acme import client as acme_client
    from mojo.helpers.acme import jws

    key = jws.generate_key()
    names = ["example.com", "*.example.com", "www.example.com"]
    csr_der = acme_client.make_csr(key, names)

    csr = x509.load_der_x509_csr(csr_der)
    san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    values = san.value.get_values_for_type(x509.DNSName)
    for name in names:
        assert name in values, f"the CSR must cover {name}, got {values}"
    assert "*.example.com" in values, \
        f"a wildcard name must survive into the SAN extension, got {values}"
    assert len(values) == len(names), \
        f"the CSR must carry exactly the requested names, got {values}"
    assert csr.is_signature_valid, "the CSR self-signature must verify"
    assert isinstance(csr.signature_hash_algorithm, hashes.SHA256), \
        f"the CSR must be signed with SHA-256, got {csr.signature_hash_algorithm}"


@th.django_unit_test("acme csr: names are normalized and de-duplicated")
def test_acme_make_csr_normalizes(opts):
    from cryptography import x509
    from mojo import errors as me
    from mojo.helpers.acme import client as acme_client
    from mojo.helpers.acme import jws

    key = jws.generate_key()
    csr = x509.load_der_x509_csr(
        acme_client.make_csr(key, ["Example.COM", "example.com.", " *.example.com "]))
    values = csr.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert values == ["example.com", "*.example.com"], \
        f"names must be lowercased, dot-stripped and de-duplicated, got {values}"

    raised = None
    try:
        acme_client.make_csr(key, [])
    except me.ValueException as err:
        raised = err
    assert raised is not None, "a CSR with no names must be refused, not silently built"
