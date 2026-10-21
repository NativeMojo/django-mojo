"""Thin ORM creation bridge: room -> membership lock order, atomic numbering."""
from django.db import models, transaction
from .read_state import _UNSET


def save_message(message, args, kwargs):
    from ..models import ChatRoom, ChatMessage, ChatMembership
    from .read_state import advance_locked
    handler = kwargs.pop("read_handler", _UNSET)
    publisher = kwargs.pop("read_publisher", None)
    if not message._state.adding:
        from .backend import ordinary_save_fields
        fields = kwargs.get("update_fields")
        protected = {"room", "room_id", "room_seq"}
        if fields is not None and protected.intersection(fields):
            raise ValueError("Message identity changes require chat maintenance")
        if fields is None or {"room", "room_id", "room_seq"}.intersection(fields):
            old = ChatMessage.objects.filter(pk=message.pk).values("room_id", "room_seq").get()
            if old["room_id"] != message.room_id or old["room_seq"] != message.room_seq:
                raise ValueError("Message identity changes require chat maintenance")
        ordinary_save_fields(message, kwargs, protected)
        return models.Model.save(message, *args, **kwargs)
    if message.pk is not None:
        raise ValueError("New cursor messages require a database-assigned primary key")
    if message.room_seq is not None:
        raise ValueError("New message room_seq is assigned by the chat service")
    with transaction.atomic(using=kwargs.get("using")):
        from .locks import lock_users
        lock_users([message.user_id, message.flagged_by_id], using=kwargs.get("using") or "default")
        room = ChatRoom.objects.select_for_update().get(pk=message.room_id)
        if room.read_state_generation == 0:
            room.read_state_generation = 1
        room.message_seq += 1
        ChatRoom.objects.filter(pk=room.pk).update(
            message_seq=room.message_seq, read_state_generation=room.read_state_generation)
        message.room_seq = room.message_seq
        message._chat_read_generation = room.read_state_generation
        models.Model.save(message, *args, **kwargs)
        if message.user_id:
            member = ChatMembership.objects.select_for_update().filter(
                room_id=room.pk, user_id=message.user_id, status__in=["active", "muted"]).first()
            if member:
                advance_locked(member, room, message, reason="own_send", handler=handler, publisher=publisher)
