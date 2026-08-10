"""Replay-safe WebApp ``MOJO_DEPLOY_KEY`` lifecycle operations."""

import uuid

from django.db import transaction

from mojo import errors as me
from mojo.apps.edge.models import WebApp, WebAppKeyOperation


def _operation_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise me.ValueException("operation_id must be a UUID")


def _existing_receipt(operation_id, web_app, action):
    receipt = WebAppKeyOperation.objects.filter(
        operation_id=operation_id,
        web_app=web_app,
    ).select_related("api_key").first()
    if receipt is None:
        return None
    if receipt.action != action:
        raise me.ValueException("operation_id was already used for another operation")
    return receipt


def _key_status(web_app):
    api_key = web_app.api_key
    if api_key is None:
        return {"linked": False, "active": False, "api_key": None}
    return {
        "linked": True,
        "active": bool(api_key.is_active),
        "api_key": api_key.pk,
        "name": api_key.name,
        "created": api_key.created,
        "modified": api_key.modified,
        "last_used": api_key.last_used,
    }


def status(web_app):
    """Return safe metadata only; a deployment token is never read back."""
    current = WebApp.objects.select_related("api_key").get(pk=web_app.pk)
    metadata = _key_status(current)
    latest = WebAppKeyOperation.objects.filter(web_app=current).first()
    metadata["last_action"] = latest.action if latest else None
    metadata["last_operation_at"] = latest.created if latest else None
    return metadata


def _mint_locked(locked, previous=None):
    from mojo.apps.account.models import ApiKey

    api_key, token = ApiKey.create_for_group(
        locked.group,
        f"webapp:{locked.slug}",
        permissions={"release_webapp": True})
    # This specialized credential is truly reveal-once. Authentication needs
    # only token_hash; remove the encrypted raw copy before commit so neither
    # the generic ApiKey token graph nor a later server call can recover it.
    api_key.clear_secrets()
    api_key.save(update_fields=["mojo_secrets", "modified"])

    locked.api_key = api_key
    locked.save(update_fields=["api_key", "modified"])
    if previous is not None:
        previous.is_active = False
        previous.save(update_fields=["is_active", "modified"])
    return api_key, token


@transaction.atomic
def link(web_app, rotate=False):
    """Compatibility service for trusted bootstrap tooling.

    REST callers use :func:`link_once`, which requires an operation receipt.
    """
    # api_key is nullable; joining it here creates an outer join PostgreSQL
    # cannot lock with SELECT FOR UPDATE. Resolve it separately via the FK.
    locked = WebApp.objects.select_for_update().select_related(
        "group").get(pk=web_app.pk)
    previous = locked.api_key
    if previous is not None and not rotate:
        raise me.ValueException(
            "this WebApp already has a deployment key; pass rotate explicitly")
    api_key, token = _mint_locked(locked, previous)
    locked.log(
        f"Release key {'rotated' if previous else 'minted'} for '{locked.slug}'",
        "edge:webapp_key")
    return locked, api_key, token, bool(previous)


@transaction.atomic
def link_once(web_app, action, actor, operation_id):
    """Mint or rotate exactly once for a client-generated operation UUID.

    A replay returns the receipt and current safe status, never the token.
    Losing the first response therefore requires an explicit rotation.
    """
    if action not in (WebAppKeyOperation.ACTION_MINT,
                      WebAppKeyOperation.ACTION_ROTATE):
        raise me.ValueException("action must be mint or rotate")
    operation_id = _operation_uuid(operation_id)
    locked = WebApp.objects.select_for_update().select_related(
        "group").get(pk=web_app.pk)
    receipt = _existing_receipt(operation_id, locked, action)
    if receipt is not None:
        return {
            "replayed": True,
            "operation_id": str(receipt.operation_id),
            "token": None,
            "status": _key_status(locked),
        }

    previous = locked.api_key
    if action == WebAppKeyOperation.ACTION_MINT and previous is not None:
        raise me.ValueException(
            "this WebApp already has a deployment key; choose rotate explicitly")
    if action == WebAppKeyOperation.ACTION_ROTATE and previous is None:
        raise me.ValueException(
            "this WebApp has no deployment key; choose create explicitly")

    api_key, token = _mint_locked(locked, previous)
    receipt = WebAppKeyOperation.objects.create(
        operation_id=operation_id,
        action=action,
        web_app=locked,
        api_key=api_key,
        actor=actor,
    )
    locked.log(
        f"MOJO_DEPLOY_KEY {action} for '{locked.slug}' "
        f"(operation {receipt.operation_id})",
        "edge:webapp_key")
    return {
        "replayed": False,
        "operation_id": str(receipt.operation_id),
        "token": token,
        "status": _key_status(locked),
    }


@transaction.atomic
def revoke_once(web_app, actor, operation_id):
    """Deactivate and unlink a WebApp key exactly once."""
    operation_id = _operation_uuid(operation_id)
    locked = WebApp.objects.select_for_update().select_related(
        "group").get(pk=web_app.pk)
    receipt = _existing_receipt(
        operation_id, locked, WebAppKeyOperation.ACTION_REVOKE)
    if receipt is not None:
        return {
            "replayed": True,
            "operation_id": str(receipt.operation_id),
            "status": _key_status(locked),
        }

    previous = locked.api_key
    if previous is not None:
        previous.is_active = False
        previous.save(update_fields=["is_active", "modified"])
        locked.api_key = None
        locked.save(update_fields=["api_key", "modified"])
    receipt = WebAppKeyOperation.objects.create(
        operation_id=operation_id,
        action=WebAppKeyOperation.ACTION_REVOKE,
        web_app=locked,
        api_key=previous,
        actor=actor,
    )
    locked.log(
        f"MOJO_DEPLOY_KEY revoked for '{locked.slug}' "
        f"(operation {receipt.operation_id})",
        "edge:webapp_key")
    return {
        "replayed": False,
        "operation_id": str(receipt.operation_id),
        "status": _key_status(locked),
    }
