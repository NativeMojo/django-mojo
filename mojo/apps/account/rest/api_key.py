import mojo.decorators as md
from mojo.apps.account.models import ApiKey


@md.URL('group/apikey')
@md.URL('group/apikey/<int:pk>')
@md.uses_model_security(ApiKey)
def on_group_apikey(request, pk=None):
    return ApiKey.on_rest_request(request, pk)
