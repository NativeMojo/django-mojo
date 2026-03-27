from mojo import decorators as md
from mojo.apps.incident.models import IPSet


@md.URL('ipset')
@md.URL('ipset/<int:pk>')
def on_ipset(request, pk=None):
    return IPSet.on_rest_request(request, pk)
