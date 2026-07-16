"""Single source of truth for the User/Group disable lifecycle.

All writes to `metadata.protected.disable.*` and the paired `is_active` flip
go through this module. Callers: REST POST_SAVE_ACTIONS, the inactive sweep,
and `pii_anonymize`.

Schema for `metadata.protected.disable`:

    {
      "reason": "admin|abuse|archived|inactive|anonymized|self|None",
      "at": "<iso>",
      "by_user_id": <int|None>,
      "by_username": "<str>",
      "note": "<str|None>",
      "exempt_from_auto_disable": <bool>,
      "warning": {
        "sent_at": "<iso>",
        "days_until_disable_at_send": <int|None>
      },
      "history": [
        {
          "at": "<iso>", "reason": ..., "by_user_id": ..., "by_username": ..., "note": ...,
          "reactivated_at": "<iso|None>", "reactivated_by_user_id": ...,
          "reactivated_by_username": ..., "reactivated_note": ...
        }
      ]
    }

History is FIFO-capped at HISTORY_CAP. Long-term audit lives in incident events
and `logit.Log`, not on the user record.
"""
import uuid

from mojo.helpers import dates, logit
from mojo import errors as merrors


HISTORY_CAP = 20

USER_REST_REASONS = frozenset({"admin", "abuse"})
GROUP_REST_REASONS = frozenset({"admin", "abuse", "archived"})


def _now_iso():
    return dates.utcnow().isoformat()


def _ensure_dict(value):
    if not isinstance(value, dict):
        return {}
    return dict(value)


def _by_fields(by_user):
    if by_user is None or not getattr(by_user, "pk", None):
        return None, "system"
    username = getattr(by_user, "username", None) or "system"
    return by_user.pk, username


def _category_prefix(entity):
    name = type(entity).__name__.lower()
    if name == "user":
        return "account"
    return name


def _trim_history(history):
    if len(history) > HISTORY_CAP:
        return history[-HISTORY_CAP:]
    return history


def _read_disable(entity):
    """Return a deep-copied disable block from entity.metadata."""
    meta = _ensure_dict(entity.metadata)
    protected = _ensure_dict(meta.get("protected"))
    return _ensure_dict(protected.get("disable"))


def _write_metadata(entity, new_metadata, *, atomic_with_active=None, extra_updates=None):
    """Persist new_metadata. If atomic_with_active is True/False, also flip
    is_active atomically and return the row count touched. extra_updates are
    additional column values applied in the SAME atomic UPDATE (used by
    disable_entity to rotate a User's auth_key with the is_active flip).

    When atomic_with_active is None, the caller will save() entity.metadata
    themselves (used by record_anonymize, where pii_anonymize controls the save).
    """
    Model = type(entity)
    if atomic_with_active is None:
        entity.metadata = new_metadata
        entity.save(update_fields=["metadata", "modified"])
        return 1
    # Atomic flip + metadata write together.
    target_state = bool(atomic_with_active)
    current_state = not target_state
    return Model.objects.filter(
        pk=entity.pk, is_active=current_state,
    ).update(is_active=target_state, metadata=new_metadata, **(extra_updates or {}))


def disconnect_realtime(entity):
    """Best-effort: force-close a disabled/revoked User's live websockets
    (cross-process via the realtime pub/sub disconnect channel). WS auth
    happens once at connect, so without this a disabled user's sockets would
    live until they drop naturally. The auth_key rotation is the guarantee;
    the socket drop is hygiene — a Redis/realtime failure must never make a
    disable or revoke fail."""
    if type(entity).__name__ != "User":
        return
    try:
        from mojo.apps.realtime import manager
        manager.disconnect_user("user", entity.pk)
    except Exception:
        logit.warning(f"disable_service: realtime disconnect failed for user pk={entity.pk}")


def disable_entity(entity, *, reason, by_user=None, note=None, request=None):
    """Disable a User or Group.

    Atomic. Raises ValueException if the entity is already disabled.
    Clears any pending warning. Emits logit + incident event.
    """
    Model = type(entity)
    by_user_id, by_username = _by_fields(by_user)
    now_iso = _now_iso()

    # Re-read to avoid clobbering concurrent metadata writes.
    fresh = Model.objects.filter(pk=entity.pk).only("metadata", "is_active").first()
    if fresh is None:
        raise merrors.ValueException(f"{Model.__name__} not found")
    if not fresh.is_active:
        raise merrors.ValueException(f"{Model.__name__} is already disabled")

    meta = _ensure_dict(fresh.metadata)
    protected = _ensure_dict(meta.get("protected"))
    disable_block = _ensure_dict(protected.get("disable"))

    disable_block.update({
        "reason": reason,
        "at": now_iso,
        "by_user_id": by_user_id,
        "by_username": by_username,
        "note": note,
    })
    disable_block.pop("warning", None)

    protected["disable"] = disable_block
    meta["protected"] = protected

    # DM-042 kill switch: for Users, rotate auth_key in the SAME atomic UPDATE
    # as the is_active flip. Every outstanding JWT fails its signature check on
    # the next request, and a later reactivation does NOT resurrect tokens
    # minted before the disable — the holder must re-authenticate.
    extra_updates = None
    if hasattr(entity, "auth_key"):
        extra_updates = {"auth_key": uuid.uuid4().hex}

    updated = _write_metadata(entity, meta, atomic_with_active=False, extra_updates=extra_updates)
    if not updated:
        raise merrors.ValueException(f"{Model.__name__} is already disabled")

    entity.refresh_from_db()
    disconnect_realtime(entity)

    prefix = _category_prefix(entity)
    kind = "auto_disabled" if reason == "inactive" else "disabled"
    label = _entity_label(entity)
    msg = f"{Model.__name__} {label} disabled (reason={reason}, by={by_username})"
    if note:
        msg += f", note={note}"
    entity.model_logit(request, msg, kind=kind, level="warn")

    _emit_incident(
        entity,
        details=msg,
        title=f"Disabled: {label}",
        category=f"{prefix}:{kind}",
        level=4,
        request=request,
    )
    return entity


def reactivate_entity(entity, *, by_user=None, note=None, request=None):
    """Reactivate a disabled entity.

    Pushes the live disable block to `history` with `reactivated_*` fields.
    Atomic. Raises ValueException if already active.
    """
    Model = type(entity)
    by_user_id, by_username = _by_fields(by_user)
    now_iso = _now_iso()

    fresh = Model.objects.filter(pk=entity.pk).only("metadata", "is_active").first()
    if fresh is None:
        raise merrors.ValueException(f"{Model.__name__} not found")
    if fresh.is_active:
        raise merrors.ValueException(f"{Model.__name__} is already active")

    meta = _ensure_dict(fresh.metadata)
    protected = _ensure_dict(meta.get("protected"))
    disable_block = _ensure_dict(protected.get("disable"))

    history_entry = {
        "at": disable_block.get("at"),
        "reason": disable_block.get("reason"),
        "by_user_id": disable_block.get("by_user_id"),
        "by_username": disable_block.get("by_username"),
        "note": disable_block.get("note"),
        "reactivated_at": now_iso,
        "reactivated_by_user_id": by_user_id,
        "reactivated_by_username": by_username,
        "reactivated_note": note,
    }
    history = list(disable_block.get("history") or [])
    history.append(history_entry)
    history = _trim_history(history)

    new_block = {"history": history}
    if "exempt_from_auto_disable" in disable_block:
        new_block["exempt_from_auto_disable"] = disable_block["exempt_from_auto_disable"]

    protected["disable"] = new_block
    meta["protected"] = protected

    updated = _write_metadata(entity, meta, atomic_with_active=True)
    if not updated:
        raise merrors.ValueException(f"{Model.__name__} is already active")

    entity.refresh_from_db()

    prefix = _category_prefix(entity)
    label = _entity_label(entity)
    msg = f"{Model.__name__} {label} reactivated (by={by_username})"
    if note:
        msg += f", note={note}"
    entity.model_logit(request, msg, kind="reactivated", level="info")

    _emit_incident(
        entity,
        details=msg,
        title=f"Reactivated: {label}",
        category=f"{prefix}:reactivated",
        level=2,
        request=request,
    )
    return entity


def record_anonymize(entity, *, by_user=None, request=None):
    """Record a permanent anonymization in the disable namespace.

    Pushes any prior live disable block to history with `reactivated_at=null`,
    then writes a fresh metadata dict containing only the disable namespace
    (wiping any other PII-bearing metadata keys). Caller is responsible for
    `is_active=False` and the actual save.
    """
    by_user_id, by_username = _by_fields(by_user)
    now_iso = _now_iso()

    existing_meta = _ensure_dict(entity.metadata)
    existing_protected = _ensure_dict(existing_meta.get("protected"))
    existing_disable = _ensure_dict(existing_protected.get("disable"))

    history = list(existing_disable.get("history") or [])
    if existing_disable.get("reason"):
        history.append({
            "at": existing_disable.get("at"),
            "reason": existing_disable.get("reason"),
            "by_user_id": existing_disable.get("by_user_id"),
            "by_username": existing_disable.get("by_username"),
            "note": existing_disable.get("note"),
            "reactivated_at": None,
            "reactivated_by_user_id": None,
            "reactivated_by_username": None,
            "reactivated_note": "Anonymized; not reactivated",
        })
        history = _trim_history(history)

    entity.metadata = {
        "protected": {
            "disable": {
                "reason": "anonymized",
                "at": now_iso,
                "by_user_id": by_user_id,
                "by_username": by_username,
                "note": None,
                "history": history,
            }
        }
    }


def mark_warning(entity, *, days_until_disable):
    """Set the inactivity warning marker. Clears legacy keys to prevent drift."""
    meta = _ensure_dict(entity.metadata)
    protected = _ensure_dict(meta.get("protected"))
    disable_block = _ensure_dict(protected.get("disable"))

    disable_block["warning"] = {
        "sent_at": _now_iso(),
        "days_until_disable_at_send": int(days_until_disable) if days_until_disable is not None else None,
    }
    protected["disable"] = disable_block
    protected.pop("disable_warned", None)
    protected.pop("disable_warn_date", None)
    meta["protected"] = protected

    entity.metadata = meta
    entity.save(update_fields=["metadata", "modified"])


def clear_warning(entity):
    """Clear both new and legacy warning markers. Returns True if anything changed."""
    meta = _ensure_dict(entity.metadata)
    protected = _ensure_dict(meta.get("protected"))
    disable_block = _ensure_dict(protected.get("disable"))

    changed = False
    if "warning" in disable_block:
        del disable_block["warning"]
        protected["disable"] = disable_block
        changed = True
    if "disable_warned" in protected:
        del protected["disable_warned"]
        changed = True
    if "disable_warn_date" in protected:
        del protected["disable_warn_date"]
        changed = True

    if changed:
        meta["protected"] = protected
        entity.metadata = meta
        entity.save(update_fields=["metadata", "modified"])
    return changed


def is_exempt(entity):
    """True if entity is exempt from auto-disable (new or legacy flag)."""
    meta = _ensure_dict(entity.metadata)
    protected = _ensure_dict(meta.get("protected"))
    if protected.get("no_disable") is True:
        return True
    disable_block = _ensure_dict(protected.get("disable"))
    return disable_block.get("exempt_from_auto_disable") is True


def get_warning_sent_at(entity):
    """Return the warning timestamp string from the new or legacy shape, or None."""
    meta = _ensure_dict(entity.metadata)
    protected = _ensure_dict(meta.get("protected"))
    disable_block = _ensure_dict(protected.get("disable"))
    warning = _ensure_dict(disable_block.get("warning"))
    sent_at = warning.get("sent_at")
    if sent_at:
        return sent_at
    if protected.get("disable_warned") and protected.get("disable_warn_date"):
        return protected.get("disable_warn_date")
    return None


def has_warning(entity):
    """True if entity has an active warning marker (new or legacy)."""
    return get_warning_sent_at(entity) is not None


def migrate_legacy(entity):
    """Idempotently rewrite legacy keys into the new disable.* namespace.

    Leaves legacy keys in place — a follow-up release will remove them.
    No-op if the new namespace is already populated. Returns True if changed.
    """
    meta = _ensure_dict(entity.metadata)
    protected = _ensure_dict(meta.get("protected"))
    if not protected:
        return False

    disable_block = _ensure_dict(protected.get("disable"))
    already_migrated = (
        disable_block.get("warning")
        or "exempt_from_auto_disable" in disable_block
        or disable_block.get("reason")
    )
    if already_migrated:
        return False

    has_legacy = (
        protected.get("disable_warned") is not None
        or protected.get("disable_warn_date") is not None
        or protected.get("no_disable") is not None
    )
    if not has_legacy:
        return False

    if protected.get("no_disable") is True:
        disable_block["exempt_from_auto_disable"] = True
    if protected.get("disable_warned") is True:
        disable_block["warning"] = {
            "sent_at": protected.get("disable_warn_date"),
            "days_until_disable_at_send": None,
        }

    protected["disable"] = disable_block
    meta["protected"] = protected
    entity.metadata = meta
    entity.save(update_fields=["metadata", "modified"])
    return True


def _entity_label(entity):
    Model = type(entity)
    if Model.__name__ == "User":
        return f"{getattr(entity, 'username', '?')} (id={entity.pk})"
    if Model.__name__ == "Group":
        return f"{getattr(entity, 'name', '?')} (id={entity.pk})"
    return f"id={entity.pk}"


def _emit_incident(entity, *, details, title, category, level, request=None):
    """Best-effort incident event emission. Failures are logged but never raised."""
    try:
        from mojo.apps.incident import report_event
        Model = type(entity)
        kwargs = dict(
            details=details,
            title=title,
            category=category,
            level=level,
            model_name=Model.get_model_string(),
            model_id=entity.pk,
        )
        if Model.__name__ == "User":
            kwargs["uid"] = entity.pk
        if request is not None:
            kwargs["request"] = request
        report_event(**kwargs)
    except Exception:
        logit.error(
            f"disable_service: failed to emit incident event for "
            f"{type(entity).__name__} pk={entity.pk}",
            exc_info=True,
        )
