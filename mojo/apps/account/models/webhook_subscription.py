from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models
from mojo.models import MojoModel
from mojo import errors as merrors


class WebhookSubscription(models.Model, MojoModel):
    """A Group-scoped subscription to a stream of webhook events.

    One row per `(Group, receiver URL)` pair. `events` is a free-form list of
    event-name strings; the framework imposes no vocabulary. When a SaaS calls
    `services.webhooks.dispatch(group, event_type, data)`, an async fan-out job
    queues one signed `publish_webhook` per active row whose `events` list
    contains `event_type`. Signing uses the Group's existing webhook secret —
    rotation rolls every subscription at once.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.ForeignKey(
        "account.Group", related_name="webhook_subscriptions",
        on_delete=models.CASCADE,
    )
    url = models.URLField()
    events = models.JSONField(default=list)
    is_active = models.BooleanField(default=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created"]

    class RestMeta:
        VIEW_PERMS = ["manage_group", "manage_groups", "groups"]
        SAVE_PERMS = ["manage_group", "manage_groups", "groups"]
        DELETE_PERMS = ["manage_group", "manage_groups", "groups"]
        CAN_DELETE = True
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "modified",
                    "url", "events", "is_active",
                ],
                "graphs": {
                    "group": "basic",
                },
            },
            "detail": {
                "fields": [
                    "id", "created", "modified",
                    "url", "events", "is_active", "metadata",
                ],
                "graphs": {
                    "group": "basic",
                },
            },
        }

    def __str__(self):
        return f"WebhookSubscription({self.group_id} -> {self.url})"

    def on_rest_pre_save(self, changed_fields, created):
        """Validate URL (https-only, syntactically valid) and events shape
        before the row is written. Fails closed with a clear ValueException
        the REST layer turns into a 400.
        """
        url = self.url or ""
        if not url.startswith("https://"):
            raise merrors.ValueException(
                "url must start with https:// (http and other schemes are not allowed)"
            )
        try:
            URLValidator(schemes=["https"])(url)
        except ValidationError as e:
            raise merrors.ValueException(f"url is not a valid URL: {e.messages[0] if e.messages else url!r}")
        # Reject URLs that embed credentials in the userinfo component
        # (e.g. https://user:pass@host/). The bare https:// prefix check above
        # would otherwise allow these through, and the credentials would be
        # logged in job metadata and outbound request lines.
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            raise merrors.ValueException(
                "url must not include credentials (user:pass@) — strip the userinfo component"
            )

        events = self.events
        if events is None:
            events = []
            self.events = events
        if not isinstance(events, list):
            raise merrors.ValueException(
                f"events must be a list of strings, got {type(events).__name__}"
            )
        for entry in events:
            if not isinstance(entry, str) or not entry:
                raise merrors.ValueException(
                    f"events entries must be non-empty strings, got {entry!r}"
                )
