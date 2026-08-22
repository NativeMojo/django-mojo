from copy import deepcopy


NAME = "email"

SUMMARY_PATH = "/api/aws/email/summary"
TEST_PATH = "/api/aws/email/test"
DEFAULT_PATH = "/api/aws/email/mailbox-default"
AUDIT_PREFIX = "/api/aws/email/domain/"


def describe(capabilities):
    values = {
        "view": capabilities["email"],
        "manage": capabilities["email"],
    }
    return {"id": NAME, "enabled": values["view"], "capabilities": values}


_DOMAINS = {
    # id 1: healthy — verified, production access, sending and receiving ready
    1: {"id": 1, "name": "mojo.example", "region": "us-east-1",
        "status": "ready", "receiving_enabled": True,
        "can_send": True, "can_recv": True, "dns_mode": "route53",
        "checked_at": "2026-08-18T09:14:00+00:00"},
    # id 2: sandbox-only — verified but no production access
    2: {"id": 2, "name": "sandbox.example", "region": "us-east-1",
        "status": "verified", "receiving_enabled": False,
        "can_send": False, "can_recv": False, "dns_mode": "manual",
        "checked_at": "2026-08-17T16:02:00+00:00"},
    # id 3: receiving half-configured — sends fine, inbound rule broken
    3: {"id": 3, "name": "inbound.example", "region": "us-west-2",
        "status": "verified", "receiving_enabled": True,
        "can_send": True, "can_recv": False, "dns_mode": "manual",
        "checked_at": "2026-08-16T11:40:00+00:00"},
}

_MAILBOXES = [
    {"id": 11, "email": "support@mojo.example", "domain": 1,
     "domain_name": "mojo.example", "allow_inbound": True,
     "allow_outbound": True, "is_system_default": True,
     "is_domain_default": True},
    {"id": 12, "email": "noreply@mojo.example", "domain": 1,
     "domain_name": "mojo.example", "allow_inbound": False,
     "allow_outbound": False, "is_system_default": False,
     "is_domain_default": False},
    {"id": 13, "email": "test@sandbox.example", "domain": 2,
     "domain_name": "sandbox.example", "allow_inbound": False,
     "allow_outbound": True, "is_system_default": False,
     "is_domain_default": False},
    {"id": 14, "email": "inbox@inbound.example", "domain": 3,
     "domain_name": "inbound.example", "allow_inbound": True,
     "allow_outbound": True, "is_system_default": False,
     "is_domain_default": True},
]

_AUDITS = {
    1: {"domain": "mojo.example", "region": "us-east-1", "status": "ready",
        "audit_pass": True,
        "checks": {"ses_verified": True, "dkim_verified": True,
                   "ses_production_access": True,
                   "notification_topics_ok": True},
        "items": [{"resource": "ses.identity.verification",
                   "desired": "Success", "current": "Success", "status": "ok"}],
        "recommendations": []},
    2: {"domain": "sandbox.example", "region": "us-east-1",
        "status": "verified", "audit_pass": False,
        "checks": {"ses_verified": True, "dkim_verified": True,
                   "ses_production_access": False,
                   "notification_topics_ok": True},
        "items": [{"resource": "ses.account.production_access",
                   "desired": "enabled", "current": "sandbox",
                   "status": "conflict"}],
        "recommendations": [{
            "resource": "ses.account.production_access", "severity": "low",
            "action": "Review SES production-access readiness",
            "explanation": "The account is still in the SES sandbox, so only "
                           "verified recipients receive mail."}]},
    3: {"domain": "inbound.example", "region": "us-west-2",
        "status": "verified", "audit_pass": False,
        "checks": {"ses_verified": True, "dkim_verified": True,
                   "ses_production_access": True,
                   "notification_topics_ok": True,
                   "receiving_rule_s3_ok": False},
        "items": [{"resource": "ses.receiving_rule.s3",
                   "desired": "inbound/inbound.example/",
                   "current": "missing", "status": "conflict"}],
        "recommendations": [{
            "resource": "ses.receiving_rule.s3", "severity": "medium",
            "action": "Create or configure S3 bucket for incoming emails",
            "explanation": "Receiving is enabled but the S3 delivery rule is "
                           "missing, so inbound mail has nowhere to land."}]},
}

# Each preview from_email exercises one documented test-send failure branch.
_TEST_FAILURES = {
    "noreply@mojo.example": ("outbound_not_allowed",
                             "Outbound sending is disabled for "
                             "noreply@mojo.example."),
    "ghost@mojo.example": ("mailbox_not_found",
                           "No mailbox is configured for ghost@mojo.example."),
    "test@sandbox.example": ("domain_not_verified",
                             "The mailbox's domain is not verified for "
                             "sending yet — run a check on the domain to see "
                             "what is missing."),
}


def _summary(handler):
    state = handler.email_state
    if state == "unset":
        return {"schema_version": 1,
                "posture": {"default_sender_configured": False,
                            "default_sender_conflict": False,
                            "templates_installed": False,
                            "missing_template_count": 9},
                "domain_count": 0, "mailbox_count": 0,
                "domains": [], "mailboxes": []}
    domains = [deepcopy(row) for row in _DOMAINS.values()]
    mailboxes = deepcopy(_MAILBOXES)
    posture = {"default_sender_configured": True,
               "default_sender_conflict": False,
               "templates_installed": True, "missing_template_count": 0}
    if state == "conflict":
        mailboxes[3]["is_system_default"] = True
        posture.update({"default_sender_configured": False,
                        "default_sender_conflict": True})
    return {"schema_version": 1, "posture": posture,
            "domain_count": len(domains), "mailbox_count": len(mailboxes),
            "domains": domains, "mailboxes": mailboxes}


# The preview server wraps every dict body in {"status", "data"} on the way
# out, and the portal's api() unwraps exactly that one layer. These handlers
# used to wrap a second time, so every Email read reached the page as
# {status, data} and the portal saw no domains at all — in v1 as well as v2.
def get(handler, parsed):
    if parsed.path == SUMMARY_PATH:
        return 200, _summary(handler)
    if parsed.path.startswith(AUDIT_PREFIX) and parsed.path.endswith("/audit"):
        try:
            pk = int(parsed.path[len(AUDIT_PREFIX):].split("/", 1)[0])
        except ValueError:
            return None
        audit = _AUDITS.get(pk)
        if audit is None:
            return 404, {"error": "EmailDomain not found", "code": 404}
        return 200, deepcopy(audit)
    return None


def post(handler, path, payload):
    if path == TEST_PATH:
        from_email = str(payload.get("from_email") or "")
        to = str(payload.get("to") or "")
        if not from_email or not to:
            return 200, {
                "sent": False, "error_code": "invalid_request",
                "error": "Choose the mailbox to send from."
                if not from_email else
                "At least one 'to' recipient is required"}
        failure = _TEST_FAILURES.get(from_email)
        if failure:
            return 200, {
                "sent": False, "error_code": failure[0], "error": failure[1]}
        if to.startswith("fail@"):
            return 200, {
                "sent": True, "message_id": None, "status": "failed",
                "status_reason": "MessageRejected: address is on the "
                                 "suppression list"}
        return 200, {
            "sent": True, "message_id": "preview-message-id",
            "status": "sending", "status_reason": None}
    if path == DEFAULT_PATH:
        try:
            pk = int(payload.get("mailbox"))
        except (TypeError, ValueError):
            return 400, {"error": "mailbox must be a mailbox id"}
        box = next((row for row in _MAILBOXES if row["id"] == pk), None)
        if box is None:
            return 404, {"error": "Mailbox not found", "code": 404}
        scope = payload.get("scope") or "system"
        handler.email_state = "configured"
        return 200, {
            "mailbox": pk, "email": box["email"], "scope": scope,
            "is_system_default": scope == "system",
            "is_domain_default": scope == "domain" or box["is_domain_default"],
        }
    return None


def reset(handler, fixtures, **options):
    handler.email_state = options.get("email_state", "configured")
