"""
GitHub webhook decorator for django-mojo endpoints.

@md.requires_github_webhook()
    Validates the X-Hub-Signature-256 header on incoming GitHub webhooks
    using HMAC-SHA256 with the GITHUB_WEBHOOK_SECRET setting.
    Rejects with 403 if the signature is missing or invalid.

Usage:
    @md.POST("webhook/github")
    @md.public_endpoint()
    @md.requires_github_webhook()
    def on_github_webhook(request):
        event = request.META.get("HTTP_X_GITHUB_EVENT")
        payload = request.DATA
        ...
"""
from functools import wraps

import mojo.errors
from mojo.helpers import logit

logger = logit.get_logger("github_webhook", "github.log")

__all__ = ["requires_github_webhook"]


def requires_github_webhook():
    """Validate the GitHub webhook signature on an incoming request.

    Reads request.body and the X-Hub-Signature-256 header, then verifies
    the HMAC-SHA256 signature using verify_webhook_signature(). Returns
    403 if the signature is missing, invalid, or the secret is not configured.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            from mojo.apps.github.services.github_app import verify_webhook_signature

            signature = request.META.get("HTTP_X_HUB_SIGNATURE_256", "")
            if not verify_webhook_signature(request.body, signature):
                logger.warning(
                    f"github_webhook: invalid signature from {request.ip}"
                )
                raise mojo.errors.PermissionDeniedException(
                    "Invalid webhook signature", 403, 403
                )
            return func(request, *args, **kwargs)
        return wrapper
    return decorator
