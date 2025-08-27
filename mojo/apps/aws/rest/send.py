from typing import Any, Dict, List, Optional

from mojo import decorators as md
from mojo import JsonResponse
from mojo.apps.aws.models import Mailbox, SentMessage, EmailDomain
from mojo.helpers.aws.ses import EmailSender
from mojo.helpers.settings import settings
from mojo.helpers import logit

logger = logit.get_logger(__name__)


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


@md.URL("aws/email/send")
@md.requires_perms("manage_aws")
def on_send_email(request):
    """
    Send an email through AWS SES using a Mailbox resolved by from_email.

    Request (POST JSON):
    {
      "from_email": "support@example.com",            // required, resolves Mailbox
      "to": ["user@example.org"],                     // required (list or string)
      "cc": [],                                       // optional
      "bcc": [],                                      // optional
      "subject": "Hello",                             // required if not using template_name
      "body_text": "Text body",                       // optional
      "body_html": "<p>HTML body</p>",                // optional
      "reply_to": ["replies@example.com"],            // optional
      "template_name": "ses-template-optional",       // optional, uses AWS SES template if provided
      "template_context": { ... },                    // optional, for SES template
      "aws_access_key": "...",                        // optional, defaults to settings
      "aws_secret_key": "...",                        // optional, defaults to settings
      "allow_unverified": false                       // optional, allow send even if domain.status != 'verified'
    }

    Behavior:
    - Resolves the Mailbox by from_email (case-insensitive).
    - Ensures mailbox.allow_outbound is True.
    - Uses mailbox.domain.region (or settings.AWS_REGION) to send via SES.
    - If template_name is provided, uses EmailSender.send_template_email (AWS SES template).
      Otherwise uses EmailSender.send_email with subject/body_text/body_html.
    - Creates a SentMessage row and updates with SES MessageId and status.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    data: Dict[str, Any] = getattr(request, "DATA", {}) or {}

    from_email = (data.get("from_email") or "").strip()
    if not from_email:
        return JsonResponse({"error": "from_email is required"}, status=400)

    # Resolve Mailbox by email (case-insensitive)
    mailbox = Mailbox.objects.select_related("domain").filter(email__iexact=from_email).first()
    if not mailbox:
        return JsonResponse({"error": f"Mailbox not found for from_email={from_email}", "code": 404}, status=404)

    if not mailbox.allow_outbound:
        return JsonResponse({"error": "Outbound sending is disabled for this mailbox", "code": 403}, status=403)

    domain: EmailDomain = mailbox.domain
    region = domain.region or getattr(settings, "AWS_REGION", "us-east-1")

    # Domain verification check (optional bypass)
    if not data.get("allow_unverified", False):
        if (domain.status or "").lower() != "verified":
            return JsonResponse({
                "error": "Domain is not verified for sending in SES",
                "domain": domain.name,
                "domain_status": domain.status,
            }, status=400)

    to = _as_list(data.get("to"))
    cc = _as_list(data.get("cc"))
    bcc = _as_list(data.get("bcc"))
    reply_to = _as_list(data.get("reply_to")) or [from_email]

    if not to:
        return JsonResponse({"error": "At least one recipient in 'to' is required"}, status=400)

    subject = (data.get("subject") or "").strip()
    body_text = data.get("body_text")
    body_html = data.get("body_html")
    template_name = (data.get("template_name") or "").strip() or None
    template_context = data.get("template_context") or {}

    # If not using a template, require subject or at least one of body_text/body_html.
    if not template_name and not (subject or body_text or body_html):
        return JsonResponse({
            "error": "Provide either a template_name or a subject/body_text/body_html payload"
        }, status=400)

    # Optional AWS creds override
    access_key = data.get("aws_access_key") or settings.AWS_KEY
    secret_key = data.get("aws_secret_key") or settings.AWS_SECRET

    sender = EmailSender(access_key=access_key, secret_key=secret_key, region=region)

    # Create SentMessage record first
    sent = SentMessage.objects.create(
        mailbox=mailbox,
        to_addresses=to,
        cc_addresses=cc,
        bcc_addresses=bcc,
        subject=subject or None,
        body_text=body_text,
        body_html=body_html,
        template_name=template_name,
        template_context=template_context if isinstance(template_context, dict) else {},
        status=SentMessage.STATUS_SENDING,
    )

    try:
        if template_name:
            resp = sender.send_template_email(
                source=from_email,
                to_addresses=to,
                template_name=template_name,
                template_data=template_context if isinstance(template_context, dict) else {},
                cc_addresses=cc or None,
                bcc_addresses=bcc or None,
                reply_to_addresses=reply_to or None,
            )
        else:
            resp = sender.send_email(
                source=from_email,
                to_addresses=to,
                subject=subject or "",
                body_text=body_text,
                body_html=body_html,
                cc_addresses=cc or None,
                bcc_addresses=bcc or None,
                reply_to_addresses=reply_to or None,
            )

        message_id = resp.get("MessageId")
        if message_id:
            sent.ses_message_id = message_id
            # Let SNS delivery/bounce/complaint update final status
            sent.status = SentMessage.STATUS_SENDING
            sent.save(update_fields=["ses_message_id", "status", "modified"])
            return JsonResponse({
                "status": True,
                "data": {
                    "id": sent.id,
                    "ses_message_id": message_id,
                    "status": sent.status,
                }
            })
        else:
            # Failure from SES helper (returned {'Error': ...} or unexpected shape)
            sent.status = SentMessage.STATUS_FAILED
            sent.status_reason = resp.get("Error") or str(resp)
            sent.save(update_fields=["status", "status_reason", "modified"])
            return JsonResponse({
                "error": "SES send failed",
                "details": sent.status_reason,
                "sent_id": sent.id
            }, status=502)

    except Exception as e:
        logger.error(f"Send error for mailbox={from_email}: {e}")
        sent.status = SentMessage.STATUS_FAILED
        sent.status_reason = str(e)
        sent.save(update_fields=["status", "status_reason", "modified"])
        return JsonResponse({"error": str(e), "sent_id": sent.id}, status=500)
