# Email API — REST API Reference

## Permissions Required

- `manage_aws` or `manage_users` (admin-level)

Most email operations are backend-only (triggered by application logic). The REST API is primarily for administration.

## Email Templates

### List Templates

**GET** `/api/aws/emailtemplate`

```json
{
  "status": true,
  "count": 5,
  "data": [
    {"id": 1, "name": "welcome", "subject": "Welcome to {{app_name}}!"},
    {"id": 2, "name": "password_reset_code", "subject": "Your reset code"}
  ]
}
```

### Get Template

**GET** `/api/aws/emailtemplate/1`

```json
{
  "status": true,
  "data": {
    "id": 1,
    "name": "welcome",
    "subject": "Welcome to {{app_name}}!",
    "body_html": "<p>Hello {{display_name}}</p>",
    "body_text": "Hello {{display_name}}"
  }
}
```

### Create / Update Template

**POST** `/api/aws/emailtemplate`

```json
{
  "name": "order_confirmation",
  "subject": "Your order #{{order_id}} is confirmed",
  "body_html": "<p>Thank you {{display_name}}, your order is confirmed.</p>",
  "body_text": "Thank you {{display_name}}, your order is confirmed."
}
```

Template variables use `{{variable_name}}` syntax.

## Mailboxes

### List Mailboxes

**GET** `/api/aws/mailbox`

```json
{
  "status": true,
  "data": [
    {
      "id": 1,
      "email": "noreply@myapp.example.com",
      "allow_outbound": true,
      "allow_inbound": false,
      "is_system_default": true
    }
  ]
}
```

## Sent Messages (Audit Log)

### List Sent Messages

**GET** `/api/aws/sentmessage`

```
GET /api/aws/sentmessage?to_email=alice@example.com&sort=-created
```

```json
{
  "status": true,
  "data": [
    {
      "id": 501,
      "created": "2024-01-15T10:00:00Z",
      "to_email": "alice@example.com",
      "from_email": "noreply@myapp.example.com",
      "subject": "Welcome to MyApp!",
      "template": "welcome",
      "status": "sent"
    }
  ]
}
```

## Filtering

```
GET /api/aws/sentmessage?status=sent&dr_start=2024-01-01
GET /api/aws/sentmessage?to_email=alice@example.com
```
