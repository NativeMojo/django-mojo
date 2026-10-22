"""
Microsoft ID-token validation (maestro #9105, plan #8967 B9).

Every case signs a token with an RSA key generated here and serves the
matching JWKS from a fake ``fetch_jwks`` set on the provider instance, so no
network is used. Client ID and tenant go through ``_validate_id_token``'s
keyword seams. The JWKS cache and the re-fetch guard live in this checkout's
Redis, and the cases share tenant cache keys, which is why the module sits in
the serial package.
"""
import time
import uuid

from testit import helpers as th

CLIENT_ID = "00000000-0000-0000-0000-0000000000a1"
WORK_TID = "72f988bf-86f1-41af-91ab-2d7cd011db47"
OTHER_TID = "11111111-2222-4333-8444-555555555555"
CONSUMERS_TID = "9188040d-6c67-4c5b-b112-36a304b66dad"
TEMPLATE_ISSUER = "https://login.microsoftonline.com/{tenantid}/v2.0"
NONCE = "nonce-9105-abc"
VERIFY_FAILED = "Microsoft sign-in could not be verified"

_KEYS = {}


def _rsa():
    if "rsa" not in _KEYS:
        from cryptography.hazmat.primitives.asymmetric import rsa
        _KEYS["rsa"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _KEYS["rsa2"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _KEYS["rsa"], _KEYS["rsa2"]


def _jwk(private_key, kid, issuer=TEMPLATE_ISSUER):
    import json
    from jwt.algorithms import RSAAlgorithm
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "kty": "RSA"})
    if issuer is not None:
        jwk["issuer"] = issuer
    return jwk


def _claims(tid=WORK_TID, **over):
    now = int(time.time())
    claims = {
        "aud": CLIENT_ID,
        "iss": f"https://login.microsoftonline.com/{tid}/v2.0",
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "tid": tid,
        "oid": "00000000-0000-0000-66f3-3332eca7ea81",
        "sub": "pairwise-sub",
        "nonce": NONCE,
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "xms_edov": True,
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


def _token(claims, key=None, kid="k1", alg="RS256"):
    import jwt
    key = key if key is not None else _rsa()[0]
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


class _Provider:
    """A MicrosoftOAuthProvider whose fetch_jwks serves `keys` and counts calls."""

    def __init__(self, keys, tenant):
        from mojo.apps.account.services.oauth.microsoft import (
            MicrosoftOAuthProvider, JWKS_CACHE_KEY, JWKS_REFETCH_KEY)
        from mojo.helpers.redis import get_connection
        self.svc = MicrosoftOAuthProvider()
        self.fetches = 0
        self.keys = keys
        self.tenant = tenant

        def fetch(tenant_arg):
            self.fetches += 1
            return {"keys": list(self.keys)}

        self.svc.fetch_jwks = fetch
        r = get_connection()
        r.delete(JWKS_CACHE_KEY.format(tenant=tenant), JWKS_REFETCH_KEY.format(tenant=tenant))

    def validate(self, token, nonce=NONCE, client_id=CLIENT_ID):
        return self.svc._validate_id_token(token, nonce, client_id=client_id, tenant=self.tenant)


def _rejected(provider, token, why, **kw):
    try:
        provider.validate(token, **kw)
    except ValueError as exc:
        assert str(exc) == VERIFY_FAILED, (
            f"{why}: the error shown to the caller must be the generic one, got {exc!r}")
        return
    raise AssertionError(f"{why}: the token must be rejected")


def _default(tenant="common"):
    return _Provider([_jwk(_rsa()[0], "k1")], tenant)


@th.django_unit_test("microsoft id token: a well-formed token validates and yields its claims")
def test_ms_token_good(opts):
    p = _default()
    claims = p.validate(_token(_claims()))
    assert claims["tid"] == WORK_TID, f"tid must come back, got {claims.get('tid')}"
    assert claims["oid"].endswith("ea81"), f"oid must come back, got {claims.get('oid')}"
    assert p.fetches == 1, f"one JWKS fetch expected, got {p.fetches}"
    p.validate(_token(_claims()))
    assert p.fetches == 1, f"the second validation must use the cached JWKS, got {p.fetches} fetches"


@th.django_unit_test("microsoft id token: a token signed by another key is rejected")
def test_ms_token_bad_signature(opts):
    _rejected(_default(), _token(_claims(), key=_rsa()[1]), "bad signature")


@th.django_unit_test("microsoft id token: a token for another audience is rejected")
def test_ms_token_wrong_aud(opts):
    _rejected(_default(), _token(_claims(aud="someone-else")), "wrong aud")


@th.django_unit_test("microsoft id token: an expired token is rejected")
def test_ms_token_expired(opts):
    now = int(time.time())
    _rejected(_default(), _token(_claims(iat=now - 7200, nbf=now - 7200, exp=now - 600)),
              "expired beyond the 60 s leeway")


@th.django_unit_test("microsoft id token: a token not yet valid (nbf in the future) is rejected")
def test_ms_token_nbf_future(opts):
    now = int(time.time())
    _rejected(_default(), _token(_claims(nbf=now + 600, iat=now)), "nbf in the future")


@th.django_unit_test("microsoft id token: a wrong or missing nonce is rejected")
def test_ms_token_wrong_nonce(opts):
    p = _default()
    _rejected(p, _token(_claims(nonce="other")), "nonce mismatch")
    _rejected(p, _token(_claims(nonce=None)), "nonce missing from the token")
    _rejected(p, _token(_claims()), "no nonce in the state", nonce=None)


@th.django_unit_test("microsoft id token: a tid that is not a lower-case GUID is rejected")
def test_ms_token_tid_not_guid(opts):
    p = _default()
    _rejected(p, _token(_claims(tid="not-a-guid",
                                iss="https://login.microsoftonline.com/not-a-guid/v2.0")),
              "tid not a GUID")
    upper = WORK_TID.upper()
    _rejected(p, _token(_claims(tid=upper, iss=f"https://login.microsoftonline.com/{upper}/v2.0")),
              "tid in upper case")


@th.django_unit_test("microsoft id token: an iss that does not name the tid is rejected")
def test_ms_token_iss_tid_mismatch(opts):
    _rejected(_default(), _token(_claims(iss=f"https://login.microsoftonline.com/{OTHER_TID}/v2.0")),
              "iss names another tenant than tid")


@th.django_unit_test("microsoft id token: a key whose issuer names another tenant cannot sign for this one")
def test_ms_token_key_issuer_scoped(opts):
    p = _Provider([_jwk(_rsa()[0], "k1",
                        issuer=f"https://login.microsoftonline.com/{OTHER_TID}/v2.0")], "common")
    _rejected(p, _token(_claims()), "key issuer is a different exact tenant")
    p = _Provider([_jwk(_rsa()[0], "k1",
                        issuer="https://login.microsoftonline.com/{tenantid}/v2.0/evil")], "common")
    _rejected(p, _token(_claims()), "template issuer that does not match after substitution")


@th.django_unit_test("microsoft id token: the consumers key cannot sign a work-tenant token")
def test_ms_token_consumers_key_for_work_tenant(opts):
    consumers_issuer = f"https://login.microsoftonline.com/{CONSUMERS_TID}/v2.0"
    p = _Provider([_jwk(_rsa()[0], "k1", issuer=consumers_issuer)], "common")
    _rejected(p, _token(_claims(tid=WORK_TID)), "consumers exact-issuer key on a work token")
    claims = p.validate(_token(_claims(tid=CONSUMERS_TID)))
    assert claims["tid"] == CONSUMERS_TID, "the consumers key must still sign a personal-account token"


@th.django_unit_test("microsoft id token: a signing key without an issuer is rejected")
def test_ms_token_key_without_issuer(opts):
    p = _Provider([_jwk(_rsa()[0], "k1", issuer=None)], "common")
    _rejected(p, _token(_claims()), "key with no issuer field")


@th.django_unit_test("microsoft id token: alg none and HS256 key confusion are rejected")
def test_ms_token_alg_confusion(opts):
    import base64
    import json
    import jwt
    from cryptography.hazmat.primitives import serialization
    p = _default()

    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    none_token = f"{b64({'alg': 'none', 'kid': 'k1', 'typ': 'JWT'})}.{b64(_claims())}."
    _rejected(p, none_token, "alg none")

    public_pem = _rsa()[0].public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    header = b64({"alg": "HS256", "kid": "k1", "typ": "JWT"})
    body = b64(_claims())
    import hashlib
    import hmac
    sig = base64.urlsafe_b64encode(
        hmac.new(public_pem, f"{header}.{body}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    _rejected(p, f"{header}.{body}.{sig}", "HS256 signed with the public key")
    assert jwt.get_unverified_header(f"{header}.{body}.{sig}")["alg"] == "HS256", \
        "test token must really carry HS256"


@th.django_unit_test("microsoft id token: an unknown kid re-fetches once, then is throttled")
def test_ms_token_unknown_kid_refetch(opts):
    p = _default()
    p.validate(_token(_claims()))
    assert p.fetches == 1, f"first validation fetches once, got {p.fetches}"

    # A rotated key appears upstream: one re-fetch finds it.
    p.keys = [_jwk(_rsa()[0], "k1"), _jwk(_rsa()[1], "k2")]
    claims = p.validate(_token(_claims(), key=_rsa()[1], kid="k2"))
    assert claims["tid"] == WORK_TID, "the rotated key must validate after one re-fetch"
    assert p.fetches == 2, f"an unknown kid must trigger exactly one re-fetch, got {p.fetches}"

    # Another unknown kid inside 5 minutes: throttled, no fetch, rejected.
    _rejected(p, _token(_claims(), kid="k3"), "unknown kid while the re-fetch guard is set")
    assert p.fetches == 2, f"the re-fetch must be throttled for 5 minutes, got {p.fetches} fetches"


@th.django_unit_test("microsoft id token: tenant policy for organizations, consumers, a GUID and common")
def test_ms_token_tenant_policy(opts):
    work = _token(_claims(tid=WORK_TID))
    personal = _token(_claims(tid=CONSUMERS_TID))

    p = _Provider([_jwk(_rsa()[0], "k1")], "organizations")
    assert p.validate(work)["tid"] == WORK_TID, "organizations must accept a work account"
    _rejected(p, personal, "organizations must refuse a personal account")

    p = _Provider([_jwk(_rsa()[0], "k1")], "consumers")
    assert p.validate(personal)["tid"] == CONSUMERS_TID, "consumers must accept a personal account"
    _rejected(p, work, "consumers must refuse a work account")

    p = _Provider([_jwk(_rsa()[0], "k1")], WORK_TID)
    assert p.validate(work)["tid"] == WORK_TID, "a GUID tenant must accept its own accounts"
    _rejected(p, _token(_claims(tid=OTHER_TID)), "a GUID tenant must refuse another tenant")

    p = _Provider([_jwk(_rsa()[0], "k1")], "common")
    assert p.validate(work)["tid"] == WORK_TID, "common must accept a work account"
    assert p.validate(personal)["tid"] == CONSUMERS_TID, "common must accept a personal account"


@th.django_unit_test("microsoft id token: get_profile keys identity on tid:oid and reads xms_edov")
def test_ms_profile_shape(opts):
    p = _default()
    original = p.svc._validate_id_token
    p.svc._validate_id_token = lambda token, nonce: original(
        token, nonce, client_id=CLIENT_ID, tenant="common")
    profile = p.svc.get_profile({"id_token": _token(_claims())}, {"nonce": NONCE})
    assert profile["uid"] == f"{WORK_TID}:00000000-0000-0000-66f3-3332eca7ea81", \
        f"uid must be tid:oid, got {profile['uid']}"
    assert profile["email"] == "ada@example.com", f"email must come back, got {profile['email']}"
    assert profile["email_verified"] is True, "xms_edov true must give email_verified True"

    profile = p.svc.get_profile({"id_token": _token(_claims(xms_edov=None))}, {"nonce": NONCE})
    assert profile["email_verified"] is False, "absent xms_edov must give email_verified False"
    profile = p.svc.get_profile({"id_token": _token(_claims(xms_edov="true"))}, {"nonce": NONCE})
    assert profile["email_verified"] is False, "a string 'true' is not xms_edov True"
    profile = p.svc.get_profile({"id_token": _token(_claims(email=None))}, {"nonce": NONCE})
    assert profile["email"] is None and profile["email_verified"] is False, \
        f"no email must give email None, unverified, got {profile}"

    raised = False
    try:
        p.svc.get_profile({"access_token": "x"}, {"nonce": NONCE})
    except ValueError:
        raised = True
    assert raised, "a token response without id_token must be refused"
