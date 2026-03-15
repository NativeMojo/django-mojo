from django.db import models
from mojo.models import MojoModel
from mojo.helpers import dates
from mojo.helpers.settings import settings

NOTIFICATION_DEFAULT_EXPIRY = settings.get("NOTIFICATION_DEFAULT_EXPIRY", 3600)


class Notification(models.Model, MojoModel):
    """
    User inbox notification. Created by Notification.send() which also delivers
    via WebSocket and device push. Expires automatically via cron.

    Usage:
        user.notify("Your order shipped", action_url="/orders/123")
        Notification.send("Maintenance in 10 min", group=group)
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    user = models.ForeignKey(
        "account.User", on_delete=models.CASCADE, related_name="notifications")
    group = models.ForeignKey(
        "account.Group", on_delete=models.SET_NULL,
        null=True, blank=True, default=None, related_name="notifications")

    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default="")
    kind = models.CharField(max_length=80, default="general", db_index=True)
    data = models.JSONField(default=dict, blank=True)
    action_url = models.CharField(max_length=500, blank=True, null=True, default=None)

    is_unread = models.BooleanField(default=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)

    class Meta:
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["user", "is_unread"]),
            models.Index(fields=["user", "created"]),
        ]

    class RestMeta:
        VIEW_PERMS = ["owner"]
        SAVE_PERMS = ["owner"]
        POST_SAVE_ACTIONS = ["mark_read"]
        LIST_DEFAULT_FILTERS = {"is_unread": True}
        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "title", "body", "kind",
                    "data", "action_url", "is_unread", "expires_at"
                ]
            }
        }

    def __str__(self):
        return f"{self.title} -> {self.user} ({'unread' if self.is_unread else 'read'})"

    def on_action_mark_read(self, value):
        self.is_unread = False
        self.save(update_fields=["is_unread", "modified"])
        return {"status": True}

    @classmethod
    def send(cls, title, body="", user=None, group=None, kind="general",
             data=None, action_url=None, expires_in=NOTIFICATION_DEFAULT_EXPIRY,
             push=True, ws=True):
        """
        Create inbox notification(s) and deliver via WebSocket + device push.

        Args:
            title:      Notification title
            body:       Notification body text
            user:       Target User instance (or None if group send)
            group:      Target Group instance — fans out to all active members
            kind:       Category string for client-side routing (default "general")
            data:       Arbitrary JSON payload
            action_url: Deep-link URL
            expires_in: Seconds until expiry (None = persistent until read)
            push:       Send device push notification
            ws:         Send WebSocket message
        """
        if data is None:
            data = {}

        expires_at = None
        if expires_in is not None:
            expires_at = dates.add(seconds=expires_in)

        # Build recipient list
        users = []
        if user:
            users.append(user)
        if group:
            for ms in group.members.filter(is_active=True).select_related("user"):
                if ms.user_id not in [u.pk for u in users]:
                    users.append(ms.user)

        ws_payload = {
            "type": "notification",
            "title": title,
            "body": body,
            "kind": kind,
            "action_url": action_url,
            "data": data,
        }

        from mojo.apps.account.services.notification_prefs import is_notification_allowed

        notifications = []
        for recipient in users:
            # Check in-app preference before creating inbox notification
            if not is_notification_allowed(recipient, kind, "in_app"):
                # Still attempt push/ws if those channels are allowed
                if push and is_notification_allowed(recipient, kind, "push"):
                    try:
                        recipient.push_notification(
                            title=title, body=body, data=data,
                            category=kind, action_url=action_url,
                        )
                    except Exception:
                        pass
                continue

            notif = cls(
                user=recipient,
                group=group,
                title=title,
                body=body,
                kind=kind,
                data=data,
                action_url=action_url,
                expires_at=expires_at,
            )
            notif.save()
            notifications.append(notif)

            if ws:
                try:
                    from mojo.apps import realtime
                    realtime.send_to_user("user", recipient.pk, ws_payload)
                except Exception:
                    pass

            if push and is_notification_allowed(recipient, kind, "push"):
                try:
                    recipient.push_notification(
                        title=title, body=body, data=data,
                        category=kind, action_url=action_url,
                    )
                except Exception:
                    pass

        return notifications
