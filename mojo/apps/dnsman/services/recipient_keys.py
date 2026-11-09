"""Named protected-purpose recipient issuance; no trust grant or rotation."""
import copy

from django.db import transaction

from mojo import errors as me
from mojo.apps.dnsman.fleet_settings import project_uuid

PURPOSE = "acme_validation_consumer"
PERMISSION = "dnsman_acme_federation"


def _recipient(value, lock=False):
    from mojo.apps.account.models import Group
    from mojo.apps.account.services import group_hierarchy
    try:
        project_uuid(value)
    except ValueError:
        raise me.ValueException("A valid exact recipient project UUID is required") from None
    # UUIDs are supplied explicitly, never inferred from a downstream tenant.
    rows = Group.objects.filter(uuid=value)
    if lock:
        # NO KEY: excludes other writers of the row, not the tenant's inserts.
        rows = group_hierarchy.lock_rows(rows)
    rows = list(rows[:2])
    if len(rows) != 1 or not rows[0].is_effectively_active():
        raise me.ValueException("Recipient project is unavailable", code=404, status=404)
    return rows[0]


def _grant(request, group):
    from mojo.apps.account.models import ApiKey
    return ApiKey(group=group).can_change_permission(PERMISSION, True, request)


def _actor(request, lock=False):
    from mojo.apps.account.services import system_setup, provider_setup
    actor = system_setup.require_request_admin(request)
    actor = provider_setup._superuser(actor, lock=lock)
    current = copy.copy(request)
    current.user = actor
    return current


def lookup(request, payload):
    request = _actor(request)
    if not isinstance(payload, dict) or set(payload) != {"consumer_project_uuid"}:
        raise me.ValueException("Only consumer_project_uuid is accepted")
    group = _recipient(payload["consumer_project_uuid"])
    can_issue = _grant(request, group)
    return {"purpose": PURPOSE, "consumer_project_uuid": group.uuid,
            "consumer_label": group.name, "permission": PERMISSION,
            "can_issue": can_issue,
            "grant_explanation": "Protected platform grant required; issuance does not grant allocation trust."}


def issue(request, payload):
    from mojo.apps.account.models import ApiKey, User
    request = _actor(request)
    fields = {"consumer_project_uuid", "expected_consumer_label", "label", "purpose"}
    if (not isinstance(payload, dict) or set(payload) != fields or payload["purpose"] != PURPOSE
            or not isinstance(payload["label"], str) or not 1 <= len(payload["label"].strip()) <= 200
            or any(ord(c) < 32 for c in payload["label"])
            or not isinstance(payload["expected_consumer_label"], str)):
        raise me.ValueException("Invalid named-purpose recipient request")
    with transaction.atomic():
        User.objects.select_for_update().order_by("pk").first()
        request = _actor(request, lock=True)
        group = _recipient(payload["consumer_project_uuid"], lock=True)
        if group.name != payload["expected_consumer_label"]:
            raise me.ValueException("Recipient project changed; reload before issuing", code=409, status=409)
        if not _grant(request, group):
            raise me.PermissionDeniedException("Protected recipient issuance is not permitted")
        key, token = ApiKey.create_for_group(
            group, payload["label"].strip(), permissions={PERMISSION: True})
        key.log(f"Protected ACME consumer credential issued by administrator {request.user.pk}",
                "api_key:acme_consumer_issued")
    return {"purpose": PURPOSE, "consumer_project_uuid": group.uuid,
            "consumer_label": group.name, "api_key": {"id": key.pk, "name": key.name,
            "is_active": key.is_active, "permission": PERMISSION}, "token": token}
