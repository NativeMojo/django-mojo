"""
Hosted OAuth sign-up under the page's group (maestro #11788).

The hosted login and register pages now pass their GROUP_UUID to OAuth begin,
so the new-account gate in _find_or_create_user sees the group: its
registration.enabled and registration.methods apply, the new account joins
the group and USER_REGISTERED_HANDLER gets it. Begin without a group is
unchanged.

Every case runs for google and microsoft. The state comes from a real begin;
the account step runs in-process on that state, as the callback does. Each
case makes its own groups and emails, so the module is parallel-safe.
"""
import json
import uuid as _uuid

from testit import helpers as th
from tests.test_register import _capture

TESTIT_TIER = "extended"

PROVIDERS = ("google", "microsoft")
CAPTURE_REGISTER = "tests.test_register._capture.capture_register"
# Microsoft is off by default; switch it on in the system config for begin,
# as the Microsoft tests do. The group's own login.methods does the same.
LOGIN_ON = ["password", "google", "microsoft"]
REG_ON = ["password", "google", "microsoft"]


def _suffix():
    return _uuid.uuid4().hex[:10]


def _make_group(name, registration=None, extra_metadata=None):
    from mojo.apps.account.models import Group
    auth_config = {"login": {"methods": LOGIN_ON}}
    if registration is not None:
        auth_config["registration"] = registration
    metadata = {"auth_config": auth_config}
    metadata.update(extra_metadata or {})
    return Group.objects.create(
        name=f"test-11788-{name}-{_suffix()}", uuid=_uuid.uuid4().hex,
        is_active=True, metadata=metadata)


def _begin(opts, provider, group=None, redirect_uri=None):
    """Run a real begin and return its server-side state record."""
    from urllib.parse import quote
    from mojo.apps.account.services.oauth import get_provider
    params = []
    if redirect_uri:
        params.append(f"redirect_uri={quote(redirect_uri, safe='')}")
    if group is not None:
        params.append(f"group_uuid={group.uuid}")
    url = f"/api/auth/oauth/{provider}/begin" + ("?" + "&".join(params) if params else "")
    headers = {"X-Mojo-Test-Auth-Config": json.dumps({"login": {"methods": LOGIN_ON}})}
    resp = opts.client.get(url, headers=headers)
    assert resp.status_code == 200, f"{provider} begin must succeed, got {resp.status_code}: {resp.response}"
    record = get_provider(provider).peek_state(resp.response.data.state)
    assert record is not None, f"{provider} begin must store its state"
    return record


def _profile(provider, email):
    profile = {"uid": f"{provider}-11788-{_suffix()}", "email": email,
               "display_name": "Hosted Group User"}
    if provider == "microsoft":
        profile["email_verified"] = True
    return profile


class _Request:
    """The callback's request, as the handler sees it: loopback, in test mode,
    with this case's capture id and the capture handler named."""
    def __init__(self, capture_id):
        self.META = {
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_X_MOJO_TEST_CAPTURE_ID": capture_id,
            "HTTP_X_MOJO_TEST_USER_REGISTERED_HANDLER": CAPTURE_REGISTER,
        }


def _signup(provider, profile, state, capture_id=None):
    from mojo.apps.account.rest.oauth import _find_or_create_user
    request = _Request(capture_id) if capture_id else None
    return _find_or_create_user(provider, profile, state_data=state, request=request)


def _refused(provider, profile, state):
    from mojo import errors as merrors
    try:
        _signup(provider, profile, state)
    except merrors.PermissionDeniedException:
        return True
    return False


def _no_rows(provider, email, profile, group):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.oauth import OAuthConnection
    from mojo.apps.account.models.member import GroupMember
    assert not User.objects.filter(email=email).exists(), f"{provider}: no user may be created"
    assert not OAuthConnection.objects.filter(provider=provider, provider_uid=profile["uid"]).exists(), \
        f"{provider}: no OAuth connection may be created"
    assert not GroupMember.objects.filter(group=group, user__email=email).exists(), \
        f"{provider}: no membership may be created"


def _cleanup(emails=(), groups=()):
    from mojo.apps.account.models import User
    User.objects.filter(email__in=list(emails)).delete()
    for group in groups:
        group.delete()


@th.django_unit_test("hosted oauth: begin with group_uuid puts it in state; without it the state is as before")
def test_begin_group_in_state(opts):
    from mojo.apps.account.models import Group
    redirect_uri = f"https://brand-{_suffix()}.example.test/login"
    group = _make_group("state", extra_metadata={"allowed_redirect_urls": [redirect_uri]})
    try:
        for provider in PROVIDERS:
            record = _begin(opts, provider, group)
            assert record.get("group_uuid") == str(group.uuid), f"{provider}: state must carry the group, got {record}"
            plain = _begin(opts, provider)
            assert "group_uuid" not in plain, f"{provider}: begin without a group must not add one, got {plain}"
            # Naming the group also lets begin accept the group's own redirect URLs.
            record = _begin(opts, provider, group, redirect_uri=redirect_uri)
            assert record.get("frontend_uri") == redirect_uri, \
                f"{provider}: the group's allowed_redirect_urls must be accepted, got {record}"
        # And the group is touched, as on every hosted call that names it.
        assert Group.objects.get(pk=group.pk).last_activity is not None, "begin must touch the named group"
    finally:
        _cleanup(groups=[group])


@th.django_unit_test("hosted oauth: a group without the provider in registration.methods refuses a new account")
def test_methods_refuse(opts):
    group = _make_group("methods", registration={"methods": ["password"]})
    emails = []
    try:
        for provider in PROVIDERS:
            email = f"h11788-methods-{provider}-{_suffix()}@example.com"
            emails.append(email)
            profile = _profile(provider, email)
            assert _refused(provider, profile, _begin(opts, provider, group)), \
                f"{provider}: sign-up must be refused when registration.methods leaves it out"
            _no_rows(provider, email, profile, group)
    finally:
        _cleanup(emails, [group])


@th.django_unit_test("hosted oauth: a group with registration closed refuses a new account")
def test_registration_closed_refuses(opts):
    group = _make_group("closed", registration={"enabled": False, "methods": REG_ON})
    emails = []
    try:
        for provider in PROVIDERS:
            email = f"h11788-closed-{provider}-{_suffix()}@example.com"
            emails.append(email)
            profile = _profile(provider, email)
            assert _refused(provider, profile, _begin(opts, provider, group)), \
                f"{provider}: sign-up must be refused when registration.enabled is false"
            _no_rows(provider, email, profile, group)
    finally:
        _cleanup(emails, [group])


@th.django_unit_test("hosted oauth: an existing account still signs in on a closed group, with no membership added")
def test_existing_user_signs_in(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.member import GroupMember
    group = _make_group("existing", registration={"enabled": False})
    emails = []
    try:
        for provider in PROVIDERS:
            email = f"h11788-existing-{provider}-{_suffix()}@example.com"
            emails.append(email)
            user = User(username=email, email=email)
            user.save()
            profile = _profile(provider, email)
            # By email: OAuth confirms the address and links a connection.
            found, conn, created = _signup(provider, profile, _begin(opts, provider, group))
            assert found.pk == user.pk and created is False, f"{provider}: must sign in the existing account by email"
            # By connection: the second sign-in finds the connection.
            found, conn2, created = _signup(provider, profile, _begin(opts, provider, group))
            assert found.pk == user.pk and conn2.pk == conn.pk and created is False, \
                f"{provider}: must sign in the existing account by its connection"
            assert not GroupMember.objects.filter(group=group, user=user).exists(), \
                f"{provider}: OAuth sign-in must not add an existing account to the group"
    finally:
        _cleanup(emails, [group])


@th.django_unit_test("hosted oauth: an invited email signs in on a closed group")
def test_invited_email_signs_in(opts):
    from mojo.apps.account.models import User
    group = _make_group("invited", registration={"enabled": False})
    emails = []
    try:
        for provider in PROVIDERS:
            email = f"h11788-invited-{provider}-{_suffix()}@example.com"
            emails.append(email)
            group.invite(email)
            invited = User.objects.get(email=email)
            found, conn, created = _signup(provider, _profile(provider, email), _begin(opts, provider, group))
            assert found.pk == invited.pk and created is False, \
                f"{provider}: the invited person must sign in to the invited account"
    finally:
        _cleanup(emails, [group])


@th.django_unit_test("hosted oauth: an open group creates the account, adds it to the group and reports the group")
def test_open_group_creates_member(opts):
    from mojo.apps.account.models.member import GroupMember
    group = _make_group("open", registration={"methods": list(PROVIDERS)})
    emails = []
    try:
        for provider in PROVIDERS:
            email = f"h11788-open-{provider}-{_suffix()}@example.com"
            emails.append(email)
            capture_id = _capture.new_capture_id()
            try:
                user, conn, created = _signup(provider, _profile(provider, email),
                                              _begin(opts, provider, group), capture_id)
                assert created is True, f"{provider}: the account must be created"
                assert GroupMember.objects.filter(group=group, user=user).exists(), \
                    f"{provider}: the new account must join the group"
                calls = _capture.read_capture(capture_id).get("register", [])
                assert len(calls) == 1, f"{provider}: the handler must fire once, got {calls}"
                assert calls[0]["group_uuid"] == str(group.uuid) and calls[0]["source"] == "oauth", \
                    f"{provider}: the handler must get the group, got {calls[0]}"
            finally:
                _capture.clear_capture(capture_id)
    finally:
        _cleanup(emails, [group])


@th.django_unit_test("hosted oauth: with no group the account is created as before, ungated and with no group")
def test_no_group_unchanged(opts):
    from mojo.apps.account.models.member import GroupMember
    emails = []
    try:
        for provider in PROVIDERS:
            email = f"h11788-nogroup-{provider}-{_suffix()}@example.com"
            emails.append(email)
            capture_id = _capture.new_capture_id()
            try:
                user, conn, created = _signup(provider, _profile(provider, email),
                                              _begin(opts, provider), capture_id)
                assert created is True, f"{provider}: the account must be created with no group"
                assert not GroupMember.objects.filter(user=user).exists(), \
                    f"{provider}: no membership may be made with no group"
                calls = _capture.read_capture(capture_id).get("register", [])
                assert len(calls) == 1 and calls[0]["group_uuid"] is None, \
                    f"{provider}: the handler must get no group, got {calls}"
            finally:
                _capture.clear_capture(capture_id)
    finally:
        _cleanup(emails)


@th.django_unit_test("hosted oauth: a restrictive group leaves another group's open sign-up alone")
def test_other_group_unaffected(opts):
    closed = _make_group("strict", registration={"enabled": False})
    open_group = _make_group("other", registration={"methods": REG_ON})
    emails = []
    try:
        for provider in PROVIDERS:
            email = f"h11788-other-{provider}-{_suffix()}@example.com"
            emails.append(email)
            profile = _profile(provider, email)
            assert _refused(provider, profile, _begin(opts, provider, closed)), \
                f"{provider}: the closed group must refuse"
            user, conn, created = _signup(provider, profile, _begin(opts, provider, open_group))
            assert created is True, f"{provider}: the open group must still create the account"
    finally:
        _cleanup(emails, [closed, open_group])


@th.django_unit_test("hosted oauth: a begin that names group Y gets Y's rules")
def test_named_group_rules_apply(opts):
    # The contract: the gate follows the group the request names, as password
    # sign-up does. The hosted page on host X can only name X's group.
    open_x = _make_group("hostx", registration={"methods": REG_ON})
    closed_y = _make_group("groupy", registration={"methods": ["password"]})
    emails = []
    try:
        for provider in PROVIDERS:
            email = f"h11788-named-{provider}-{_suffix()}@example.com"
            emails.append(email)
            profile = _profile(provider, email)
            assert _refused(provider, profile, _begin(opts, provider, closed_y)), \
                f"{provider}: naming group Y must apply Y's rules"
            _no_rows(provider, email, profile, closed_y)
    finally:
        _cleanup(emails, [open_x, closed_y])
