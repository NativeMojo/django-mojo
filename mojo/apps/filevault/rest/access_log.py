import mojo.decorators as md
from mojo.apps.filevault.models import VaultAccessLog


@md.URL('accesslog')
@md.URL('accesslog/<int:pk>')
@md.uses_model_security(VaultAccessLog)
def on_vault_access_log(request, pk=None):
    """Read-only access trail. Creates/updates/deletes are refused by RestMeta."""
    return VaultAccessLog.on_rest_request(request, pk)
