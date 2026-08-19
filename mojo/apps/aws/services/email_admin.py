"""Admin email management: the read model behind the portal's Email surface.

Everything here reads persisted state — ``EmailDomain.status`` / ``can_send``
/ ``can_recv`` as last written by ``audit_email_domain(persist=True)`` — and
makes ZERO AWS calls. The live re-check stays on the existing
``GET /api/aws/email/domain/<pk>/audit`` endpoint, whose ``persist=True`` side
effect is what keeps these readings fresh.

``email_posture()`` is the one source of truth for the default-sender /
templates posture; the admin-settings ``email_posture`` resolver delegates to
it, and both the summary and the dashboard source reuse it.

``test_send`` exists so the portal's test-send NEVER answers 500: every
foreseeable failure comes back as a structured ``{"sent": False, "error",
"error_code"}`` dict, and an SES-side failure comes back as a successful call
whose SentMessage carries ``status="failed"``.
"""

DOMAIN_LIMIT = 100
MAILBOX_LIMIT = 200


def email_posture():
    """The default-sender / templates posture. One implementation, reused by
    the admin-settings resolver, the summary, and the dashboard source."""
    from mojo.apps.aws.models import Mailbox
    from mojo.apps.aws.services.email_templates import shipped_status
    defaults = Mailbox.objects.filter(
        is_system_default=True, allow_outbound=True).count()
    template_status = shipped_status()
    return {
        "default_sender_configured": defaults == 1,
        "default_sender_conflict": defaults > 1,
        "templates_installed": len(template_status["missing"]) == 0,
        "missing_template_count": len(template_status["missing"]),
    }


def _domains():
    from mojo.apps.aws.models import EmailDomain
    return list(EmailDomain.objects.order_by("name")[:DOMAIN_LIMIT])


def summarize():
    """Domains, mailboxes, and posture for the management page.

    Never returns credential material — not even the masked forms; the page
    has no business hinting at a stored SES credential.
    """
    from mojo.apps.aws.models import EmailDomain, Mailbox
    domains = _domains()
    mailboxes = list(Mailbox.objects.select_related("domain")
                     .order_by("email")[:MAILBOX_LIMIT])
    return {
        "schema_version": 1,
        "posture": email_posture(),
        "domain_count": EmailDomain.objects.count(),
        "mailbox_count": Mailbox.objects.count(),
        "domains": [{
            "id": row.pk,
            "name": row.name,
            "region": row.aws_region,
            "status": row.status,
            "receiving_enabled": row.receiving_enabled,
            "can_send": row.can_send,
            "can_recv": row.can_recv,
            "dns_mode": row.dns_mode,
            "checked_at": row.modified.isoformat(),
        } for row in domains],
        "mailboxes": [{
            "id": row.pk,
            "email": row.email,
            "domain": row.domain_id,
            "domain_name": row.domain.name,
            "allow_inbound": row.allow_inbound,
            "allow_outbound": row.allow_outbound,
            "is_system_default": row.is_system_default,
            "is_domain_default": row.is_domain_default,
        } for row in mailboxes],
    }


def dashboard_source():
    """The Dashboard's email row — configuration evidence, not availability.

    ``configured`` False (no EmailDomain rows) renders as an ABSENT row, never
    a red one, and the status is deliberately never ``unhealthy``: a broken
    email setup is not an outage, which is also why this source is not in
    ``AVAILABILITY_SOURCES``.
    """
    from mojo.apps.aws.models import Mailbox
    domains = _domains()
    if not domains:
        return {"_collector_status": "unconfigured", "configured": False,
                "domains": 0}
    posture = email_posture()
    sendable = [row for row in domains if row.can_send]
    default = Mailbox.objects.filter(
        is_system_default=True).order_by("pk").first()
    data = {
        "configured": True,
        "domains": len(domains),
        "sendable_domains": len(sendable),
        "names": [row.name for row in domains[:4]],
        "default_sender": default.email if default else None,
        "posture": posture,
        "checked_at": max(row.modified for row in domains).isoformat(),
    }
    problems = []
    if not sendable:
        problems.append("no_sendable_domain")
    if posture["default_sender_conflict"]:
        problems.append("default_sender_conflict")
    elif not posture["default_sender_configured"]:
        problems.append("no_default_sender")
    if not posture["templates_installed"]:
        problems.append("templates_missing")
    if problems:
        data["_collector_status"] = "degraded"
        data["_collector_reason"] = problems[0]
        data["problems"] = problems
    else:
        data["_collector_status"] = "healthy"
    return data


def test_send(from_email, to, subject=None, body_text=None, body_html=None):
    """Send one test email; every foreseeable failure is a structured answer.

    Returns ``{"sent": True, "message_id", "status", "status_reason"}`` when
    the pipeline ran — including ``status="failed"`` for an SES-side refusal —
    and ``{"sent": False, "error", "error_code"}`` for everything the send
    path can raise. Never raises.
    """
    from mojo.apps.aws.services import email as email_service

    from_email = str(from_email or "").strip()
    if not from_email:
        return {"sent": False, "error_code": "invalid_request",
                "error": "Choose the mailbox to send from."}
    try:
        sent = email_service.send_email(
            from_email=from_email, to=to, subject=subject,
            body_text=body_text, body_html=body_html)
    except email_service.MailboxNotFound:
        return {"sent": False, "error_code": "mailbox_not_found",
                "error": f"No mailbox is configured for {from_email}."}
    except email_service.OutboundNotAllowed:
        return {"sent": False, "error_code": "outbound_not_allowed",
                "error": f"Outbound sending is disabled for {from_email}."}
    except email_service.DomainNotVerified:
        return {"sent": False, "error_code": "domain_not_verified",
                "error": "The mailbox's domain is not verified for sending "
                         "yet — run a check on the domain to see what is "
                         "missing."}
    except ValueError as err:
        return {"sent": False, "error_code": "invalid_request",
                "error": str(err)}
    except Exception as err:
        # Reached before SES on an unconfigured install (no AWS credentials
        # anywhere) or on any other environment problem. Still a 200.
        return {"sent": False, "error_code": "configuration_error",
                "error": "The send could not be attempted: "
                         f"{err.__class__.__name__}."}
    return {
        "sent": True,
        "message_id": sent.ses_message_id or None,
        "status": sent.status,
        "status_reason": sent.status_reason or None,
    }
