"""Initialize every supported membership creation door at a locked head."""
from django.db import models, transaction
from .backend import read_backend, ordinary_save_fields


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
        rooms = {r.pk: r for r in ChatRoom.objects.using(alias).select_for_update().filter(
            pk__in={m.room_id for m in members}).order_by("pk")}
        for member in members:
            _initialize(member, rooms[member.room_id].message_seq)
        return models.QuerySet.bulk_create(queryset, members, **kwargs)
