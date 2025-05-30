from mojo import decorators as jd
from mojo.apps.account.models import Group, GroupMember


@jd.URL('group')
@jd.URL('group/<int:pk>')
def on_group(request, pk=None):
    return Group.on_rest_request(request, pk)


@jd.URL('group/member')
@jd.URL('group/member/<int:pk>')
def on_group_member(request, pk=None):
    return GroupMember.on_rest_request(request, pk)
