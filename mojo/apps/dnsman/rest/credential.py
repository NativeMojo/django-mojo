import mojo.decorators as md
import mojo.errors as me
from mojo.apps.dnsman.models import DnsCredential
from mojo.apps.dnsman.services import onboarding


@md.URL('credential')
@md.URL('credential/<int:pk>')
@md.uses_model_security(DnsCredential)
def on_credential(request, pk=None):
    """List / detail / update. Creation goes through credential/link so a
    credential is never stored before the provider confirms it works."""
    return DnsCredential.on_rest_request(request, pk)


@md.POST('credential/link')
@md.requires_params("provider", "api_key", "api_secret")
def on_credential_link(request):
    """
    Link (or rotate) a provider credential.

    The working credential IS the proof of control, so it is verified against
    the provider API before anything is persisted. A failed first link stores
    nothing at all.
    """
    DnsCredential.rest_check_permission_or_raise(request, "SAVE_PERMS")

    credential = None
    pk = request.DATA.get("credential", None)
    if pk:
        credential = DnsCredential.get_instance_or_404(pk)
        # Rotation targets an existing row — re-check against THAT row, whose
        # group may differ from the caller's currently selected one.
        DnsCredential.rest_check_permission_or_raise(request, "SAVE_PERMS", credential)

    result = onboarding.link_credential(
        group=request.group,
        provider=request.DATA.get("provider"),
        api_key=request.DATA.get("api_key"),
        api_secret=request.DATA.get("api_secret"),
        name=request.DATA.get("name", None),
        credential=credential,
    )
    return result.on_rest_get(request)
