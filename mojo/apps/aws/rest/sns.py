from typing import Any, Dict, Optional
import json
import time
import requests

from mojo import decorators as md
from mojo import JsonResponse
from mojo.helpers import logit
from mojo.helpers.settings import settings
from mojo.helpers.aws.inbound_email import process_inbound_email_from_s3

# from mojo.apps.aws.models import SentMessage  # Uncomment when implementing status updates
# from mojo.apps.aws.models import IncomingEmail  # Uncomment when implementing inbound storage

logger = logit.get_logger("email", "email.log")


# Simple in-memory cache for SNS signing certificates
# Key: SigningCertURL, Value: (fetched_at_epoch_seconds, pem_bytes)
_SNS_CERT_CACHE: Dict[str, tuple[float, bytes]] = {}
_SNS_CERT_TTL_SECONDS = settings.get('SNS_CERT_TTL_SECONDS', 3600)  # default 1 hour


def _json_loads_safe(data: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(data)
    except Exception:
        return None


def _validate_sns_signature(sns: Dict[str, Any]) -> bool:
    """
    Validate Amazon SNS signature for SubscriptionConfirmation and Notification messages.

    Behavior:
    - When settings.DEBUG is True and settings.get('SNS_VALIDATION_BYPASS_DEBUG', False) is True,
      this returns True to simplify local development.
    - Otherwise performs full validation and uses an in-memory certificate cache to
      reduce network calls to the SigningCertURL.
    """
    try:
        import base64
        from urllib.parse import urlparse
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend  # noqa: F401
    except Exception as e:
        logger.error(f"SNS signature validation unavailable (missing dependencies): {e}")
        return False

    # DEBUG bypass (opt-in)
    if getattr(settings, "DEBUG", False) and bool(getattr(settings, "SNS_VALIDATION_BYPASS_DEBUG", False)):
        logger.info("SNS signature validation bypassed (DEBUG mode with SNS_VALIDATION_BYPASS_DEBUG=True)")
        return True

    signing_cert_url = sns.get("SigningCertURL")
    signature_b64 = sns.get("Signature")
    msg_type = sns.get("Type")

    if not signing_cert_url or not signature_b64 or not msg_type:
        return False

    # Validate SigningCertURL
    parsed = urlparse(signing_cert_url)
    if parsed.scheme.lower() != "https":
        logger.warning("SNS SigningCertURL is not HTTPS")
        return False
    hostname = (parsed.hostname or "").lower()
    # Allow sns.amazonaws.com and sns.<region>.amazonaws.com
    if not (hostname == "sns.amazonaws.com" or (hostname.endswith(".amazonaws.com") and hostname.startswith("sns."))):
        logger.warning(f"SNS SigningCertURL host not allowed: {hostname}")
        return False

    # Build canonical string per AWS docs
    def build_canonical_notification(m: Dict[str, Any]) -> bytes:
        # Order: Message, MessageId, Subject (if present), Timestamp, TopicArn, Type
        lines = []
        def add(k):
            v = m.get(k)
            if v is not None:
                lines.append(f"{k}\n{v}\n")
        add("Message")
        add("MessageId")
        if m.get("Subject") is not None:
            add("Subject")
        add("Timestamp")
        add("TopicArn")
        add("Type")
        return "".join(lines).encode("utf-8")

    def build_canonical_subscription(m: Dict[str, Any]) -> bytes:
        # Order: Message, MessageId, SubscribeURL, Timestamp, Token, TopicArn, Type
        lines = []
        def add(k):
            v = m.get(k)
            if v is not None:
                lines.append(f"{k}\n{v}\n")
        add("Message")
        add("MessageId")
        add("SubscribeURL")
        add("Timestamp")
        add("Token")
        add("TopicArn")
        add("Type")
        return "".join(lines).encode("utf-8")

    if msg_type in ("Notification",):
        canonical = build_canonical_notification(sns)
    elif msg_type in ("SubscriptionConfirmation", "UnsubscribeConfirmation"):
        canonical = build_canonical_subscription(sns)
    else:
        # Unknown type; do not accept
        return False

    # Fetch certificate with caching
    pem_bytes: Optional[bytes] = None
    cache_entry = _SNS_CERT_CACHE.get(signing_cert_url)
    now = time.time()
    if cache_entry:
        fetched_at, cached_pem = cache_entry
        if now - fetched_at < _SNS_CERT_TTL_SECONDS:
            pem_bytes = cached_pem
        else:
            # expired, drop from cache
            _SNS_CERT_CACHE.pop(signing_cert_url, None)
    if pem_bytes is None:
        try:
            resp = requests.get(signing_cert_url, timeout=10)
            resp.raise_for_status()
            pem_bytes = resp.content
            _SNS_CERT_CACHE[signing_cert_url] = (now, pem_bytes)
        except Exception as e:
            logger.error(f"Failed to fetch SNS SigningCert: {e}")
            return False

    # Parse certificate and verify signature
    try:
        cert = x509.load_pem_x509_certificate(pem_bytes)
        pubkey = cert.public_key()
    except Exception as e:
        logger.error(f"Failed to load SNS SigningCert: {e}")
        return False

    # Verify signature (try SHA1 then SHA256 for compatibility)
    try:
        signature = base64.b64decode(signature_b64)
    except Exception as e:
        logger.error(f"Invalid SNS signature (base64 decode): {e}")
        return False

    for hash_algo in (hashes.SHA1(), hashes.SHA256()):
        try:
            pubkey.verify(
                signature,
                canonical,
                padding.PKCS1v15(),
                hash_algo
            )
            return True
        except Exception:
            continue

    logger.error("SNS signature verification failed")
    return False


def _handle_subscription_confirmation(sns: Dict[str, Any]) -> Dict[str, Any]:
    subscribe_url = sns.get("SubscribeURL")
    topic_arn = sns.get("TopicArn")
    if subscribe_url:
        try:
            resp = requests.get(subscribe_url, timeout=10)
            logger.info(f"SNS subscription confirmed for topic {topic_arn}: {resp.status_code}")
            return {"confirmed": True, "status_code": resp.status_code}
        except Exception as e:
            logger.error(f"Failed to confirm SNS subscription for topic {topic_arn}: {e}")
            return {"confirmed": False, "error": str(e)}
    logger.warning("SubscriptionConfirmation missing SubscribeURL")
    return {"confirmed": False, "error": "missing_subscribe_url"}


def _parse_sns_request(request) -> Optional[Dict[str, Any]]:
    # SNS sends JSON in the raw body (content-type text/plain or json), not x-www-form-urlencoded
    try:
        body = request.body.decode("utf-8") if hasattr(request, "body") else (request.DATA or "")
    except Exception:
        body = request.DATA or ""
    if isinstance(body, dict):
        # Some frameworks may parse JSON automatically
        return body
    return _json_loads_safe(body)


def _handle_inbound_notification(message: Dict[str, Any]) -> None:
    """
    Handle SES inbound event delivered via SNS:
    - Determine S3 bucket/key from receipt.action and mail.messageId/prefix
    - Parse/store the message and attachments
    - Associate to Mailbox (if matched) and enqueue async handler
    """
    mail = (message.get("mail") or {})
    receipt = (message.get("receipt") or {})
    msg_id = mail.get("messageId")
    recipients = receipt.get("recipients") or mail.get("destination") or []

    action = (receipt.get("action") or {})
    bucket = action.get("bucketName") or action.get("bucket")
    key = action.get("objectKey")
    prefix = action.get("objectKeyPrefix") or ""

    # Derive key if not present
    if not key and msg_id:
        key = f"{prefix}{msg_id}"

    if not bucket or not key:
        logger.error(f"Inbound SNS missing bucket/key; msg_id={msg_id} bucket={bucket} key={key} prefix={prefix}")
        return

    try:
        process_inbound_email_from_s3(bucket, key, recipients_hint=recipients)
        logger.info(f"Inbound email processed: s3://{bucket}/{key}")
    except Exception as e:
        # Try fallback with '.eml' suffix if initial guess fails
        if msg_id and prefix and not key.endswith(".eml"):
            fallback_key = f"{prefix}{msg_id}.eml"
            try:
                process_inbound_email_from_s3(bucket, fallback_key, recipients_hint=recipients)
                logger.info(f"Inbound email processed with fallback key: s3://{bucket}/{fallback_key}")
                return
            except Exception as e2:
                logger.error(f"Fallback inbound processing failed for s3://{bucket}/{fallback_key}: {e2}")
        logger.error(f"Inbound processing failed for s3://{bucket}/{key}: {e}")


def _handle_bounce_notification(message: Dict[str, Any]) -> None:
    """
    Handle SES bounce notification delivered via SNS.
    Updates SentMessage status to 'bounced' with details.
    """
    from mojo.apps.aws.models import SentMessage  # local import to avoid circulars
    mid = message.get("mail", {}).get("messageId")
    details = message.get("bounce") or {}
    logger.info(f"Received bounce for SES MessageId: {mid}")
    if not mid:
        return
    sent = SentMessage.objects.filter(ses_message_id=mid).first()
    if not sent:
        logger.warning(f"No SentMessage found for bounce MessageId={mid}")
        return
    sent.status = SentMessage.STATUS_BOUNCED
    try:
        sent.status_reason = json.dumps(details)
    except Exception:
        sent.status_reason = str(details)
    sent.save(update_fields=["status", "status_reason", "modified"])


def _handle_complaint_notification(message: Dict[str, Any]) -> None:
    """
    Handle SES complaint notification delivered via SNS.
    Updates SentMessage status to 'complained' with details.
    """
    from mojo.apps.aws.models import SentMessage
    mid = message.get("mail", {}).get("messageId")
    details = message.get("complaint") or {}
    logger.info(f"Received complaint for SES MessageId: {mid}")
    if not mid:
        return
    sent = SentMessage.objects.filter(ses_message_id=mid).first()
    if not sent:
        logger.warning(f"No SentMessage found for complaint MessageId={mid}")
        return
    sent.status = SentMessage.STATUS_COMPLAINED
    try:
        sent.status_reason = json.dumps(details)
    except Exception:
        sent.status_reason = str(details)
    sent.save(update_fields=["status", "status_reason", "modified"])


def _handle_delivery_notification(message: Dict[str, Any]) -> None:
    """
    Handle SES delivery notification delivered via SNS.
    Updates SentMessage status to 'delivered' with details.
    """
    from mojo.apps.aws.models import SentMessage
    mid = message.get("mail", {}).get("messageId")
    details = message.get("delivery") or {}
    logger.info(f"Received delivery for SES MessageId: {mid}")
    if not mid:
        return
    sent = SentMessage.objects.filter(ses_message_id=mid).first()
    if not sent:
        logger.warning(f"No SentMessage found for delivery MessageId={mid}")
        return
    sent.status = SentMessage.STATUS_DELIVERED
    try:
        sent.status_reason = json.dumps(details)
    except Exception:
        sent.status_reason = str(details)
    sent.save(update_fields=["status", "status_reason", "modified"])


def _handle_sns(kind: str, request):
    """
    Common SNS webhook handler:
    - Validates SNS signature (TODO)
    - Handles SubscriptionConfirmation
    - Handles Notification (parses Message and dispatches by notificationType)
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    sns = _parse_sns_request(request)
    if not sns:
        return JsonResponse({"error": "Invalid SNS payload"}, status=400)

    # Optional: compare with HTTP header x-amz-sns-message-type for consistency
    msg_type = sns.get("Type")
    topic_arn = sns.get("TopicArn")
    logger.info(f"SNS webhook ({kind}) Type={msg_type} TopicArn={topic_arn}")

    # Validate SNS signature and allowed topic
    if not _validate_sns_signature(sns):
        return JsonResponse({"error": "Invalid SNS signature"}, status=403)
    # Ensure TopicArn matches a configured/known ARN
    def _is_allowed_topic(topic: Optional[str]) -> bool:
        if not topic:
            return False
        try:
            from django.db.models import Q
            from mojo.apps.aws.models import EmailDomain
            return EmailDomain.objects.filter(
                Q(sns_topic_bounce_arn=topic) |
                Q(sns_topic_complaint_arn=topic) |
                Q(sns_topic_delivery_arn=topic) |
                Q(sns_topic_inbound_arn=topic)
            ).exists()
        except Exception as e:
            logger.error(f"TopicArn allow-check failed: {e}")
            return False
    if not _is_allowed_topic(topic_arn):
        return JsonResponse({"error": "Disallowed TopicArn"}, status=403)

    if msg_type == "SubscriptionConfirmation":
        res = _handle_subscription_confirmation(sns)
        return JsonResponse({"status": True, "data": res})

    if msg_type == "Notification":
        # SNS Message may be a JSON string
        message_raw = sns.get("Message", "")
        message = _json_loads_safe(message_raw) or {"raw": message_raw}
        notification_type = (message.get("notificationType") or kind).lower()

        if kind == "inbound" or notification_type in ("received", "inbound"):
            _handle_inbound_notification(message)
        elif kind == "bounce" or notification_type == "bounce":
            _handle_bounce_notification(message)
        elif kind == "complaint" or notification_type == "complaint":
            _handle_complaint_notification(message)
        elif kind == "delivery" or notification_type == "delivery":
            _handle_delivery_notification(message)
        else:
            logger.info(f"SNS webhook ({kind}) received unknown notificationType: {notification_type}")

        return JsonResponse({"status": True})

    # Unhandled types (UnsubscribeConfirmation, etc.) can be handled here if needed
    logger.info(f"SNS webhook ({kind}) received unhandled Type: {msg_type}")
    return JsonResponse({"status": True, "info": f"Unhandled Type: {msg_type}"})


@md.URL("email/sns/inbound")
@md.public_endpoint()
def on_sns_inbound(request):
    """
    Public webhook endpoint for SES inbound (S3 + SNS).
    """
    return _handle_sns("inbound", request)


@md.URL("email/sns/bounce")
@md.public_endpoint()
def on_sns_bounce(request):
    """
    Public webhook endpoint for SES bounce notifications.
    """
    return _handle_sns("bounce", request)


@md.URL("email/sns/complaint")
@md.public_endpoint()
def on_sns_complaint(request):
    """
    Public webhook endpoint for SES complaint notifications.
    """
    return _handle_sns("complaint", request)


@md.URL("email/sns/delivery")
@md.public_endpoint()
def on_sns_delivery(request):
    """
    Public webhook endpoint for SES delivery notifications.
    """
    return _handle_sns("delivery", request)
