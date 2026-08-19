"""
Admin email management REST endpoints

    GET  /api/aws/email/summary          - domains, mailboxes, posture (no AWS calls)
    POST /api/aws/email/test             - send one test email; ALWAYS answers 200
    POST /api/aws/email/mailbox-default  - set the system/domain default mailbox

One capability tier for all three: ``manage_aws`` / ``comms`` / ``admin``
(there is no read-only email permission anywhere in this repo, and this
surface does not invent one). Every endpoint refuses key-backed sessions; the
two writes additionally require recent authentication (inert unless
``FRESH_AUTH_WINDOW`` is set, same as the capacity/maintenance applies).

The live re-check ("Check now") is NOT here — the existing
``GET /api/aws/email/domain/<pk>/audit`` endpoint already runs the SES audit
with ``persist=True``, which is what keeps the summary and the Dashboard row
fresh.
"""

from mojo import decorators as md
from mojo import errors as me
from mojo.helpers.response import JsonResponse
from mojo.apps.aws.services import email_admin, mailbox_defaults


@md.GET("email/summary")
@md.denies_key_backed_session()
@md.requires_global_perms("manage_aws", "comms", "admin")
def on_email_summary(request):
    return JsonResponse({"status": True, "data": email_admin.summarize()})


@md.POST("email/test")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_aws", "comms", "admin")
def on_email_test(request):
    """One test send. Never a 500: every foreseeable failure is a structured
    ``error`` / ``error_code`` inside a 200 envelope (see email_admin.test_send)."""
    data = request.DATA
    result = email_admin.test_send(
        from_email=data.get("from_email"),
        to=data.get("to"),
        subject=data.get("subject"),
        body_text=data.get("body_text"),
        body_html=data.get("body_html"))
    return JsonResponse({"status": True, "data": result})


@md.POST("email/mailbox-default")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_aws", "comms", "admin")
def on_email_mailbox_default(request):
    """Set the system-wide (scope=system) or per-domain (scope=domain) default
    mailbox through the one locked writer shared with configure_email and the
    REST Mailbox save path."""
    from mojo.apps.aws.models import Mailbox
    from mojo.apps.account.services.admin_platform import audit_after_commit

    scope = str(request.DATA.get("scope") or "system")
    if scope not in ("system", "domain"):
        raise me.ValueException("scope must be 'system' or 'domain'")
    try:
        pk = int(request.DATA.get("mailbox"))
    except (TypeError, ValueError):
        raise me.ValueException("mailbox must be a mailbox id")
    mailbox = Mailbox.objects.select_related("domain").filter(pk=pk).first()
    if mailbox is None:
        raise me.ValueException("Mailbox not found", code=404, status=404)
    if not mailbox.allow_outbound:
        raise me.ValueException(
            "Outbound sending is disabled for this mailbox — enable it "
            "before making it a default sender.")
    if scope == "system":
        mailbox_defaults.claim_system_default(mailbox)
    else:
        mailbox_defaults.claim_domain_default(mailbox)
    audit_after_commit(request.user, "mailbox_default",
                       f"{scope}:{mailbox.email}")
    return JsonResponse({"status": True, "data": {
        "mailbox": mailbox.pk,
        "email": mailbox.email,
        "scope": scope,
        "is_system_default": mailbox.is_system_default,
        "is_domain_default": mailbox.is_domain_default,
    }})
