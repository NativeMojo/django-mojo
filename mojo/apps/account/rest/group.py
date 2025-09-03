from mojo import decorators as md
from mojo.apps.account.models import Group, GroupMember
from mojo.helpers.response import JsonResponse


@md.URL('group')
@md.URL('group/<int:pk>')
def on_group(request, pk=None):
    return Group.on_rest_request(request, pk)


@md.URL('group/member')
@md.URL('group/member/<int:pk>')
def on_group_member(request, pk=None):
    return GroupMember.on_rest_request(request, pk)


@md.POST('group/member/invite')
@md.requires_params('email', 'group')
def on_group_invite_member(request):
    ms = request.group.invite(request.DATA['email'])
    return ms.on_rest_get(request)


@md.GET('group/<int:pk>/member')
def on_group_me_member(request, pk=None):
    request.group = Group.objects.filter(pk=pk).last()
    if request.group is None:
        return Group.rest_error_response(request, 403, error="GET permission denied: Group")
    request.group.touch()
    member = request.group.get_member_for_user(request.user)
    if member is None:
        return Group.rest_error_response(request, 403, error="GET permission denied: Member")
    return member.on_rest_get(request)
