"""
Simple Python service API for sending emails via Mailbox + AWS SES with Django-templated support.

This module provides a minimal, ergonomic interface for Django/Python code
to send emails using a configured Mailbox and AWS SES. It reuses existing helpers
and persists a SentMessage record for observability and webhook status updates.

Usage:

    from mojo.apps.aws.services import email as email_service

    # Simple send (no template)
    sent = email_service.send_email(
        from_email="support@example.com",
        to=["user1@example.org", "user2@example.org"],
        subject="Welcome",
        body_text="Welcome to our service!",
        body_html="<p>Welcome to our service!</p>",
    )
    print(sent.id, sent.ses_message_id, sent.status)

    # Send with Django EmailTemplate (stored in DB)
    sent = email_service.send_with_template(
        from_email="support@example.com",
        to="user@example.org",
        template_name="welcome",
        context={"first_name": "Ada"}
    )

    # Send with SES template (must exist in SES)
    sent = email_service.send_template_email(
        from_email="support@example.com",
        to="user@example.org",
        template_name="welcome-template",
        template_context={"first_name": "Ada"}
    )

Notes:
- Only Mailbox-owned addresses can send (enforced via allow_outbound flag).
- The envelope MAIL FROM and From header will be the Mailbox email.
- Domain verification is required unless allow_unverified=True is passed.
- Attachments are not supported by this simple API. To send attachments,
  you can build a MIME message and call EmailSender.send_raw_email directly.
- reply_to defaults to from_email, and can be overridden.

This API stores a SentMessage row immediately with status="sending" and SES MessageId
(if available). Final delivery/bounce/complaint updates are handled asynchronously
by the SNS webhooks.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Union
from django.db import transaction

from mojo.apps.aws.models import Mailbox, SentMessage, EmailDomain, EmailTemplate
from mojo.helpers.aws.ses import EmailSender
from mojo.helpers.settings import settings
from mojo.helpers import logit


logger = logit.get_logger("email", "email.log")



# Exceptions

class MailboxNotFound(Exception):
    pass


class OutboundNotAllowed(Exception):
    pass


class DomainNotVerified(Exception):
    pass


# Internal helpers

def _as_list(value: Union[str, Sequence[str], None]) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    v = str(value).strip()
    return [v] if v else []


def _get_mailbox(from_email: str) -> Mailbox:
    mb = Mailbox.objects.select_related("domain").filter(email__iexact=from_email.strip()).first()
    if not mb:
        raise MailboxNotFound(f"Mailbox not found for from_email={from_email!r}")
    if not mb.allow_outbound:
        raise OutboundNotAllowed(f"Outbound sending is disabled for mailbox {mb.email}")
    return mb


def _choose_region(mb: Mailbox, region: Optional[str]) -> str:
    return region or (mb.domain.region if isinstance(mb.domain, EmailDomain) else None) or getattr(settings, "AWS_REGION", "us-east-1")


def _check_domain_verified(mb: Mailbox, allow_unverified: bool):
    if allow_unverified:
        return
    if not mb.domain.is_verified:
        raise DomainNotVerified(f"Domain {mb.domain.name if mb.domain_id else '(unknown)'} is not verified for sending (status={mb.domain.status})")


def _get_sender(access_key: Optional[str], secret_key: Optional[str], region: str) -> EmailSender:
    return EmailSender(
        access_key=access_key or settings.AWS_KEY,
        secret_key=secret_key or settings.AWS_SECRET,
        region=region,
    )


# Public API

def send_email(
    from_email: str,
    to: Union[str, Sequence[str]],
    *,
    subject: Optional[str] = None,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    cc: Optional[Union[str, Sequence[str]]] = None,
    bcc: Optional[Union[str, Sequence[str]]] = None,
    reply_to: Optional[Union[str, Sequence[str]]] = None,
    allow_unverified: bool = False,
    aws_access_key: Optional[str] = None,
    aws_secret_key: Optional[str] = None,
    region: Optional[str] = None,
) -> SentMessage:
    """
    Send an email using a Mailbox (resolved by from_email) and AWS SES.

    Args:
        from_email: The sending address (must match a configured Mailbox).
        to: One or more recipient addresses.
        subject: Email subject (required if no template is used).
        body_text: Optional plain text body.
        body_html: Optional HTML body.
        cc, bcc, reply_to: Optional addressing.
        allow_unverified: If True, bypass domain verification check (use with caution).
        aws_access_key, aws_secret_key: Optional per-call AWS credentials.
        region: Optional AWS region; defaults to mailbox.domain.region or settings.AWS_REGION.

    Returns:
        SentMessage instance persisted to the database.

    Raises:
        MailboxNotFound, OutboundNotAllowed, DomainNotVerified on validation errors.
    """
    mailbox = _get_mailbox(from_email)
    _check_domain_verified(mailbox, allow_unverified)

    region_final = _choose_region(mailbox, region)
    to_list = _as_list(to)
    cc_list = _as_list(cc)
    bcc_list = _as_list(bcc)
    # Default reply_to to from_email if not provided
    reply_to_list = _as_list(reply_to) or [mailbox.email]

    if not to_list:
        raise ValueError("At least one 'to' recipient is required")
    if not (subject or body_text or body_html):
        raise ValueError("Provide at least one of subject, body_text, or body_html")

    sender = _get_sender(aws_access_key, aws_secret_key, region_final)

    with transaction.atomic():
        sent = SentMessage.objects.create(
            mailbox=mailbox,
            to_addresses=to_list,
            cc_addresses=cc_list,
            bcc_addresses=bcc_list,
            subject=subject or None,
            body_text=body_text,
            body_html=body_html,
            status=SentMessage.STATUS_SENDING,
        )

        try:
            resp = sender.send_email(
                source=mailbox.email,
                to_addresses=to_list,
                subject=subject or "",
                body_text=body_text,
                body_html=body_html,
                cc_addresses=cc_list or None,
                bcc_addresses=bcc_list or None,
                reply_to_addresses=reply_to_list or None,
            )
            msg_id = resp.get("MessageId")
            if msg_id:
                sent.ses_message_id = msg_id
                sent.save(update_fields=["ses_message_id", "modified"])
                return sent
            # Failure path
            sent.status = SentMessage.STATUS_FAILED
            sent.status_reason = resp.get("Error") or str(resp)
            sent.save(update_fields=["status", "status_reason", "modified"])
            return sent

        except Exception as e:
            logger.error(f"send_email error for mailbox={mailbox.email}: {e}")
            sent.status = SentMessage.STATUS_FAILED
            sent.status_reason = str(e)
            sent.save(update_fields=["status", "status_reason", "modified"])
            return sent


def send_with_template(
   from_email: str,
   to: Union[str, Sequence[str]],
   *,
   template_name: str,
   context: Optional[Dict[str, Any]] = None,
   cc: Optional[Union[str, Sequence[str]]] = None,
   bcc: Optional[Union[str, Sequence[str]]] = None,
   reply_to: Optional[Union[str, Sequence[str]]] = None,
   allow_unverified: bool = False,
   aws_access_key: Optional[str] = None,
   aws_secret_key: Optional[str] = None,
   region: Optional[str] = None,
) -> SentMessage:
   """
   Send using a Django EmailTemplate stored in DB.
   Renders subject/text/html with the provided context and sends via SES.
   """
   mailbox = _get_mailbox(from_email)
   _check_domain_verified(mailbox, allow_unverified)

   region_final = _choose_region(mailbox, region)
   to_list = _as_list(to)
   cc_list = _as_list(cc)
   bcc_list = _as_list(bcc)
   # Default reply_to to from_email if not provided
   reply_to_list = _as_list(reply_to) or [mailbox.email]

   if not to_list:
       raise ValueError("At least one 'to' recipient is required")
   if not template_name:
       raise ValueError("template_name is required")

   # Load and render template
   tpl = EmailTemplate.objects.filter(name=template_name).first()
   if not tpl:
       raise ValueError(f"EmailTemplate not found: {template_name}")
   rendered = tpl.render_all(context or {})

   subject = (rendered.get("subject") or "").strip()
   body_text = rendered.get("text")
   body_html = rendered.get("html")

   if not (subject or body_text or body_html):
       raise ValueError("Rendered template produced no subject/text/html")

   sender = _get_sender(aws_access_key, aws_secret_key, region_final)

   with transaction.atomic():
       sent = SentMessage.objects.create(
           mailbox=mailbox,
           to_addresses=to_list,
           cc_addresses=cc_list,
           bcc_addresses=bcc_list,
           subject=subject or None,
           body_text=body_text,
           body_html=body_html,
           template_name=tpl.name,
           template_context=context or {},
           status=SentMessage.STATUS_SENDING,
       )

       try:
           resp = sender.send_email(
               source=mailbox.email,
               to_addresses=to_list,
               subject=subject or "",
               body_text=body_text,
               body_html=body_html,
               cc_addresses=cc_list or None,
               bcc_addresses=bcc_list or None,
               reply_to_addresses=reply_to_list or None,
           )
           msg_id = resp.get("MessageId")
           if msg_id:
               sent.ses_message_id = msg_id
               sent.save(update_fields=["ses_message_id", "modified"])
               return sent
           # Failure path
           sent.status = SentMessage.STATUS_FAILED
           sent.status_reason = resp.get("Error") or str(resp)
           sent.save(update_fields=["status", "status_reason", "modified"])
           return sent
       except Exception as e:
           logger.error(f"send_with_template error for mailbox={mailbox.email}: {e}")
           sent.status = SentMessage.STATUS_FAILED
           sent.status_reason = str(e)
           sent.save(update_fields=["status", "status_reason", "modified"])
           return sent

def send_template_email(
    from_email: str,
    to: Union[str, Sequence[str]],
    *,
    template_name: str,
    template_context: Optional[Dict[str, Any]] = None,
    cc: Optional[Union[str, Sequence[str]]] = None,
    bcc: Optional[Union[str, Sequence[str]]] = None,
    reply_to: Optional[Union[str, Sequence[str]]] = None,
    allow_unverified: bool = False,
    aws_access_key: Optional[str] = None,
    aws_secret_key: Optional[str] = None,
    region: Optional[str] = None,
) -> SentMessage:
    """
    Send an email using a SES template and a Mailbox (resolved by from_email).

    Args:
        from_email: The sending address (must match a configured Mailbox).
        to: One or more recipient addresses.
        template_name: Name of the SES template.
        template_context: Dict used to render the SES template.
        cc, bcc, reply_to: Optional addressing.
        allow_unverified: If True, bypass domain verification check (use with caution).
        aws_access_key, aws_secret_key: Optional per-call AWS credentials.
        region: Optional AWS region; defaults to mailbox.domain.region or settings.AWS_REGION.

    Returns:
        SentMessage instance persisted to the database.
    """
    mailbox = _get_mailbox(from_email)
    _check_domain_verified(mailbox, allow_unverified)

    region_final = _choose_region(mailbox, region)
    to_list = _as_list(to)
    cc_list = _as_list(cc)
    bcc_list = _as_list(bcc)
    # Default reply_to to from_email if not provided
    reply_to_list = _as_list(reply_to) or [mailbox.email]
    template_context = template_context if isinstance(template_context, dict) else {}

    if not to_list:
        raise ValueError("At least one 'to' recipient is required")
    if not template_name:
        raise ValueError("template_name is required for template-based sending")

    sender = _get_sender(aws_access_key, aws_secret_key, region_final)

    with transaction.atomic():
        sent = SentMessage.objects.create(
            mailbox=mailbox,
            to_addresses=to_list,
            cc_addresses=cc_list,
            bcc_addresses=bcc_list,
            template_name=template_name,
            template_context=template_context,
            status=SentMessage.STATUS_SENDING,
        )

        try:
            resp = sender.send_template_email(
                source=mailbox.email,
                to_addresses=to_list,
                template_name=template_name,
                template_data=template_context,
                cc_addresses=cc_list or None,
                bcc_addresses=bcc_list or None,
                reply_to_addresses=reply_to_list or None,
            )
            msg_id = resp.get("MessageId")
            if msg_id:
                sent.ses_message_id = msg_id
                sent.save(update_fields=["ses_message_id", "modified"])
                return sent
            # Failure path
            sent.status = SentMessage.STATUS_FAILED
            sent.status_reason = resp.get("Error") or str(resp)
            sent.save(update_fields=["status", "status_reason", "modified"])
            return sent

        except Exception as e:
            logger.error(f"send_template_email error for mailbox={mailbox.email}: {e}")
            sent.status = SentMessage.STATUS_FAILED
            sent.status_reason = str(e)
            sent.save(update_fields=["status", "status_reason", "modified"])
            return sent
