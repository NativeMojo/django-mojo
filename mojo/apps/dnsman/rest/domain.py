import mojo.decorators as md
from mojo.apps.dnsman.models import Domain


@md.URL('domain')
@md.URL('domain/<int:pk>')
@md.uses_model_security(Domain)
def on_domain(request, pk=None):
    """
    Domain list / detail / update.

    CAN_CREATE is False on the model: rows are created only by the registrar
    and onboarding services, so there is no create route here. Saves can reach
    only auto_renew / privacy / credential / metadata (NO_SAVE_FIELDS), and the
    model's on_rest_saved pushes registrar-visible changes upstream.
    """
    return Domain.on_rest_request(request, pk)
