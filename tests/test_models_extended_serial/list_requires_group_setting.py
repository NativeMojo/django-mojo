"""REST_LIST_REQUIRES_GROUP — the site setting for models a site doesn't own (#8937).

The setting names model labels whose lists require a group, exactly as if the
model declared RestMeta.LIST_REQUIRES_GROUP. It reloads the test server's
settings, so it runs serially. The flag-on-the-model contract is in
tests/test_models/list_requires_group.py.
"""
import uuid as _uuid

from testit import helpers as th

PREFIX = "lrgset8937"
PERM = "view_listgroup_probe"
PWORD = "Lrg##setting99"
OPEN_PROBE = "/api/testit_support/list_group_probe_open"


@th.django_unit_setup()
def setup_list_requires_group_setting(opts):
    from mojo.apps.account.models import User, Group, GroupMember
    from testit_support.models import ListGroupProbeOpen

    ListGroupProbeOpen.objects.filter(name__startswith=PREFIX).delete()
    GroupMember.objects.filter(group__name__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__startswith=f"{PREFIX}_").delete()

    tag = _uuid.uuid4().hex[:6]
    opts.lrgset_groups = []
    email = f"{PREFIX}_member@example.com"
    member = User.objects.create_user(username=email, email=email, password=PWORD)
    member.is_active = True
    member.is_email_verified = True
    member.requires_mfa = False
    member.save()
    for key in ("a", "b"):
        group = Group.objects.create(name=f"{PREFIX}_{key}_{tag}", kind="organization", is_active=True)
        opts.lrgset_groups.append(group.pk)
        gm, _ = GroupMember.objects.get_or_create(user=member, group=group)
        gm.is_active = True
        gm.permissions = {PERM: True}
        gm.save()
        ListGroupProbeOpen.objects.create(name=f"{PREFIX}_{key}", group=group)
    opts.lrgset_email = email


def _login(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    opts.client.logout()
    assert opts.client.login(opts.lrgset_email, PWORD), \
        f"login failed: {opts.client.last_response.body}"


def _names(resp):
    data = (resp.json or {}).get("data") or []
    return sorted(r.get("name") for r in data if str(r.get("name", "")).startswith(PREFIX))


@th.django_unit_test("setting: a listed model label requires a group like the RestMeta flag")
def test_setting_flags_unowned_model(opts):
    _login(opts)
    resp = opts.client.get(OPEN_PROBE, params={"size": 100})
    assert resp.status_code == 200 and _names(resp) == [f"{PREFIX}_a", f"{PREFIX}_b"], \
        f"without the setting the control keeps the union, got {resp.status_code}: {_names(resp)}"

    with th.server_settings(REST_LIST_REQUIRES_GROUP=["testit_support.ListGroupProbeOpen"]):
        _login(opts)
        resp = opts.client.get(OPEN_PROBE, params={"size": 100})
        assert resp.status_code == 400, \
            f"with the setting a groupless list must be 400, got {resp.status_code}: {resp.json}"
        assert "group required" in str((resp.json or {}).get("error", "")), \
            f"expected 'group required', got {resp.json}"
        assert _names(resp) == [], f"refused list leaked rows: {_names(resp)}"

        resp = opts.client.get(OPEN_PROBE, params={"size": 100, "group": opts.lrgset_groups[0]})
        assert resp.status_code == 200 and _names(resp) == [f"{PREFIX}_a"], \
            f"naming a group must still list it, got {resp.status_code}: {_names(resp)}"
