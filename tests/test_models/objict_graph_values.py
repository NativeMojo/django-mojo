"""Maestro item 9301 — an objict (or any dict subclass) in a graph serializes
as data, not null.

OptimizedGraphSerializer._serialize_value_fast detected model instances with
`hasattr(value, "pk") and hasattr(value, "_meta")`, before its dict branch.
objict answers every attribute lookup (missing keys read as None), so an objict
passed both checks and serialized as its `pk`: None. jsonfield_as_objict turns
a JSON field into an objict in place (Group.get_metadata does this), so the
group's metadata then disappeared from REST replies. The CSV formatter had the
same check and wrote "None" for an objict cell.

The fix detects a model instance with isinstance(value, Model).

The real-model case uses Group's existing "default" graph, which lists
metadata; nothing is patched or installed on the model.
"""
import uuid
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "bug"

NESTED = {
    "config": {"has_latch": True},
    "bins": {"left": {"sn": "x"}},
    "version": "1.0.311",
}


def _value(value, field=None):
    from mojo.serializers.core.serializer import OptimizedGraphSerializer
    return OptimizedGraphSerializer(None)._serialize_value_fast(value, field)


@th.django_unit_setup()
def setup_objict_graph_values(opts):
    from mojo.apps.account.models import Group, User

    tag = uuid.uuid4().hex[:8]
    Group.objects.filter(name__startswith="item9301_").delete()
    User.objects.filter(username__startswith="item9301_").delete()

    group = Group(name=f"item9301_{tag}", kind="group", metadata=dict(NESTED))
    group.save()
    opts.group_id = group.pk

    parent = Group(name=f"item9301_parent_{tag}", kind="group")
    parent.save()
    group.parent = parent
    group.save()
    opts.parent_id = parent.pk


@th.django_unit_test("a nested objict serializes as its data, not None (THE regression)")
def test_objict_serializes_as_data(opts):
    from objict import objict

    plain = _value(dict(NESTED))
    assert_eq(plain, NESTED, "control: a plain dict serializes intact")

    result = _value(objict.fromdict(NESTED))
    assert_eq(result, NESTED, f"an objict must serialize as its data, got {result!r}")
    assert_true(type(result) is dict, "the output is a plain dict")


@th.django_unit_test("objicts inside dicts, dicts inside objicts, and deep nesting")
def test_nested_dict_subclasses(opts):
    from objict import objict

    class Bag(dict):
        pass

    inner = objict.fromdict({"sn": "x"})
    assert_eq(_value({"left": inner}), {"left": {"sn": "x"}},
              "an objict inside a dict")

    outer = objict()
    outer["plain"] = {"a": 1, "when": None}
    assert_eq(_value(outer), {"plain": {"a": 1, "when": None}},
              "a dict inside an objict")

    deep = objict.fromdict({"l1": {"l2": {"l3": {"l4": {"leaf": [1, {"k": "v"}]}}}}})
    assert_eq(_value(deep), {"l1": {"l2": {"l3": {"l4": {"leaf": [1, {"k": "v"}]}}}}},
              "objicts nested four levels deep, with a list")

    assert_eq(_value([objict.fromdict({"a": 1}), Bag(b=2)]), [{"a": 1}, {"b": 2}],
              "dict subclasses inside a list")
    assert_eq(_value(Bag(pk=7, _meta="not a model")), {"pk": 7, "_meta": "not a model"},
              "a dict subclass with pk and _meta keys is still data")


@th.django_unit_test("model instances still serialize as their pk")
def test_model_values_unchanged(opts):
    from mojo.apps.account.models import Group

    group = Group.objects.get(pk=opts.group_id)
    parent_field = Group._meta.get_field("parent")
    assert_eq(_value(group.parent, parent_field), opts.parent_id,
              "a foreign key value serializes as its pk")
    assert_eq(_value(group), opts.group_id,
              "a model instance with no field serializes as its pk")


@th.django_unit_test("Group metadata turned into an objict still appears in the default graph")
def test_group_default_graph_keeps_metadata(opts):
    from objict import objict
    from mojo.apps.account.models import Group
    from mojo.serializers.core.serializer import OptimizedGraphSerializer

    group = Group.objects.get(pk=opts.group_id)
    before = OptimizedGraphSerializer(group, graph="default", bypass_cache=True).serialize()
    assert_eq(before.get("metadata"), NESTED, "control: metadata as stored")

    group = Group.objects.get(pk=opts.group_id)
    group.get_metadata()
    assert_true(isinstance(group.metadata, objict), "get_metadata made it an objict")
    data = OptimizedGraphSerializer(group, graph="default", bypass_cache=True).serialize()
    assert_eq(data.get("metadata"), NESTED,
              f"metadata after get_metadata must still serialize, got {data.get('metadata')!r}")
    assert_eq(data.get("id"), opts.group_id, "the group's own fields are unchanged")
    assert_eq(data.get("parent", {}).get("id"), opts.parent_id,
              "the nested parent graph is unchanged")


@th.django_unit_test("CSV writes an objict cell as data, and a model cell as its pk")
def test_csv_objict_cell(opts):
    from objict import objict
    from mojo.apps.account.models import Group
    from mojo.serializers.formats.csv import CsvFormatter

    fmt = CsvFormatter()
    cell = fmt._format_csv_value(objict.fromdict({"sn": "x"}))
    assert_true(cell != "None" and "sn" in cell, f"objict cell must carry its data, got {cell!r}")
    group = Group.objects.get(pk=opts.group_id)
    assert_eq(fmt._format_csv_value(group), str(opts.group_id), "a model cell is its pk")
