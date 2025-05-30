from mojo import decorators as jd
from mojo.metrics import redis_metrics as metrics

@jd.POST('record')
def on_record(request, pk=None):
    # TODO add permission check based on category
    metrics.record()


@jd.URL('group/member')
@jd.URL('group/member/<int:pk>')
def on_group_member(request, pk=None):
    return GroupMember.on_rest_request(request, pk)
