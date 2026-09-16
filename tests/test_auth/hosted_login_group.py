"""Hosted login group context reaches the existing extension (#4622).

These exercise real HTTP, SMS verification and signed WebAuthn assertions.
Only the existing loopback test header selects a capture handler; authentication
and group resolution are not mocked. All identities and groups are synthetic.
"""
import hashlib
import json
import os

from testit import helpers as th

TESTIT_TIER = "bug"
PASSWORD = "Hosted4622##secret"
ORIGIN = "https://hosted4622.test"
RP_ID = "hosted4622.test"
HANDLER = "tests.test_auth.hosted_login_group.capture_login"


def capture_login(*, user, request, source, is_new_user):
    from tests.test_register import _capture
    group = getattr(request, "group", None)
    _capture._append(request, "login", {
        "user_id": user.pk,
        "group_id": group.pk if group is not None else None,
        "source": source,
        "is_new_user": is_new_user,
    })


@th.django_unit_setup()
def setup_hosted_login_group(opts):
    from cryptography.hazmat.primitives.asymmetric import ec
    from fido2.cose import ES256
    from fido2.utils import websafe_encode
    from fido2.webauthn import Aaguid, AttestedCredentialData
    from mojo.apps.account.models import Group, Passkey, User

    # The test DB survives runs; remove only this module's fixtures first.
    User.objects.filter(username__startswith="hosted4622_").delete()
    Group.objects.filter(name__startswith="hosted4622_").delete()
    opts.hosted_user = User.objects.create_user(
        username="hosted4622_member", email="hosted4622_member@example.com",
        password=PASSWORD)
    opts.hosted_user.is_active = True
    opts.hosted_user.is_email_verified = True
    opts.hosted_user.phone_number = "+15554622001"
    opts.hosted_user.is_phone_verified = True
    opts.hosted_user.requires_mfa = False
    opts.hosted_user.save()
    opts.hosted_mfa = User.objects.create_user(
        username="hosted4622_mfa", email="hosted4622_mfa@example.com",
        password=PASSWORD)
    opts.hosted_mfa.is_active = True
    opts.hosted_mfa.is_email_verified = True
    opts.hosted_mfa.phone_number = "+15554622002"
    opts.hosted_mfa.is_phone_verified = True
    opts.hosted_mfa.requires_mfa = True
    opts.hosted_mfa.save()
    opts.hosted_group = Group.objects.create(name="hosted4622_member")
    opts.hosted_group.add_member(opts.hosted_user)
    opts.hosted_group.add_member(opts.hosted_mfa)
    opts.hosted_foreign = Group.objects.create(name="hosted4622_foreign")
    opts.hosted_inactive = Group.objects.create(name="hosted4622_inactive", is_active=False)
    opts.hosted_key = ec.generate_private_key(ec.SECP256R1())
    credential_id = os.urandom(32)
    attested = AttestedCredentialData.create(
        Aaguid(os.urandom(16)), credential_id,
        ES256.from_cryptography_key(opts.hosted_key.public_key()))
    opts.hosted_passkey = Passkey.objects.create(
        user=opts.hosted_user, token=websafe_encode(bytes(attested)),
        credential_id=websafe_encode(credential_id), rp_id=RP_ID,
        sign_count=0, transports=["internal"], friendly_name="Synthetic #4622")


def _post(opts, path, payload, rate_key=None):
    from mojo.decorators.limits import clear_rate_limits
    from tests.test_register import _capture
    opts.client.logout()
    if rate_key:
        clear_rate_limits(ip="127.0.0.1", key=rate_key)
        for user in (opts.hosted_user, opts.hosted_mfa):
            clear_rate_limits(key=rate_key, account_id=user.pk)
            clear_rate_limits(user_id=user.pk)
        muid = opts.client.session.cookies.get("_muid")
        if muid:
            clear_rate_limits(key=rate_key, muid=muid)
    capture_id = _capture.new_capture_id()
    try:
        resp = opts.client.post(path, payload, headers={
            "Origin": ORIGIN,
            "X-Mojo-Test-User-Login-Handler": HANDLER,
            "X-Mojo-Test-Capture-Id": capture_id,
        })
        calls = _capture.read_capture(capture_id).get("login", [])
        return resp, calls
    finally:
        _capture.clear_capture(capture_id)


def _assert_login(resp, calls, user, group, source):
    th.assert_eq(resp.status_code, 200, "synthetic credentials must authenticate")
    th.assert_true(bool(resp.response.data.get("access_token")), "completed login must issue a token")
    th.assert_eq(calls, [{
        "user_id": user.pk, "group_id": group.pk if group else None,
        "source": source, "is_new_user": False,
    }], "the login callback must receive request.group exactly once")


def _sms(opts, group, code="462201"):
    from mojo.helpers import dates
    opts.hosted_user.refresh_from_db()
    opts.hosted_user.set_secret("sms_otp_code", "462201")
    opts.hosted_user.set_secret("sms_otp_ts", int(dates.utcnow().timestamp()))
    opts.hosted_user.save()
    return _post(opts, "/api/auth/sms/verify", {
        "username": opts.hosted_user.username, "code": code,
        "group_uuid": group.get_uuid(),
    }, "sms_verify")


def _passkey(opts, discoverable=False, invalid=False):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from fido2.utils import websafe_encode
    from fido2.webauthn import AuthenticatorData
    payload = {"group_uuid": opts.hosted_group.get_uuid()}
    if not discoverable:
        payload["username"] = opts.hosted_user.username
    begin, calls = _post(opts, "/api/auth/passkeys/login/begin", payload)
    th.assert_eq(begin.status_code, 200, "passkey begin must produce a real challenge")
    th.assert_eq(calls, [], "an unfinished passkey challenge must not fire login")
    opts.hosted_passkey.refresh_from_db()
    client_data = json.dumps({
        "type": "webauthn.get", "origin": ORIGIN,
        "challenge": "invalid-challenge" if invalid else begin.response.data.publicKey.challenge,
        "crossOrigin": False,
    }).encode()
    authenticator_data = AuthenticatorData.create(
        hashlib.sha256(RP_ID.encode()).digest(),
        AuthenticatorData.FLAG.UP | AuthenticatorData.FLAG.UV,
        opts.hosted_passkey.sign_count + 1)
    signature = opts.hosted_key.sign(
        bytes(authenticator_data) + hashlib.sha256(client_data).digest(),
        ec.ECDSA(hashes.SHA256()))
    return _post(opts, "/api/auth/passkeys/login/complete", {
        "group_uuid": opts.hosted_group.get_uuid(),
        "challenge_id": begin.response.data.challenge_id,
        "credential": {
            "id": opts.hosted_passkey.credential_id,
            "rawId": opts.hosted_passkey.credential_id, "type": "public-key",
            "response": {
                "clientDataJSON": websafe_encode(client_data),
                "authenticatorData": websafe_encode(bytes(authenticator_data)),
                "signature": websafe_encode(signature),
            },
        },
    }, "passkey_login")


@th.django_unit_test("hosted: named passkey carries group to login callback")
def test_named_passkey_group(opts):
    resp, calls = _passkey(opts)
    _assert_login(resp, calls, opts.hosted_user, opts.hosted_group, "passkey")


@th.django_unit_test("hosted: discoverable passkey carries group to login callback")
def test_discoverable_passkey_group(opts):
    resp, calls = _passkey(opts, discoverable=True)
    _assert_login(resp, calls, opts.hosted_user, opts.hosted_group, "passkey")


@th.django_unit_test("hosted: rejected passkey assertion never fires login callback")
def test_failed_passkey_no_callback(opts):
    resp, calls = _passkey(opts, invalid=True)
    th.assert_eq(resp.status_code, 403, "a signed assertion for the wrong challenge must be refused")
    th.assert_eq(calls, [], "a refused assertion must not fire login")


@th.django_unit_test("hosted: SMS completion carries group to login callback")
def test_sms_group(opts):
    resp, calls = _sms(opts, opts.hosted_group)
    _assert_login(resp, calls, opts.hosted_user, opts.hosted_group, "sms")


@th.django_unit_test("hosted: invalid SMS code never fires login callback")
def test_failed_sms_no_callback(opts):
    resp, calls = _sms(opts, opts.hosted_group, code="000000")
    th.assert_true(resp.status_code in (401, 403), "bad SMS code must fail authentication")
    th.assert_eq(calls, [], "a refused SMS code must not fire login")


@th.django_unit_test("hosted: inactive group is not resolved on login")
def test_inactive_group_unresolved(opts):
    resp, calls = _sms(opts, opts.hosted_inactive)
    _assert_login(resp, calls, opts.hosted_user, None, "sms")


@th.django_unit_test("hosted: foreign group is context but not sign-in attribution")
def test_nonmember_group_not_attributed(opts):
    from mojo.apps.incident.models.event import Event
    before = Event.objects.filter(uid=opts.hosted_user.pk, category="login").count()
    resp, calls = _sms(opts, opts.hosted_foreign)
    _assert_login(resp, calls, opts.hosted_user, opts.hosted_foreign, "sms")
    rows = Event.objects.filter(uid=opts.hosted_user.pk, category="login")
    th.assert_eq(rows.count(), before + 1, "foreign group must not prevent successful sign-in recording")
    row = rows.order_by("-id").first()
    th.assert_true(row.group_id is None, "nonmember group must never receive sign-in attribution")
    th.assert_true("origin_group_id" not in row.metadata, "nonmember group must not appear in provenance")


@th.django_unit_test("hosted: pending MFA never fires login callback")
def test_pending_mfa_no_callback(opts):
    resp, calls = _post(opts, "/api/login", {
        "username": opts.hosted_mfa.username, "password": PASSWORD,
        "group_uuid": opts.hosted_group.get_uuid(),
    }, "login")
    th.assert_eq(resp.status_code, 200, "password step must issue an MFA challenge")
    th.assert_true(resp.response.data.mfa_required is True, "test must reach pending MFA")
    th.assert_true(not resp.response.data.get("access_token"), "unfinished MFA must not issue access token")
    th.assert_eq(calls, [], "unfinished MFA must not fire login")


@th.django_unit_test("hosted: JWT handoff exchange retains consumer group context")
def test_handoff_group(opts):
    from mojo.apps.account.services import auth_handoff
    code = auth_handoff.create_handoff_code(opts.hosted_user)
    resp, calls = _post(opts, "/api/auth/exchange", {
        "code": code, "group_uuid": opts.hosted_group.get_uuid(),
    }, "auth_exchange")
    _assert_login(resp, calls, opts.hosted_user, opts.hosted_group, "handoff")
