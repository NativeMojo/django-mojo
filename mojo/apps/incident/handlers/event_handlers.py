"""
Event handlers for incident processing.

All handlers are dispatched asynchronously via the job queue. RuleSet.run_handler()
publishes a job per handler spec; the job function loads the event/incident and
executes the handler. This keeps the detection pipeline fast (Event → Rules → Incident)
while offloading all handler work (emails, SMS, fleet broadcasts, etc.) to background jobs.

The async job entry point is `execute_handler(payload)` at the bottom of this module.

Supported handler schemes:
- job://module.function?param1=value1 — publish async job
- email://perm@manage_security — email users with permission (verified only)
- email://protected@incident_emails — email users who opted in via metadata (verified only)
- email://john.doe — email a specific user by username (verified only)
- sms://perm@manage_security — SMS users with permission (verified only)
- sms://protected@incident_sms — SMS users who opted in via metadata (verified only)
- notify://perm@manage_security — in-app + push notification
- block://?ttl=3600 — fleet-wide IP block
- ticket://?status=open&priority=8 — create support ticket

Target resolution (comma-separated, mix and match):
    perm@name       — all active users with that permission
    protected@key   — all active users with metadata.protected.{key} = True
    username        — single user by username

All notification handlers resolve targets to Users.
No notifications are sent to addresses not associated with a User.
"""
from mojo.helpers.settings import settings
from mojo.helpers import logit

logger = logit.get_logger(__name__, "incident.log")

INCIDENT_EMAIL_FROM = settings.get_static("INCIDENT_EMAIL_FROM", None)
ADMIN_PORTAL_URL = settings.get_static("ADMIN_PORTAL_URL", None)


def _resolve_users(targets, require_email=False, require_phone=False):
    """
    Resolve handler targets to a list of User instances.

    Target formats:
        "perm@manage_security"          — all active users with that permission
        "protected@incident_emails"     — all active users with metadata.protected.{key} = True
        "john.doe"                      — single user by username

    Args:
        targets: list of target strings
        require_email: only include users with is_email_verified=True and a non-empty email
        require_phone: only include users with is_phone_verified=True and a phone_number

    Returns:
        list of User instances (deduplicated)
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    users = []
    seen = set()

    def _add_user(user):
        if user.pk not in seen:
            seen.add(user.pk)
            users.append(user)

    def _apply_filters(qs):
        if require_email:
            qs = qs.filter(is_email_verified=True).exclude(email="")
        if require_phone:
            qs = qs.filter(is_phone_verified=True).exclude(phone_number=None)
        return qs

    for target in targets:
        if target.startswith("perm@"):
            # Permission-based: all users with this permission flag
            perm_name = target[5:]
            qs = User.objects.filter(
                is_active=True,
                **{f"permissions__{perm_name}": True},
            )
            for user in _apply_filters(qs):
                _add_user(user)

        elif target.startswith("protected@"):
            # Metadata opt-in: users with metadata.protected.{key} = True
            meta_key = target[10:]
            qs = User.objects.filter(
                is_active=True,
                **{f"metadata__protected__{meta_key}": True},
            )
            for user in _apply_filters(qs):
                _add_user(user)

        else:
            # Username lookup
            try:
                user = User.objects.get(username=target, is_active=True)
                if require_email and (not user.is_email_verified or not user.email):
                    logger.warning("User %s has unverified email, skipping", target)
                    continue
                if require_phone and (not user.is_phone_verified or not user.phone_number):
                    logger.warning("User %s has unverified phone, skipping", target)
                    continue
                _add_user(user)
            except User.DoesNotExist:
                logger.warning("User not found for target: %s", target)

    return users


def _build_incident_context(event):
    """Build a context dict for email/SMS templates."""
    ctx = {
        "event_id": event.pk,
        "category": event.category,
        "level": event.level,
        "source_ip": getattr(event, "source_ip", None),
        "title": event.title or "Alert",
        "details": event.details or "",
    }
    if event.incident_id:
        ctx["incident_id"] = event.incident_id
        if ADMIN_PORTAL_URL:
            ctx["incident_url"] = f"{ADMIN_PORTAL_URL}/incidents/{event.incident_id}"
    return ctx


class JobHandler:
    """
    Publish an async job via the mojo job queue.

    Handler syntax:
        job://module.path.function_name?param1=value1&param2=value2

    The job function receives a payload dict containing event context
    plus all query string params from the handler URL.
    """

    def __init__(self, handler_name, **params):
        self.handler_name = handler_name
        self.params = params

    def run(self, event):
        try:
            from mojo.apps import jobs
            payload = _build_incident_context(event)
            payload.update(self.params)
            jobs.publish(self.handler_name, payload)
            return True
        except Exception:
            logger.exception("JobHandler failed for %s", self.handler_name)
            return False


class EmailHandler:
    """
    Send email alerts to Users resolved by permission or ID.

    Handler syntax:
        email://perm@manage_security
        email://perm@manage_security,42
        email://perm@manage_security?template=incident_critical

    Only sends to users with is_email_verified=True.
    Requires INCIDENT_EMAIL_FROM setting (must match a configured Mailbox).
    """

    def __init__(self, target, **params):
        self.targets = [t.strip() for t in target.split(",") if t.strip()]
        self.params = params

    def run(self, event):
        if not INCIDENT_EMAIL_FROM:
            logger.warning("EmailHandler: INCIDENT_EMAIL_FROM not configured, skipping")
            return False

        if not self.targets:
            return False

        try:
            users = _resolve_users(self.targets, require_email=True)
            if not users:
                logger.warning("EmailHandler: no eligible users found for %s", self.targets)
                return False

            from mojo.apps.aws.services import email as email_service

            ctx = _build_incident_context(event)
            template = self.params.get("template")
            emails = [u.email for u in users]

            if template:
                email_service.send_with_template(
                    from_email=INCIDENT_EMAIL_FROM,
                    to=emails,
                    template_name=template,
                    context=ctx,
                )
            else:
                subject = f"[Incident] {event.category}: {ctx['title']}"
                body_lines = [
                    f"Category: {event.category}",
                    f"Level: {event.level}",
                    f"Source IP: {ctx['source_ip'] or 'N/A'}",
                    f"Title: {ctx['title']}",
                    f"Details: {ctx['details'] or 'N/A'}",
                ]
                if ctx.get("incident_url"):
                    body_lines.append(f"\nView incident: {ctx['incident_url']}")
                elif ctx.get("incident_id"):
                    body_lines.append(f"Incident ID: {ctx['incident_id']}")

                email_service.send_email(
                    from_email=INCIDENT_EMAIL_FROM,
                    to=emails,
                    subject=subject,
                    body_text="\n".join(body_lines),
                )
            return True
        except Exception:
            logger.exception("EmailHandler failed for %s", self.targets)
            return False


class SmsHandler:
    """
    Send SMS alerts to Users resolved by permission or ID.

    Handler syntax:
        sms://perm@manage_security
        sms://perm@manage_security,42

    Only sends to users with is_phone_verified=True.
    """

    def __init__(self, target, **params):
        self.targets = [t.strip() for t in target.split(",") if t.strip()]
        self.params = params

    def run(self, event):
        if not self.targets:
            return False

        try:
            users = _resolve_users(self.targets, require_phone=True)
            if not users:
                logger.warning("SmsHandler: no eligible users found for %s", self.targets)
                return False

            from mojo.apps import phonehub

            ctx = _build_incident_context(event)
            message_parts = [
                f"[Incident] {event.category}: {ctx['title']}",
                f"Level: {event.level}",
            ]
            if ctx.get("source_ip"):
                message_parts.append(f"IP: {ctx['source_ip']}")
            if ctx.get("incident_url"):
                message_parts.append(ctx["incident_url"])

            message = "\n".join(message_parts)

            sent = False
            for user in users:
                try:
                    phonehub.send_sms(
                        phone_number=user.phone_number,
                        message=message,
                        user=user,
                    )
                    sent = True
                except Exception:
                    logger.exception("SmsHandler: failed to send SMS to user %s", user.pk)

            return sent
        except Exception:
            logger.exception("SmsHandler failed for %s", self.targets)
            return False


class NotifyHandler:
    """
    Send in-app + push notification to Users.

    Handler syntax:
        notify://perm@manage_security
        notify://perm@manage_security,42
    """

    def __init__(self, target, **params):
        self.targets = [t.strip() for t in target.split(",") if t.strip()]
        self.params = params

    def run(self, event):
        if not self.targets:
            return False

        try:
            from mojo.apps.account.models import Notification

            users = _resolve_users(self.targets)
            if not users:
                logger.warning("NotifyHandler: no eligible users found for %s", self.targets)
                return False

            ctx = _build_incident_context(event)
            title = f"[{event.category}] {ctx['title']}"

            sent = False
            for user in users:
                Notification.send(
                    title=title,
                    body=ctx["details"],
                    user=user,
                    kind="incident_alert",
                    data=ctx,
                    action_url=ctx.get("incident_url"),
                )
                sent = True

            return sent
        except Exception:
            logger.exception("NotifyHandler failed for %s", self.targets)
            return False


class BlockHandler:
    """
    Fleet-wide IP blocking via broadcast.

    Handler syntax:
        block://?ttl=3600
        block://?ttl=600&reason=auto:ruleset
    """

    def __init__(self, target=None, **params):
        self.params = params

    def run(self, event):
        try:
            ip = getattr(event, "source_ip", None)
            if not ip:
                ip = (event.metadata or {}).get("source_ip")
            if not ip:
                return False

            ttl = int(self.params.get("ttl", 600)) or None

            # Build reason with incident/event reference for traceability
            # Use | separator to avoid ambiguity with colons in reason values
            reason_parts = [self.params.get("reason", "auto:ruleset")]
            if event.incident_id:
                reason_parts.append(f"incident:{event.incident_id}")
            reason_parts.append(f"event:{event.pk}")
            reason = "|".join(reason_parts)

            from mojo.apps.account.models import GeoLocatedIP
            geo = GeoLocatedIP.geolocate(ip, auto_refresh=False)
            result = geo.block(reason=reason, ttl=ttl)

            # Record action on incident and resolve it
            if result and event.incident_id:
                try:
                    from mojo.apps.incident.models import Incident
                    incident = Incident.objects.get(pk=event.incident_id)
                    ttl_display = f"{ttl}s" if ttl else "permanent"
                    incident.add_history("handler:block",
                        note=f"IP {ip} blocked ({ttl_display}), reason: {reason}")
                    if incident.status not in ("resolved", "ignored"):
                        incident.status = "resolved"
                        incident.save(update_fields=["status"])
                        incident.add_history("status_changed",
                            note=f"Auto-resolved: IP {ip} blocked by block handler")
                        incident.check_delete_on_resolution()
                except Exception:
                    logger.exception("BlockHandler: failed to update incident %s", event.incident_id)

            return result
        except Exception:
            logger.exception("BlockHandler failed for event %s", event.pk)
            return False


class TicketHandler:
    """
    Create a support ticket linked to the incident.

    Handler syntax:
        ticket://?status=open&priority=8&title=Investigate&category=security
    """

    def __init__(self, target=None, **params):
        self.params = params

    def run(self, event):
        try:
            from mojo.apps.incident.models import Ticket
            title = self.params.get("title") or (getattr(event, "title", None) or "Auto-generated ticket")
            description = self.params.get("description") or (getattr(event, "details", None) or "")
            status = self.params.get("status", "open")
            category = self.params.get("category", "incident")
            try:
                priority = int(self.params.get("priority", getattr(event, "level", 1) or 1))
            except Exception:
                priority = 1

            assignee = None
            assignee_id = self.params.get("assignee")
            if assignee_id:
                try:
                    from django.contrib.auth import get_user_model
                    User = get_user_model()
                    assignee = User.objects.filter(id=int(assignee_id)).first()
                except Exception:
                    assignee = None

            Ticket.objects.create(
                title=title,
                description=description,
                status=status,
                priority=priority,
                category=category,
                assignee=assignee,
                incident=getattr(event, "incident", None),
                metadata={**getattr(event, "metadata", {})},
            )
            return True
        except Exception:
            logger.exception("TicketHandler failed for event %s", event.pk)
            return False


class LLMHandler:
    """
    Invoke the LLM security agent to triage an incident.

    Handler syntax:
        llm://                          — use defaults
        llm://claude-sonnet-4-20250514   — specify model (future use)
        llm://?action=assess            — action hint for prompt

    The handler publishes a job that runs the full LLM agent loop
    with tool use. The agent investigates the incident, decides if it's
    noise or real, and takes action (ignore, resolve, block, create ticket).
    """

    def __init__(self, target=None, **params):
        self.target = target
        self.params = params

    def run(self, event):
        try:
            from mojo.apps import jobs
            payload = {
                "event_id": event.pk,
                "incident_id": event.incident_id,
                "ruleset_id": None,
            }
            # Try to get ruleset_id from the incident
            if event.incident_id:
                try:
                    from mojo.apps.incident.models import Incident
                    incident = Incident.objects.get(pk=event.incident_id)
                    if incident.rule_set_id:
                        payload["ruleset_id"] = incident.rule_set_id
                except Exception:
                    pass

            jobs.publish(
                "mojo.apps.incident.handlers.llm_agent.execute_llm_handler",
                payload,
                channel="incident_handlers",
            )
            return True
        except Exception:
            logger.exception("LLMHandler failed for event %s", event.pk)
            return False


# ---------------------------------------------------------------------------
# Async job entry point
# ---------------------------------------------------------------------------

HANDLER_MAP = {
    "job": JobHandler,
    "email": EmailHandler,
    "sms": SmsHandler,
    "notify": NotifyHandler,
    "block": BlockHandler,
    "ticket": TicketHandler,
    "llm": LLMHandler,
}


def execute_handler(job):
    """
    Job function called by the job engine to execute a single handler spec.

    The job engine calls func(job) where job is a Job model instance.
    The actual data is in job.payload with keys:
        handler_spec: The full handler URL string (e.g. "email://perm@manage_security?template=critical")
        event_id: ID of the Event that triggered this handler
        incident_id: ID of the associated Incident (optional)
    """
    from urllib.parse import urlparse, parse_qs

    payload = job.payload
    spec = payload.get("handler_spec")
    event_id = payload.get("event_id")
    incident_id = payload.get("incident_id")

    if not spec or not event_id:
        logger.error("execute_handler: missing handler_spec or event_id in payload")
        return

    # Load the event
    try:
        from mojo.apps.incident.models import Event
        event = Event.objects.get(pk=event_id)
    except Exception:
        logger.exception("execute_handler: failed to load event %s", event_id)
        return

    # Load the incident (optional)
    incident = None
    if incident_id:
        try:
            from mojo.apps.incident.models import Incident
            incident = Incident.objects.get(pk=incident_id)
        except Exception:
            logger.warning("execute_handler: incident %s not found", incident_id)

    # Parse and execute the handler
    try:
        handler_url = urlparse(spec)
        handler_type = handler_url.scheme
        params = {k: v[0] for k, v in parse_qs(handler_url.query).items()}

        handler_cls = HANDLER_MAP.get(handler_type)
        if not handler_cls:
            logger.warning("execute_handler: unknown handler type %s", handler_type)
            return

        if handler_type in ("job", "block", "ticket"):
            handler = handler_cls(handler_url.netloc or None, **params)
        else:
            handler = handler_cls(handler_url.netloc, **params)

        result = handler.run(event)

        # Record handler execution in incident history
        if incident:
            status_text = "succeeded" if result else "failed"
            incident.add_history(f"handler:{handler_type}",
                note=f"Handler {spec} {status_text}")

    except Exception:
        logger.exception("execute_handler: failed to run handler %s for event %s", spec, event_id)
        if incident:
            incident.add_history(f"handler:{handler_type}",
                note=f"Handler {spec} failed (exception)")
