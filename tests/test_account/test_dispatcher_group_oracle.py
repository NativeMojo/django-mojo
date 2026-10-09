"""The dispatcher's `group` check must not tell a confined credential whether
a group id exists (maestro #6986).

A confined credential (an ApiKey or a GroupScopedToken) naming a group it may
not use gets one response, whether that group is another tenant's, inactive or
not there at all, and the refused request writes nothing to the other group.
"""
from testit import helpers as th

TESTIT_TIER = "framework"

PREFIX = "dgo6986_"
PASSWORD = "dgo6986##Pass99"
ROUTE = "api/group"
REFUSAL = {"error": "Group not accessible with this API key", "code": 403}
INVALID = {"error": "Invalid group ID", "code": 400}
# No group can have this id: far above anything the long-lived database holds.
UNUSED_ID = 2_000_000_000


def _apikey(token):
    return {"Authorization": f"apikey {token}"}


def _grouptoken(token):
    return {"Authorization": f"grouptoken {token}"}


def _raw(opts, headers, **params):
    """Status and the exact response bytes. The test client only exposes the
    parsed body, and the claim under test is about the bytes."""
    sent = {"Content-Type": "application/json"}
    sent.update(headers)
    resp = opts.client.session.get(f"{opts.client.host}{ROUTE}", params=params, headers=sent)
    return resp.status_code, resp.content


def _named(body, keys):
    """The keys the dispatcher wrote. The server adds its own name to every
    JSON body; that is not the dispatcher's and is the same in every case."""
    import json
    data = json.loads(body)
    return {key: data.get(key) for key in keys}


def _activity(group_id):
    from mojo.apps.account.models import Group
    return Group.objects.values_list("last_activity", flat=True).get(pk=group_id)


def _credentials(opts):
    return (("ApiKey", _apikey(opts.key_token)), ("GroupScopedToken", _grouptoken(opts.group_token)))


@th.django_unit_setup()
def setup_dispatcher_group_oracle(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.account.services import group_token

    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()

    own = Group.objects.create(name=f"{PREFIX}own", kind="organization", uuid=f"{PREFIX}own-uuid")
    other = Group.objects.create(name=f"{PREFIX}other", kind="organization", uuid=f"{PREFIX}other-uuid")
    inactive = Group.objects.create(name=f"{PREFIX}inactive", kind="organization", is_active=False)
    assert not Group.objects.filter(pk=UNUSED_ID).exists(), "control: the unused id must not exist"

    member = User.objects.create(username=f"{PREFIX}member", email=f"{PREFIX}member@example.com")
    member.set_password(PASSWORD)
    member.save()
    own.add_member(member)

    _, opts.key_token = ApiKey.create_for_group(
        group=own, name=f"{PREFIX}key", permissions={"groups": True})
    opts.group_token = group_token.mint(member, own)
    # A key whose own group has since been switched off. It still signs in,
    # with no group of its own, which is the only way a confined credential
    # reaches the dispatcher's group_uuid branch.
    dark = Group.objects.create(name=f"{PREFIX}dark", kind="organization")
    _, opts.dark_key_token = ApiKey.create_for_group(
        group=dark, name=f"{PREFIX}dark_key", permissions={"groups": True})
    Group.objects.filter(pk=dark.pk).update(is_active=False)
    opts.own_id, opts.other_id, opts.inactive_id = own.pk, other.pk, inactive.pk
    opts.other_uuid = other.uuid
    opts.member_email = member.email


@th.django_unit_test()
def test_confined_credential_gets_one_refusal_for_any_group_not_its_own(opts):
    for name, headers in _credentials(opts):
        answers = {
            "another active group": _raw(opts, headers, group=opts.other_id),
            "an inactive group": _raw(opts, headers, group=opts.inactive_id),
            "an unused id": _raw(opts, headers, group=UNUSED_ID),
        }
        expected = answers["another active group"]
        assert expected[0] == 403, f"{name}: another active group must be refused with 403, got {expected[0]}"
        assert _named(expected[1], REFUSAL) == REFUSAL, \
            f"{name}: the refusal must keep its existing body, got {expected[1]!r}"
        for case, answer in answers.items():
            assert answer == expected, \
                f"{name}: {case} must get the same status and bytes as another active group, " \
                f"got {answer!r} against {expected!r}"


@th.django_unit_test()
def test_refused_request_does_not_stamp_the_other_group(opts):
    from mojo.apps.account.models import Group

    for name, headers in _credentials(opts):
        Group.objects.filter(pk=opts.other_id).update(last_activity=None)
        status, _ = _raw(opts, headers, group=opts.other_id)
        assert status == 403, f"control ({name}): the other group must be refused, got {status}"
        assert _activity(opts.other_id) is None, \
            f"{name}: a refused request must not stamp activity on the other group"

        Group.objects.filter(pk=opts.other_id).update(last_activity=None)
        # A credential that already carries its own group never reaches the
        # group_uuid branch, so there is no refusal to assert here; the other
        # group must stay unstamped either way.
        _raw(opts, headers, group_uuid=opts.other_uuid)
        assert _activity(opts.other_id) is None, \
            f"{name}: naming the other group by group_uuid must not stamp activity on it"


@th.django_unit_test()
def test_group_uuid_refusal_does_not_stamp_the_other_group(opts):
    from mojo.apps.account.models import Group

    headers = _apikey(opts.dark_key_token)
    Group.objects.filter(pk=opts.other_id).update(last_activity=None)
    status, body = _raw(opts, headers, group_uuid=opts.other_uuid)
    assert status == 403, \
        f"control: a key with no group of its own naming another group's uuid must be refused, got {status}"
    assert _named(body, REFUSAL) == REFUSAL, f"the refusal must keep its existing body, got {body!r}"
    assert _activity(opts.other_id) is None, \
        "a request refused by group_uuid must not stamp activity on the other group"


@th.django_unit_test()
def test_allowed_request_still_stamps_the_callers_own_group(opts):
    from mojo.apps.account.models import Group

    for name, headers in _credentials(opts):
        Group.objects.filter(pk=opts.own_id).update(last_activity=None)
        status, body = _raw(opts, headers, group=opts.own_id)
        assert status != 403 or b"Group not accessible" not in body, \
            f"{name}: the caller's own group must pass the dispatcher, got {status} {body!r}"
        assert _activity(opts.own_id) is not None, \
            f"{name}: an allowed request must stamp activity on the caller's own group"


@th.django_unit_test()
def test_non_integer_group_is_still_a_400(opts):
    for name, headers in _credentials(opts):
        status, body = _raw(opts, headers, group="not-a-number")
        assert status == 400, f"{name}: a non-integer group must still be a 400, got {status}"
        assert _named(body, INVALID) == INVALID, \
            f"{name}: the 400 body must be unchanged, got {body!r}"


@th.django_unit_test()
def test_ordinary_user_is_not_given_the_confined_refusal(opts):
    # An unconfined caller is out of scope: a missing or inactive group still
    # reaches the endpoint with no group, as before.
    assert opts.client.login(opts.member_email, PASSWORD), "control: the member must be able to sign in"
    try:
        headers = {"Authorization": f"Bearer {opts.client.access_token}"}
        for case, group_id in (("an unused id", UNUSED_ID), ("an inactive group", opts.inactive_id)):
            status, body = _raw(opts, headers, group=group_id)
            assert b"Group not accessible" not in body, \
                f"an ordinary user naming {case} must not get the confined refusal, got {status} {body!r}"
            assert status != 400, f"an ordinary user naming {case} must reach the endpoint, got 400"
    finally:
        opts.client.logout()
