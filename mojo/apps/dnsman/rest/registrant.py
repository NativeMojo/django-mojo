"""
Read and write the ICANN registrant contact used to register domains.

Two scopes, one path. `?group=<id>` addresses that group's own contact; no
group addresses the HOUSE contact — the operator's own personal data, and the
registrant of record for every tenant that has not set one. They are gated
differently on purpose:

- **group scope** — `manage_dns` on that group. A tenant admin holds it on a
  GroupMember row, which is reachable only by naming their own group, so they
  can edit their contact and never see the house one.
- **global scope** — `require_platform_admin` on top of the model check. The
  model check alone would not do it: with no `?group=` it falls through to the
  caller's GLOBAL permission dict, and the house contact is not tenant data.

Reads are gated at WRITE level (`SAVE_PERMS`) deliberately. The payload is the
registrant's legal name, street address, phone number and email — PII — so
`view_dns`, which exists for read-only DNS visibility, must not reach it. Note
`get_rest_meta_prop` returns the FIRST non-None key rather than a union, so
`["SAVE_PERMS", "VIEW_PERMS"]` resolves to `SAVE_PERMS` = manage_dns/security.

What this endpoint never does is report on a contact the caller cannot edit.
`contact`, `source` and `problems` describe THIS SCOPE'S OWN ROW; a group with
nothing of its own gets nulls plus `inherited: true`. Reporting which fields of
an inherited contact are malformed would tell a tenant about the house one.
"""

import mojo.decorators as md
import mojo.errors as me
from mojo.apps.dnsman.models import Domain
from mojo.apps.dnsman.rest.gates import require_platform_admin
from mojo.apps.dnsman.services import registrar


def _authorize(request):
    """
    The gate for both verbs, in an order that matters.

    AUTHENTICATION FIRST. The group-resolution guard below answers differently
    for a group that exists and is active (falls through, eventually 401) than
    for one that does not (400 naming the reason) — which, reached by an
    anonymous caller, is a free group-id enumeration oracle: anonymous requests
    are exempt from the throttle. The model check that would normally raise the
    401 runs last here, so it cannot be relied on to close this. Every other
    group-scoped endpoint answers 401 to both, and so must this one.

    Then the group-resolution guard. `Group.get_active` returns None for a
    deactivated or typo'd id, silently and by design, so without this a tenant
    whose group was just deactivated would fall into the global branch and be
    refused by the platform gate — a routine tenant mistake reported as a
    platform-boundary denial, in the incident channel that exists to alert on
    real ones. Same guard, same reason as rest/purchase.py's adopt route; BOTH
    keys, because the dispatcher also populates request.group from
    ?group_uuid=.
    """
    if request.user is None or not getattr(request.user, "is_authenticated", False):
        raise me.PermissionDeniedException(
            "Permission denied: unauthenticated",
            code=401, status=401,
            branch="unauthenticated",
            permission_keys=["SAVE_PERMS", "VIEW_PERMS"],
            model_name="Domain",
            event_type="unauthenticated")

    if ("group" in request.DATA or "group_uuid" in request.DATA) and request.group is None:
        raise me.ValueException(
            "The requested group does not exist or is not active — "
            "omit 'group' entirely to address the house registrant contact")

    if request.group is None:
        require_platform_admin(request, "Managing the house registrant contact")

    Domain.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"])
    return request.group


def _payload(group):
    """The same body for GET and POST — what this scope holds, and what is in
    effect for it."""
    own, source = registrar.read_contact(group)
    configured = registrar.contact_configured(group)
    return dict(
        scope="group" if group is not None else "global",
        group=group.pk if group is not None else None,
        contact=own,
        source=source,
        # A contact is in effect here but belongs to a scope above this one:
        # the parent chain, or the house account. Its VALUES stay invisible.
        inherited=bool(
            group is not None and own is None and registrar._resolve_contact(group)),
        effective_configured=configured,
        # This scope's own row only. Never the inherited contact.
        problems=registrar.validate_contact(own) if own is not None else [],
    )


@md.GET('registrant')
def on_registrant_get(request):
    """The contact this scope owns, for editing. PII — manage_dns, not view_dns."""
    group = _authorize(request)
    return _payload(group)


@md.POST('registrant')
def on_registrant_save(request):
    """
    Save or clear this scope's contact. `{"contact": {...}}` or `{"clear": true}`.

    Validation happens before the row is written, so a contact AWS would bounce
    is a readable 400 here rather than a failed registration after money has
    already moved. Clearing the global scope reverts to the deployment's conf
    file when it sets one — the response reports which.
    """
    group = _authorize(request)

    # A form-encoded "false" arrives as a truthy string; bool() on it would
    # wipe the contact the caller was trying to save.
    clear = request.DATA.get("clear", False)
    if isinstance(clear, str):
        clear = clear.strip().lower() in ("1", "true", "yes")
    if clear:
        registrar.clear_contact(group)
        return _payload(group)

    contact = request.DATA.get("contact")
    if not contact:
        raise me.ValueException(
            "missing required parameters: contact (or send clear=true)")
    registrar.save_contact(contact, group)
    return _payload(group)
