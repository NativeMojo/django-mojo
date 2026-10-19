"""#8648: a parent graph's persistent cache never carries one viewer's nested
user to another.

When a parent graph sets cache_ttl, `GroupMember_<pk>_default` is cached with
its nested user inside. A viewer without the account view therefore neither
reads nor writes the persistent cache. Here because it sets cache_ttl on
GroupMember's shared RestMeta: unsafe under the parallel default tier.
"""
import contextlib

from testit import helpers as th

PREFIX = "nupc8648_"
VIEWER = f"{PREFIX}viewer@test.com"
ACCOUNT = f"{PREFIX}account@test.com"
TARGET = f"{PREFIX}target@test.com"

PRIVATE_KEYS = {"permissions", "is_superuser", "requires_mfa",
                "requires_password_change", "has_password", "has_passkey", "metadata"}


@th.django_unit_setup()
def setup_nested_private_cache(opts):
    from mojo.apps.account.models import User, Group, GroupMember

    User.objects.filter(email__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    group = Group.objects.create(name=f"{PREFIX}group", kind="organization")
    opts.viewer_id = User.objects.create_user(username=VIEWER, email=VIEWER, password="x" * 16).pk
    account = User.objects.create_user(username=ACCOUNT, email=ACCOUNT, password="x" * 16)
    account.permissions = {"view_users": True}
    account.save()
    opts.account_id = account.pk
    target = User.objects.create_user(username=TARGET, email=TARGET, password="x" * 16)
    member = GroupMember(user=target, group=group)
    member.save()
    opts.member_id = member.pk


@contextlib.contextmanager
def _cached_member_graph(ttl):
    from mojo.apps.account.models import GroupMember
    graph = GroupMember.RestMeta.GRAPHS["default"]
    had, old = "cache_ttl" in graph, graph.get("cache_ttl")
    graph["cache_ttl"] = ttl
    try:
        yield
    finally:
        if had:
            graph["cache_ttl"] = old
        else:
            graph.pop("cache_ttl", None)


def _render(member_id, user_id, backend=None, **kwargs):
    from objict import objict
    from mojo.apps.account.models import User, GroupMember
    from mojo.models.rest import ACTIVE_REQUEST
    from mojo.serializers.core.serializer import OptimizedGraphSerializer

    request = objict(user=User.objects.get(pk=user_id), method="GET",
                     api_key=None, group_token=None)
    token = ACTIVE_REQUEST.set(request)
    try:
        member = GroupMember.objects.get(pk=member_id)
        serializer = OptimizedGraphSerializer(member, graph="default", **kwargs)
        # One in-memory backend shared by both reads stands in for the
        # deployment's persistent cache.
        serializer._cache_backend = backend
        return serializer.serialize()
    finally:
        ACTIVE_REQUEST.reset(token)


@th.django_unit_test("case 13a: an account viewer's cached member row is not served to a restricted viewer")
def test_persistent_cache_not_shared(opts):
    from mojo.serializers.core.cache.memory import MemoryCacheBackend

    backend = MemoryCacheBackend()
    with _cached_member_graph(60):
        full = _render(opts.member_id, opts.account_id, backend=backend)
        assert PRIVATE_KEYS & set(full["user"]), "the account viewer must get the full nested user"
        assert backend.get(f"GroupMember_{opts.member_id}_default") is not None, \
            "the account viewer's read must fill the persistent cache, or this test proves nothing"
        restricted = _render(opts.member_id, opts.viewer_id, backend=backend)
    leaked = PRIVATE_KEYS & set(restricted["user"])
    assert not leaked, f"a restricted viewer must not be served the cached full user: {sorted(leaked)}"
    assert restricted["user"].get("email") == TARGET, "the restricted viewer still gets the email"


@th.django_unit_test("case 13b: bypass_cache output is stripped too")
def test_bypass_cache_stripped(opts):
    restricted = _render(opts.member_id, opts.viewer_id, bypass_cache=True)
    leaked = PRIVATE_KEYS & set(restricted["user"])
    assert not leaked, f"bypass_cache must not skip the strip: {sorted(leaked)}"
