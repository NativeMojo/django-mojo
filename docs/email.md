# Email System Documentation

Django-MOJO provides a complete email system built on AWS SES (Simple Email Service) with support for sending, receiving, templates, and delivery tracking.

## Table of Contents

- [Overview](#overview)
- [Core Concepts](#core-concepts)
- [Email Domains](#email-domains)
- [Mailboxes](#mailboxes)
- [Sending Emails](#sending-emails)
- [Email Templates](#email-templates)
- [Receiving Emails](#receiving-emails)
- [Delivery Tracking](#delivery-tracking)
- [Configuration](#configuration)
- [API Reference](#api-reference)

## Overview

The email system (`mojo.apps.aws`) provides:

- **Domain Management**: Configure and verify email domains in AWS SES
- **Mailbox Management**: Individual email addresses with send/receive controls
- **Templating**: Django template-based email composition with variable substitution
- **Sending**: Multiple methods for sending emails (plain, HTML, templated)
- **Receiving**: Inbound email processing via SES + S3
- **Tracking**: Full delivery lifecycle tracking (sent, delivered, bounced, complained)
- **SNS Integration**: Automatic webhook handling for delivery events

## Core Concepts

### Architecture

```
EmailDomain (example.com)
  ├── Mailboxes (support@example.com, noreply@example.com)
  ├── AWS SES Configuration (verification, DKIM, SPF)
  ├── SNS Topics (bounce, complaint, delivery notifications)
  └── S3 Bucket (inbound email storage)

Mailbox
  ├── send_email() → SentMessage
  ├── send_template_email() → SentMessage
  └── Inbound emails → IncomingEmail

EmailTemplate (Django templates)
  ├── subject_template
  ├── html_template
  └── text_template
```

### Models

1. **EmailDomain**: Represents a verified domain in SES (e.g., example.com)
2. **Mailbox**: Individual email address within a domain (e.g., support@example.com)
3. **EmailTemplate**: Reusable email templates with Django template syntax
4. **SentMessage**: Outbound email tracking with delivery status
5. **IncomingEmail**: Inbound emails received via SES

## Email Domains

### Creating a Domain

```python
from mojo.apps.aws.models import EmailDomain

domain = EmailDomain.objects.create(
    name="example.com",
    region="us-east-1",
    receiving_enabled=True,
    s3_inbound_bucket="my-email-bucket",
    s3_inbound_prefix="inbound/example.com/",
    dns_mode="manual"  # or "route53" for automatic DNS
)

# Set AWS credentials (stored encrypted)
domain.set_aws_key("AKIA...")
domain.set_aws_secret("secret...")
domain.save()
```

### Domain Properties

- **name**: The domain name (e.g., example.com)
- **region**: AWS region for SES operations
- **status**: Domain verification status (`pending`, `ready`, `missing`)
- **receiving_enabled**: Enable inbound email processing
- **s3_inbound_bucket**: S3 bucket for storing received emails
- **s3_inbound_prefix**: S3 key prefix for organization
- **dns_mode**: DNS configuration method (`manual`, `route53`, `godaddy`)
- **can_send**: Computed flag indicating send readiness
- **can_recv**: Computed flag indicating receive readiness

### Domain Verification

After creating a domain, verify it in AWS SES:

1. The domain is automatically registered with SES on creation
2. DNS records (DKIM, SPF, MX) must be configured
3. Run audit to check verification status:

```python
from mojo.helpers.aws.ses_domain import audit_domain_config

audit_result = audit_domain_config(
    domain="example.com",
    region="us-east-1"
)
print(audit_result)
```

### SNS Topics

The domain automatically creates SNS topics for:
- **Bounce notifications**: `sns_topic_bounce_arn`
- **Complaint notifications**: `sns_topic_complaint_arn`
- **Delivery notifications**: `sns_topic_delivery_arn`
- **Inbound notifications**: `sns_topic_inbound_arn`

These topics are used to track email delivery lifecycle.

## Mailboxes

### Creating a Mailbox

```python
from mojo.apps.aws.models import Mailbox, EmailDomain

domain = EmailDomain.objects.get(name="example.com")

mailbox = Mailbox.objects.create(
    domain=domain,
    email="support@example.com",
    allow_inbound=True,
    allow_outbound=True,
    is_system_default=True  # Optional: make this the default mailbox
)
```

### Mailbox Properties

- **email**: Full email address (must match domain)
- **domain**: Parent EmailDomain
- **allow_inbound**: Enable receiving emails at this address
- **allow_outbound**: Enable sending emails from this address
- **async_handler**: Optional dotted path to async processor (e.g., "myapp.handlers:process_email")
- **is_system_default**: System-wide default mailbox (only one allowed)
- **is_domain_default**: Default mailbox for this domain (one per domain)

### Default Mailboxes

```python
# Get system-wide default
default_mailbox = Mailbox.get_system_default()

# Get domain-specific default
domain_mailbox = Mailbox.get_domain_default("example.com")

# Smart default (prefers domain, falls back to system)
mailbox = Mailbox.get_default(domain="example.com", prefer_domain=True)
```

## Sending Emails

### Method 1: Direct Service API

```python
from mojo.apps.aws.services import email as email_service

# Simple email
sent = email_service.send_email(
    from_email="support@example.com",
    to=["user@customer.com", "admin@customer.com"],
    subject="Welcome to Our Service",
    body_text="Welcome! This is a plain text email.",
    body_html="<html><body><h1>Welcome!</h1><p>This is HTML email.</p></body></html>",
    cc=["manager@customer.com"],
    reply_to=["help@example.com"]
)

print(f"Sent with MessageId: {sent.ses_message_id}")
print(f"Status: {sent.status}")
```

### Method 2: Via Mailbox Instance

```python
from mojo.apps.aws.models import Mailbox

mailbox = Mailbox.objects.get(email="support@example.com")

# Send plain email
sent = mailbox.send_email(
    to="user@customer.com",
    subject="Your Order Confirmation",
    body_text="Your order #12345 has been confirmed.",
    body_html="<p>Your order <strong>#12345</strong> has been confirmed.</p>"
)
```

### Method 3: Using Django Templates

```python
# Create template first (see Email Templates section)

# Send using template
sent = mailbox.send_template_email(
    to="user@customer.com",
    template_name="order_confirmation",
    context={
        "order_number": "12345",
        "customer_name": "John Doe",
        "total": "$99.99"
    }
)
```

### Advanced Sending Options

```python
# With CC, BCC, custom reply-to
sent = email_service.send_email(
    from_email="noreply@example.com",
    to="user@customer.com",
    subject="Weekly Newsletter",
    body_html="<h1>This Week's Updates</h1>",
    cc=["team@example.com"],
    bcc=["archive@example.com"],
    reply_to=["support@example.com"]
)

# Allow unverified domain (use with caution)
sent = email_service.send_email(
    from_email="test@newdomain.com",
    to="user@customer.com",
    subject="Test Email",
    body_text="Testing...",
    allow_unverified=True
)

# Custom AWS credentials
sent = email_service.send_email(
    from_email="support@example.com",
    to="user@customer.com",
    subject="Custom Credentials",
    body_text="Sent with different AWS account",
    aws_access_key="AKIA...",
    aws_secret_key="secret...",
    region="eu-west-1"
)
```

## Email Templates

### Template Model

Templates use Django's template syntax for rendering subject, HTML body, and plain text body.

### Creating a Template

```python
from mojo.apps.aws.models import EmailTemplate

template = EmailTemplate.objects.create(
    name="order_confirmation",
    subject_template="Order #{{ order_number }} Confirmed",
    html_template="""
        <html>
        <body>
            <h1>Thank you, {{ customer_name }}!</h1>
            <p>Your order <strong>#{{ order_number }}</strong> has been confirmed.</p>
            <p>Total: {{ total }}</p>
            <p>Expected delivery: {{ delivery_date|date:"F d, Y" }}</p>
        </body>
        </html>
    """,
    text_template="""
        Thank you, {{ customer_name }}!
        
        Your order #{{ order_number }} has been confirmed.
        Total: {{ total }}
        Expected delivery: {{ delivery_date }}
    """,
    metadata={
        "description": "Order confirmation email",
        "category": "transactional"
    }
)
```

### Using Templates

```python
# Via service API
sent = email_service.send_with_template(
    from_email="orders@example.com",
    to="customer@email.com",
    template_name="order_confirmation",
    context={
        "customer_name": "Jane Smith",
        "order_number": "67890",
        "total": "$149.99",
        "delivery_date": datetime.date(2025, 2, 15)
    }
)

# Via mailbox instance
mailbox = Mailbox.objects.get(email="orders@example.com")
sent = mailbox.send_template_email(
    to="customer@email.com",
    template_name="order_confirmation",
    context={"order_number": "67890", "customer_name": "Jane Smith", "total": "$149.99"}
)
```

### Domain-Specific Template Overrides

Templates support domain-specific overrides. If a template named `{domain.name}.{template_name}` exists, it will be used instead of the base template:

```python
# Base template
EmailTemplate.objects.create(
    name="welcome",
    subject_template="Welcome!",
    html_template="<p>Welcome to our service!</p>"
)

# Domain-specific override for example.com
EmailTemplate.objects.create(
    name="example.com.welcome",
    subject_template="Welcome to Example Corp!",
    html_template="<p>Welcome to Example Corp's premium service!</p>"
)

# When sending from example.com mailbox, the override is automatically used
mailbox = Mailbox.objects.get(email="support@example.com")
sent = mailbox.send_template_email(
    to="user@customer.com",
    template_name="welcome",  # Automatically uses "example.com.welcome"
    context={}
)
```

### Template Rendering

Templates can be rendered independently for testing:

```python
template = EmailTemplate.objects.get(name="order_confirmation")

# Render all parts
rendered = template.render_all({
    "customer_name": "Test User",
    "order_number": "12345",
    "total": "$50.00"
})

print(rendered["subject"])  # "Order #12345 Confirmed"
print(rendered["html"])     # Rendered HTML
print(rendered["text"])     # Rendered plain text

# Render individual parts
subject = template.render_subject(context)
html = template.render_html(context)
text = template.render_text(context)
```

## Receiving Emails

### Setup

1. Configure domain with receiving enabled:

```python
domain = EmailDomain.objects.create(
    name="example.com",
    region="us-east-1",
    receiving_enabled=True,
    s3_inbound_bucket="my-inbound-emails",
    s3_inbound_prefix="inbound/example.com/"
)
```

2. Create mailboxes with inbound enabled:

```python
mailbox = Mailbox.objects.create(
    domain=domain,
    email="support@example.com",
    allow_inbound=True,
    async_handler="myapp.handlers:process_support_email"  # Optional
)
```

3. Configure SES receipt rules (done automatically on domain creation)

### Inbound Email Processing

When an email arrives:

1. SES receives the email and stores it in S3
2. SES publishes to SNS topic
3. SNS webhook handler creates `IncomingEmail` record
4. Email is parsed and associated with matching mailbox
5. If mailbox has `async_handler`, it's invoked asynchronously

### IncomingEmail Model

```python
from mojo.apps.aws.models import IncomingEmail

# Query incoming emails
emails = IncomingEmail.objects.filter(
    mailbox__email="support@example.com",
    processed=False
)

for email in emails:
    print(f"From: {email.from_address}")
    print(f"Subject: {email.subject}")
    print(f"Body: {email.text_body}")
    print(f"S3 URL: {email.s3_object_url}")
    
    # Mark as processed
    email.processed = True
    email.process_status = "success"
    email.save()
```

### Custom Async Handlers

Define a handler for processing inbound emails:

```python
# myapp/handlers.py

def process_support_email(incoming_email):
    """
    Custom handler for processing support emails.
    Called asynchronously when email arrives.
    
    Args:
        incoming_email: IncomingEmail instance
    """
    from mojo.apps.aws.models import IncomingEmail
    
    # Access email data
    subject = incoming_email.subject
    from_addr = incoming_email.from_address
    body = incoming_email.text_body or incoming_email.html_body
    
    # Your processing logic
    if "urgent" in subject.lower():
        # Create ticket, send alert, etc.
        pass
    
    # Mark as processed
    incoming_email.processed = True
    incoming_email.process_status = "success"
    incoming_email.save()
```

Set handler on mailbox:

```python
mailbox.async_handler = "myapp.handlers:process_support_email"
mailbox.save()
```

## Delivery Tracking

### SentMessage Status Lifecycle

```
queued → sending → delivered
                ↘ bounced
                ↘ complained
                ↘ failed
```

### Status Codes

- **queued**: Email queued for sending
- **sending**: Submitted to SES, awaiting delivery
- **delivered**: Successfully delivered to recipient
- **bounced**: Delivery failed (hard or soft bounce)
- **complained**: Recipient marked as spam
- **failed**: Send attempt failed
- **unknown**: Status unclear

### Querying Sent Messages

```python
from mojo.apps.aws.models import SentMessage

# Get recent sent emails
recent = SentMessage.objects.filter(
    mailbox__email="support@example.com"
).order_by("-created")[:10]

# Check delivery status
for msg in recent:
    print(f"{msg.subject}: {msg.status}")
    if msg.status == SentMessage.STATUS_BOUNCED:
        print(f"  Bounce reason: {msg.status_reason}")

# Get bounced emails
bounced = SentMessage.objects.filter(
    status=SentMessage.STATUS_BOUNCED,
    created__gte=datetime.now() - timedelta(days=7)
)

# Get complaints
complaints = SentMessage.objects.filter(
    status=SentMessage.STATUS_COMPLAINED
)
```

### SNS Webhooks

Status updates are received automatically via SNS webhooks:

- **Bounce**: Updates `status` to `bounced`, stores bounce details in `status_reason`
- **Complaint**: Updates `status` to `complained`, stores complaint details
- **Delivery**: Updates `status` to `delivered`

Webhook endpoints are automatically configured when creating an EmailDomain.

## Configuration

### Django Settings

```python
# settings.py

# AWS credentials (can be overridden per-domain)
AWS_KEY = "AKIA..."
AWS_SECRET = "secret..."
AWS_REGION = "us-east-1"

# Email defaults
MOJO_EMAIL_DEFAULT_FROM = "noreply@example.com"

# S3 bucket for inbound emails
MOJO_EMAIL_INBOUND_BUCKET = "my-inbound-emails"
```

### Environment Variables

```bash
# AWS credentials
export AWS_ACCESS_KEY_ID="AKIA..."
export AWS_SECRET_ACCESS_KEY="secret..."
export AWS_DEFAULT_REGION="us-east-1"

# Email settings
export MOJO_EMAIL_DEFAULT_FROM="noreply@example.com"
export MOJO_EMAIL_INBOUND_BUCKET="my-inbound-emails"
```

## API Reference

### Service Functions

#### `send_email()`

```python
from mojo.apps.aws.services import email as email_service

sent = email_service.send_email(
    from_email: str,
    to: Union[str, List[str]],
    subject: Optional[str] = None,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    cc: Optional[Union[str, List[str]]] = None,
    bcc: Optional[Union[str, List[str]]] = None,
    reply_to: Optional[Union[str, List[str]]] = None,
    allow_unverified: bool = False,
    aws_access_key: Optional[str] = None,
    aws_secret_key: Optional[str] = None,
    region: Optional[str] = None
) -> SentMessage
```

**Args:**
- `from_email`: Sending address (must match a Mailbox)
- `to`: One or more recipient addresses
- `subject`: Email subject
- `body_text`: Plain text body
- `body_html`: HTML body
- `cc`, `bcc`, `reply_to`: Optional addressing
- `allow_unverified`: Bypass domain verification check
- `aws_access_key`, `aws_secret_key`: Override AWS credentials
- `region`: Override AWS region

**Returns:** `SentMessage` instance

**Raises:**
- `MailboxNotFound`: Invalid from_email
- `OutboundNotAllowed`: Mailbox has allow_outbound=False
- `DomainNotVerified`: Domain not verified (unless allow_unverified=True)

#### `send_with_template()`

```python
sent = email_service.send_with_template(
    from_email: str,
    to: Union[str, List[str]],
    template_name: str,
    context: Optional[Dict[str, Any]] = None,
    cc: Optional[Union[str, List[str]]] = None,
    bcc: Optional[Union[str, List[str]]] = None,
    reply_to: Optional[Union[str, List[str]]] = None,
    allow_unverified: bool = False,
    aws_access_key: Optional[str] = None,
    aws_secret_key: Optional[str] = None,
    region: Optional[str] = None
) -> SentMessage
```

Send using Django EmailTemplate from database.

**Args:**
- `from_email`: Sending address
- `to`: Recipients
- `template_name`: Name of EmailTemplate
- `context`: Template context variables
- Other args same as `send_email()`

**Returns:** `SentMessage` instance

**Raises:** Same as `send_email()`, plus `ValueError` if template not found

#### `send_template_email()`

```python
sent = email_service.send_template_email(
    from_email: str,
    to: Union[str, List[str]],
    template_name: str,
    template_context: Optional[Dict[str, Any]] = None,
    # ... other args same as send_email()
) -> SentMessage
```

Send using AWS SES template (must exist in SES).

### Mailbox Methods

#### `send_email()`

```python
mailbox = Mailbox.objects.get(email="support@example.com")

sent = mailbox.send_email(
    to: Union[str, List[str]],
    subject: Optional[str] = None,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    cc: Optional[Union[str, List[str]]] = None,
    bcc: Optional[Union[str, List[str]]] = None,
    reply_to: Optional[Union[str, List[str]]] = None,
    **kwargs
) -> SentMessage
```

#### `send_template_email()`

```python
sent = mailbox.send_template_email(
    to: Union[str, List[str]],
    template_name: str,
    context: Optional[Dict[str, Any]] = None,
    cc: Optional[Union[str, List[str]]] = None,
    bcc: Optional[Union[str, List[str]]] = None,
    reply_to: Optional[Union[str, List[str]]] = None,
    **kwargs
) -> SentMessage
```

Automatically checks for domain-specific template overrides.

### EmailTemplate Methods

#### `render_all()`

```python
template = EmailTemplate.objects.get(name="welcome")

rendered = template.render_all(context: Optional[Dict] = None) -> Dict[str, Optional[str]]
# Returns: {"subject": "...", "text": "...", "html": "..."}
```

#### `render_subject()`, `render_html()`, `render_text()`

```python
subject = template.render_subject(context: Optional[Dict] = None) -> Optional[str]
html = template.render_html(context: Optional[Dict] = None) -> Optional[str]
text = template.render_text(context: Optional[Dict] = None) -> Optional[str]
```

### Class Methods

#### `Mailbox.get_system_default()`

```python
default = Mailbox.get_system_default() -> Optional[Mailbox]
```

Get system-wide default mailbox.

#### `Mailbox.get_domain_default()`

```python
default = Mailbox.get_domain_default(domain: Union[str, EmailDomain]) -> Optional[Mailbox]
```

Get default mailbox for specific domain.

#### `Mailbox.get_default()`

```python
default = Mailbox.get_default(
    domain: Optional[Union[str, EmailDomain]] = None,
    prefer_domain: bool = True
) -> Optional[Mailbox]
```

Smart default: prefers domain default, falls back to system default.

## Examples

### Complete Sending Flow

```python
from mojo.apps.aws.models import EmailDomain, Mailbox, EmailTemplate
from mojo.apps.aws.services import email as email_service

# 1. Setup domain
domain = EmailDomain.objects.create(
    name="mycompany.com",
    region="us-east-1"
)
domain.set_aws_key("AKIA...")
domain.set_aws_secret("secret...")
domain.save()

# 2. Create mailbox
mailbox = Mailbox.objects.create(
    domain=domain,
    email="orders@mycompany.com",
    allow_outbound=True,
    is_system_default=True
)

# 3. Create template
template = EmailTemplate.objects.create(
    name="order_shipped",
    subject_template="Your Order #{{ order_id }} Has Shipped!",
    html_template="""
        <h1>Good news, {{ customer_name }}!</h1>
        <p>Your order #{{ order_id }} has shipped.</p>
        <p>Tracking: {{ tracking_number }}</p>
    """,
    text_template="""
        Good news, {{ customer_name }}!
        Your order #{{ order_id }} has shipped.
        Tracking: {{ tracking_number }}
    """
)

# 4. Send email
sent = mailbox.send_template_email(
    to="customer@email.com",
    template_name="order_shipped",
    context={
        "customer_name": "John Doe",
        "order_id": "12345",
        "tracking_number": "1Z999AA10123456784"
    }
)

print(f"Email sent! MessageId: {sent.ses_message_id}")
print(f"Status: {sent.status}")
```

### Bulk Sending

```python
from mojo.apps.aws.models import Mailbox

mailbox = Mailbox.objects.get(email="newsletter@company.com")
subscribers = ["user1@example.com", "user2@example.com", "user3@example.com"]

for email in subscribers:
    sent = mailbox.send_template_email(
        to=email,
        template_name="weekly_newsletter",
        context={"week": "2025-01-27"}
    )
    print(f"Sent to {email}: {sent.ses_message_id}")
```

### Error Handling

```python
from mojo.apps.aws.services import email as email_service
from mojo.apps.aws.services.email import (
    MailboxNotFound, 
    OutboundNotAllowed, 
    DomainNotVerified
)

try:
    sent = email_service.send_email(
        from_email="support@example.com",
        to="user@customer.com",
        subject="Test",
        body_text="Test email"
    )
except MailboxNotFound as e:
    print(f"Mailbox not configured: {e}")
except OutboundNotAllowed as e:
    print(f"Sending disabled: {e}")
except DomainNotVerified as e:
    print(f"Domain not verified: {e}")
except Exception as e:
    print(f"Send failed: {e}")
```

## Best Practices

1. **Always verify domains** before sending production emails
2. **Use templates** for consistency and easier maintenance
3. **Monitor bounce rates** and handle bounced addresses
4. **Implement complaint handling** to maintain sender reputation
5. **Use domain-specific overrides** for white-label scenarios
6. **Set reply_to addresses** appropriately
7. **Test templates** with `render_all()` before sending
8. **Handle errors gracefully** with proper exception handling
9. **Use BCC sparingly** to avoid spam complaints
10. **Track delivery status** via SentMessage records

## Troubleshooting

### Domain Not Verified

Check verification status:

```python
domain = EmailDomain.objects.get(name="example.com")
print(domain.status)  # Should be "ready"
print(domain.can_send)  # Should be True
```

Run audit:

```python
from mojo.helpers.aws.ses_domain import audit_domain_config
audit_domain_config(domain="example.com", region="us-east-1")
```

### Emails Not Sending

1. Check mailbox `allow_outbound` is True
2. Verify domain status is "ready"
3. Check AWS credentials are set correctly
4. Review SentMessage `status_reason` for errors

### Emails Not Receiving

1. Check mailbox `allow_inbound` is True
2. Verify `receiving_enabled` on domain
3. Confirm S3 bucket exists and is accessible
4. Check SES receipt rules are configured
5. Verify MX records point to SES

### Template Rendering Errors

Test template rendering:

```python
template = EmailTemplate.objects.get(name="my_template")
try:
    rendered = template.render_all({"var1": "value1"})
    print(rendered)
except Exception as e:
    print(f"Template error: {e}")
```
