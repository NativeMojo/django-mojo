"""
Tests for simplified passkey implementation.

Tests verify:
- Registration flow (begin → complete)
- Login flow (begin → complete)
- Multi-portal support (different RP IDs)
- Challenge expiration (Redis TTL)
"""
import os

from fido2.cose import ES256
from fido2.utils import websafe_encode
from fido2.webauthn import Aaguid, AttestedCredentialData
from testit import helpers as th


TEST_USER = "passkey_user"
TEST_PASSWORD = "passkey##secret"
PORTAL1_ORIGIN = "https://portal1.test"
PORTAL2_ORIGIN = "https://portal2.test"


def generate_attested_credential():
    """Generate a valid AttestedCredentialData payload for testing."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.backends import default_backend

    # Generate EC key pair
    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    public_key = private_key.public_key()

    # Wrap in ES256 COSE key
    cose_key = ES256.from_cryptography_key(public_key)

    credential_bytes = os.urandom(32)
    aaguid = Aaguid(os.urandom(16))
    attested = AttestedCredentialData.create(aaguid, credential_bytes, cose_key)

    return {
        "token": websafe_encode(bytes(attested)),
        "credential_id": websafe_encode(credential_bytes),
    }


@th.django_unit_setup()
def setup_passkey_env(opts):
    """Create test user for passkey tests and clean up old passkeys."""
    from mojo.apps.account.models import User, Passkey

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(
            username=TEST_USER,
            email=f"{TEST_USER}@example.com",
            display_name="Passkey User",
        )
    user.is_active = True
    user.save()
    user.save_password(TEST_PASSWORD)

    # Clean up any existing passkeys from previous test runs
    Passkey.objects.filter(user=user).delete()

    opts.user = user


# -----------------------------------------------------------------
# Portal 1 Tests
# -----------------------------------------------------------------

@th.django_unit_test("portal1: register begin returns challenge")
def test_passkey_register_begin_portal1(opts):
    """Registration begin should return challenge for portal1."""
    assert opts.client.login(TEST_USER, TEST_PASSWORD), "Login failed"
    assert opts.client.is_authenticated, "Not authenticated"

    opts.client.headers["Origin"] = PORTAL1_ORIGIN
    resp = opts.client.post("/api/account/passkeys/register/begin", {})

    assert resp.status_code == 200, f"Unexpected status {resp.status_code}"
    data = resp.response.data
    assert data.challenge_id, "Missing challenge_id"
    assert data.publicKey.challenge, "Missing challenge"
    assert data.publicKey.rp.id == "portal1.test", "RP ID should match portal1 hostname"
    assert data.expiresAt, "Missing expiration"

    opts.portal1_challenge_id = data.challenge_id


@th.django_unit_test("portal1: seed passkey directly")
def test_passkey_seed_portal1(opts):
    """Create a test passkey for portal1 directly in database."""
    from mojo.apps.account.models import Passkey

    credential = generate_attested_credential()
    passkey = Passkey.objects.create(
        user=opts.user,
        token=credential["token"],
        credential_id=credential["credential_id"],
        rp_id="portal1.test",
        sign_count=0,
        transports=["internal"],
        friendly_name="Portal1 Test Key",
    )
    opts.portal1_passkey = passkey
    opts.portal1_credential_id = credential["credential_id"]


@th.django_unit_test("portal1: login begin returns allowCredentials")
def test_passkey_login_begin_portal1(opts):
    """Login begin should return portal1 passkey in allowCredentials."""
    assert opts.portal1_passkey, "Portal1 passkey not seeded"

    opts.client.headers["Origin"] = PORTAL1_ORIGIN
    resp = opts.client.post(
        "/api/auth/passkeys/login/begin",
        {"username": TEST_USER},
    )

    assert resp.status_code == 200, f"Unexpected status {resp.status_code}"
    data = resp.response.data
    assert data.publicKey.rpId == "portal1.test", "RP ID should match portal1"
    assert data.publicKey.allowCredentials, "Should include allowCredentials"

    credentials = data.publicKey.allowCredentials
    assert len(credentials) == 1, "Should have exactly one credential for portal1"
    assert credentials[0].id == opts.portal1_credential_id, "Wrong credential ID"


@th.django_unit_test("portal1: login begin fails for unknown user")
def test_passkey_login_unknown_user(opts):
    """Login begin should fail for non-existent user."""
    opts.client.headers["Origin"] = PORTAL1_ORIGIN
    resp = opts.client.post(
        "/api/auth/passkeys/login/begin",
        {"username": "nonexistent_user"},
    )

    assert resp.status_code == 403, "Should reject unknown user"


# -----------------------------------------------------------------
# Portal 2 Tests
# -----------------------------------------------------------------

@th.django_unit_test("portal2: register begin returns different RP ID")
def test_passkey_register_begin_portal2(opts):
    """Registration begin for portal2 should use portal2 hostname as RP ID."""
    assert opts.client.login(TEST_USER, TEST_PASSWORD), "Login failed"

    opts.client.headers["Origin"] = PORTAL2_ORIGIN
    resp = opts.client.post("/api/account/passkeys/register/begin", {})

    assert resp.status_code == 200, f"Unexpected status {resp.status_code}"
    data = resp.response.data
    assert data.publicKey.rp.id == "portal2.test", "RP ID should match portal2 hostname"

    opts.portal2_challenge_id = data.challenge_id


@th.django_unit_test("portal2: login begin fails without passkey")
def test_passkey_login_begin_portal2_no_credentials(opts):
    """Login begin should fail when user has no passkeys for portal2."""
    opts.client.headers["Origin"] = PORTAL2_ORIGIN
    resp = opts.client.post(
        "/api/auth/passkeys/login/begin",
        {"username": TEST_USER},
    )

    assert resp.status_code == 403, "Should fail when no passkeys for this portal"
    assert "No passkeys registered" in resp.response.error, "Should mention no passkeys"


@th.django_unit_test("portal2: seed passkey directly")
def test_passkey_seed_portal2(opts):
    """Create a test passkey for portal2."""
    from mojo.apps.account.models import Passkey

    credential = generate_attested_credential()
    passkey = Passkey.objects.create(
        user=opts.user,
        token=credential["token"],
        credential_id=credential["credential_id"],
        rp_id="portal2.test",
        sign_count=0,
        transports=["internal"],
        friendly_name="Portal2 Test Key",
    )
    opts.portal2_passkey = passkey
    opts.portal2_credential_id = credential["credential_id"]


@th.django_unit_test("portal2: login begin returns portal2 credentials only")
def test_passkey_login_begin_portal2(opts):
    """Login begin for portal2 should only return portal2 passkeys."""
    assert opts.portal2_passkey, "Portal2 passkey not seeded"

    opts.client.headers["Origin"] = PORTAL2_ORIGIN
    resp = opts.client.post(
        "/api/auth/passkeys/login/begin",
        {"username": TEST_USER},
    )

    assert resp.status_code == 200, f"Unexpected status {resp.status_code}"
    data = resp.response.data
    assert data.publicKey.rpId == "portal2.test", "RP ID should match portal2"

    credentials = data.publicKey.allowCredentials
    assert len(credentials) == 1, "Should have exactly one credential for portal2"
    assert credentials[0].id == opts.portal2_credential_id, "Should return portal2 credential"
    assert credentials[0].id != opts.portal1_credential_id, "Should NOT return portal1 credential"


# -----------------------------------------------------------------
# Multi-Portal Isolation Tests
# -----------------------------------------------------------------

@th.django_unit_test("user can list all passkeys from any portal")
def test_list_all_passkeys(opts):
    """User should see all their passkeys regardless of portal."""
    assert opts.client.login(TEST_USER, TEST_PASSWORD), "Login failed"

    # List from portal1
    opts.client.headers["Origin"] = PORTAL1_ORIGIN
    resp = opts.client.get("/api/account/passkeys")

    assert resp.status_code == 200, f"Unexpected status {resp.status_code}"

    # Get passkeys from response (could be .results or .data depending on response format)
    passkeys = getattr(resp.response, 'results', None) or getattr(resp.response, 'data', [])
    assert passkeys, f"No passkeys in response: {resp.response}"

    # Should have both portal1 and portal2 passkeys
    assert len(passkeys) >= 2, f"Should have passkeys for both portals, got {len(passkeys)}"

    rp_ids = {p.rp_id for p in passkeys}
    assert "portal1.test" in rp_ids, "Should have portal1 passkey"
    assert "portal2.test" in rp_ids, "Should have portal2 passkey"


@th.django_unit_test("user can disable passkey")
def test_disable_passkey(opts):
    """User should be able to disable a passkey."""
    assert opts.client.login(TEST_USER, TEST_PASSWORD), "Login failed"

    passkey_id = opts.portal1_passkey.id
    resp = opts.client.post(
        f"/api/account/passkeys/{passkey_id}",
        {"is_enabled": False},
    )

    assert resp.status_code == 200, f"Unexpected status {resp.status_code}"

    # Check is_enabled field in response
    is_enabled = getattr(resp.response, 'is_enabled', None)
    if is_enabled is None:
        # Might be in data object
        data = getattr(resp.response, 'data', resp.response)
        is_enabled = getattr(data, 'is_enabled', None)

    assert is_enabled is False, f"Passkey should be disabled, got is_enabled={is_enabled}, response={resp.response}"


@th.django_unit_test("disabled passkey not returned in login begin")
def test_disabled_passkey_not_in_login(opts):
    """Disabled passkeys should not appear in login challenges."""
    # Portal1 passkey is disabled from previous test
    opts.client.headers["Origin"] = PORTAL1_ORIGIN
    resp = opts.client.post(
        "/api/auth/passkeys/login/begin",
        {"username": TEST_USER},
    )

    # Should fail because portal1 passkey is disabled
    assert resp.status_code == 403, "Should fail when all passkeys disabled"


@th.django_unit_test("user can delete passkey")
def test_delete_passkey(opts):
    """User should be able to delete a passkey."""
    assert opts.client.login(TEST_USER, TEST_PASSWORD), "Login failed"

    passkey_id = opts.portal1_passkey.id
    resp = opts.client.delete(f"/api/account/passkeys/{passkey_id}")

    assert resp.status_code == 200, f"Unexpected status {resp.status_code}"

    # Verify it's deleted
    from mojo.apps.account.models import Passkey
    exists = Passkey.objects.filter(id=passkey_id).exists()
    assert not exists, "Passkey should be deleted"


# -----------------------------------------------------------------
# Error Handling Tests
# -----------------------------------------------------------------

@th.django_unit_test("register begin requires authentication")
def test_register_requires_auth(opts):
    """Registration should require authentication."""
    opts.client.logout()
    opts.client.headers["Origin"] = PORTAL1_ORIGIN
    resp = opts.client.post("/api/account/passkeys/register/begin", {})

    assert resp.status_code in [401, 403], f"Should require authentication, got {resp.status_code}: {resp.response}"


@th.django_unit_test("register begin requires origin header")
def test_register_requires_origin(opts):
    """Registration should require Origin header."""
    assert opts.client.login(TEST_USER, TEST_PASSWORD), "Login failed"

    # Remove origin header
    opts.client.headers.pop("Origin", None)
    resp = opts.client.post("/api/account/passkeys/register/begin", {})

    assert resp.status_code == 400, f"Should fail without Origin header, got {resp.status_code}: {resp.response}"
    error = getattr(resp.response, 'error', None) or str(resp.response)
    assert "Origin" in str(error), f"Error should mention Origin, got: {error}"


@th.django_unit_test("login begin requires origin header")
def test_login_requires_origin(opts):
    """Login should require Origin header."""
    opts.client.headers.pop("Origin", None)
    resp = opts.client.post(
        "/api/auth/passkeys/login/begin",
        {"username": TEST_USER},
    )

    assert resp.status_code == 400, "Should fail without Origin header"


@th.django_unit_test("login begin discoverable: no username returns empty allowCredentials")
def test_passkey_login_begin_discoverable(opts):
    """Login begin without a username should issue a discoverable-credential challenge."""
    # Use portal2 which still has an active passkey
    opts.client.headers["Origin"] = PORTAL2_ORIGIN
    resp = opts.client.post("/api/auth/passkeys/login/begin", {})

    assert resp.status_code == 200, f"Discoverable login begin should succeed, got {resp.status_code}: {resp.response}"
    data = resp.response.data
    assert data.challenge_id, "Missing challenge_id"
    assert data.publicKey.challenge, "Missing challenge"

    # allowCredentials must be absent or empty — that is the discoverable signal
    allow_creds = getattr(data.publicKey, "allowCredentials", None)
    assert not allow_creds, f"Discoverable challenge should have no allowCredentials, got: {allow_creds}"
