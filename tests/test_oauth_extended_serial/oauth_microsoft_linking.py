"""
Microsoft account linking (maestro #9105, plan #8967 B3 and "Settled first").

Calls ``_find_or_create_user`` and ``_lookup_known_user`` directly with
Microsoft-shaped profiles. Email links accounts only when Microsoft verified
it (``email_verified`` True, from ``xms_edov``); an unverified email never
links and is never claimed by a new account.
"""
from testit import helpers as th

PROVIDER = "microsoft"
TID = "72f988bf-86f1-41af-91ab-2d7cd011db47"


def _profile(oid, email, verified, **extra):
    profile = {
        "uid": f"{TID}:{oid}",
        "email": email,
        "email_verified": verified,
        "display_name": extra.pop("display_name", "Ms Linking User"),
        "given_name": extra.pop("given_name", None),
        "family_name": extra.pop("family_name", None),
    }
    profile.update(extra)
    return profile


def _clean(emails=(), oids=(), usernames=()):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.oauth import OAuthConnection
    for oid in oids:
        conn = OAuthConnection.objects.filter(
            provider=PROVIDER, provider_uid=f"{TID}:{oid}").select_related("user").first()
        if conn:
            conn.user.delete()
    if emails:
        User.objects.filter(email__in=list(emails)).delete()
        OAuthConnection.objects.filter(provider=PROVIDER, email__in=list(emails)).delete()
    if usernames:
        User.objects.filter(username__in=list(usernames)).delete()


def _existing_user(username, email, verified=False):
    from mojo.apps.account.models import User
    user = User(username=username, email=email)
    user.is_email_verified = verified
    user.save()
    return user


@th.django_unit_test("microsoft linking: a verified email links to the existing account and verifies it")
def test_ms_link_verified(opts):
    from mojo.apps.account.rest.oauth import _find_or_create_user
    email, oid = "ms_link_verified@example.com", "oid-link-verified"
    _clean([email], [oid], ["ms_link_verified"])
    user = _existing_user("ms_link_verified", email, verified=False)
    try:
        linked, conn, created = _find_or_create_user(PROVIDER, _profile(oid, email, True))
        assert linked.pk == user.pk, "a verified email must link to the existing account"
        assert created is False, "linking must not report a new account"
        assert conn.provider_uid == f"{TID}:{oid}", f"connection uid must be tid:oid, got {conn.provider_uid}"
        user.refresh_from_db()
        assert user.is_email_verified is True, "linking on a verified email must mark it verified"
    finally:
        _clean([email], [oid], ["ms_link_verified"])


@th.django_unit_test("microsoft linking: an unverified or absent-verification email that matches an account is refused, creating nothing")
def test_ms_link_unverified_refused(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import User
    from mojo.apps.account.models.oauth import OAuthConnection
    from mojo.apps.account.rest.oauth import _find_or_create_user
    email = "ms_link_refused@example.com"
    _clean([email], ["oid-refused-false", "oid-refused-absent"], ["ms_link_refused"])
    user = _existing_user("ms_link_refused", email, verified=False)
    try:
        users_before = User.objects.count()
        # get_profile maps an absent xms_edov to False (test_ms_profile_shape);
        # here the two oids check a second refusal creates nothing either.
        for oid in ("oid-refused-false", "oid-refused-absent"):
            profile = _profile(oid, email, False)
            raised = None
            try:
                _find_or_create_user(PROVIDER, profile)
            except merrors.PermissionDeniedException as exc:
                raised = exc
            assert raised is not None, f"{oid}: an unverified email matching an account must be refused"
            assert "already uses this email" in str(raised), f"{oid}: refusal wording, got {raised}"
            assert not OAuthConnection.objects.filter(
                provider=PROVIDER, provider_uid=f"{TID}:{oid}").exists(), \
                f"{oid}: no connection may be created on refusal"
        assert User.objects.count() == users_before, "no user may be created on refusal"
        user.refresh_from_db()
        assert user.is_email_verified is False, "a refused sign-in must not verify the existing email"
    finally:
        _clean([email], ["oid-refused-false", "oid-refused-absent"], ["ms_link_refused"])


@th.django_unit_test("microsoft linking: an unverified new email creates an account that does not claim the address")
def test_ms_create_unverified_withholds_email(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.rest.oauth import _find_or_create_user
    email, oid = "ms_unverified_new@example.com", "oid-unverified-new"
    _clean([email], [oid])
    try:
        user, conn, created = _find_or_create_user(
            PROVIDER, _profile(oid, email, False, given_name="Grace", family_name="Hopper"))
        assert created is True, "an unknown identity must create an account"
        assert user.email is None, f"the unverified address must not be claimed, got {user.email}"
        assert user.is_email_verified is False, "the new account must not be email-verified"
        assert conn.email == email, f"the connection keeps the reported address, got {conn.email}"
        assert user.username, "the new account must have a username"
        assert not User.objects.filter(email=email).exists(), "no account may hold the unverified address"
    finally:
        _clean([email], [oid])


@th.django_unit_test("microsoft linking: a verified new email creates a verified account with that email")
def test_ms_create_verified(opts):
    from mojo.apps.account.rest.oauth import _find_or_create_user
    email, oid = "ms_verified_new@example.com", "oid-verified-new"
    _clean([email], [oid])
    try:
        user, conn, created = _find_or_create_user(PROVIDER, _profile(oid, email, True))
        assert created is True, "an unknown identity must create an account"
        assert user.email == email, f"a verified email is claimed, got {user.email}"
        assert user.is_email_verified is True, "a verified email creates a verified account"
    finally:
        _clean([email], [oid])


@th.django_unit_test("microsoft linking: no email at all creates an email-less account")
def test_ms_create_no_email(opts):
    from mojo.apps.account.rest.oauth import _find_or_create_user
    oid = "0f0e0d0c-0b0a-0908-0706-050403020100"
    _clean([], [oid])
    try:
        user, conn, created = _find_or_create_user(
            PROVIDER, _profile(oid, None, False, display_name=None))
        assert created is True, "a phone-only Microsoft account must still sign up"
        assert user.email is None, f"no email expected, got {user.email}"
        assert conn.email is None, f"no connection email expected, got {conn.email}"
        assert user.username.startswith("ms-"), \
            f"with no names the username falls back to ms-<oid>, got {user.username}"
        again, _, created_again = _find_or_create_user(PROVIDER, _profile(oid, None, False))
        assert again.pk == user.pk and created_again is False, "the second sign-in lands in the same account"
    finally:
        _clean([], [oid])


@th.django_unit_test("microsoft linking: an existing tid:oid signs in whatever the email says")
def test_ms_existing_connection_wins(opts):
    from mojo.apps.account.rest.oauth import _find_or_create_user
    email, other, oid = "ms_conn_owner@example.com", "ms_conn_other@example.com", "oid-conn-wins"
    _clean([email, other], [oid], ["ms_conn_other"])
    other_user = _existing_user("ms_conn_other", other, verified=True)
    try:
        user, _, _ = _find_or_create_user(PROVIDER, _profile(oid, email, True))
        again, _, created = _find_or_create_user(PROVIDER, _profile(oid, other, False))
        assert again.pk == user.pk, "the tid:oid connection decides the account, not the email"
        assert again.pk != other_user.pk, "an email matching another account must not move the sign-in"
        assert created is False, "no new account on a known tid:oid"
    finally:
        _clean([email, other], [oid], ["ms_conn_other"])


@th.django_unit_test("microsoft linking: the group's registration.methods gate covers microsoft")
def test_ms_registration_gate(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Group
    from mojo.apps.account.models.oauth import OAuthConnection
    from mojo.apps.account.rest.oauth import _find_or_create_user
    reg_uuid = "msr1234567890abcdef01234567890ab"
    oid = "oid-reg-gate"
    _clean([], [oid])
    Group.objects.filter(uuid=reg_uuid).delete()
    group = Group.objects.create(
        name="test-ms-reg-gated", uuid=reg_uuid, is_active=True,
        metadata={"auth_config": {"registration": {"methods": ["password"]}}})
    try:
        raised = False
        try:
            _find_or_create_user(PROVIDER, _profile(oid, None, False),
                                 state_data={"group_uuid": reg_uuid})
        except merrors.PermissionDeniedException:
            raised = True
        assert raised, "signup must be refused when the group's registration.methods omits microsoft"
        assert not OAuthConnection.objects.filter(provider=PROVIDER, provider_uid=f"{TID}:{oid}").exists(), \
            "no connection may be created when the gate blocks the signup"
    finally:
        _clean([], [oid])
        group.delete()


@th.django_unit_test("microsoft linking: the geofence pre-check ignores an unverified email")
def test_ms_lookup_known_user_ignores_unverified(opts):
    from mojo.apps.account.rest.oauth import _lookup_known_user
    email = "ms_geofence_lookup@example.com"
    _clean([email], [], ["ms_geofence_lookup"])
    user = _existing_user("ms_geofence_lookup", email, verified=True)
    try:
        assert _lookup_known_user(PROVIDER, _profile("oid-geo", email, False)) is None, \
            "an unverified email must not resolve another account (bypass_geofence borrowing)"
        found = _lookup_known_user(PROVIDER, _profile("oid-geo", email, True))
        assert found is not None and found.pk == user.pk, "a verified email still resolves the account"
    finally:
        _clean([email], [], ["ms_geofence_lookup"])


@th.django_unit_test("microsoft linking: after an email-less create, a magic-link lookup for that address finds no account")
def test_ms_pre_hijack_regression(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.rest.oauth import _find_or_create_user
    email, oid = "ms_prehijack_victim@example.com", "oid-prehijack"
    _clean([email], [oid])
    try:
        _find_or_create_user(PROVIDER, _profile(oid, email, False))
        # Magic login and forgot-password resolve the account through
        # User.lookup_from_request -> User.lookup(email=...).
        assert User.lookup(email=email) is None, \
            "the attacker's Microsoft account must not be reachable by the victim's address"
    finally:
        _clean([email], [oid])
