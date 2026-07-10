from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.models import Group, GroupMember


@md.URL('group')
@md.URL('group/<int:pk>')
@md.uses_model_security(Group)
def on_group(request, pk=None):
    return Group.on_rest_request(request, pk)


@md.GET('group/uuid/<str:uuid>')
@md.uses_model_security(Group)
def on_group_by_uuid(request, uuid=None):
    """Look up a Group by its uuid and delegate to the standard REST detail
    pipeline. Permission gating is identical to `GET /api/group/<int:pk>` —
    the same RestMeta VIEW_PERMS check runs inside on_rest_request.

    Returns 404 when no group matches the uuid (matches the framework's
    standard not-found behavior for detail lookups).
    """
    group = Group.objects.filter(uuid=(uuid or "").strip()).first()
    if group is None:
        raise merrors.PermissionDeniedException("Group not found", 404, 404)
    return Group.on_rest_request(request, group.pk)


@md.URL('group/member')
@md.URL('group/member/<int:pk>')
@md.uses_model_security(GroupMember)
def on_group_member(request, pk=None):
    return GroupMember.on_rest_request(request, pk)


@md.POST('group/member/invite')
@md.requires_auth()
@md.requires_params('email', 'group')
@md.custom_security("securted by group security")
def on_group_invite_member(request):
    # An unauthenticated caller is rejected by @md.requires_auth() above (clean
    # 403) before reaching here. Guard a missing/unresolved group too: the
    # dispatcher resolves a client-supplied `group` via Group.get_active(), so an
    # inactive or nonexistent id leaves request.group None — fail closed with a
    # generic 403 (no inactive-vs-nonexistent oracle) rather than AttributeError.
    if request.group is None:
        raise merrors.PermissionDeniedException(
            reason="permission denied: Group",
            model_name="Group",
            branch="group_invite_unknown_group",
            event_type="user_permission_denied",
        )
    perms = ["manage_users", "manage_members", "manage_group", "manage_groups"]
    if not request.group.user_has_permission(request.user, perms):
        raise merrors.PermissionDeniedException()
    ms = request.group.invite(request.DATA.email)
    if "permissions" in request.DATA:
        # Route through set_permissions so can_change_permission /
        # MEMBER_PERMS_PROTECTION is enforced on the invite path too — a direct
        # on_rest_update_jsonfield write bypassed it (active_request resolves to
        # this request via the ACTIVE_REQUEST context var; set_permissions saves).
        ms.set_permissions(request.DATA.permissions)
    return ms.on_rest_get(request)


@md.POST('group/webhook_secret')
@md.requires_perms("manage_group", "manage_groups", "groups")
def on_group_webhook_secret(request):
    """Read or rotate the calling Group's webhook signing secret.

    Body shapes:
        {}              -> return the current secret; auto-mint on first call
        {"rotate": true} -> generate a new secret, return it (prior invalidated)

    The Group is taken from request.group (set by the dispatcher when the
    request includes a `group` parameter, or by ApiKey auth). Permission
    `manage_group` (or higher) on the calling user/api-key for that Group is
    required — same threshold as ApiKey CRUD.
    """
    group = getattr(request, "group", None)
    if group is None:
        raise merrors.PermissionDeniedException(
            reason="group required for webhook_secret",
            model_name="Group",
            branch="webhook_secret_missing_group",
            event_type="user_permission_denied",
        )
    if request.DATA.get("rotate") is True:
        info = group.rotate_webhook_secret()
    else:
        info = group.get_webhook_secret_info(auto_create=True)
    return {
        "status": True,
        "data": {
            "secret": info.value,
            "created_at": info.created_at,
            "last_rotated_at": info.last_rotated_at,
        },
    }


@md.GET('group/<int:pk>/member')
@md.requires_auth()
def on_group_me_member(request, pk=None):
    request.group = Group.objects.filter(pk=pk).last()
    if request.group is None:
        raise merrors.PermissionDeniedException(
            reason="GET permission denied: Group",
            model_name="Group",
            branch="group_member_endpoint_unknown_group",
            event_type="user_permission_denied",
        )
    request.group.touch()
    member = request.group.get_member_for_user(request.user, check_parents=True)
    if member is None:
        return {"status": True, "data": {"id": -1, "permissions": [] }}
    member.touch()
    return member.on_rest_get(request)
