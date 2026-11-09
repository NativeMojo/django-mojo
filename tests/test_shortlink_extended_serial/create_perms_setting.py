"""#8099 — SHORTLINK_CREATE_PERMS, set in the server's settings, gates
POST /api/shortlink/link/create end to end."""
import uuid as _uuid

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

PREFIX = "sl8099"
PWORD = "Sl8099##mojo99"
CREATE = "/api/shortlink/link/create"


def _make_member(tag, name, group, perms):
    from mojo.apps.account.models import User, GroupMember
    username = f"{PREFIX}_{name}_{tag}@shortlink.test"
    user = User.objects.create_user(username=username, email=username, password=PWORD)
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    member, _ = GroupMember.objects.get_or_create(user=user, group=group)
    member.permissions = {p: True for p in perms}
    member.save()
    return user


@th.django_unit_setup()
def setup_create_perms_setting(opts):
    from mojo.apps.account.models import User, Group, ApiKey
    from mojo.apps.shortlink.models import ShortLink
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    ShortLink.objects.filter(source__startswith=PREFIX).delete()
    ApiKey.objects.filter(name__startswith=f"{PREFIX}_").delete()
    Group.objects.filter(name__startswith=f"{PREFIX}_").delete()
    User.objects.filter(username__startswith=f"{PREFIX}_").delete()

    tag = _uuid.uuid4().hex[:8]
    opts.source = f"{PREFIX}_{tag}"
    group = Group.objects.create(name=f"{PREFIX}_g_{tag}", kind="organization")
    opts.group = group.pk
    opts.member = _make_member(tag, "member", group, [])
    opts.holder = _make_member(tag, "holder", group, ["manage_shortlinks"])
    _, opts.key = ApiKey.create_for_group(
        group=group, name=f"{PREFIX}_k_{tag}", permissions={"view_fileman": True})


def _create(opts, name):
    from mojo.apps.shortlink.models import ShortLink
    url = f"https://example.com/{opts.source}/{name}"
    resp = opts.client.post(f"{CREATE}?group={opts.group}", {"url": url, "source": opts.source})
    return resp, ShortLink.objects.filter(url=url).exists()


@th.django_unit_test("SHORTLINK_CREATE_PERMS set: only holders of the permission create links")
def test_setting_gates_route(opts):
    with th.server_settings(SHORTLINK_CREATE_PERMS=["manage_shortlinks"]):
        opts.client.login(opts.member.username, PWORD)
        resp, created = _create(opts, "member")
        assert_eq(resp.status_code, 403,
                  f"a member without manage_shortlinks must be refused, got {resp.status_code}: {resp.response}")
        assert_true(not created, "no link may be created for the refused member")

        opts.client.login(opts.holder.username, PWORD)
        resp, created = _create(opts, "holder")
        assert_eq(resp.status_code, 200,
                  f"a member holding manage_shortlinks must be allowed, got {resp.status_code}: {resp.response}")
        assert_true(created, "the holder's link must exist")

        opts.client.logout()
        opts.client.bearer = "apikey"
        opts.client.access_token = opts.key
        opts.client.is_authenticated = True
        resp, created = _create(opts, "key")
        assert_eq(resp.status_code, 403,
                  f"a key without manage_shortlinks must be refused, got {resp.status_code}: {resp.response}")
        assert_true(not created, "no link may be created for the refused key")
        opts.client.logout()
