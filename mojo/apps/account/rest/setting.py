from mojo import decorators as md
from mojo.apps.account.models.setting import Setting


@md.URL("settings")
@md.URL("settings/<int:pk>")
@md.uses_model_security(Setting)
def on_rest_request(request, pk=None):
    return Setting.on_rest_request(request, pk)
