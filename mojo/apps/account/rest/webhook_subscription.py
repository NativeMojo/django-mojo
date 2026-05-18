import mojo.decorators as md
from mojo.apps.account.models import WebhookSubscription


@md.URL('group/webhook_subscriptions')
@md.URL('group/webhook_subscriptions/<int:pk>')
@md.uses_model_security(WebhookSubscription)
def on_group_webhook_subscriptions(request, pk=None):
    return WebhookSubscription.on_rest_request(request, pk)
