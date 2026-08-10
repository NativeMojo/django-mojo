"""Single fail-closed owner for account.Group ancestor traversal."""

from mojo import errors as merrors


MAX_ANCESTOR_DEPTH = 32


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
            current = Group.objects.select_for_update().filter(pk=current_id).first()
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
