from mojo import decorators as md
from mojo.apps.account.models.notification import Notification


@md.URL("account/notification")
@md.URL("account/notification/<int:pk>")
def on_notification(request, pk=None):
    return Notification.on_rest_request(request, pk)
