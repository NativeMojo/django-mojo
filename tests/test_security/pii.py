from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "pii_test_user"
TEST_PWORD = "pii##mojo99"
PASSKEY_USER = "pii_passkey_user"
PASSKEY_ORIGIN = "https://pii.test"
FIXTURE_CREDENTIAL_PREFIX = "pii-test-cred-"
FIXTURE_DEVICE_PREFIX = "pii-test-device-"


@th.django_unit_setup()
def setup_pii(opts):
    from mojo.apps.account.models import User, Group, Passkey
    from mojo.apps.account.models.notification import Notification
    from mojo.apps.account.models.push.device import RegisteredDevice
    from mojo.apps.account.models.totp import UserTOTP
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1")

    # This database is long-lived. Find fixture users through the two tagged
    # credential rows, then remove every credential type before recreating
    # them. This also heals rows left by a fail-before run of pii_anonymize().
    stale_user_ids = set(Passkey.objects.filter(
        credential_id__startswith=FIXTURE_CREDENTIAL_PREFIX,
    ).values_list("user_id", flat=True))
    stale_user_ids.update(RegisteredDevice.objects.filter(
        device_id__startswith=FIXTURE_DEVICE_PREFIX,
    ).values_list("user_id", flat=True))
    Passkey.objects.filter(
        credential_id__startswith=FIXTURE_CREDENTIAL_PREFIX,
    ).delete()
    RegisteredDevice.objects.filter(
        device_id__startswith=FIXTURE_DEVICE_PREFIX,
    ).delete()
    UserTOTP.objects.filter(user_id__in=stale_user_ids).delete()

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=f"{TEST_USER}@example.com")
        user.save()
    # clear phone from any user that has it (unique constraint)
    User.objects.exclude(pk=user.pk).filter(phone_number="+15550001234").update(phone_number=None)
    User.objects.filter(pk=user.pk).update(phone_number=None)
    user.refresh_from_db()
    user.display_name = "Real Name"
    user.first_name = "Real"
    user.last_name = "Name"
    user.phone_number = "+15550001234"
    user.metadata = {"ip": "1.2.3.4", "dob": "1990-01-01"}
    user.permissions = {"view_data": True}
    user.is_active = True
    user.is_email_verified = True
    user.is_staff = False
    user.is_superuser = False
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk

    passkey_user = User.objects.filter(username=PASSKEY_USER).last()
    if passkey_user is None:
        passkey_user = User(
            username=PASSKEY_USER,
            email=f"{PASSKEY_USER}@example.com",
        )
        passkey_user.save()
    passkey_user.is_active = True
    passkey_user.save_password(TEST_PWORD)
    passkey_user.save()
    opts.passkey_user_id = passkey_user.pk
    opts.passkey_credential_id = f"{FIXTURE_CREDENTIAL_PREFIX}{passkey_user.pk}"
    Passkey.objects.create(
        user=passkey_user,
        token="pii-test-login-token",
        credential_id=opts.passkey_credential_id,
        rp_id="pii.test",
        is_enabled=True,
    )

    group, _ = Group.objects.get_or_create(name="pii_test_group", defaults={"kind": "organization"})
    group.add_member(user)
    opts.group_id = group.pk

    Notification.send("PII test notif", user=user, push=False, ws=False)


@th.django_unit_test("pii_anonymize: PII fields are cleared")
def test_pii_fields_cleared(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.user_id)
    summary = user.pii_anonymize()

    user.refresh_from_db()
    assert_true(user.username.startswith("deleted-"), f"username not anonymized: {user.username}")
    assert_true(user.email.endswith("@deleted.local"), f"email not anonymized: {user.email}")
    assert_eq(user.phone_number, None, "phone_number should be None")
    assert_eq(user.display_name, None, "display_name should be None")
    assert_eq(user.first_name, "", "first_name should be empty")
    assert_eq(user.last_name, "", "last_name should be empty")
    # Metadata is not emptied — it is REDUCED to the anonymization audit record.
    # pii_anonymize satisfies GDPR Art. 17 "while preserving the row for FK
    # integrity and audit trail", and record_anonymize "writes a fresh metadata
    # dict containing only the disable namespace (wiping any other PII-bearing
    # metadata keys)". Asserting {} demanded the erasure destroy its own proof.
    assert_true("ip" not in user.metadata and "dob" not in user.metadata,
                f"PII metadata keys must be wiped, got {user.metadata}")
    assert_eq(set(user.metadata), {"protected"},
              f"nothing but the protected/disable audit namespace may survive, "
              f"got {sorted(user.metadata)}")
    disable_record = user.metadata["protected"]["disable"]
    assert_eq(disable_record.get("reason"), "anonymized",
              f"the surviving record must say why the row was disabled, "
              f"got {disable_record}")
    assert_eq(user.permissions, {}, "permissions should be wiped")
    assert_true(not user.is_active, "user should be deactivated")
    assert_true(not user.is_staff, "is_staff should be False")
    assert_true(not user.is_superuser, "is_superuser should be False")
    assert_true("user_id" in summary, "summary should include user_id")


@th.django_unit_test("pii_anonymize: auth_key rotated (sessions revoked)")
def test_pii_auth_key_rotated(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.user_id)
    old_key = user.auth_key
    user.pii_anonymize()
    user.refresh_from_db()
    assert_true(user.auth_key != old_key, "auth_key should be rotated to revoke sessions")


@th.django_unit_test("pii_anonymize: passkeys deleted")
def test_pii_passkeys_deleted(opts):
    from mojo.apps.account.models import User, Passkey

    user = User.objects.get(pk=opts.user_id)
    Passkey.objects.create(
        user=user,
        token="pii-test-token",
        credential_id=f"{FIXTURE_CREDENTIAL_PREFIX}{user.pk}",
        rp_id="pii.test",
        is_enabled=True,
    )
    summary = user.pii_anonymize()

    assert_eq(Passkey.objects.filter(user_id=opts.user_id).count(), 0,
              "all passkeys should be deleted after anonymization")
    assert_eq(summary["deleted_passkeys"], 1,
              f"summary should report one deleted passkey, got {summary}")


@th.django_unit_test("pii_anonymize: push devices deleted")
def test_pii_push_devices_deleted(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.push.device import RegisteredDevice

    user = User.objects.get(pk=opts.user_id)
    RegisteredDevice.objects.create(
        user=user,
        device_token=f"pii-test-push-token-{user.pk}",
        device_id=f"{FIXTURE_DEVICE_PREFIX}{user.pk}",
        platform="ios",
    )
    summary = user.pii_anonymize()

    assert_eq(RegisteredDevice.objects.filter(user_id=opts.user_id).count(), 0,
              "all registered push devices should be deleted after anonymization")
    assert_eq(summary["deleted_devices"], 1,
              f"summary should report one deleted push device, got {summary}")


@th.django_unit_test("pii_anonymize: TOTP secrets deleted")
def test_pii_totp_deleted(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.totp import UserTOTP

    user = User.objects.get(pk=opts.user_id)
    UserTOTP.objects.create(user=user)
    summary = user.pii_anonymize()

    assert_eq(UserTOTP.objects.filter(user_id=opts.user_id).count(), 0,
              "the TOTP secret should be deleted after anonymization")
    assert_eq(summary["deleted_totp"], 1,
              f"summary should report one deleted TOTP secret, got {summary}")


@th.django_unit_test("pii_anonymize: absent OAuth and API credentials report zero counts")
def test_pii_linked_credentials_empty_summary(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.user_id)
    summary = user.pii_anonymize()
    keys = (
        "deleted_oauth_connections", "deleted_user_api_keys",
        "deleted_oauth_grants", "deleted_oauth_codes",
        "deactivated_api_keys", "detached_api_keys",
    )
    assert_eq({key: summary.get(key) for key in keys}, {key: 0 for key in keys},
              f"empty linked-credential cleanup should report six zero counts, got {summary}")


@th.django_unit_test("passkey login: inactive user matches unknown credential")
def test_inactive_passkey_login_is_generic(opts):
    from mojo.apps.account.models import User

    opts.client.logout()

    def complete(credential_id):
        return opts.client.post(
            "/api/auth/passkeys/login/complete",
            {
                "challenge_id": "pii-test-bogus-challenge",
                "credential": {"id": credential_id},
            },
            headers={"Origin": PASSKEY_ORIGIN},
        )

    active = complete(opts.passkey_credential_id)
    passkey_user = User.objects.get(pk=opts.passkey_user_id)
    passkey_user.is_active = False
    passkey_user.save(update_fields=["is_active", "modified"])
    inactive = complete(opts.passkey_credential_id)
    unknown = complete("pii-test-unknown-credential")

    active_error = active.response.error
    inactive_error = inactive.response.error
    unknown_error = unknown.response.error
    assert_eq(active.status_code, 403,
              f"active positive control should reach the passkey service, got {active.response}")
    assert_eq(inactive.status_code, unknown.status_code,
              "inactive and unknown passkeys should return the same status")
    assert_eq(inactive_error, unknown_error,
              "inactive and unknown passkeys should return the same generic error")
    assert_true(active_error != inactive_error,
                "active credential should get past lookup and fail on its bogus challenge")
    for label, response in (
        ("active", active), ("inactive", inactive), ("unknown", unknown),
    ):
        assert_true("token" not in response.response,
                    f"{label} failed passkey response must not contain a token")


@th.django_unit_test("pii_anonymize: notifications deleted")
def test_pii_notifications_deleted(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.notification import Notification

    user = User.objects.get(pk=opts.user_id)
    user.pii_anonymize()
    count = Notification.objects.filter(user=user).count()
    assert_eq(count, 0, "all notifications should be deleted after anonymization")


@th.django_unit_test("pii_anonymize: group memberships removed")
def test_pii_memberships_removed(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.member import GroupMember

    user = User.objects.get(pk=opts.user_id)
    user.pii_anonymize()
    count = GroupMember.objects.filter(user=user).count()
    assert_eq(count, 0, "group memberships should be removed after anonymization")


@th.django_unit_test("pii_anonymize: cannot login after anonymization")
def test_pii_cannot_login(opts):
    opts.client.logout()
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert_true(not opts.client.is_authenticated, "anonymized user should not be able to login")


@th.django_unit_test("pii_anonymize: row still exists (FK integrity preserved)")
def test_pii_row_preserved(opts):
    from mojo.apps.account.models import User

    assert_true(User.objects.filter(pk=opts.user_id).exists(), "user row should still exist after anonymization")


@th.django_unit_setup()
def cleanup_pii(opts):
    from mojo.apps.account.models import User, Group

    User.objects.filter(pk=opts.user_id).delete()
    Group.objects.filter(pk=opts.group_id).delete()
