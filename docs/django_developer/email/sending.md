# Sending Email — Django Developer Reference

The preferred way to send email is via the `aws` module. It resolves the system default mailbox automatically so you never need to instantiate a `Mailbox` directly.

```python
from mojo.apps import aws
```

---

## `aws.send_email()`

Send a plain email from the system default mailbox.

```python
aws.send_email(
    to="alice@example.com",
    subject="Hello Alice",
    body_html="<p>Welcome aboard.</p>",
    body_text="Welcome aboard."
)
```

### Signature

```python
aws.send_email(
    to,                     # str or list[str] — one or more recipients
    subject=None,           # str
    body_text=None,         # str — plain text body
    body_html=None,         # str — HTML body
    cc=None,                # str or list[str]
    bcc=None,               # str or list[str]
    reply_to=None,          # str or list[str]
    mailbox=None,           # Mailbox instance — defaults to system default
    **kwargs                # passed through to the underlying email service
)
```

### Parameters

| Parameter | Type | Description |
|---|---|---|
| `to` | `str` or `list[str]` | Recipient address(es) |
| `subject` | `str` | Email subject line |
| `body_text` | `str` | Plain text body (optional but recommended) |
| `body_html` | `str` | HTML body |
| `cc` | `str` or `list[str]` | CC address(es) |
| `bcc` | `str` or `list[str]` | BCC address(es) |
| `reply_to` | `str` or `list[str]` | Reply-to address(es) |
| `mailbox` | `Mailbox` | Override the sending mailbox. Defaults to `Mailbox.get_system_default()` |

### Returns

`SentMessage` instance.

### Examples

```python
# Single recipient
aws.send_email(
    to="alice@example.com",
    subject="Direct Send",
    body_html="<p>Content</p>",
    body_text="Content"
)

# Multiple recipients with CC/BCC
aws.send_email(
    to=["alice@example.com", "bob@example.com"],
    cc="manager@example.com",
    bcc="audit@example.com",
    subject="Team Update",
    body_html="<p>Update content</p>"
)

# Explicit from address via a specific mailbox
from mojo.apps.aws.models import Mailbox

mailbox = Mailbox.objects.get(email="noreply@myapp.example.com")
aws.send_email(
    to="alice@example.com",
    subject="From a specific sender",
    body_html="<p>Hello</p>",
    mailbox=mailbox
)
```

---

## `aws.send_template_email()`

Send an email using a named `EmailTemplate` stored in the database. Subject and body are rendered from the template with the supplied context.

```python
aws.send_template_email(
    to="alice@example.com",
    template_name="welcome",
    context={"display_name": "Alice", "app_name": "MyApp"}
)
```

### Signature

```python
aws.send_template_email(
    to,                     # str or list[str] — one or more recipients
    template_name,          # str — name of the EmailTemplate record
    context=None,           # dict — template variables
    cc=None,                # str or list[str]
    bcc=None,               # str or list[str]
    reply_to=None,          # str or list[str]
    mailbox=None,           # Mailbox instance — defaults to system default
    **kwargs                # passed through to the underlying email service
)
```

### Parameters

| Parameter | Type | Description |
|---|---|---|
| `to` | `str` or `list[str]` | Recipient address(es) |
| `template_name` | `str` | Name of the `EmailTemplate` in the database |
| `context` | `dict` | Variables substituted into subject and body |
| `cc` | `str` or `list[str]` | CC address(es) |
| `bcc` | `str` or `list[str]` | BCC address(es) |
| `reply_to` | `str` or `list[str]` | Reply-to address(es) |
| `mailbox` | `Mailbox` | Override the sending mailbox. Defaults to `Mailbox.get_system_default()` |

### Returns

`SentMessage` instance.

### Domain Template Overrides

When a mailbox belongs to a domain, `send_template_email` automatically checks for a domain-specific template override before falling back to the base template.

If the template `"{domain_name}.{template_name}"` exists, it is used instead. This allows per-tenant email branding with no code changes.

```
welcome              → base template (fallback)
acmecorp.welcome     → used automatically when sending from an acmecorp mailbox
```

### Template Syntax

Templates use `{{variable}}` syntax. Context values are substituted into both the subject and body fields.

### Examples

```python
# Welcome email
aws.send_template_email(
    to="alice@example.com",
    template_name="welcome",
    context={"display_name": "Alice", "app_name": "MyApp"}
)

# Password reset — code method
aws.send_template_email(
    to=user.email,
    template_name="password_reset_code",
    context={"code": "482917", "display_name": user.display_name}
)

# Password reset — link method
aws.send_template_email(
    to=user.email,
    template_name="password_reset_link",
    context={"token": reset_token, "display_name": user.display_name}
)

# Group invite
aws.send_template_email(
    to=user.email,
    template_name="group_invite",
    context={"group": group.to_dict("basic"), "display_name": user.display_name}
)
```

---

## Via User Model

`User.send_template_email()` is a convenience wrapper that resolves the correct mailbox for the user (user's org domain → system default). Prefer this when sending to a specific user.

```python
user.send_template_email(
    "welcome",
    context={"display_name": user.display_name}
)

user.send_template_email(
    "group_invite",
    context={"group": group.to_dict("basic")},
    group=group   # routes mailbox selection through the group's domain
)
```

---

## Async Sending

For high-volume or non-blocking sends, route through the jobs system:

```python
from mojo.apps import jobs

jobs.enqueue("mojo.apps.aws.send_email", kwargs={
    "to": "alice@example.com",
    "subject": "...",
    "body_html": "..."
})
```

---

## Built-in Templates

These template names are used by the framework. Create matching `EmailTemplate` records with your desired content.

| Template Name | Used By | Key Context Variables |
|---|---|---|
| `invite` | `User.send_invite()` | `user`, `token` |
| `group_invite` | `GroupMember.send_invite()` | `group`, `display_name` |
| `password_reset_code` | Forgot password (code flow) | `code`, `display_name` |
| `password_reset_link` | Forgot password (link flow) | `token`, `display_name` |
| `magic_login_link` | Magic login | `token`, `display_name` |
| `email_verify` | Email verification | `token`, `display_name` |

---

## Common Context Variables

| Variable | Source |
|---|---|
| `display_name` | `user.display_name` |
| `full_name` | `user.full_name` |
| `username` | `user.username` |
| `email` | `user.email` |
| `app_name` | From settings or context |
| `code` | OTP code |
| `token` | Signed token (reset, invite, verify) |
| `group` | `group.to_dict("basic")` |