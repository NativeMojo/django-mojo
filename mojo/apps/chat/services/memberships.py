"""Initialize every supported membership creation door at a locked head."""
from django.db import models, transaction
from .backend import read_backend, ordinary_save_fields
from .read_state import _UNSET


def _initialize(member, head):
    member.last_read_seq = head
    member.last_read_message_id = None
    member.last_read_at = None
    member.unread_exception_count = 0


def save_membership(member, args, kwargs, *, backend=None):
    if read_backend(backend) != "cursor":
        return models.Model.save(member, *args, **kwargs)
    from ..models import ChatRoom, ChatMembership
    if member._state.adding:
        if member.pk is not None:
            raise ValueError("New cursor memberships require a database-assigned primary key")
        with transaction.atomic(using=kwargs.get("using")):
            from .locks import lock_users
            lock_users([member.user_id], using=kwargs.get("using") or "default")
            room = ChatRoom.objects.select_for_update().get(pk=member.room_id)
            _initialize(member, room.message_seq)
            return models.Model.save(member, *args, **kwargs)
    fields = kwargs.get("update_fields")
    protected = {"room", "room_id", "user", "user_id", "last_read_seq",
                 "last_read_message_id", "last_read_at", "unread_exception_count"}
    if fields is not None and protected.intersection(fields):
        raise ValueError("Service-owned chat fields require chat services")
    if fields is None or {"room", "room_id", "user", "user_id"}.intersection(fields):
        old = ChatMembership.objects.filter(pk=member.pk).values("room_id", "user_id").get()
        if old["room_id"] != member.room_id or old["user_id"] != member.user_id:
            raise ValueError("Membership identity changes require chat maintenance")
    ordinary_save_fields(member, kwargs, protected)
    return models.Model.save(member, *args, **kwargs)


def create_memberships(queryset, members, kwargs, *, backend=None):
    queryset._for_write = True
    alias = queryset.db
    if read_backend(backend) != "cursor":
        return models.QuerySet.bulk_create(queryset, members, **kwargs)

    from ..models import ChatRoom
    if kwargs.get("update_conflicts"):
        raise ValueError("Cursor membership bulk upsert is unsupported")
    with transaction.atomic(using=alias):
        from .locks import lock_users
        lock_users([member.user_id for member in members], using=alias)
        rooms = {r.pk: r for r in ChatRoom.objects.using(alias).select_for_update().filter(
            pk__in={m.room_id for m in members}).order_by("pk")}
        for member in members:
            _initialize(member, rooms[member.room_id].message_seq)
        return models.QuerySet.bulk_create(queryset, members, **kwargs)

def set_join_boundary(membership_ids, joined_at, *, handler=_UNSET, publisher=None, backend=None):
    """Apply a trusted privacy boundary without clearing later unread arrivals.

    This rare operation locks affected rooms, then targeted seats. Tightening
    retains only currently unread, newly entitled extant identities (hidden
    rows included). Relaxing never resurrects old history as unread. Normal
    room numbering/generations and other members are unchanged. Returns the
    number of updated seats. Hosts own the decision to change private history.
    """
    from ..models import ChatMembership, ChatRoom, ChatUnreadException
    from .read_state import unread_queryset, schedule_read_event
    boundary = ChatMembership._meta.get_field("joined_at").to_python(joined_at)
    if boundary is None:
        raise ValueError("A chat join boundary requires a timestamp")
    ids = sorted(set(int(pk) for pk in membership_ids))
    if read_backend(backend) != "cursor":
        return ChatMembership.objects.using("default").filter(pk__in=ids).update(joined_at=boundary)
    # Membership identity is immutable through ordinary cursor writers. Resolve
    # the room roster first, then follow the same room -> seat lock order as sends.
    room_ids = sorted(set(ChatMembership.objects.using("default").filter(
        pk__in=ids).values_list("room_id", flat=True)))
    with transaction.atomic(using="default"):
        rooms = {room.pk: room for room in ChatRoom.objects.using("default").select_for_update().filter(
            pk__in=room_ids).order_by("pk")}
        members = list(ChatMembership.objects.using("default").select_for_update(of=("self",)).filter(
            pk__in=ids).order_by("room_id", "pk"))
        if any(member.room_id not in rooms for member in members):
            raise ValueError("Membership room changed while applying join boundary")
        for member in members:
            room = rooms[member.room_id]
            unread = unread_queryset(member)
            if room.kind != "channel":
                unread = unread.filter(created__gte=max(member.joined_at, boundary))
            retained = list(unread.values_list("pk", "room_seq"))
            ChatUnreadException.objects.using("default").filter(membership_id=member.pk).delete()
            ChatUnreadException.objects.using("default").bulk_create([
                ChatUnreadException(membership_id=member.pk, message_id=pk, room_seq=seq)
                for pk, seq in retained], batch_size=1000)
            read_id, read_at = member.last_read_message_id, member.last_read_at
            if room.kind != "channel" and read_at is not None and read_at < boundary:
                read_id, read_at = None, None
            ChatMembership.objects.using("default").filter(pk=member.pk).update(
                joined_at=boundary, last_read_seq=room.message_seq,
                unread_exception_count=len(retained), last_read_message_id=read_id,
                last_read_at=read_at)
            facts = dict(ack_target_id=None, ack_target_seq=None, ack_target_created=None,
                         ack_generation=room.read_state_generation, reason="join_boundary")
            schedule_read_event(room.pk, member.user_id, facts, True, handler=handler,
                                publisher=publisher, include_inactive=True)
        return len(members)
