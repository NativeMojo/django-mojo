from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.models import Group, GroupMember


@md.URL('group')
@md.URL('group/<int:pk>')
@md.uses_model_security(Group)
def on_group(request, pk=None):
    return Group.on_rest_request(request, pk)


@md.URL('group/member')
@md.URL('group/member/<int:pk>')
@md.uses_model_security(GroupMember)
def on_group_member(request, pk=None):
    return GroupMember.on_rest_request(request, pk)


@md.POST('group/member/invite')
@md.requires_params('email', 'group')
@md.custom_security("securted by group security")
def on_group_invite_member(request):
    if not request.group.user_has_permission(request.user, ["manage_users", "manage_members"]):
        raise merrors.PermissionDeniedException()
    ms = request.group.invite(request.DATA.email)
    if "permissions" in request.DATA:
        ms.on_rest_update_jsonfield("permissions", request.DATA.permissions)
        ms.save()
    return ms.on_rest_get(request)


@md.GET('group/<int:pk>/member')
@md.requires_auth()
def on_group_me_member(request, pk=None):
    request.group = Group.objects.filter(pk=pk).last()
    if request.group is None:
        return Group.rest_error_response(request, 403, error="GET permission denied: Group")
    request.group.touch()
    member = request.group.get_member_for_user(request.user, check_parents=True)
    if member is None:
        return {"status": True, "data": {"id": -1, "permissions": [] }}
    member.touch()
    return member.on_rest_get(request)
