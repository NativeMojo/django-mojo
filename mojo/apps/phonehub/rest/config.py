"""REST endpoints for phone configuration."""

import mojo.decorators as md
from mojo.apps.phonehub.models import PhoneConfig


@md.URL('config')
@md.URL('config/<int:pk>')
def on_config(request, pk=None):
    return PhoneConfig.on_rest_request(request, pk)
