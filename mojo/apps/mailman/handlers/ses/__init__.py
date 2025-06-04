from .message import on_message, on_confirm_subscription, on_notification, JsonResponse
from .complaint import on_complaint
from .bounce import on_bounce
from mojo.helpers import modules

INBOX_HANDLERS = {
    "Received": on_message,
    "SubscriptionConfirmation": on_confrim_subscription,
    "Complaint": on_complaint,
    "Notification": on_notification,
    "Bounce": on_bounce

}

def handle_request(request):
    msg = request.DATA.get("Message", "")
    handler = INBOX_HANDLERS.get(msg.notificationType, None)
    if handler is not None:
        handler(request, msg)
    else:
        Event = modules.get_model("incidents", "Event")
        if Event is None:
            Event.report(f"no ses mailman handler for: {msg.notificationType}",
                "mailman_ses", raw_message=request.DATA)
    return JsonResponse(dict(status=True))
