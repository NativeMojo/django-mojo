import io
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from mojo.helpers import logit
from mojo.helpers.settings import settings
from mojo.helpers.aws.s3 import S3
from mojo.apps.aws.models import IncomingEmail, EmailAttachment, Mailbox

# Optional tasks manager (for async handler dispatch)
try:
    from mojo.apps.tasks import get_manager as get_task_manager
except Exception:  # pragma: no cover - tasks app may be optional in some environments
    get_task_manager = None  # type: ignore


logger = logit.get_logger(__name__)


def _safe_get_header(msg: Message, name: str) -> Optional[str]:
    value = msg.get(name)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return None


def _parse_recipients(header_value: Optional[str]) -> List[str]:
    if not header_value:
        return []
    # getaddresses parses "Name <email@domain>" into tuples; keep the email part
    return [addr for _, addr in getaddresses([header_value]) if addr]


def _parse_date_hdr(date_value: Optional[str]) -> Optional[datetime]:
    if not date_value:
        return None
    try:
        dt = parsedate_to_datetime(date_value)
        # Normalize to UTC if naive
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _collect_bodies_and_attachments(msg: Message) -> Tuple[Optional[str], Optional[str], List[Dict[str, Any]]]:
    """
    Walks MIME parts and collects the best-effort text and html bodies,
    along with attachment blobs and metadata.

    Returns:
      (text_body, html_body, attachments)
      attachments: list of dicts with keys: filename, content_type, content(bytes), size_bytes, metadata
    """
    text_body: Optional[str] = None
    html_body: Optional[str] = None
    attachments: List[Dict[str, Any]] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue

            content_disposition = (part.get_content_disposition() or "").lower()  # 'attachment', 'inline', or None
            content_type = (part.get_content_type() or "").lower()
            filename = part.get_filename()

            try:
                payload = part.get_payload(decode=True)
            except Exception:
                payload = None

            # Determine if this is an attachment
            is_attachment = content_disposition == "attachment" or (filename is not None and content_type not in ("text/plain", "text/html"))

            if is_attachment:
                if not payload:
                    payload = b""
                attachments.append({
                    "filename": filename or "attachment",
                    "content_type": content_type or "application/octet-stream",
                    "content": payload,
                    "size_bytes": len(payload),
                    "metadata": {
                        "content_id": part.get("Content-ID"),
                        "disposition": content_disposition or "",
                        "content_type": content_type,
                    }
                })
                continue

            # Collect bodies
            if content_type == "text/plain" and payload is not None:
                try:
                    text_body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    text_body = (text_body or "") + "\n[Error decoding text/plain part]"
            elif content_type == "text/html" and payload is not None:
                try:
                    html_body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    html_body = (html_body or "") + "\n<!-- Error decoding text/html part -->"
    else:
        # Single part message
        content_type = (msg.get_content_type() or "").lower()
        try:
            payload = msg.get_payload(decode=True)
        except Exception:
            payload = None
        if content_type == "text/plain" and payload is not None:
            text_body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        elif content_type == "text/html" and payload is not None:
            html_body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

    return text_body, html_body, attachments


def _flatten_headers(msg: Message) -> Dict[str, str]:
    """
    Convert headers to a dict. If multiple headers share a name,
    concatenate values with commas to avoid losing data.
    """
    headers: Dict[str, str] = {}
    for k, v in msg.items():
        if k in headers:
            headers[k] = f"{headers[k]}, {v}"
        else:
            headers[k] = v
    return headers


def _compose_s3_url(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def _attachment_s3_key(base_prefix: str, incoming_id: int, filename: str, index: int) -> str:
    """
    Build the S3 object key for an attachment, under the same base prefix as the raw message.
    - base_prefix: directory-like key prefix (e.g., 'inbound/example.com/2025/08/27/')
    """
    safe_filename = filename or f"part-{index}"
    return f"{base_prefix}attachments/{incoming_id}/{safe_filename}"


def _get_base_prefix_from_key(key: str) -> str:
    """
    Return the prefix (directory path) for a given S3 key.
    If no slash, return empty string; otherwise include trailing slash.
    """
    if "/" not in key:
        return ""
    return key.rsplit("/", 1)[0].rstrip("/") + "/"


def _match_mailbox(recipients: List[str]) -> Optional[Mailbox]:
    """
    Find the first mailbox that matches any of the recipient addresses (case-insensitive).
    """
    for addr in recipients:
        mb = Mailbox.objects.filter(email__iexact=addr).first()
        if mb:
            return mb
    return None


def _enqueue_async_handler(mailbox: Mailbox, incoming_email_id: int):
    """
    Publish a task to the configured tasks system for the mailbox's async handler.
    """
    handler = (mailbox.async_handler or "").strip()
    if not handler or get_task_manager is None:
        return

    try:
        manager = get_task_manager()
        channel = settings.get("EMAIL_TASK_CHANNEL", "email")
        payload = {
            "incoming_email_id": incoming_email_id,
            "mailbox_id": mailbox.id,
            "mailbox": mailbox.email,
            "domain": mailbox.domain.name if mailbox.domain_id else None,
        }
        # Publish to the mailbox's configured handler
        manager.publish(handler, payload, channel=channel)
    except Exception as e:
        logger.error(f"Failed to enqueue async handler for incoming_email={incoming_email_id}: {e}")


def process_inbound_email_from_s3(
    bucket: str,
    key: str,
    recipients_hint: Optional[List[str]] = None,
    received_at: Optional[datetime] = None,
) -> IncomingEmail:
    """
    Process an inbound email stored as a raw MIME file in S3.
    Steps:
      1) Fetch the S3 object bytes
      2) Parse MIME headers, bodies, and attachments
      3) Store IncomingEmail and EmailAttachment rows
      4) If any recipient matches a Mailbox (and allow_inbound), associate and enqueue its async handler

    Args:
      bucket: S3 bucket containing the raw MIME message
      key: S3 key for the raw MIME message
      recipients_hint: Optional list of recipients from SES event (receipt.recipients or mail.destination)
      received_at: Optional timestamp for when SES received the message

    Returns:
      IncomingEmail instance
    """
    # 1) Get raw MIME from S3
    logger.info(f"Processing inbound email from s3://{bucket}/{key}")
    obj = S3.client.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    size_bytes = len(body)
    s3_url = _compose_s3_url(bucket, key)

    # 2) Parse MIME
    parser = BytesParser(policy=policy.default)
    msg: Message = parser.parsebytes(body)

    message_id = _safe_get_header(msg, "Message-ID") or _safe_get_header(msg, "Message-Id")
    subject = _safe_get_header(msg, "Subject")
    from_address = _safe_get_header(msg, "From")
    to_header = _safe_get_header(msg, "To")
    cc_header = _safe_get_header(msg, "Cc")
    date_header = _parse_date_hdr(_safe_get_header(msg, "Date"))
    headers = _flatten_headers(msg)

    to_addresses = _parse_recipients(to_header)
    cc_addresses = _parse_recipients(cc_header)

    # Use SES-provided recipients if supplied (they are authoritative)
    if recipients_hint:
        # Merge hints and header addresses, deduplicating
        known = set(addr.lower() for addr in to_addresses + cc_addresses)
        for r in recipients_hint:
            if r and r.lower() not in known:
                to_addresses.append(r)

    text_body, html_body, attachments = _collect_bodies_and_attachments(msg)

    # 3) Determine mailbox (first match) and allow_inbound
    mailbox: Optional[Mailbox] = _match_mailbox(to_addresses + cc_addresses)
    if mailbox and not mailbox.allow_inbound:
        mailbox = None  # Respect mailbox inbound policy

    # 4) Create IncomingEmail row
    inc = IncomingEmail.objects.create(
        mailbox=mailbox,
        s3_object_url=s3_url,
        message_id=(message_id or "").strip() or None,
        from_address=from_address,
        to_addresses=to_addresses or [],
        cc_addresses=cc_addresses or [],
        subject=subject,
        date_header=date_header,
        headers=headers,
        text_body=text_body,
        html_body=html_body,
        size_bytes=size_bytes,
        received_at=received_at or datetime.now(timezone.utc),
        processed=False,
        process_status="pending",
    )

    # 5) Store attachments to the same inbound S3 bucket under base_prefix/attachments/<incoming_id>/
    base_prefix = _get_base_prefix_from_key(key)
    for idx, att in enumerate(attachments, start=1):
        att_key = _attachment_s3_key(base_prefix, inc.id, att["filename"], idx)
        content_bytes: bytes = att.get("content") or b""
        content_type: str = att.get("content_type") or "application/octet-stream"

        # Upload to S3
        S3.client.put_object(
            Bucket=bucket,
            Key=att_key,
            Body=io.BytesIO(content_bytes),
            ContentType=content_type,
        )

        # Create EmailAttachment row
        EmailAttachment.objects.create(
            incoming_email=inc,
            filename=att["filename"] or None,
            content_type=content_type,
            size_bytes=len(content_bytes),
            stored_as=_compose_s3_url(bucket, att_key),
            metadata=att.get("metadata") or {},
        )

    # 6) Enqueue async handler if mailbox is set and has a handler
    if mailbox and mailbox.async_handler:
        _enqueue_async_handler(mailbox, inc.id)

    logger.info(f"Stored IncomingEmail id={inc.id}, attachments={len(attachments)}")
    return inc
