# Receiving Email — Django Developer Reference

## Overview

Inbound email requires:
1. A `Mailbox` with `allow_inbound=True`
2. An `async_handler` set on the mailbox (dot-notation path to a handler function)
3. AWS SES configured to forward inbound email to the API endpoint

## Mailbox Setup

```python
mailbox = Mailbox.objects.get(email="support@myapp.example.com")
mailbox.allow_inbound = True
mailbox.async_handler = "myapp.services.email.handle_support_email"
mailbox.save()
```

## Writing a Handler

```python
# myapp/services/email.py

def handle_support_email(incoming_email):
    """
    Called with an IncomingEmail instance when email arrives.
    """
    subject = incoming_email.subject
    from_addr = incoming_email.from_email
    body = incoming_email.body_text

    # Create a support ticket, notify team, etc.
    Ticket.objects.create(
        subject=subject,
        requester_email=from_addr,
        body=body
    )
```

## IncomingEmail Model

| Field | Description |
|---|---|
| `mailbox` | FK to receiving Mailbox |
| `from_email` | Sender address |
| `to_email` | Recipient address |
| `subject` | Email subject |
| `body_text` | Plain text body |
| `body_html` | HTML body (if provided) |
| `headers` | JSONField of raw headers |
| `metadata` | JSONField for handler-added data |
| `created` | Received timestamp |

## Attachments

```python
incoming_email.attachments.all()  # QuerySet of EmailAttachment
for attachment in incoming_email.attachments.all():
    filename = attachment.filename
    content_type = attachment.content_type
    file_instance = attachment.file  # FK to fileman.File
```

## SES Inbound Configuration

1. In AWS SES, configure a receipt rule for your domain
2. Set the rule to call the webhook endpoint: `POST /api/aws/email/sns/inbound`
3. The framework parses the SES notification and calls the mailbox handler
