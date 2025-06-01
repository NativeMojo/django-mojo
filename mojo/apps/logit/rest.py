from mojo import decorators as md
from mojo.apps.logit.models import Log

APP_NAME = ""

@md.URL('logs')
@md.URL('logs/<int:pk>')
def on_logs(request, pk=None):
    return Log.on_rest_request(request, pk)
