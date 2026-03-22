"""
Admin REST endpoints for bouncer data.
Provides CRUD for BotSignature and read-only access to BouncerDevice and BouncerSignal.
Operator portal gets full management via these standard REST endpoints.
"""
from mojo import decorators as md


@md.URL('account/bouncer/device')
@md.URL('account/bouncer/device/<int:pk>')
@md.uses_model_security(None)
def on_bouncer_device(request, pk=None):
    from mojo.apps.account.models.bouncer_device import BouncerDevice
    return BouncerDevice.on_rest_request(request, pk)


@md.URL('account/bouncer/signal')
@md.URL('account/bouncer/signal/<int:pk>')
@md.uses_model_security(None)
def on_bouncer_signal(request, pk=None):
    from mojo.apps.account.models.bouncer_signal import BouncerSignal
    return BouncerSignal.on_rest_request(request, pk)


@md.URL('account/bouncer/signature')
@md.URL('account/bouncer/signature/<int:pk>')
@md.uses_model_security(None)
def on_bot_signature(request, pk=None):
    from mojo.apps.account.models.bot_signature import BotSignature
    return BotSignature.on_rest_request(request, pk)
