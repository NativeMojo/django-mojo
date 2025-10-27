"""REST endpoints for SMS operations."""

import mojo.decorators as md
from mojo.apps.phonehub.models import SMS


@md.URL('sms')
@md.URL('sms/<int:pk>')
def on_sms(request, pk=None):
    """Standard CRUD for SMS messages."""
    return SMS.on_rest_request(request, pk)


@md.POST('sms/send')
@md.requires_perms("send_sms")
@md.requires_params(['to_number', 'body'])
def on_sms_send(request):
    """
    Send SMS message.

    POST /api/sms/send
    {
        "to_number": "+14155551234",
        "body": "Hello from PhoneHub!",
        "from_number": "+14155556789",  // optional
        "group": 123,  // optional
        "metadata": {}  // optional
    }

    Returns SMS object with send status.
    """
    to_number = request.DATA.get('to_number')
    body = request.DATA.get('body')
    metadata = request.DATA.get('metadata')

    # Send SMS
    sms = SMS.send(
        to_number=to_number,
        body=body,
        group=request.group,
        user=request.user,
        metadata=metadata
    )

    if sms:
        return sms.on_rest_get(request)
    return {"status": False}


@md.POST('sms/webhook/twilio')
@md.public_endpoint()
def on_sms_webhook_twilio(request):
    """
    Twilio webhook endpoint for incoming SMS.

    POST /api/sms/webhook/twilio

    Twilio will POST to this endpoint when SMS is received.
    See: https://www.twilio.com/docs/sms/tutorials/how-to-receive-and-reply-python
    """
    # Twilio sends form data
    from_number = request.DATA.get('From')
    to_number = request.DATA.get('To')
    body = request.DATA.get('Body', '')
    message_sid = request.DATA.get('MessageSid')

    if not from_number or not to_number:
        return md.response_error('Missing required fields', status=400)

    # Handle incoming SMS
    sms = SMS(
        from_number=from_number,
        to_number=to_number,
        body=body,
        provider='twilio',
        direction='inbound',
        provider_message_id=message_sid,
        metadata=dict(request.DATA)
    )
    sms.save()

    # Return TwiML response (optional - can customize)
    from django.http import HttpResponse
    return HttpResponse(
        '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        content_type='text/xml'
    )


@md.POST('sms/webhook/twilio/status')
@md.public_endpoint()
def on_sms_webhook_twilio_status(request):
    """
    Twilio webhook endpoint for SMS status updates.

    POST /api/sms/webhook/twilio/status

    Twilio will POST to this endpoint with delivery status updates.
    """
    message_sid = request.DATA.get('MessageSid')
    message_status = request.DATA.get('MessageStatus')

    if not message_sid:
        return md.response_error('Missing MessageSid', status=400)

    # Find SMS by provider message ID
    try:
        sms = SMS.objects.get(provider_message_id=message_sid)

        # Update status based on Twilio status
        status_mapping = {
            'queued': 'queued',
            'sending': 'sending',
            'sent': 'sent',
            'delivered': 'delivered',
            'failed': 'failed',
            'undelivered': 'undelivered',
        }

        new_status = status_mapping.get(message_status.lower())
        if new_status:
            sms.status = new_status

            # Update delivered_at if delivered
            if new_status == 'delivered':
                from django.utils import timezone
                sms.delivered_at = timezone.now()

            # Store error info if failed
            if new_status in ['failed', 'undelivered']:
                sms.error_code = request.DATA.get('ErrorCode')
                sms.error_message = request.DATA.get('ErrorMessage')

            sms.save()

        return {'success': True, 'status': new_status}

    except SMS.DoesNotExist:
        return md.response_error('SMS not found', status=404)
