"""
Server-side chat message creation and read visibility.

`visible_messages` is the one read bound: every reader of a room's history --
the REST history endpoint, the unread counter, and the WebSocket handler --
narrows through it, so the join-time cutoff and the disappearing-message TTL
cannot drift apart between callers.

`send_message` is the one creation path for a chat message. It whitelists the
kind, validates and size-bounds the `metadata` payload, applies the room's
policy, persists the row and publishes the room broadcast. The WebSocket
handler is a thin adapter over it, and the server-authored system messages in
`rest/rooms.py` go through it too.

Two INDEPENDENT gates, deliberately not one flag:

  client_authored     -- gates the KIND WHITELIST only.
  enforce_room_policy -- gates RATE LIMIT + ROOM RULES + MODERATION, all
                         applied to `body`.

Both default to the safe value. A server-authored `file` message carries a
user-written caption that must still be moderated and rate limited; a
server-authored join/leave message must not be. Collapsing the two onto one
flag would silently stop moderating captions.

Metadata validation and the byte cap run on EVERY send regardless of either
gate -- they are data integrity, not room policy.
"""
import json
import math
from datetime import timedelta

from django.db import transaction

from mojo.helpers import dates, logit, modules
from mojo.helpers.settings import settings

logger = logit.get_logger("chat", "chat.log")

# Sentinel for the keyword-only test seams. Production callers pass nothing and
# the value is resolved from settings here.
_UNSET = object()

# A client may always author these.
CLIENT_KINDS = frozenset({"text", "image"})
# Client-allowed only when the host registered a validator for the kind.
VALIDATED_KINDS = frozenset({"card"})
# Everything a server-side caller may author. `file` is here because production
# rows already carry it; it is in neither set above, so a client can never
# author one.
SERVER_KINDS = frozenset({"text", "image", "system", "card", "file"})

DEFAULT_METADATA_MAX_BYTES = 4096
MAX_METADATA_DEPTH = 5
MAX_METADATA_KEY_LEN = 64

# Keyed by the dotted path string, not by kind, so correcting a typo'd setting
# takes effect without a restart.
_VALIDATOR_CACHE = {}

UNSUPPORTED_KIND_ERROR = "Unsupported message kind"
VALIDATOR_UNAVAILABLE_ERROR = "This message kind is not available"
VALIDATOR_REJECTED_ERROR = "Invalid metadata for this message kind"


def _error(message, **extra):
    out = {"type": "error", "error": message}
    out.update(extra)
    return out


def _validator_map(validators):
    """The configured kind -> validator map.

    `validators` is a keyword-only test seam with a sentinel default; when it
    is unused the map comes from the `CHAT_KIND_VALIDATORS` setting. Values may
    be dotted path strings (the setting) or callables (the seam).
    """
    if validators is not _UNSET:
        return validators or {}
    configured = settings.get("CHAT_KIND_VALIDATORS", {})
    if not isinstance(configured, dict):
        logger.error("CHAT_KIND_VALIDATORS is not a dict; ignoring it")
        return {}
    return configured


def _validator_entry(kind, validators):
    """Whatever is configured for `kind`, without resolving it."""
    return _validator_map(validators).get(kind)


def _load_validator(entry):
    """Resolve one validator entry. Returns (callable_or_None, failed)."""
    if callable(entry):
        return entry, False

    path = str(entry)
    if path in _VALIDATOR_CACHE:
        cached = _VALIDATOR_CACHE[path]
        return cached, cached is None

    try:
        # Catch Exception, not ImportError: load_function wraps an unresolvable
        # path, but an exception raised INSIDE the target module at import time
        # propagates unwrapped.
        func = modules.load_function(path)
    except Exception as e:
        logger.exception(f"chat: could not load kind validator '{path}': {e}")
        _VALIDATOR_CACHE[path] = None
        return None, True

    if not callable(func):
        logger.error(f"chat: kind validator '{path}' is not callable")
        _VALIDATOR_CACHE[path] = None
        return None, True

    _VALIDATOR_CACHE[path] = func
    return func, False


def _max_bytes(max_bytes):
    if max_bytes is not _UNSET:
        return int(max_bytes)
    return settings.get(
        "CHAT_METADATA_MAX_BYTES", DEFAULT_METADATA_MAX_BYTES, kind="int")


def _check_structure(value, depth):
    """Recursive JSON-shape walk. Returns an error string, or None."""
    if depth > MAX_METADATA_DEPTH:
        return f"metadata is nested deeper than {MAX_METADATA_DEPTH} levels"

    if value is None or isinstance(value, (str, bool, int)):
        # bool before int on purpose: bool is a subclass of int.
        return None

    if isinstance(value, float):
        if not math.isfinite(value):
            # json.dumps happily emits NaN/Infinity, which is invalid JSON and
            # breaks every non-Python consumer.
            return "metadata numbers must be finite"
        return None

    if isinstance(value, (list, tuple)):
        for item in value:
            error = _check_structure(item, depth + 1)
            if error:
                return error
        return None

    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return "metadata keys must be strings"
            if len(key) > MAX_METADATA_KEY_LEN:
                return (f"metadata keys must be {MAX_METADATA_KEY_LEN} "
                        "characters or fewer")
            error = _check_structure(item, depth + 1)
            if error:
                return error
        return None

    return "metadata contains an unsupported value type"


def _check_size(metadata, limit):
    """Byte cap on the compact JSON encoding. Returns an error string or None."""
    try:
        encoded = json.dumps(
            metadata, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return "metadata is not JSON serializable"
    if len(encoded) > limit:
        return f"metadata exceeds the {limit} byte limit"
    return None


def validate_metadata(room, user, kind, metadata, *,
                      validators=_UNSET, max_bytes=_UNSET):
    """Validate one message's metadata payload.

    Returns (validated_dict, None) or (None, error_dict).

    `room` and `user` are needed only to call the kind's validator, whose
    contract is `validator(room, user, metadata) -> metadata`.

    `validators` and `max_bytes` are keyword-only test seams with sentinel
    defaults; production callers pass neither.
    """
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        return None, _error("metadata must be an object")

    limit = _max_bytes(max_bytes)

    error = _check_structure(metadata, 1)
    if error:
        return None, _error(error)
    error = _check_size(metadata, limit)
    if error:
        return None, _error(error)

    entry = _validator_entry(kind, validators)
    if not entry:
        return metadata, None

    validator, failed = _load_validator(entry)
    if failed:
        # Fail closed, and only for this kind: a payload nobody vetted must not
        # persist. `text` has no validator and is unaffected.
        return None, _error(VALIDATOR_UNAVAILABLE_ERROR)

    try:
        validated = validator(room, user, metadata)
    except Exception as e:
        # Never leak the exception text to the client.
        logger.exception(f"chat: {kind} validator rejected a payload: {e}")
        return None, _error(VALIDATOR_REJECTED_ERROR)

    if validated is None:
        validated = {}
    if not isinstance(validated, dict):
        logger.error(f"chat: {kind} validator returned a non-dict payload")
        return None, _error(VALIDATOR_REJECTED_ERROR)

    # A validator may normalize; re-check its output so normalization cannot
    # smuggle past the shape walk or the byte cap.
    error = _check_structure(validated, 1) or _check_size(validated, limit)
    if error:
        logger.error(f"chat: {kind} validator produced invalid metadata: {error}")
        return None, _error(VALIDATOR_REJECTED_ERROR)

    return validated, None


def _kind_allowed(kind, client_authored, validators):
    if not client_authored:
        return kind in SERVER_KINDS
    if kind in CLIENT_KINDS:
        return True
    if kind in VALIDATED_KINDS:
        return bool(_validator_entry(kind, validators))
    return False


def send_message(room, user, body, kind="text", metadata=None, *,
                 client_authored=True, enforce_room_policy=True,
                 broadcast=True, broadcast_extra=None,
                 client_key=None, publisher=None,
                 validators=_UNSET, max_bytes=_UNSET):
    """Create, validate, persist and publish one chat message.

    Returns (message, None) on success, (None, error_dict) on refusal.

    client_authored
        Gates the KIND WHITELIST only. True (the default) admits
        CLIENT_KINDS plus any VALIDATED_KINDS with a registered validator.
        False admits SERVER_KINDS.
    enforce_room_policy
        Gates the RATE LIMIT, the room rules and MODERATION, all applied to
        `body`. Defaults True so a caller that forgets it gets the safe
        behavior.
    broadcast
        Publish the `chat_message` frame on the room topic. Server-authored
        callers that publish their own event pass False.
    broadcast_extra
        Merged into the broadcast frame -- how the handler puts `client_key`
        on the wire.
    client_key
        Stored on the row. The idempotency LOOKUP stays in the handler; it is
        client-authored replay protection and is meaningless server-side.
        An IntegrityError from the unique constraint propagates to the caller.
    publisher, validators, max_bytes
        Test seams with sentinel/None defaults; production passes none of them.
    """
    from ..models import ChatMessage
    from ..rules import (
        check_rules, check_moderation, check_rate_limit, check_payload_rules)
    from mojo.apps.realtime import publish_topic

    publish = publisher or publish_topic
    body = (body or "").strip()

    # 1. Kind whitelist
    if not _kind_allowed(kind, client_authored, validators):
        return None, _error(UNSUPPORTED_KIND_ERROR)

    # 2. Metadata validation -- unconditional on both gates.
    metadata, error = validate_metadata(
        room, user, kind, metadata, validators=validators, max_bytes=max_bytes)
    if error:
        return None, error

    # 3. Body rule. `text` keeps today's behavior exactly; every other kind is
    # satisfied by a body OR non-empty validated metadata.
    if kind == "text" and not body:
        return None, _error("body is required")
    if not body and not metadata:
        return None, _error("body or metadata is required")

    decision = "allow"
    if enforce_room_policy:
        # 4. Rate limit
        if not check_rate_limit(room, user):
            return None, _error("Rate limit exceeded")

        # 5. Room rules
        rule_errors = check_rules(room, body, kind)
        if rule_errors:
            return None, _error(rule_errors[0])

        # 6. Card payload rules -- client-authored sends only. This exists to
        # stop a CLIENT smuggling a link past a room owner who set
        # allow_urls=false; a server-derived reference is not that.
        if client_authored:
            payload_errors = check_payload_rules(room, metadata)
            if payload_errors:
                return None, _error(payload_errors[0])

        # 7. Moderation -- `body` is the moderated surface. The classifier is
        # deliberately NOT run over payloads: ids and slugs produce false
        # positives with no recourse.
        decision, reasons = check_moderation(body)
        if decision == "block":
            return None, _error("Message blocked by moderation", reasons=reasons)

    # 8. Persist
    with transaction.atomic():
        msg = ChatMessage.objects.create(
            room=room,
            user=user,
            body=body,
            kind=kind,
            metadata=metadata,
            moderation_decision=decision,
            client_key=client_key,
        )

    # 9. Publish
    if broadcast:
        msg_data = {
            "type": "chat_message",
            "message_id": msg.pk,
            "room_id": room.pk,
            "user_id": getattr(user, "pk", None),
            "body": body,
            "kind": kind,
            "metadata": metadata,
            "created": msg.created.isoformat(),
        }
        if decision == "warn":
            msg_data["moderation_decision"] = "warn"
        if broadcast_extra:
            msg_data.update(broadcast_extra)
        publish(room.topic, msg_data)

    # 10. Bump the room so room lists reorder
    room.save(update_fields=["modified"])

    return msg, None


# ---------------------------------------------------------------------------
# Read visibility
# ---------------------------------------------------------------------------


def visible_messages(room, membership):
    """Messages in `room` that `membership` is allowed to see.

    Three bounds, all AND-ed: unflagged, the join-time history cutoff for
    invite-based rooms, and the room's disappearing-message TTL. `membership`
    is None only on the manage_chat moderator read path -- a moderator
    reviewing a room they never joined has no joined_at to be bound by.
    Ordering, cursor and limit stay with the caller.

    The cutoff is excluded for `channel` rather than allowlisted for
    `direct`/`group` on purpose. `ChatRoom.kind` is a caller-settable
    CharField (`choices` is never enforced and `CREATE_PERMS` is
    `["authenticated"]`), so an allowlist would fail OPEN on any kind nobody
    reasoned about -- a room created with an arbitrary kind would hand a
    re-added member the full history they missed. The exclusion fails closed.

    `__gte`, not `__gt`: a founding participant whose membership and whose
    first message land in the same microsecond keeps that message.
    """
    from ..models import ChatMessage

    qs = ChatMessage.objects.filter(room=room, is_flagged=False)
    if membership and room.kind != "channel":
        qs = qs.filter(created__gte=membership.joined_at)
    ttl = room.get_rule("disappearing_ttl", 0)
    if ttl:
        qs = qs.filter(created__gte=dates.utcnow() - timedelta(seconds=ttl))
    return qs
