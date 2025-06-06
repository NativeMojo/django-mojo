"""
Example message handlers for the notification system.

These handlers demonstrate how to process incoming and outgoing messages
across different notification channels (email, SMS, WhatsApp, etc.).
"""

import logging
import asyncio
from typing import Dict, Any
from django.conf import settings
from django.utils import timezone
from django.core.mail import send_mail
from django.template import Template, Context

from ..models import InboxMessage, OutboxMessage, Account
from ..utils.notifications import NotificationError

logger = logging.getLogger(__name__)


# =============================================================================
# INBOX MESSAGE HANDLERS (for processing received messages)
# =============================================================================

def on_email_received(inbox_message: InboxMessage) -> bool:
    """
    Example handler for processing received email messages.
    
    This handler demonstrates:
    - Basic message processing
    - Extracting message details
    - Logging and error handling
    - Auto-reply functionality
    
    Usage in Inbox model:
    sync_handler = "mojo.apps.notify.handlers.example_handlers.on_email_received"
    """
    try:
        logger.info(f"Processing email from {inbox_message.from_address} to {inbox_message.to_address}")
        
        # Extract message details
        sender = inbox_message.from_address
        recipient = inbox_message.to_address
        subject = inbox_message.subject or "No Subject"
        message_body = inbox_message.message
        
        # Example: Check if this is an auto-reply request
        if "auto-reply" in subject.lower() or "out of office" in subject.lower():
            # Send automatic response
            send_auto_reply(inbox_message)
        
        # Example: Process support tickets
        if recipient.startswith("support@"):
            process_support_ticket(inbox_message)
        
        # Example: Forward certain messages
        if should_forward_message(inbox_message):
            forward_message(inbox_message)
        
        # Log successful processing
        logger.info(f"Successfully processed email message {inbox_message.id}")
        return True
        
    except Exception as e:
        logger.error(f"Error processing email message {inbox_message.id}: {e}")
        return False


async def on_email_received_async(inbox_message: InboxMessage) -> bool:
    """
    Async version of email handler for high-volume processing.
    
    Usage in Inbox model:
    async_handler = "mojo.apps.notify.handlers.example_handlers.on_email_received_async"
    """
    try:
        # Use async processing for heavy operations
        await asyncio.sleep(0.1)  # Simulate async work
        
        # Process message asynchronously
        result = await process_email_async(inbox_message)
        
        logger.info(f"Async processed email message {inbox_message.id}")
        return result
        
    except Exception as e:
        logger.error(f"Async error processing email message {inbox_message.id}: {e}")
        return False


def on_sms_received(inbox_message: InboxMessage) -> bool:
    """
    Example handler for processing received SMS messages.
    
    Usage in Inbox model:
    sync_handler = "mojo.apps.notify.handlers.example_handlers.on_sms_received"
    """
    try:
        logger.info(f"Processing SMS from {inbox_message.from_address}")
        
        # Extract SMS content
        sender_phone = inbox_message.from_address
        message_text = inbox_message.message.strip()
        
        # Example: Handle SMS commands
        if message_text.upper().startswith("STOP"):
            handle_sms_unsubscribe(sender_phone)
        elif message_text.upper().startswith("START"):
            handle_sms_subscribe(sender_phone)
        elif message_text.upper().startswith("HELP"):
            send_sms_help(sender_phone)
        else:
            # Process regular SMS message
            process_sms_message(inbox_message)
        
        return True
        
    except Exception as e:
        logger.error(f"Error processing SMS message {inbox_message.id}: {e}")
        return False


def on_whatsapp_received(inbox_message: InboxMessage) -> bool:
    """
    Example handler for processing received WhatsApp messages.
    
    Usage in Inbox model:
    sync_handler = "mojo.apps.notify.handlers.example_handlers.on_whatsapp_received"
    """
    try:
        logger.info(f"Processing WhatsApp from {inbox_message.from_address}")
        
        # Extract message metadata (WhatsApp specific)
        metadata = inbox_message.metadata
        message_type = metadata.get('message_type', 'text')
        
        if message_type == 'text':
            process_whatsapp_text(inbox_message)
        elif message_type == 'image':
            process_whatsapp_image(inbox_message)
        elif message_type == 'document':
            process_whatsapp_document(inbox_message)
        
        return True
        
    except Exception as e:
        logger.error(f"Error processing WhatsApp message {inbox_message.id}: {e}")
        return False


# =============================================================================
# OUTBOX MESSAGE HANDLERS (for sending messages)
# =============================================================================

def on_email_send(outbox_message: OutboxMessage) -> bool:
    """
    Example handler for sending email messages.
    
    Usage in Outbox model:
    handler = "mojo.apps.notify.handlers.example_handlers.on_email_send"
    """
    try:
        # Mark as sending
        outbox_message.mark_sending()
        
        # Extract email details
        recipient = outbox_message.to_address
        sender = outbox_message.from_address
        subject = outbox_message.subject or "No Subject"
        message_body = outbox_message.message
        
        # Get email settings from account
        account = outbox_message.account
        smtp_settings = account.get_setting('smtp', {})
        
        # Send email using Django's email backend or custom SMTP
        if smtp_settings:
            send_via_custom_smtp(outbox_message, smtp_settings)
        else:
            send_via_django_email(outbox_message)
        
        # Mark as sent
        outbox_message.mark_sent()
        logger.info(f"Email sent successfully to {recipient}")
        return True
        
    except Exception as e:
        error_msg = f"Failed to send email to {outbox_message.to_address}: {e}"
        outbox_message.mark_failed(error_msg)
        logger.error(error_msg)
        return False


def on_sms_send(outbox_message: OutboxMessage) -> bool:
    """
    Example handler for sending SMS messages.
    
    Usage in Outbox model:
    handler = "mojo.apps.notify.handlers.example_handlers.on_sms_send"
    """
    try:
        outbox_message.mark_sending()
        
        # Extract SMS details
        phone_number = outbox_message.to_address
        message_text = outbox_message.message
        
        # Get SMS provider settings
        account = outbox_message.account
        sms_settings = account.get_setting('sms_provider', {})
        
        provider = sms_settings.get('provider', 'twilio')
        
        if provider == 'twilio':
            send_via_twilio(outbox_message, sms_settings)
        elif provider == 'aws_sns':
            send_via_aws_sns(outbox_message, sms_settings)
        else:
            raise NotificationError(f"Unknown SMS provider: {provider}")
        
        outbox_message.mark_sent()
        logger.info(f"SMS sent successfully to {phone_number}")
        return True
        
    except Exception as e:
        error_msg = f"Failed to send SMS to {outbox_message.to_address}: {e}"
        outbox_message.mark_failed(error_msg)
        logger.error(error_msg)
        return False


def on_whatsapp_send(outbox_message: OutboxMessage) -> bool:
    """
    Example handler for sending WhatsApp messages.
    
    Usage in Outbox model:
    handler = "mojo.apps.notify.handlers.example_handlers.on_whatsapp_send"
    """
    try:
        outbox_message.mark_sending()
        
        # Extract WhatsApp details
        phone_number = outbox_message.to_address
        message_text = outbox_message.message
        
        # Get WhatsApp Business API settings
        account = outbox_message.account
        wa_settings = account.get_setting('whatsapp_api', {})
        
        # Send via WhatsApp Business API
        send_via_whatsapp_api(outbox_message, wa_settings)
        
        outbox_message.mark_sent()
        logger.info(f"WhatsApp sent successfully to {phone_number}")
        return True
        
    except Exception as e:
        error_msg = f"Failed to send WhatsApp to {outbox_message.to_address}: {e}"
        outbox_message.mark_failed(error_msg)
        logger.error(error_msg)
        return False


def on_push_send(outbox_message: OutboxMessage) -> bool:
    """
    Example handler for sending push notifications.
    
    Usage in Outbox model:
    handler = "mojo.apps.notify.handlers.example_handlers.on_push_send"
    """
    try:
        outbox_message.mark_sending()
        
        # Extract push notification details
        device_token = outbox_message.to_address
        title = outbox_message.subject or "Notification"
        message_body = outbox_message.message
        metadata = outbox_message.metadata
        
        # Get push notification settings
        account = outbox_message.account
        push_settings = account.get_setting('push_provider', {})
        
        provider = push_settings.get('provider', 'fcm')
        
        if provider == 'fcm':
            send_via_fcm(outbox_message, push_settings)
        elif provider == 'apns':
            send_via_apns(outbox_message, push_settings)
        else:
            raise NotificationError(f"Unknown push provider: {provider}")
        
        outbox_message.mark_sent()
        logger.info(f"Push notification sent successfully to {device_token}")
        return True
        
    except Exception as e:
        error_msg = f"Failed to send push notification to {outbox_message.to_address}: {e}"
        outbox_message.mark_failed(error_msg)
        logger.error(error_msg)
        return False


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def send_auto_reply(inbox_message: InboxMessage):
    """Send an automatic reply to the sender"""
    from ..utils.notifications import send_email
    
    auto_reply_template = """
    Thank you for your message. This is an automated response.
    
    We have received your email and will respond within 24 hours.
    
    Original subject: {subject}
    Received at: {received_at}
    """
    
    reply_message = auto_reply_template.format(
        subject=inbox_message.subject or "No Subject",
        received_at=inbox_message.created.strftime("%Y-%m-%d %H:%M:%S")
    )
    
    send_email(
        to_address=inbox_message.from_address,
        subject=f"Re: {inbox_message.subject or 'Your Message'}",
        message=reply_message,
        from_address=inbox_message.to_address
    )


def process_support_ticket(inbox_message: InboxMessage):
    """Process a support ticket from email"""
    # This would integrate with your ticketing system
    logger.info(f"Creating support ticket for: {inbox_message.subject}")
    
    # Example: Create a ticket record
    ticket_data = {
        'email': inbox_message.from_address,
        'subject': inbox_message.subject,
        'description': inbox_message.message,
        'created_at': inbox_message.created,
    }
    
    # Save to your ticketing system
    # Ticket.objects.create(**ticket_data)


def should_forward_message(inbox_message: InboxMessage) -> bool:
    """Determine if message should be forwarded"""
    # Example logic for forwarding
    keywords = ['urgent', 'asap', 'emergency']
    message_lower = inbox_message.message.lower()
    subject_lower = (inbox_message.subject or "").lower()
    
    return any(keyword in message_lower or keyword in subject_lower for keyword in keywords)


def forward_message(inbox_message: InboxMessage):
    """Forward message to designated recipients"""
    from ..utils.notifications import send_email
    
    forward_addresses = ['manager@example.com', 'support-lead@example.com']
    
    forward_message = f"""
    Forwarded message from: {inbox_message.from_address}
    Original recipient: {inbox_message.to_address}
    Subject: {inbox_message.subject}
    Received: {inbox_message.created}
    
    Message:
    {inbox_message.message}
    """
    
    for address in forward_addresses:
        send_email(
            to_address=address,
            subject=f"FWD: {inbox_message.subject}",
            message=forward_message,
            from_address=inbox_message.to_address
        )


async def process_email_async(inbox_message: InboxMessage) -> bool:
    """Async email processing"""
    # Simulate async work like API calls, database operations, etc.
    await asyncio.sleep(0.1)
    
    # Process message
    return True


def handle_sms_unsubscribe(phone_number: str):
    """Handle SMS unsubscribe request"""
    logger.info(f"Processing SMS unsubscribe for {phone_number}")
    # Add to unsubscribe list
    # SMSUnsubscribe.objects.get_or_create(phone_number=phone_number)


def handle_sms_subscribe(phone_number: str):
    """Handle SMS subscribe request"""
    logger.info(f"Processing SMS subscribe for {phone_number}")
    # Remove from unsubscribe list
    # SMSUnsubscribe.objects.filter(phone_number=phone_number).delete()


def send_sms_help(phone_number: str):
    """Send SMS help message"""
    from ..utils.notifications import send_sms
    
    help_message = """
    SMS Commands:
    - STOP: Unsubscribe from messages
    - START: Subscribe to messages  
    - HELP: Show this help message
    """
    
    send_sms(to_address=phone_number, message=help_message)


def process_sms_message(inbox_message: InboxMessage):
    """Process regular SMS message"""
    logger.info(f"Processing SMS message: {inbox_message.message[:50]}...")


def process_whatsapp_text(inbox_message: InboxMessage):
    """Process WhatsApp text message"""
    logger.info(f"Processing WhatsApp text: {inbox_message.message[:50]}...")


def process_whatsapp_image(inbox_message: InboxMessage):
    """Process WhatsApp image message"""
    logger.info("Processing WhatsApp image message")
    # Handle image processing


def process_whatsapp_document(inbox_message: InboxMessage):
    """Process WhatsApp document message"""
    logger.info("Processing WhatsApp document message")
    # Handle document processing


def send_via_custom_smtp(outbox_message: OutboxMessage, smtp_settings: Dict[str, Any]):
    """Send email via custom SMTP settings"""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    
    # Create message
    msg = MIMEMultipart()
    msg['From'] = outbox_message.from_address
    msg['To'] = outbox_message.to_address
    msg['Subject'] = outbox_message.subject or "No Subject"
    
    # Add body
    msg.attach(MIMEText(outbox_message.message, 'plain'))
    
    # Connect and send
    server = smtplib.SMTP(smtp_settings['host'], smtp_settings.get('port', 587))
    if smtp_settings.get('use_tls', True):
        server.starttls()
    
    if smtp_settings.get('username') and smtp_settings.get('password'):
        server.login(smtp_settings['username'], smtp_settings['password'])
    
    server.send_message(msg)
    server.quit()


def send_via_django_email(outbox_message: OutboxMessage):
    """Send email via Django's default email backend"""
    send_mail(
        subject=outbox_message.subject or "No Subject",
        message=outbox_message.message,
        from_email=outbox_message.from_address,
        recipient_list=[outbox_message.to_address],
        fail_silently=False
    )


def send_via_twilio(outbox_message: OutboxMessage, sms_settings: Dict[str, Any]):
    """Send SMS via Twilio"""
    # This would use the Twilio Python SDK
    logger.info(f"Sending SMS via Twilio to {outbox_message.to_address}")
    # from twilio.rest import Client
    # client = Client(sms_settings['account_sid'], sms_settings['auth_token'])
    # message = client.messages.create(...)


def send_via_aws_sns(outbox_message: OutboxMessage, sms_settings: Dict[str, Any]):
    """Send SMS via AWS SNS"""
    logger.info(f"Sending SMS via AWS SNS to {outbox_message.to_address}")
    # import boto3
    # sns = boto3.client('sns', ...)
    # sns.publish(...)


def send_via_whatsapp_api(outbox_message: OutboxMessage, wa_settings: Dict[str, Any]):
    """Send WhatsApp message via Business API"""
    logger.info(f"Sending WhatsApp via API to {outbox_message.to_address}")
    # Implement WhatsApp Business API call


def send_via_fcm(outbox_message: OutboxMessage, push_settings: Dict[str, Any]):
    """Send push notification via Firebase Cloud Messaging"""
    logger.info(f"Sending FCM push to {outbox_message.to_address}")
    # Implement FCM API call


def send_via_apns(outbox_message: OutboxMessage, push_settings: Dict[str, Any]):
    """Send push notification via Apple Push Notification Service"""
    logger.info(f"Sending APNS push to {outbox_message.to_address}")
    # Implement APNS API call