import json

from mojo import decorators as md
from mojo import JsonResponse
from mojo.helpers import logit
from mojo.helpers.settings import settings
from mojo.helpers.aws.inbound_email import process_inbound_email_from_s3

# from mojo.apps.aws.models import SentMessage  # Uncomment when implementing status updates
# from mojo.apps.aws.models import IncomingEmail  # Uncomment when implementing inbound storage

logger = logit.get_logger("email", "email.log")


def _json_loads_safe(data):
    try:
        return json.loads(data)
    except Exception:
        return None


def _handle_inbound_notification(message):
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


def _handle_bounce_notification(message):
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


def _handle_complaint_notification(message):
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


def _handle_delivery_notification(message):
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


def _handle_sns(kind, request):
    """
    Common SNS webhook handler:
    - Validates SNS signature (TODO)
    - Handles SubscriptionConfirmation
    - Handles Notification (parses Message and dispatches by notificationType)
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    from mojo.apps.aws.services import sns as sns_service
    try:
        sns = sns_service.parse_request(request)
    except sns_service.SNSPayloadError:
        return JsonResponse({"error": "Invalid SNS payload"}, status=400)

    # Optional: compare with HTTP header x-amz-sns-message-type for consistency
    msg_type = sns.get("Type")
    topic_arn = sns.get("TopicArn")
    logger.info(f"SNS webhook ({kind}) Type={msg_type} TopicArn={topic_arn}")

    # Ensure TopicArn matches a configured/known ARN
    def _is_allowed_topic(topic):
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
    allowed_topics = [topic_arn] if _is_allowed_topic(topic_arn) else []
    try:
        sns_service.verify_envelope(
            sns, allowed_topics, request=request, allow_debug=True,
        )
    except sns_service.SNSTransientError:
        return JsonResponse({"error": "SNS verification unavailable"}, status=503)
    except sns_service.SNSAuthorizationError:
        return JsonResponse({"error": "SNS authorization failed"}, status=403)

    if msg_type == "SubscriptionConfirmation":
        try:
            status_code = sns_service.confirm_subscription(sns)
        except sns_service.SNSTransientError:
            return JsonResponse({"error": "SNS confirmation failed"}, status=503)
        except sns_service.SNSAuthorizationError:
            return JsonResponse({"error": "SNS authorization failed"}, status=403)
        return JsonResponse({"status": True, "data": {"confirmed": True, "status_code": status_code}})

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


@md.URL("cloudwatch/sns/alarm")
@md.public_endpoint()
def on_cloudwatch_alarm(request):
    """Receive signed, allowlisted CloudWatch alarm notifications from SNS."""
    from mojo.apps.aws.services import sns as sns_service
    from mojo.apps.aws.services.cloudwatch_alarms import (
        CloudWatchDispatchError,
        CloudWatchPayloadError,
        process_notification,
    )

    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    try:
        envelope = sns_service.parse_request(request)
    except sns_service.SNSPayloadError:
        return JsonResponse({"error": "Invalid SNS payload"}, status=400)

    allowed_topics = settings.get_static(
        "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS", [], kind="list"
    )
    try:
        sns_service.verify_envelope(
            envelope, allowed_topics, request=request, allow_debug=False,
        )
    except sns_service.SNSTransientError:
        return JsonResponse({"error": "SNS verification unavailable"}, status=503)
    except sns_service.SNSAuthorizationError:
        return JsonResponse({"error": "SNS authorization failed"}, status=403)

    msg_type = envelope.get("Type")
    if msg_type == "SubscriptionConfirmation":
        try:
            status_code = sns_service.confirm_subscription(envelope)
        except sns_service.SNSTransientError:
            return JsonResponse({"error": "SNS confirmation failed"}, status=503)
        except sns_service.SNSAuthorizationError:
            return JsonResponse({"error": "SNS authorization failed"}, status=403)
        return JsonResponse({
            "status": True,
            "data": {"confirmed": True, "status_code": status_code},
        })
    if msg_type != "Notification":
        return JsonResponse({"error": "Unsupported SNS message type"}, status=400)
    try:
        result = process_notification(envelope)
    except CloudWatchPayloadError:
        return JsonResponse({"error": "Invalid CloudWatch alarm payload"}, status=400)
    except CloudWatchDispatchError:
        return JsonResponse({"error": "Alarm dispatch incomplete"}, status=503)
    except Exception:
        logger.exception("CloudWatch SNS processing failed")
        return JsonResponse({"error": "Alarm processing failed"}, status=503)
    return JsonResponse({"status": True, "data": result})
