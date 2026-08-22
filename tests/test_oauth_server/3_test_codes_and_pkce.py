"""
PKCE validation and the single-use authorization code.

The property that matters most here is that a presented code is spent whatever
happens next: a wrong verifier, a wrong client or a wrong redirect all burn it,
and presenting a burnt code is treated as replay — the grant family dies and a
security incident is written.
"""
import base64
import datetime
import hashlib
import secrets

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TEST_USER = "oauth_codes_user"
CLIENT_ID = "testit-oauth-codes-client"
OTHER_CLIENT_ID = "testit-oauth-codes-other"
REDIRECT = "http://127.0.0.1:8100/cb"
RESOURCE = "https://oauth.testit.example/api/testit/oauth-codes-probe"


def _verifier():
    return secrets.token_urlsafe(48)[:64]


def _challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


@th.django_unit_setup()
def setup_codes(opts):
    from mojo.apps.account.models import OAuthClient, User

    User.objects.filter(username=TEST_USER).delete()
    OAuthClient.objects.filter(
        client_id__in=[CLIENT_ID, OTHER_CLIENT_ID]).delete()

    user = User(username=TEST_USER, display_name=TEST_USER,
                email=f"{TEST_USER}@example.com")
    user.save()
    OAuthClient(client_id=CLIENT_ID, kind="dcr", client_name="Codes Client",
                redirect_uris=[REDIRECT]).save()
    OAuthClient(client_id=OTHER_CLIENT_ID, kind="dcr", client_name="Other",
                redirect_uris=[REDIRECT]).save()


def _fixtures():
    from mojo.apps.account.models import OAuthClient, User

    return (User.objects.get(username=TEST_USER),
            OAuthClient.objects.get(client_id=CLIENT_ID),
            OAuthClient.objects.get(client_id=OTHER_CLIENT_ID))


def _mint(user, client, challenge, redirect=REDIRECT):
    from mojo.apps.account.services.oauth_server import codes

    return codes.mint_code(user, client, redirect, challenge, "mcp", RESOURCE,
                           1700000000)


@th.django_unit_test("only a well-formed S256 challenge is accepted")
def test_validate_pkce_challenge(opts):
    from mojo.apps.account.services.oauth_server import codes

    verifier = _verifier()
    challenge = _challenge(verifier)
    assert_eq(codes.validate_pkce_challenge("S256", challenge), challenge,
              "a well-formed S256 challenge must be accepted verbatim")

    bad = [
        ("plain", challenge, "OAuth 2.1 forbids the plain method"),
        (None, challenge, "a missing method must be refused"),
        ("S256", None, "a missing challenge must be refused"),
        ("S256", "short", "a challenge under 43 characters must be refused"),
        ("S256", "!" * 50, "a challenge outside the unreserved set must be refused"),
        ("S256", "a" * 200, "a challenge over 128 characters must be refused"),
    ]
    for method, value, why in bad:
        refused = False
        try:
            codes.validate_pkce_challenge(method, value)
        except ValueError:
            refused = True
        assert_true(refused, f"({method!r}, {value!r}) must be refused: {why}")


@th.django_unit_test("verify_pkce accepts the real verifier and nothing else")
def test_verify_pkce(opts):
    from mojo.apps.account.services.oauth_server import codes

    verifier = _verifier()
    challenge = _challenge(verifier)
    assert_true(codes.verify_pkce(challenge, verifier),
                "the verifier that produced the challenge must verify")
    assert_true(not codes.verify_pkce(challenge, _verifier()),
                "a different verifier must not verify")
    assert_true(not codes.verify_pkce(challenge, "short"),
                "a malformed verifier must not verify")
    assert_true(not codes.verify_pkce("not-a-challenge", verifier),
                "a malformed challenge must not verify")


@th.django_unit_test("a minted code exchanges once and is then spent")
def test_mint_and_consume(opts):
    from mojo.apps.account.models import OAuthCode
    from mojo.apps.account.services.oauth_server import codes, tokens

    user, client, _other = _fixtures()
    verifier = _verifier()
    raw = _mint(user, client, _challenge(verifier))

    row = codes.consume_code(raw, client, REDIRECT, verifier)
    assert_eq(row.user_id, user.pk, "the code must resolve to the approving user")
    assert_eq(row.resource, RESOURCE, f"the code must carry its resource, got {row.resource}")
    assert_eq(row.auth_time, 1700000000,
              f"the approving session's auth_time must be carried, got {row.auth_time}")
    assert_true(OAuthCode.objects.get(pk=row.pk).consumed,
                "a successfully exchanged code must be marked consumed")

    # The loopback port may differ from the one registered at consent.
    verifier2 = _verifier()
    raw2 = _mint(user, client, _challenge(verifier2))
    row2 = codes.consume_code(raw2, client, "http://127.0.0.1:59123/cb", verifier2)
    assert_true(row2 is not None,
                "a loopback redirect on a different port must still exchange")

    refused = False
    try:
        codes.consume_code("not-a-real-code", client, REDIRECT, verifier)
    except tokens.TokenError as err:
        refused = err.code == "invalid_grant"
    assert_true(refused, "an unknown code must be refused as invalid_grant")


@th.django_unit_test("a failed exchange burns the code, whatever the reason")
def test_failed_exchange_burns_the_code(opts):
    from mojo.apps.account.models import OAuthCode
    from mojo.apps.account.services.oauth_server import codes, tokens
    from mojo.helpers import dates

    user, client, other = _fixtures()

    def _attempt(build):
        verifier = _verifier()
        raw = _mint(user, client, _challenge(verifier))
        row = OAuthCode.objects.get(code_hash=codes._sha256_hex(raw))
        code, presented_client, redirect, presented_verifier = build(
            raw, verifier, row)
        error = None
        try:
            codes.consume_code(code, presented_client, redirect, presented_verifier)
        except tokens.TokenError as err:
            error = err.code
        return error, OAuthCode.objects.get(pk=row.pk)

    cases = [
        ("wrong client",
         lambda raw, v, row: (raw, other, REDIRECT, v)),
        ("wrong redirect",
         lambda raw, v, row: (raw, client, "https://evil.example/cb", v)),
        ("wrong verifier",
         lambda raw, v, row: (raw, client, REDIRECT, _verifier())),
    ]
    for why, build in cases:
        error, row = _attempt(build)
        assert_eq(error, "invalid_grant",
                  f"an exchange with the {why} must answer invalid_grant, got {error!r}")
        assert_true(row.consumed,
                    f"an exchange with the {why} must still burn the code")

    # Expired: the row is aged out from under the exchange.
    verifier = _verifier()
    raw = _mint(user, client, _challenge(verifier))
    OAuthCode.objects.filter(code_hash=codes._sha256_hex(raw)).update(
        expires=dates.utcnow() - datetime.timedelta(seconds=5))
    error = None
    try:
        codes.consume_code(raw, client, REDIRECT, verifier)
    except tokens.TokenError as err:
        error = err.code
    assert_eq(error, "invalid_grant",
              f"an expired code must answer invalid_grant, got {error!r}")


@th.django_unit_test("replaying a consumed code revokes the grant it produced")
def test_code_replay_revokes_the_grant(opts):
    from mojo.apps.account.models import OAuthCode, OAuthGrant
    from mojo.apps.account.services.oauth_server import codes, tokens

    user, client, _other = _fixtures()
    verifier = _verifier()
    raw = _mint(user, client, _challenge(verifier))
    row = codes.consume_code(raw, client, REDIRECT, verifier)

    grant = tokens.create_grant(user, client, ["mcp"], RESOURCE, row.auth_time)
    OAuthCode.objects.filter(pk=row.pk).update(grant=grant)

    error = None
    try:
        codes.consume_code(raw, client, REDIRECT, verifier)
    except tokens.TokenError as err:
        error = err.code
    assert_eq(error, "invalid_grant",
              f"a replayed code must answer invalid_grant, got {error!r}")

    fresh = OAuthGrant.objects.get(pk=grant.pk)
    assert_true(not fresh.is_active,
                "replaying a code must revoke the grant that code produced")
    assert_eq(fresh.revoked_reason, "code_replay",
              f"the revocation must be attributed to the replay, "
              f"got {fresh.revoked_reason!r}")


@th.django_unit_test("minting purges long-expired codes")
def test_stale_codes_are_purged(opts):
    from mojo.apps.account.models import OAuthCode
    from mojo.apps.account.services.oauth_server import codes
    from mojo.helpers import dates

    user, client, _other = _fixtures()
    stale = OAuthCode(
        client=client, user=user, code_hash=secrets.token_hex(32),
        redirect_uri=REDIRECT, code_challenge=_challenge(_verifier()),
        scope="mcp", resource=RESOURCE, auth_time=1700000000,
        expires=dates.utcnow() - datetime.timedelta(days=3))
    stale.save()

    _mint(user, client, _challenge(_verifier()))
    assert_true(not OAuthCode.objects.filter(pk=stale.pk).exists(),
                "minting a code must opportunistically purge long-expired rows")
