# Django Notification System

A comprehensive multi-channel notification system for Django that supports email, SMS, WhatsApp, push notifications, and more.

## Overview

This notification system provides a unified approach to handling both incoming and outgoing messages across multiple communication channels. It's designed to be flexible, scalable, and easy to integrate with existing Django applications.

## Architecture

The system consists of several key components:

- **Account**: Represents a notification service account (email domain, SMS provider, etc.)
- **Inbox**: Receives messages from external sources
- **InboxMessage**: Stores received messages
- **Outbox**: Sends messages to external recipients
- **OutboxMessage**: Stores outgoing messages with status tracking

## Models

### Account

Represents a notification service account that can send/receive messages.

```python
from mojo.apps.notify.models import Account

# Create an email account
email_account = Account.objects.create(
    kind=Account.EMAIL,
    domain='example.com',
    group=my_group,
    settings={
        'smtp': {
            'host': 'smtp.example.com',
            'port': 587,
            'username': 'user@example.com',
            'password': 'password',
            'use_tls': True
        }
    }
)

# Create an SMS account
sms_account = Account.objects.create(
    kind=Account.SMS,
    domain='+1234567890',
    group=my_group,
    settings={
        'sms_provider': {
            'provider': 'twilio',
            'account_sid': 'your_sid',
            'auth_token': 'your_token'
        }
    }
)
```

### Inbox

Defines an inbox for receiving messages.

```python
from mojo.apps.notify.models import Inbox

# Create an email inbox
email_inbox = Inbox.objects.create(
    account=email_account,
    address='support@example.com',
    sync_handler='myapp.handlers.on_email_received',
    async_handler='myapp.handlers.on_email_received_async'
)

# Create an SMS inbox
sms_inbox = Inbox.objects.create(
    account=sms_account,
    address='+1234567890',
    sync_handler='myapp.handlers.on_sms_received'
)
```

### Outbox

Defines an outbox for sending messages.

```python
from mojo.apps.notify.models import Outbox

# Create an email outbox
email_outbox = Outbox.objects.create(
    account=email_account,
    group=my_group,
    address='noreply@example.com',
    handler='myapp.handlers.on_email_send',
    rate_limit=1000  # Max 1000 emails per hour
)

# Create an SMS outbox
sms_outbox = Outbox.objects.create(
    account=sms_account,
    group=my_group,
    address='+1234567890',
    handler='myapp.handlers.on_sms_send',
    rate_limit=100  # Max 100 SMS per hour
)
```

## Usage Examples

### Sending Messages

```python
from mojo.apps.notify.utils.notifications import send_email, send_sms, send_whatsapp, send_push

# Send an email
email_message = send_email(
    to_address='user@example.com',
    subject='Welcome!',
    message='Welcome to our platform!',
    from_address='welcome@example.com',
    user=user_instance,
    metadata={'campaign': 'welcome_series'}
)

# Send an SMS
sms_message = send_sms(
    to_address='+1987654321',
    message='Your verification code is: 123456',
    user=user_instance
)

# Send a WhatsApp message
whatsapp_message = send_whatsapp(
    to_address='+1987654321',
    message='Hello from WhatsApp!',
    metadata={'template_id': 'greeting'}
)

# Send a push notification
push_message = send_push(
    to_address='device_token_here',
    title='New Message',
    message='You have a new message!',
    user=user_instance,
    metadata={'badge_count': 5}
)

# Schedule a message for later
from django.utils import timezone
from datetime import timedelta

scheduled_message = send_email(
    to_address='user@example.com',
    subject='Reminder',
    message='Don\'t forget about your appointment tomorrow!',
    scheduled_at=timezone.now() + timedelta(hours=24)
)
```

### Bulk Messaging

```python
from mojo.apps.notify.utils.notifications import BulkNotifier

# Send bulk emails
recipients = ['user1@example.com', 'user2@example.com', 'user3@example.com']
messages = BulkNotifier.send_bulk_email(
    recipients=recipients,
    subject='Newsletter',
    message='Check out our latest updates!',
    batch_size=50
)

# Send bulk SMS
phone_numbers = ['+1111111111', '+2222222222', '+3333333333']
sms_messages = BulkNotifier.send_bulk_sms(
    recipients=phone_numbers,
    message='Flash sale - 50% off everything!',
    batch_size=25
)
```

### Processing Received Messages

Messages are automatically processed when received if handlers are configured. You can also manually process them:

```python
from mojo.apps.notify.utils.notifications import MessageProcessor

# Process a single message
success = MessageProcessor.process_inbox_message(inbox_message)

# Bulk process messages for an inbox
processed_count = MessageProcessor.bulk_process_inbox_messages(
    inbox=my_inbox,
    limit=100
)
```

## Message Handlers

### Creating Handlers

Handlers are Python functions that process incoming or outgoing messages. Here are examples:

#### Inbox Handler (Receiving Messages)

```python
# myapp/handlers.py
import logging
from mojo.apps.notify.models import InboxMessage

logger = logging.getLogger(__name__)

def on_email_received(inbox_message: InboxMessage) -> bool:
    """Handle received email messages"""
    try:
        # Extract message details
        sender = inbox_message.from_address
        subject = inbox_message.subject
        message = inbox_message.message
        
        # Process the message
        if 'support' in inbox_message.to_address:
            # Create support ticket
            create_support_ticket(sender, subject, message)
        elif 'billing' in inbox_message.to_address:
            # Handle billing inquiry
            handle_billing_inquiry(inbox_message)
        
        # Send auto-reply if needed
        if should_send_auto_reply(inbox_message):
            send_auto_reply(inbox_message)
        
        logger.info(f"Processed email from {sender}")
        return True
        
    except Exception as e:
        logger.error(f"Error processing email: {e}")
        return False

async def on_email_received_async(inbox_message: InboxMessage) -> bool:
    """Async version for high-volume processing"""
    # Implement async processing logic
    return True
```

#### Outbox Handler (Sending Messages)

```python
def on_email_send(outbox_message: OutboxMessage) -> bool:
    """Handle sending email messages"""
    try:
        # Mark as sending
        outbox_message.mark_sending()
        
        # Get email settings
        account = outbox_message.account
        smtp_settings = account.get_setting('smtp', {})
        
        # Send the email
        send_via_smtp(outbox_message, smtp_settings)
        
        # Mark as sent
        outbox_message.mark_sent(message_id='external_message_id')
        return True
        
    except Exception as e:
        # Mark as failed
        outbox_message.mark_failed(str(e))
        return False
```

### Handler Configuration

Configure handlers in your models:

```python
# In your inbox
inbox.sync_handler = 'myapp.handlers.on_email_received'
inbox.async_handler = 'myapp.handlers.on_email_received_async'

# In your outbox
outbox.handler = 'myapp.handlers.on_email_send'
```

## Management Commands

### Process Notifications

Use the management command to process messages:

```bash
# Process all pending messages once
python manage.py process_notifications

# Run as daemon (continuous processing)
python manage.py process_notifications --daemon --interval=30

# Process only inbox messages
python manage.py process_notifications --inbox-only

# Process only outbox messages
python manage.py process_notifications --outbox-only

# Process only email messages
python manage.py process_notifications --kind=email

# Retry failed messages
python manage.py process_notifications --retry-failed --max-age-hours=12

# Limit batch size
python manage.py process_notifications --limit=50
```

### Production Deployment

For production, run the processor as a daemon:

```bash
# Using systemd or supervisor
python manage.py process_notifications --daemon --interval=10
```

## Message Status Tracking

### Outbox Message Status

- `PENDING`: Message is queued for sending
- `SENDING`: Message is currently being sent
- `SENT`: Message was successfully sent
- `FAILED`: Message failed to send
- `CANCELLED`: Message was cancelled

### Status Checking

```python
# Check message status
if outbox_message.is_sent:
    print("Message delivered successfully")
elif outbox_message.is_failed:
    print(f"Message failed: {outbox_message.error_message}")
    if outbox_message.can_retry:
        print("Message can be retried")

# Retry failed messages
if outbox_message.can_retry:
    outbox_message.reset_for_retry()
```

## Rate Limiting

Configure rate limits on outboxes:

```python
# Limit to 1000 messages per hour
outbox.rate_limit = 1000
outbox.save()

# Check rate limit
if outbox.check_rate_limit():
    # Safe to send
    pass
else:
    # Rate limit exceeded
    pass
```

## Message Metadata

Use metadata to store additional information:

```python
# Email with attachments info
email_message = send_email(
    to_address='user@example.com',
    subject='Invoice',
    message='Please find your invoice attached.',
    metadata={
        'attachments': ['invoice.pdf'],
        'category': 'billing',
        'priority': 'high'
    }
)

# SMS with delivery tracking
sms_message = send_sms(
    to_address='+1234567890',
    message='Your order has shipped!',
    metadata={
        'order_id': '12345',
        'tracking_url': 'https://track.example.com/12345'
    }
)

# Access metadata
attachment_info = email_message.get_metadata_value('attachments')
```

## Statistics and Monitoring

```python
from mojo.apps.notify.utils.notifications import MessageStats

# Get outbox statistics
stats = MessageStats.get_outbox_stats(my_outbox)
print(f"Total sent: {stats['sent_messages']}")
print(f"Failed: {stats['failed_messages']}")
print(f"Recent (24h): {stats['recent_messages']}")

# Get inbox statistics  
inbox_stats = MessageStats.get_inbox_stats(my_inbox)
print(f"Unprocessed: {inbox_stats['unprocessed_messages']}")
```

## Integration Examples

### With Django Signals

```python
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User
from mojo.apps.notify.utils.notifications import send_email

@receiver(post_save, sender=User)
def send_welcome_email(sender, instance, created, **kwargs):
    if created:
        send_email(
            to_address=instance.email,
            subject='Welcome!',
            message=f'Welcome to our platform, {instance.first_name}!',
            user=instance,
            metadata={'trigger': 'user_registration'}
        )
```

### With Celery (Async)

```python
from celery import shared_task
from mojo.apps.notify.utils.notifications import send_email

@shared_task
def send_notification_email(user_id, subject, message):
    from django.contrib.auth.models import User
    user = User.objects.get(id=user_id)
    
    return send_email(
        to_address=user.email,
        subject=subject,
        message=message,
        user=user
    )
```

### With REST API

```python
# views.py
from rest_framework.decorators import api_view
from rest_framework.response import Response
from mojo.apps.notify.utils.notifications import send_sms

@api_view(['POST'])
def send_sms_notification(request):
    phone = request.data.get('phone')
    message = request.data.get('message')
    
    sms_message = send_sms(
        to_address=phone,
        message=message,
        user=request.user
    )
    
    return Response({
        'message_id': sms_message.id,
        'status': sms_message.status
    })
```

## Configuration

### Settings

Add to your Django settings:

```python
# settings.py

# Notification system settings
NOTIFICATION_SETTINGS = {
    'DEFAULT_FROM_EMAIL': 'noreply@example.com',
    'DEFAULT_SMS_FROM': '+1234567890',
    'RATE_LIMIT_WINDOW': 3600,  # 1 hour in seconds
    'MAX_RETRY_ATTEMPTS': 3,
    'PROCESSING_BATCH_SIZE': 100,
}

# Add to installed apps
INSTALLED_APPS = [
    # ... other apps
    'mojo.apps.notify',
]
```

### Database Migration

Run migrations to create the notification tables:

```bash
python manage.py makemigrations notify
python manage.py migrate notify
```

## Admin Interface

The system includes Django admin integration for managing accounts, inboxes, outboxes, and messages. Access it at `/admin/notify/`.

## Security Considerations

1. **API Keys**: Store sensitive settings like API keys in environment variables
2. **Rate Limiting**: Configure appropriate rate limits to prevent abuse
3. **Validation**: Validate all input addresses and content
4. **Permissions**: Use Django's permission system to control access
5. **Logging**: Log all message activities for audit trails

## Troubleshooting

### Common Issues

1. **Handler Import Errors**: Ensure handler functions are properly importable
2. **Rate Limit Exceeded**: Check outbox rate limits and adjust as needed
3. **Failed Messages**: Check error messages in OutboxMessage.error_message
4. **Missing Settings**: Verify account settings are properly configured

### Debugging

```python
# Enable debug logging
import logging
logging.getLogger('mojo.apps.notify').setLevel(logging.DEBUG)

# Check message status
message = OutboxMessage.objects.get(id=123)
print(f"Status: {message.status}")
print(f"Error: {message.error_message}")
print(f"Retry count: {message.retry_count}")
```

## Performance Tips

1. Use async handlers for high-volume processing
2. Configure appropriate batch sizes
3. Monitor rate limits and adjust as needed
4. Use database indexes on frequently queried fields
5. Consider using Celery for background processing
6. Implement message archiving for old messages

## Support

For issues and questions:
1. Check the logs for error messages
2. Verify handler configuration
3. Test with small batches first
4. Monitor rate limits and quotas