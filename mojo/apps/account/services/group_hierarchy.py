"""Single fail-closed owner for account.Group ancestor traversal."""

from mojo import errors as merrors


MAX_ANCESTOR_DEPTH = 32


def lock_rows(queryset):
    """The row lock a Group writer takes: FOR NO KEY UPDATE where the backend
    has it (PostgreSQL), else the backend's plain row lock.

    Every INSERT of a row that references a group takes FOR KEY SHARE on the
    group row (at the insert where the constraint is immediate, at commit for
    Django's deferred foreign keys) and holds it until its transaction ends.
    FOR UPDATE conflicts with that, so a save of a busy tenant's row waits for
    a gap in child inserts and is passed by newer ones; FOR NO KEY UPDATE does
    not conflict with it and still excludes every other writer of the row
    (another save, a delete, a select_for_update with or without no_key). A
    save that leaves the unique columns (uuid, auth_domain) as they are never
    needs the key lock; one that changes them is escalated to it by PostgreSQL
    itself. The alias is resolved the way select_for_update() resolves it, so
    a backend without the NO KEY mode keeps its current lock instead of
    raising NotSupportedError.
    """
    from django.db import connections, router
    alias = queryset._db or router.db_for_write(queryset.model)
    no_key = connections[alias].features.has_select_for_no_key_update
    return queryset.select_for_update(no_key=no_key)


def ancestors(group, include_self=False, max_depth=MAX_ANCESTOR_DEPTH,
              lock=False):
    """Return nearest-first ancestors, raising on cycles or excessive depth."""
    from mojo.apps.account.models.group import Group

    chain = []
    seen = set()
    current = group if include_self else getattr(group, "parent", None)
    while current is not None:
        current_id = getattr(current, "pk", None)
        if current_id is None:
            raise merrors.ValueException("Group hierarchy contains an unsaved parent")
        if current_id in seen:
            raise merrors.ValueException("Group hierarchy contains a cycle")
        if len(chain) >= int(max_depth):
            raise merrors.ValueException("Group hierarchy exceeds the supported depth")
        seen.add(current_id)
        if lock:
            current = lock_rows(Group.objects.filter(pk=current_id)).first()
            if current is None:
                raise merrors.ValueException("Group hierarchy contains a missing parent")
        chain.append(current)
        current = current.parent
    return chain


def validate_parent(group, parent, lock=False):
    """Reject self-parenting, descendant parenting, cycles, and deep chains."""
    if parent is None:
        return
    if group.pk is not None and parent.pk == group.pk:
        raise merrors.ValueException("A group cannot be its own parent")
    chain = ancestors(parent, include_self=True, lock=lock)
    if group.pk is not None and any(item.pk == group.pk for item in chain):
        raise merrors.ValueException("A group cannot be parented beneath its descendant")
