"""Maestro item 52 — a reverse OneToOne in a graph's "graphs" serializes as an
object, not [].

Django's OneToOneRel SUBCLASSES ManyToOneRel. The serializer tested the many
branch with `isinstance(field, (ManyToManyField, ManyToOneRel))`, so a reverse
OneToOne matched it, had no `.all()`, and fell through to a literal `[]`.

The fix classifies relations through named SINGLE_RELATIONS / MANY_RELATIONS
tuples with the single-object check tested FIRST at every site. Adding
OneToOneRel to the many tuple would have been a silent no-op.

(mojo/serializers/simple.py carried the same bug in a worse form — no `.all()`
guard and no try/except, so it raised AttributeError — but that module has
since been removed: it was unimportable on Python 3.12 and nothing
referenced it.)

Fixtures are in-tree: User.totp is a reverse OneToOne (account.UserTOTP has
OneToOneField(User, related_name="totp")); User.devices is a reverse FK, used
as the control that must still serialize as a list.
"""
import uuid
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "bug"  # #2792 tier curation

GRAPH_O2O = "item52_reverse_o2o"
GRAPH_FK = "item52_reverse_fk"


@th.django_unit_setup()
def setup_reverse_one_to_one(opts):
    from mojo.apps.account.models import User

    tag = uuid.uuid4().hex[:8]
    User.objects.filter(username__startswith="item52_").delete()

    user = User.objects.create_user(
        username=f"item52_{tag}@example.com",
        email=f"item52_{tag}@example.com",
        password="Item52##pw99")
    user.save()
    opts.user_id = user.pk

    # A user with NO totp row, to pin the absent case.
    bare = User.objects.create_user(
        username=f"item52_bare_{tag}@example.com",
        email=f"item52_bare_{tag}@example.com",
        password="Item52##pw99")
    bare.save()
    opts.bare_user_id = bare.pk

    # Install temporary graphs additively, under names nothing else requests.
    # bin/run_tests runs modules as threads in one process, so these must be
    # popped again in the last test rather than left on the class.
    User.RestMeta.GRAPHS[GRAPH_O2O] = {
        "fields": ["id", "username"],
        "graphs": {"totp": "basic"},
    }
    User.RestMeta.GRAPHS[GRAPH_FK] = {
        "fields": ["id", "username"],
        "graphs": {"devices": "basic"},
    }


def _serialize(user, graph):
    from mojo.serializers.core.serializer import OptimizedGraphSerializer
    return OptimizedGraphSerializer(user, graph=graph)._serialize_instance_cached(user)


@th.django_unit_test("reverse OneToOne in a graph serializes as an OBJECT, not [] (THE regression)")
def test_reverse_o2o_serializes_as_object(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.totp import UserTOTP

    user = User.objects.get(pk=opts.user_id)
    UserTOTP.objects.filter(user=user).delete()
    totp = UserTOTP(user=user)
    totp.save()

    data = _serialize(user, GRAPH_O2O)
    got = data.get("totp")
    assert_true(not isinstance(got, list),
                f"a reverse OneToOne must not serialize as a list — got {got!r}")
    assert_true(isinstance(got, dict),
                f"a reverse OneToOne must serialize as a single object, got {type(got).__name__}: {got!r}")
    assert_eq(got.get("id"), totp.pk,
              f"the serialized object must be the related row, got id={got.get('id')!r} want {totp.pk}")


@th.django_unit_test("absent reverse OneToOne serializes as null (pin — already passing pre-fix)")
def test_reverse_o2o_absent_is_null(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.totp import UserTOTP

    bare = User.objects.get(pk=opts.bare_user_id)
    UserTOTP.objects.filter(user=bare).delete()

    data = _serialize(bare, GRAPH_O2O)
    assert_true(data.get("totp") is None,
                f"a missing reverse OneToOne must serialize as null, got {data.get('totp')!r}")


@th.django_unit_test("reverse FK still serializes as a list (control)")
def test_reverse_fk_still_list(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.user_id)
    data = _serialize(user, GRAPH_FK)
    assert_true(isinstance(data.get("devices"), list),
                f"a reverse FK must still serialize as a list, got {data.get('devices')!r}")


@th.django_unit_test("query optimizer routes a reverse OneToOne to select_related, not prefetch")
def test_optimizer_selects_reverse_o2o(opts):
    """NOTE: _apply_query_optimizations is currently DEAD CODE — nothing calls
    it. This asserts the method is internally correct, not that a live query
    got faster. Do not wire it into serialize() as a drive-by; that is a
    framework-wide behavior change with its own risk profile.
    """
    from mojo.apps.account.models import User
    from mojo.serializers.core.serializer import OptimizedGraphSerializer

    qs = User.objects.filter(pk=opts.user_id)
    ser = OptimizedGraphSerializer(qs, graph=GRAPH_O2O, many=True)
    optimized = ser._apply_query_optimizations(qs)

    select_related = optimized.query.select_related
    assert_true(select_related,
                "a reverse OneToOne in the graph should have produced a select_related")
    if isinstance(select_related, dict):
        assert_true("totp" in select_related,
                    f"totp should be select_related, got {select_related!r}")
    assert_true("totp" not in [p for p in getattr(optimized, "_prefetch_related_lookups", ())],
                "a reverse OneToOne must NOT be prefetch_related — it is a single object")


@th.django_unit_test("teardown: temporary graphs removed from User.RestMeta")
def test_zz_cleanup_graphs(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.totp import UserTOTP

    User.RestMeta.GRAPHS.pop(GRAPH_O2O, None)
    User.RestMeta.GRAPHS.pop(GRAPH_FK, None)
    UserTOTP.objects.filter(user_id__in=[opts.user_id, opts.bare_user_id]).delete()
    User.objects.filter(pk__in=[opts.user_id, opts.bare_user_id]).delete()

    assert_true(GRAPH_O2O not in User.RestMeta.GRAPHS,
                "the temporary o2o graph must be removed so it cannot leak into other modules")
    assert_true(GRAPH_FK not in User.RestMeta.GRAPHS,
                "the temporary fk graph must be removed so it cannot leak into other modules")
