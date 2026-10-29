"""#1831 — POST /api/shortlink/link/create must not put another tenant's private
File behind a public shortlink.

The route used to load any File by numeric id and shorten it. A file-backed
link now needs the File's share permission (SAVE_PERMS: manage_files, files or
owner), checked against the file's own group, and every refusal looks the same.
"""
import os
import tempfile
import uuid as _uuid

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "bug"

PREFIX = "sl1831"
PWORD = "Sl1831##mojo99"
CREATE = "/api/shortlink/link/create"


def _make_user(tag, name, global_perms=None):
    from mojo.apps.account.models import User
    username = f"{PREFIX}_{name}_{tag}@shortlink.test"
    user = User.objects.create_user(username=username, email=username, password=PWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    if global_perms:
        user.add_permission(list(global_perms))
        user.save()
    return user


def _join(user, group, perms):
    from mojo.apps.account.models import GroupMember
    member, _ = GroupMember.objects.get_or_create(user=user, group=group)
    member.permissions = {p: True for p in perms}
    member.save()


def _make_file(opts, fm, owner, name):
    from mojo.apps.fileman.models import File
    content = b"private bytes"
    f = File(filename=name, content_type="text/plain", category="text",
             file_size=len(content), file_manager=fm, user=owner, group=fm.group)
    f.generate_storage_filename()
    f.save()
    path = os.path.join(opts.tmpdir, f.storage_file_path.lstrip("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)
    f.mark_as_completed(commit=True)
    f.refresh_from_db()
    return f


def _as(opts, user):
    opts.client.logout()
    assert_true(opts.client.login(user.username, PWORD), f"login failed for {user.username}")


def _as_key(opts, token):
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True


def _create(opts, data, group=None):
    path = CREATE if group is None else f"{CREATE}?group={group}"
    return opts.client.post(path, dict(data, source=opts.source))


def _link_for(resp):
    from mojo.apps.shortlink.models import ShortLink
    url = resp.response.data.short_link
    code = url.rstrip("/").rsplit("/", 1)[-1]
    return ShortLink.objects.filter(code=code).first()


@th.django_unit_setup()
def setup_create_route_access(opts):
    from mojo.apps.account.models import User, Group, ApiKey
    from mojo.apps.fileman.models import FileManager, File
    from mojo.apps.shortlink.models import ShortLink
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Leftovers from an earlier run on this long-lived database.
    old_users = User.objects.filter(username__startswith=f"{PREFIX}_")
    ShortLink.objects.filter(source__startswith=PREFIX).delete()
    File.objects.filter(user__in=old_users).delete()
    FileManager.objects.filter(name__startswith=f"{PREFIX}_").delete()
    ApiKey.objects.filter(name__startswith=f"{PREFIX}_").delete()
    Group.objects.filter(name__startswith=f"{PREFIX}_").delete()
    old_users.delete()

    tag = _uuid.uuid4().hex[:8]
    opts.source = f"{PREFIX}_{tag}"
    group_a = Group.objects.create(name=f"{PREFIX}_a_{tag}", kind="organization")
    group_b = Group.objects.create(name=f"{PREFIX}_b_{tag}", kind="organization")
    opts.group_a, opts.group_b = group_a.pk, group_b.pk

    owner = _make_user(tag, "owner")
    _join(owner, group_b, [])
    outsider = _make_user(tag, "outsider")
    _join(outsider, group_a, ["manage_files", "view_fileman"])
    manager_b = _make_user(tag, "manager")
    _join(manager_b, group_b, ["manage_files"])
    viewer_b = _make_user(tag, "viewer")
    _join(viewer_b, group_b, ["view_fileman"])
    global_a = _make_user(tag, "global", global_perms=["manage_files"])
    _join(global_a, group_a, [])
    opts.owner, opts.outsider, opts.manager_b = owner, outsider, manager_b
    opts.viewer_b, opts.global_a = viewer_b, global_a

    opts.tmpdir = tempfile.mkdtemp(prefix="mojo_sl1831_")
    fm = FileManager(name=f"{PREFIX}_b_{tag}", backend_type="file", backend_url="file://",
                     user=owner, group=group_b, is_active=True, is_default=False,
                     is_public=False)
    fm.save()
    fm.set_setting("base_path", opts.tmpdir)
    fm.save(update_fields=["mojo_secrets", "modified"])
    opts.file_b = _make_file(opts, fm, owner, "private_b.txt").pk
    inactive = _make_file(opts, fm, owner, "inactive_b.txt")
    inactive.is_active = False
    inactive.save()
    opts.inactive_b = inactive.pk
    opts.missing = (File.objects.order_by("-pk").values_list("pk", flat=True).first() or 0) + 100000

    _, opts.key_a = ApiKey.create_for_group(
        group=group_a, name=f"{PREFIX}_a_{tag}", permissions={"view_fileman": True})
    _, opts.key_b = ApiKey.create_for_group(
        group=group_b, name=f"{PREFIX}_b_{tag}", permissions={"manage_files": True})


@th.django_unit_test("create: a member of another group cannot link a private file")
def test_other_group_member_refused(opts):
    from mojo.apps.shortlink.models import ShortLink
    _as(opts, opts.outsider)
    resp = _create(opts, {"file": opts.file_b}, group=opts.group_a)
    assert_eq(resp.status_code, 403,
              f"another tenant's file must be refused, got {resp.status_code}: {resp.response}")
    assert_true(not ShortLink.objects.filter(file_id=opts.file_b).exists(),
                "no shortlink may exist for the refused file")


@th.django_unit_test("create: missing, malformed, inactive and unauthorized files fail alike")
def test_uniform_denial(opts):
    _as(opts, opts.outsider)
    cases = {
        "missing": opts.missing,
        "text": "abc",
        "bool": True,
        "zero": 0,
        "too_many_digits": "9" * 5000,
        "beyond_bigint": 10 ** 30,
        "inactive": opts.inactive_b,
        "unauthorized": opts.file_b,
    }
    seen = {}
    for name, value in cases.items():
        resp = _create(opts, {"file": value})
        seen[name] = (resp.status_code, resp.response)
    first = seen["unauthorized"]
    assert_eq(first[0], 403, f"unauthorized file must be a 403, got {first}")
    for name, got in seen.items():
        assert_eq(got, first, f"{name} must answer exactly like an unauthorized file: {got} vs {first}")


@th.django_unit_test("create: owner and a manage_files member of the file's group may link it")
def test_allowed_callers(opts):
    _as(opts, opts.owner)
    resp = _create(opts, {"file": opts.file_b})
    assert_eq(resp.status_code, 200, f"owner must be allowed, got {resp.status_code}: {resp.response}")
    link = _link_for(resp)
    assert_true(link is not None and link.file_id == opts.file_b, "owner's link must point at the file")

    _as(opts, opts.manager_b)
    resp = _create(opts, {"file": opts.file_b}, group=opts.group_b)
    assert_eq(resp.status_code, 200,
              f"manage_files member of B must be allowed, got {resp.status_code}: {resp.response}")
    link = _link_for(resp)
    assert_eq(link.group_id, opts.group_b, "manager's link belongs to group B")


@th.django_unit_test("create: view_fileman alone cannot share a file (share needs SAVE_PERMS)")
def test_view_only_refused(opts):
    _as(opts, opts.viewer_b)
    resp = _create(opts, {"file": opts.file_b}, group=opts.group_b)
    assert_eq(resp.status_code, 403,
              f"view_fileman-only member must be refused, got {resp.status_code}: {resp.response}")


@th.django_unit_test("create: the link stays in the caller's group, not the file's")
def test_caller_group_restored(opts):
    _as(opts, opts.global_a)
    resp = _create(opts, {"file": opts.file_b}, group=opts.group_a)
    assert_eq(resp.status_code, 200,
              f"global manage_files holder must be allowed, got {resp.status_code}: {resp.response}")
    link = _link_for(resp)
    assert_eq(link.group_id, opts.group_a,
              f"link group must be the caller's group A ({opts.group_a}), got {link.group_id}")


@th.django_unit_test("create: API keys are bound to their tenant and permissions")
def test_api_keys(opts):
    _as_key(opts, opts.key_a)
    resp = _create(opts, {"file": opts.file_b})
    assert_eq(resp.status_code, 403,
              f"group-A key must be refused for B's file, got {resp.status_code}: {resp.response}")

    _as_key(opts, opts.key_b)
    resp = _create(opts, {"file": opts.file_b})
    assert_eq(resp.status_code, 200,
              f"group-B key with manage_files must be allowed, got {resp.status_code}: {resp.response}")
    link = _link_for(resp)
    assert_true(link.user_id is None, f"an unlinked key's link has no user, got {link.user_id}")
    opts.client.logout()


@th.django_unit_test("create: URL-only links still need only a signed-in user")
def test_url_only_unchanged(opts):
    _as(opts, opts.outsider)
    resp = _create(opts, {"url": "https://example.com/sl1831", "track_clicks": False})
    assert_eq(resp.status_code, 200, f"URL-only create must work, got {resp.status_code}: {resp.response}")
    link = _link_for(resp)
    assert_eq(link.url, "https://example.com/sl1831", "URL-only link keeps its url")
