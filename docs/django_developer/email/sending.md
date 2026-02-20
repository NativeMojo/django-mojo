# Sending Email — Django Developer Reference

## Via Mailbox Instance

```python
from mojo.apps.aws.models import Mailbox

mailbox = Mailbox.get_system_default()

# Simple send
mailbox.send_email(
    to="alice@example.com",
    subject="Hello Alice",
    body_html="<p>This is a test.</p>",
    body_text="This is a test."
)

# With CC/BCC
mailbox.send_email(
    to="alice@example.com",
    cc=["bob@example.com"],
    bcc=["admin@example.com"],
    subject="Team Update",
    body_html="<p>Update content</p>"
)
```

## Via Template

```python
# Using the template named "welcome"
mailbox.send_template_email(
    template_name="welcome",
    to="alice@example.com",
    context={"display_name": "Alice", "app_name": "MyApp"}
)
```

Templates use `{{variable}}` syntax. Context values are substituted into subject and body.

## Via User Model

`User` has a `send_template_email` convenience method:

```python
user.send_template_email(
    "welcome",
    {"display_name": user.display_name}
)
```

This resolves the mailbox automatically (user's org → system default).

## Via Service Module

```python
from mojo.apps.aws import services as email_service

email_service.send(
    to="alice@example.com",
    subject="Direct Send",
    body_html="<p>Content</p>",
    from_email="noreply@myapp.example.com"
)
```

## Password Reset Emails

The `User` model's password reset flow uses templates:
- `password_reset_code` — OTP code reset
- `password_reset_link` — Link-based reset

Create these templates in `EmailTemplate` with your desired content.

## Async Sending

For high-volume or non-blocking sends, route through the jobs system:

```python
from mojo.apps import jobs

jobs.enqueue("mojo.apps.aws.services.send", kwargs={
    "to": "alice@example.com",
    "subject": "...",
    "body_html": "..."
})
```

## Common Template Variables

Build your templates to accept standard variables:

| Variable | Source |
|---|---|
| `display_name` | `user.display_name` |
| `username` | `user.username` |
| `email` | `user.email` |
| `app_name` | From settings or context |
| `code` | OTP code (password reset) |
| `token` | Reset token (password reset) |
