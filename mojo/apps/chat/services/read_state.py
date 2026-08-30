"""
Read-state resolution for chat.

Two functions, one contract. `resolve_read_target` turns a client-supplied
`up_to_message_id` into the newest message the caller could legitimately have
named at or below it -- or None. `mark_read` writes the receipts and the
`last_read_at` that follow from it. The WebSocket handler (`chat_read`) and
`POST /api/chat/room/read` both narrow through them, so the two paths cannot
drift, and `chat_react` resolves its target through the same bound.

Why this exists: both read paths used to accept any integer at all, write
receipts for messages the caller was never entitled to see, stamp
`last_read_at = utcnow()`, and republish the client's raw number to the room --
so the sender's read indicator was whatever the reader claimed. The REST path
additionally called a bare `int()` on the value and raised `ValueError` out of
the view on anything non-numeric.

**The join bound is applied. The disappearing-message TTL is NOT.** This is the
load-bearing decision in this module, so it is spelled out here rather than
left to be rediscovered:

TTL expiry is monotonic in age, and age is monotonic in pk. If message N has
aged out of the room's TTL window, every message below N has too -- a
TTL-bounded `filter(pk__lte=N)` is therefore *empty*, not "the newest message
still on screen". Resolving through it would discard the read entirely -- no
receipt, no `last_read_at`, no broadcast -- in exactly the race the clamp
exists to survive, and the sender's read indicator would never update for a
message the user genuinely read. `cleanup.run_cleanup` is wired to nothing, so
expired rows persist indefinitely and nothing self-heals.

Visibility belongs on **which id you may name** (the join bound: entitlement,
which does not expire), not on **which messages the acknowledgement covers**.
Use `services.messages.visible_messages` for display; use
`join_bounded_messages` here for entitlement.
"""
from .messages import join_bounded_messages


def resolve_read_target(room, membership, up_to_message_id):
    """Clamp a client-supplied read bound to the newest message the caller
    could legitimately have seen at or below it. Returns a ChatMessage or None.

    None means "nothing to acknowledge": the value was not a usable message id,
    or the caller is entitled to no message at or below it. Callers must write
    nothing and broadcast nothing in that case.
    """
    try:
        up_to = int(up_to_message_id)
    except (TypeError, ValueError):
        # A non-numeric id is a client bug, not a server error. Returning None
        # is what keeps the REST view from raising ValueError into a 500.
        return None
    if up_to < 1:
        return None

    return join_bounded_messages(room, membership).filter(
        pk__lte=up_to,
    ).order_by("-pk").first()


def mark_read(room, user, membership, up_to_message_id):
    """Resolve the target, write the receipts and `last_read_at`.

    Returns the resolved ChatMessage, or None when nothing resolved -- in which
    case nothing at all was written.
    """
    from ..models import ChatReadReceipt

    target = resolve_read_target(room, membership, up_to_message_id)
    if target is None:
        return None

    if room.kind != "channel":
        # Receipts are bounded by pk, `last_read_at` by `created`: `created` is
        # auto_now_add set in Python while pk comes from the database sequence,
        # so under concurrent inserts the two orderings can disagree. The
        # pairing is chosen to match how `GET /api/chat/unread` reads each --
        # it is not a claim that they agree.
        unread_ids = join_bounded_messages(room, membership).filter(
            pk__lte=target.pk,
        ).exclude(
            user=user,  # Don't create receipts for own messages
        ).exclude(
            read_receipts__user=user,  # Skip already-read messages
        ).values_list("pk", flat=True)

        receipts = [
            ChatReadReceipt(message_id=msg_id, user=user)
            for msg_id in unread_ids
        ]
        if receipts:
            ChatReadReceipt.objects.bulk_create(receipts, ignore_conflicts=True)

    # Monotonic on purpose. There is no mark-unread feature, and the channel
    # branch of `GET /api/chat/unread` counts `created__gt=last_read_at`, so
    # rewinding would resurrect already-read messages as unread.
    if membership is not None and (
            membership.last_read_at is None
            or target.created > membership.last_read_at):
        membership.last_read_at = target.created
        membership.save(update_fields=["last_read_at"])

    return target
