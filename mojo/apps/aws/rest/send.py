from mojo.apps.aws.services import send_template_email
from typing import Any, Dict, List, Optional

from mojo import decorators as md
from mojo import JsonResponse
from mojo.apps.aws.models import Mailbox, SentMessage, EmailDomain, EmailTemplate
from mojo.helpers.aws.ses import EmailSender
from mojo.helpers.settings import settings
from mojo.helpers import logit

logger = logit.get_logger("email", "email.log")


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


@md.URL("email/send")
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
      "template_name": "db-template-optional",        // optional, uses DB EmailTemplate if provided
      "ses_template_name": "ses-template-optional",   // optional, uses AWS SES managed template
      "template_context": { ... },                    // optional, for DB/SES template context
      "aws_access_key": "...",                        // optional, defaults to settings
      "aws_secret_key": "...",                        // optional, defaults to settings
      "allow_unverified": false                       // optional, allow send even if domain.status != 'verified'
    }

    Behavior:
    - Resolves the Mailbox by from_email (case-insensitive).
    - Ensures mailbox.allow_outbound is True.
    - Uses mailbox.domain.region (or settings.AWS_REGION) to send via SES.
    - If template_name is provided and matches a DB EmailTemplate, renders and uses EmailSender.send_email with the rendered subject/body.
      If ses_template_name is provided, uses EmailSender.send_template_email (AWS SES managed template).
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

    if template_name:
        res = mailbox.send_template_email(
            to, template_name, template_context,
            cc, bcc, reply_to)
    else:
        res = mailbox.send_email(
            to=to,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to
        )
    return res.on_rest_get(request)
