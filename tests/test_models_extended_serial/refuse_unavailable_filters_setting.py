"""MOJO_REST_REFUSE_UNAVAILABLE_FILTERS = False keeps the pre-1.43 drop (#10675).

The default refuses a relation filter the caller may not use with a 400; that
contract is in tests/test_models/rest_filter_walks.py. This file covers the
temporary compatibility switch: with it off, the refused key is dropped as
before, so the list and a `_stats` bundle come back unfiltered. It reloads the
test server's settings, so it runs serially.
"""
import json

from testit import helpers as th

P = "rufs10675_"
PW = "rufs10675##mojo99x"
MEMBER = P + "member"
KEY = "public_messages__email__startswith"


@th.django_unit_setup()
def setup_refuse_unavailable_filters_setting(opts):
    from mojo.apps.account.models import User, Group, GroupMember, PublicMessage

    PublicMessage.objects.filter(email__startswith=P).delete()
    GroupMember.objects.filter(group__name__startswith=P).delete()
    Group.objects.filter(name__startswith=P).delete()
    User.objects.filter(username__startswith=P).delete()

    group = Group.objects.create(name=P + "brand", kind="organization")
    member = User(username=MEMBER, email=MEMBER + "@test.com")
    member.save()
    member.is_active = True
    member.is_email_verified = True
    member.save_password(PW)
    member.save()
    GroupMember(user=member, group=group).save()
    PublicMessage.objects.create(
        group=group, kind="contact", name="Jane", email=P + "jane@example.org",
        subject="s", message="m", ip_address="203.0.113.7")


def _login(opts):
    opts.client.logout()
    assert opts.client.login(username=MEMBER, password=PW), f"{MEMBER} could not sign in"


@th.django_unit_test("switch off: a refused walk is dropped, list and _stats unfiltered")
def test_switch_off_keeps_drop(opts):
    params = {"name__startswith": P, KEY: P + "zzz"}
    _login(opts)
    resp = opts.client.get("/api/group", params=params)
    assert resp.status_code == 400, \
        f"by default the walk must be refused, got {resp.status_code}: {resp.response}"

    with th.server_settings(MOJO_REST_REFUSE_UNAVAILABLE_FILTERS=False):
        _login(opts)
        resp = opts.client.get("/api/group", params=params)
        assert resp.status_code == 200, \
            f"with the switch off the walk must be dropped, got {resp.status_code}: {resp.response}"
        assert resp.response.count == 1, \
            f"the dropped walk must leave the member's one group, got {resp.response.count}"
        stats = json.dumps({"miss": {KEY: P + "zzz"}})
        resp = opts.client.get("/api/group", params={
            "name__startswith": P, "_mode": "count", "_stats": stats})
        assert resp.status_code == 200, f"_stats failed: {resp.response}"
        got = resp.response.get("stats") or {}
        assert got.get("miss") == 1, \
            f"with the switch off a _stats bundle must count as if unfiltered, got {got}"
