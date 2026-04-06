"""Comms domain tools — send notifications via SMS, email, push, and in-app."""
from mojo.apps.assistant import tool
from mojo.helpers import logit

logger = logit.get_logger(__name__, "assistant.log")

MAX_RECIPIENTS = 100

VALID_CHANNELS = {"sms", "email", "push", "in_app"}

# Exactly one of these keys must be present in the recipients object
RECIPIENT_KEYS = {"usernames", "permission", "group_id", "metadata", "email_domain"}


def _resolve_recipients(recipients):
    """
    Resolve a recipients spec into a list of active User objects.

    Returns (users, errors) where errors is a list of
    {"username": ..., "reason": ...} dicts for users that couldn't be resolved.
    """
    from mojo.apps.account.models import User

    if not recipients or not isinstance(recipients, dict):
        return [], [{"reason": "recipients is required"}]

    # Ensure exactly one resolution strategy
    keys_present = RECIPIENT_KEYS & set(recipients.keys())
    if len(keys_present) != 1:
        return [], [{"reason": f"Provide exactly one of: {', '.join(sorted(RECIPIENT_KEYS))}"}]

    key = keys_present.pop()
    errors = []

    if key == "usernames":
        usernames = recipients["usernames"]
        if not isinstance(usernames, list) or not usernames:
            return [], [{"reason": "usernames must be a non-empty list"}]
        users = []
        for uname in usernames:
            try:
                user = User.objects.get(username=uname)
            except User.DoesNotExist:
                # Try by email as fallback
                try:
                    user = User.objects.get(email__iexact=uname)
                except User.DoesNotExist:
                    errors.append({"username": uname, "reason": "user not found"})
                    continue
            if not user.is_active:
                errors.append({"username": uname, "reason": "user is inactive"})
                continue
            users.append(user)
        return users, errors

    if key == "permission":
        perm = recipients["permission"]
        if not perm or not isinstance(perm, str):
            return [], [{"reason": "permission must be a non-empty string"}]
        # Handle special boolean fields on User
        if perm == "is_superuser":
            users = list(User.objects.filter(is_superuser=True, is_active=True))
        elif perm == "is_staff":
            users = list(User.objects.filter(is_staff=True, is_active=True))
        else:
            # JSONField permissions lookup (PostgreSQL)
            users = list(User.objects.filter(
                permissions__has_key=perm, is_active=True,
            ))
        return users, errors

    if key == "group_id":
        group_id = recipients["group_id"]
        from mojo.apps.account.models import GroupMember
        members = GroupMember.objects.filter(
            group_id=group_id, is_active=True, user__is_active=True,
        ).select_related("user")
        users = [m.user for m in members]
        return users, errors

    if key == "metadata":
        meta_filter = recipients["metadata"]
        if not isinstance(meta_filter, dict) or not meta_filter:
            return [], [{"reason": "metadata must be a non-empty dict"}]
        users = list(User.objects.filter(
            metadata__contains=meta_filter, is_active=True,
        ))
        return users, errors

    if key == "email_domain":
        domain = recipients["email_domain"]
        if not domain or not isinstance(domain, str):
            return [], [{"reason": "email_domain must be a non-empty string"}]
        users = list(User.objects.filter(
            email__iendswith="@" + domain, is_active=True,
        ))
        return users, errors

    return [], [{"reason": f"Unknown recipient key: {key}"}]


def _send_in_app(users, params):
    """Send in-app notifications. Returns (sent, skipped, failed, errors)."""
    from mojo.apps.account.models.notification import Notification
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    title = params.get("title") or params.get("subject") or "Notification"
    body = params.get("body", "")
    kind = params.get("kind", "general")
    action_url = params.get("action_url")
    sent = 0
    skipped = 0
    failed = 0
    errors = []

    for user in users:
        if not is_notification_allowed(user, kind, "in_app"):
            skipped += 1
            errors.append({"user_id": user.pk, "username": user.username, "reason": "opted out of in_app notifications"})
            continue
        try:
            Notification.send(
                title, body, user=user, kind=kind,
                action_url=action_url, push=False, ws=True,
            )
            sent += 1
        except Exception as e:
            failed += 1
            errors.append({"user_id": user.pk, "username": user.username, "reason": str(e)[:200]})

    return sent, skipped, failed, errors


def _send_push(users, params):
    """Send push notifications. Returns (sent, skipped, failed, errors)."""
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    title = params.get("title") or params.get("subject") or "Notification"
    body = params.get("body", "")
    kind = params.get("kind", "general")
    action_url = params.get("action_url")
    sent = 0
    skipped = 0
    failed = 0
    errors = []

    for user in users:
        if not is_notification_allowed(user, kind, "push"):
            skipped += 1
            errors.append({"user_id": user.pk, "username": user.username, "reason": "opted out of push notifications"})
            continue
        try:
            deliveries = user.push_notification(
                title=title, body=body, category=kind, action_url=action_url,
            )
            if deliveries:
                sent += 1
            else:
                skipped += 1
                errors.append({"user_id": user.pk, "username": user.username, "reason": "no registered devices"})
        except Exception as e:
            failed += 1
            errors.append({"user_id": user.pk, "username": user.username, "reason": str(e)[:200]})

    return sent, skipped, failed, errors


def _send_sms(users, params):
    """Send SMS messages. Returns (sent, skipped, failed, errors)."""
    from mojo.apps.phonehub.models.sms import SMS
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    body = params.get("body", "")
    kind = params.get("kind", "general")
    sent = 0
    skipped = 0
    failed = 0
    errors = []

    for user in users:
        if not user.phone_number:
            skipped += 1
            errors.append({"user_id": user.pk, "username": user.username, "reason": "no phone number on file"})
            continue
        if not is_notification_allowed(user, kind, "sms"):
            skipped += 1
            errors.append({"user_id": user.pk, "username": user.username, "reason": "opted out of SMS notifications"})
            continue
        try:
            sms = SMS.send(body, to_number=user.phone_number, user=user)
            if sms.is_failed:
                failed += 1
                errors.append({"user_id": user.pk, "username": user.username, "reason": sms.error_message or "send failed"})
            else:
                sent += 1
        except Exception as e:
            failed += 1
            errors.append({"user_id": user.pk, "username": user.username, "reason": str(e)[:200]})

    return sent, skipped, failed, errors


def _send_email(users, params):
    """Send emails. Returns (sent, skipped, failed, errors)."""
    from mojo.apps.account.services.notification_prefs import is_notification_allowed

    subject = params.get("subject") or params.get("title") or "Notification"
    body = params.get("body", "")
    kind = params.get("kind", "general")
    sent = 0
    skipped = 0
    failed = 0
    errors = []

    for user in users:
        if not user.email:
            skipped += 1
            errors.append({"user_id": user.pk, "username": user.username, "reason": "no email address"})
            continue
        if not is_notification_allowed(user, kind, "email"):
            skipped += 1
            errors.append({"user_id": user.pk, "username": user.username, "reason": "opted out of email notifications"})
            continue
        try:
            result = user.send_email(subject=subject, body_text=body)
            if result is None:
                failed += 1
                errors.append({"user_id": user.pk, "username": user.username, "reason": "no mailbox configured"})
            elif hasattr(result, "status") and result.status == "failed":
                failed += 1
                errors.append({"user_id": user.pk, "username": user.username, "reason": result.status_reason or "send failed"})
            else:
                sent += 1
        except Exception as e:
            failed += 1
            errors.append({"user_id": user.pk, "username": user.username, "reason": str(e)[:200]})

    return sent, skipped, failed, errors


CHANNEL_DISPATCHERS = {
    "in_app": _send_in_app,
    "push": _send_push,
    "sms": _send_sms,
    "email": _send_email,
}


@tool(
    name="send_notification",
    domain="comms",
    permission="comms",
    mutates=True,
    description=(
        "Send a notification to users via SMS, email, push, or in-app. "
        "Specify recipients by usernames, permission, group_id, metadata filter, or email_domain. "
        "IMPORTANT: Always confirm with the user before sending. "
        "Returns a delivery summary with sent/failed/skipped counts."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "enum": ["sms", "email", "push", "in_app"],
                "description": "Delivery channel",
            },
            "body": {
                "type": "string",
                "description": "Message body text",
            },
            "title": {
                "type": "string",
                "description": "Notification title (used by push and in_app)",
            },
            "subject": {
                "type": "string",
                "description": "Email subject (used by email channel)",
            },
            "action_url": {
                "type": "string",
                "description": "Deep-link URL (used by in_app and push)",
            },
            "kind": {
                "type": "string",
                "description": "Notification kind for preference routing (default: general)",
                "default": "general",
            },
            "recipients": {
                "type": "object",
                "description": (
                    "Who to send to. Provide exactly one of: "
                    'usernames (list of usernames/emails), '
                    'permission (permission key like "is_superuser"), '
                    'group_id (integer), '
                    'metadata (dict to match against user metadata), '
                    'email_domain (string like "REDACTED.com")'
                ),
            },
        },
        "required": ["channel", "body", "recipients"],
    },
)
def _tool_send_notification(params, user):
    channel = params.get("channel")
    if channel not in VALID_CHANNELS:
        return {"error": f"Invalid channel: {channel}. Must be one of: {', '.join(sorted(VALID_CHANNELS))}"}

    body = params.get("body")
    if not body:
        return {"error": "body is required"}

    recipients_spec = params.get("recipients")
    users, resolve_errors = _resolve_recipients(recipients_spec)

    if not users and resolve_errors:
        return {
            "error": "No recipients could be resolved",
            "details": resolve_errors,
        }

    if not users:
        return {"error": "No active users matched the recipients filter."}

    if len(users) > MAX_RECIPIENTS:
        return {
            "error": f"Too many recipients ({len(users)}). Maximum is {MAX_RECIPIENTS}.",
            "total_matched": len(users),
        }

    dispatcher = CHANNEL_DISPATCHERS[channel]
    sent, skipped, failed, send_errors = dispatcher(users, params)

    all_errors = resolve_errors + send_errors

    return {
        "sent": sent,
        "skipped": skipped,
        "failed": failed,
        "total_recipients": len(users),
        "errors": all_errors if all_errors else [],
    }
