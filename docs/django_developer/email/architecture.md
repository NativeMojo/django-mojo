# Email Architecture & Setup — Django Developer Reference

## Core Models

| Model | Purpose |
|---|---|
| `EmailDomain` | Verified sending domain (SES) |
| `Mailbox` | Email address (sender/receiver) |
| `EmailTemplate` | Named email templates with subject/body |
| `SentMessage` | Audit record of every sent email |
| `IncomingEmail` | Received email with parsed metadata |
| `EmailAttachment` | Attachments linked to incoming/sent emails |

## Setup

### 1. Install the App

```python
INSTALLED_APPS = [
    ...
    "mojo.apps.aws",
]
```

### 2. AWS SES Credentials

```python
# settings.py
AWS_ACCESS_KEY_ID = "AKIA..."
AWS_SECRET_ACCESS_KEY = "..."
AWS_DEFAULT_REGION = "us-east-1"
AWS_SES_REGION = "us-east-1"
```

### 3. Create an EmailDomain

```python
from mojo.apps.aws.models import EmailDomain

domain = EmailDomain.objects.create(
    domain="myapp.example.com",
    is_active=True
)
# Run SES verification (add DNS records as instructed by AWS)
```

### 4. Create a Mailbox

```python
from mojo.apps.aws.models import Mailbox

mailbox = Mailbox.objects.create(
    domain=domain,
    email="noreply@myapp.example.com",
    allow_outbound=True,
    is_system_default=True
)
```

## Mailbox Model

```python
class Mailbox(models.Model, MojoModel):
    domain = models.ForeignKey(EmailDomain, ...)
    email = models.EmailField(unique=True)
    allow_inbound = models.BooleanField(default=False)
    allow_outbound = models.BooleanField(default=True)
    is_system_default = models.BooleanField(default=False)
    is_domain_default = models.BooleanField(default=False)
    async_handler = models.CharField(...)  # For inbound processing
```

## Getting the Default Mailbox

```python
from mojo.apps.aws.models import Mailbox

# System-wide default
mailbox = Mailbox.get_system_default()

# Default for a domain
mailbox = Mailbox.get_domain_default("myapp.example.com")

# Best available (group → domain → system)
mailbox = Mailbox.get_default(group=request.group)
```

## EmailTemplate Model

Templates use a simple subject/body system with variable substitution.

```python
from mojo.apps.aws.models import EmailTemplate

template = EmailTemplate.objects.create(
    name="welcome",
    subject="Welcome to {{app_name}}!",
    body_html="<p>Hello {{display_name}}, welcome aboard!</p>",
    body_text="Hello {{display_name}}, welcome aboard!"
)
```

## SentMessage Audit

Every sent email creates a `SentMessage` record for compliance and debugging:

```python
from mojo.apps.aws.models import SentMessage

sent = SentMessage.objects.filter(to_email="alice@example.com").order_by("-created")
```

## Settings

| Setting | Description |
|---|---|
| `AWS_ACCESS_KEY_ID` | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key |
| `AWS_DEFAULT_REGION` | AWS region |
| `AWS_SES_REGION` | SES-specific region (if different) |
| `EMAIL_DEFAULT_FROM` | Default from address |
