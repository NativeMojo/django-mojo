"""Fail-closed Group parent validation and traversal regressions."""

from testit import helpers as th


PREFIX = "people_hierarchy_"


@th.django_unit_setup()
def setup_group_hierarchy(opts):
    from mojo.apps.account.models import Group

    Group.objects.filter(name__startswith=PREFIX).delete()
    root = Group.objects.create(name=f"{PREFIX}root")
    child = Group.objects.create(name=f"{PREFIX}child", parent=root)
    grandchild = Group.objects.create(name=f"{PREFIX}grandchild", parent=child)
    opts.root_id = root.pk
    opts.child_id = child.pk
    opts.grandchild_id = grandchild.pk


@th.django_unit_test("Group rejects self-parent and descendant-parent writes")
def test_group_rejects_cycle_writes(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Group

    root = Group.objects.get(pk=opts.root_id)
    root.parent = root
    try:
        root.save(update_fields=["parent", "modified"])
    except merrors.ValueException:
        pass
    else:
        assert False, "saving a Group as its own parent must fail"

    root.refresh_from_db()
    root.parent_id = opts.grandchild_id
    try:
        root.save(update_fields=["parent", "modified"])
    except merrors.ValueException:
        pass
    else:
        assert False, "saving a Group beneath its descendant must fail"


@th.django_unit_test("corrupt Group ancestry fails closed without looping")
def test_corrupt_group_cycle_fails_closed(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Group
    from mojo.apps.account.services import group_hierarchy

    Group.objects.filter(pk=opts.root_id).update(parent_id=opts.grandchild_id)
    child = Group.objects.get(pk=opts.child_id)
    try:
        group_hierarchy.ancestors(child, include_self=True)
    except merrors.ValueException:
        pass
    else:
        assert False, "a pre-existing corrupt ancestry cycle must raise instead of looping"
    assert child.is_effectively_active() is False, \
        "authorization-facing active checks must deny a corrupt hierarchy"
