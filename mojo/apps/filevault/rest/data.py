import mojo.decorators as md
import mojo.errors as me
from mojo.apps.filevault.models import VaultData
from mojo.apps.filevault.services import vault as vault_service


@md.URL('data')
@md.URL('data/<int:pk>')
def on_vault_data(request, pk=None):
    return VaultData.on_rest_request(request, pk)


@md.POST('data/store')
@md.requires_auth()
@md.requires_params("name", "data")
def on_vault_data_store(request):
    """Encrypt and store JSON data."""
    group = request.group
    if not group:
        raise me.ValueException("Group required")

    name = request.DATA.get("name")
    data = request.DATA.get("data")
    password = request.DATA.get("password", None)
    description = request.DATA.get("description", None)
    metadata = request.DATA.get("metadata", {})

    if isinstance(data, str):
        import json
        data = json.loads(data)

    vault_data = vault_service.store_data(
        group=group,
        user=request.user,
        name=name,
        data=data,
        password=password,
        description=description,
        metadata=metadata,
    )
    return vault_data.on_rest_get(request)


@md.POST('data/<int:pk>/retrieve')
@md.requires_auth()
def on_vault_data_retrieve(request, pk=None):
    """Decrypt and return stored JSON data."""
    vault_data = VaultData.objects.filter(pk=pk).first()
    if not vault_data:
        raise me.ValueException("Data not found", code=404)

    password = request.DATA.get("password", None)

    try:
        decrypted = vault_service.retrieve_data(vault_data, password=password)
        return dict(data=decrypted)
    except ValueError as e:
        raise me.ValueException(str(e), code=403)
