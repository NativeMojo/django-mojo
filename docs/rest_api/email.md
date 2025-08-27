# Email REST API — Usage Guide and Portal Integration

Audience: Frontend/portal developers and automation/AI agents
Scope: Operate a full email system on AWS SES/SNS/S3 via MOJO’s REST APIs, with minimal AWS complexity exposed.

This guide covers:
- Concepts and data model
- Endpoints and request/response shapes
- End-to-end flows (domain onboarding, mailbox management, sending, receiving)
- Health, drift, and reconciliation
- A proposed “Portal Email Manager” high-level integration

---

## Concepts

- EmailDomain
  - Represents an SES domain identity and its AWS resources.
  - Can enable domain-level catch‑all receiving (SES receipt rule → S3 + SNS).
  - Stores SNS topic ARNs for inbound, bounce, complaint, and delivery.
  - Auto-audits and auto-reconciles on creation to adopt or create necessary resources.
  - Audit persistence: status is updated to "ready" when audit passes, or "missing" when audit fails (initial is "pending" on create). Boolean readiness flags can_send (outbound) and can_recv (inbound) are also persisted by the audit.

- Mailbox
  - Represents a single email address within a domain.
  - Only Mailbox-owned addresses can send. Inbound routing to Mailboxes is done in-app (catch‑all receipt rule at SES, routing by recipient email).
  - Optional async_handler (module:function string) to process inbound messages via the task system.

- IncomingEmail / EmailAttachment
  - Inbound messages are stored as parsed rows with headers/body and attachments.
  - Raw MIME remains in S3; attachments are stored in the same bucket.

- SentMessage
  - Outbound messages sent via SES; delivery lifecycle updated via SNS (delivered, bounced, complained).

- EmailTemplate
  - Stores Django template strings (subject/html/text) for rendering outbound emails.
  - Not to be confused with AWS SES templates. Both are supported.

---

## Base Paths and Permissions

- All endpoints summarized below start with:
  - /api/aws/email/...
- Most endpoints require the server-side permission: "manage_aws".
- Public SNS webhooks (no auth) validate Amazon SNS signatures and allowed TopicArns:
  - /api/aws/email/sns/inbound
  - /api/aws/email/sns/bounce
  - /api/aws/email/sns/complaint
  - /api/aws/email/sns/delivery

---

## Quick Start Flows

1) Create an EmailDomain
- POST /api/aws/email/domain
- Body: {"name": "example.com", "receiving_enabled": true, "s3_inbound_bucket": "inbound-bucket", "s3_inbound_prefix": "inbound/example.com/"}
- Behavior: Automatically audits and reconciles SES/SNS on create. Use onboarding to generate/apply DNS.

2) Onboard a Domain (DNS + SNS + optional receiving)
- POST /api/aws/email/domain/<id>/onboard
- Returns required DNS records (or applies via GoDaddy if credentials provided).

3) Create a Mailbox
- POST /api/aws/email/mailbox
- Body: {"domain": <domain_id>, "email": "support@example.com", "allow_inbound": true, "allow_outbound": true, "async_handler": "myapp.handlers.process_support"}

4) Send Email
- POST /api/aws/email/send
- Body: {"from_email": "support@example.com", "to": ["user@example.org"], "subject": "Hello", "body_text": "Hi there!"}

5) Optional: Use DB EmailTemplate (Django-templated)
- Create EmailTemplate via /api/aws/email/template
- Then send email via Python service (preferred) or mirror in REST (see Examples section).

6) Check Inbound and Sent Items
- GET /api/aws/email/incoming?search=...
- GET /api/aws/email/sent?search=...

---

## Endpoints

### EmailDomain

- GET /api/aws/email/domain
  - List domains (supports search/sort/pagination per MOJO patterns).
- POST /api/aws/email/domain
  - Create a domain. Automatically audits & reconciles. Fields:
    - name: string (required)
    - region: string (optional; defaults to project AWS_REGION)
    - receiving_enabled: bool (default false)
    - s3_inbound_bucket: string (required if receiving_enabled)
    - s3_inbound_prefix: string (optional)
    - dns_mode: "manual" | "godaddy" | "route53" (route53 may be future)
- GET /aws/email/domain/<id>
  - Detailed view with SNS topic ARNs, status ("pending" | "ready" | "missing"), and readiness flags can_send/can_recv (set by the last audit).
- PUT /api/aws/email/domain/<id>, DELETE /api/aws/email/domain/<id>
  - Modify or delete the domain record (deleting does not remove AWS resources automatically).

- POST /api/aws/email/domain/<id>/onboard
  - Orchestrates:
    - SES verification & DKIM → produces required DNS records.
    - Ensures SNS topics/subscriptions for bounce/complaint/delivery/inbound.
    - Optionally configures MAIL FROM and catch‑all receiving rule.
  - Request body:
    - receiving_enabled (bool), s3_inbound_bucket (string), s3_inbound_prefix (string)
    - ensure_mail_from (bool), mail_from_subdomain (string)
    - dns_mode: "manual" | "godaddy"
    - godaddy_key, godaddy_secret (when dns_mode="godaddy")
    - endpoints: {bounce, complaint, delivery, inbound} (HTTPS webhook URLs)
  - Response:
    - dns_records (for manual apply), dkim_tokens, topic_arns, receipt_rule, rule_set, notes

- GET|POST /aws/email/domain/<id>/audit
  - Returns a drift report with:
    - status ("ok" | "drifted" | "conflict"), audit_pass (boolean), checks (booleans for each subsystem), and detailed items (resource, desired, current, status).
  - Persists a summary on the EmailDomain:
    - status → "ready" if audit_pass is true, otherwise "missing" (initially "pending" on first create)
    - can_send → true when SES identity is verified, DKIM verified, SES production access is enabled, and SES identity notification topics map to the stored ARNs
    - can_recv → true (when receiving_enabled) if inbound S3 bucket exists, receipt rule S3Action and SNSAction match expectations, and SNS topics exist with at least one confirmed HTTPS subscription

- POST /api/aws/email/domain/<id>/reconcile
  - Applies safe fixes for SNS topics/mappings and receiving rule (no DNS writes).
  - Response: topic_arns, receipt_rule, rule_set, notes.

### Mailbox

- GET /api/aws/email/mailbox
- POST /api/aws/email/mailbox
  - Fields:
    - domain: FK (domain id) or domain name (implementation may accept name).
    - email: full address (unique).
    - allow_inbound, allow_outbound: booleans.
    - async_handler: "package.module:function" for task dispatch on inbound.
- GET /api/aws/email/mailbox/<id>, PUT /api/aws/email/mailbox/<id>, DELETE /api/aws/email/mailbox/<id>

Notes:
- No SES recipient-specific rules are created per mailbox; SES uses a domain-level catch‑all.
- Routing to a mailbox happens in-app, by matching recipient emails.

### Templates (Django EmailTemplate)

- GET /api/aws/email/template
- POST /api/aws/email/template
  - Fields:
    - name: unique string (required)
    - subject_template, html_template, text_template: Django template strings
    - metadata: object
- GET /api/aws/email/template/<id>, PUT /api/aws/email/template/<id>, DELETE /api/aws/email/template/<id>

### Sending

- POST /api/aws/email/send
  - Sends a plain/HTML email via SES:
    - from_email: string (required; must be a Mailbox)
    - to: string or array (required)
    - cc, bcc: string or array (optional)
    - subject: string (required if not using template rendering)
    - body_text, body_html: string (optional)
    - reply_to: string or array (optional; defaults to from_email)
    - template_name: string (optional; for AWS SES managed templates)
    - template_context: object (optional; for AWS SES managed templates)
    - allow_unverified: bool (defaults false; bypasses domain verification check)
  - Response:
    - { status: true, data: { id, ses_message_id, status } } on success
    - { error, details, sent_id } on failure

Notes:
- The REST API doesn’t expose DB EmailTemplate rendering directly; prefer Python service for that use case (see “Python Service API”).
- We can add a /send-with-template endpoint later if needed.

### Inbound and Sent Messages

- GET /api/aws/email/incoming
  - Query params:
    - search=, sort=, size=, start=, dr_field=, dr_start=, dr_end= (standard MOJO filters)
- GET /api/aws/email/incoming/<id>

- GET /api/aws/email/sent
- GET /api/aws/email/sent/<id>

### SNS Webhooks (Public)

- POST /api/aws/email/sns/inbound
- POST /api/aws/email/sns/bounce
- POST /api/aws/email/sns/complaint
- POST /api/aws/email/sns/delivery

Behavior:
- Validates SNS signature:
  - Signs cert URL must be HTTPS under sns.amazonaws.com or sns.<region>.amazonaws.com
  - Verifies signature (PKCS#1 v1.5, SHA1/SHA256) via cryptography
  - Ensures TopicArn matches a known ARN stored on any EmailDomain
- Supports SubscriptionConfirmation (auto-confirms via SubscribeURL)
- Inbound:
  - Extracts S3 bucket/key from the SES receipt action (or derives from prefix + messageId)
  - Downloads raw MIME, parses headers/body/attachments
  - Stores IncomingEmail + EmailAttachment, routes to Mailbox by recipient, enqueues Mailbox.async_handler if present
- Bounce/Complaint/Delivery:
  - Updates SentMessage status/status_reason using SES MessageId

Settings:
- SNS certificate caching in-memory (default 1 hour; override with SNS_CERT_TTL_SECONDS)
- Local dev bypass:
  - When settings.DEBUG=True and SNS_VALIDATION_BYPASS_DEBUG=True, signature validation is skipped (do not use in production).

---

## Data Shapes and Examples

### Create Domain (manual DNS flow)

Request:
```
POST /api/aws/email/domain
Content-Type: application/json

{
  "name": "example.com",
  "receiving_enabled": true,
  "s3_inbound_bucket": "my-inbound-bucket",
  "s3_inbound_prefix": "inbound/example.com/",
  "dns_mode": "manual"
}
```

Response (simplified):
```
{
  "status": true,
  "data": {
    "id": 42,
    "name": "example.com",
    "region": "us-east-1",
    "receiving_enabled": true,
    "s3_inbound_bucket": "my-inbound-bucket",
    "s3_inbound_prefix": "inbound/example.com/",
    "sns_topic_inbound_arn": "arn:aws:sns:us-east-1:123456789012:ses-example.com-inbound",
    "created": "...",
    "modified": "..."
  }
}
```

### Onboard Domain (generate DNS, configure topics, receiving)

Request:
```
POST /api/aws/email/domain/42/onboard
Content-Type: application/json

{
  "receiving_enabled": true,
  "s3_inbound_bucket": "my-inbound-bucket",
  "s3_inbound_prefix": "inbound/example.com/",
  "ensure_mail_from": true,
  "mail_from_subdomain": "feedback",
  "dns_mode": "manual",
  "endpoints": {
    "bounce": "https://portal.example.com/hooks/sns/bounce",
    "complaint": "https://portal.example.com/hooks/sns/complaint",
    "delivery": "https://portal.example.com/hooks/sns/delivery",
    "inbound": "https://portal.example.com/hooks/sns/inbound"
  }
}
```

Response (simplified):
```
{
  "status": true,
  "data": {
    "domain": "example.com",
    "dns_records": [
      { "type": "TXT", "name": "_amazonses.example.com", "value": "<token>", "ttl": 600 },
      { "type": "CNAME", "name": "<dkim1>._domainkey.example.com", "value": "<dkim1>.dkim.amazonses.com" },
      { "type": "CNAME", "name": "<dkim2>._domainkey.example.com", "value": "<dkim2>.dkim.amazonses.com" },
      { "type": "CNAME", "name": "<dkim3>._domainkey.example.com", "value": "<dkim3>.dkim.amazonses.com" },
      { "type": "MX", "name": "feedback.example.com", "value": "10 feedback-smtp.us-east-1.amazonses.com" },
      { "type": "TXT", "name": "feedback.example.com", "value": "v=spf1 include:amazonses.com ~all" }
    ],
    "topic_arns": {
      "bounce": "...",
      "complaint": "...",
      "delivery": "...",
      "inbound": "..."
    },
    "receipt_rule": "mojo-example.com-catchall",
    "rule_set": "mojo-default-receiving",
    "notes": ["Receiving catch-all rule ensured"]
  }
}
```

### Create Mailbox

Request:
```
POST /api/aws/email/mailbox
Content-Type: application/json

{
  "domain": 42,
  "email": "support@example.com",
  "allow_inbound": true,
  "allow_outbound": true,
  "async_handler": "myapp.handlers.process_support"
}
```

### Send Email

Request (plain/HTML):
```
POST /api/aws/email/send
Content-Type: application/json

{
  "from_email": "support@example.com",
  "to": ["user@example.org"],
  "subject": "Hello",
  "body_text": "Hi there!",
  "body_html": "<p>Hi there!</p>"
}
```

Response:
```
{
  "status": true,
  "data": { "id": 101, "ses_message_id": "000000000000-000-000", "status": "sending" }
}
```

---

## Python Service API (for Django code)

Preferred for server-side logic requiring DB EmailTemplate rendering:

- send_email(from_email, to, subject/body_text/body_html, ...)
- send_with_template(from_email, to, template_name, context, ...)
  - Uses EmailTemplate from DB (Django templating).
- send_template_email(from_email, to, template_name, template_context, ...)
  - Uses AWS SES managed templates.

Key details:
- from_email must match a configured Mailbox with allow_outbound.
- reply_to defaults to from_email (overridable).
- Domain must be verified unless allow_unverified=True is passed.

---

## Health, Drift, and Reconciliation

- Audit: GET|POST /aws/email/domain/<id>/audit
  - Produces a drift report (status, audit_pass, checks, items) and persists EmailDomain.status ("ready"/"missing") and readiness flags can_send/can_recv based on the audit outcome.

- Reconcile: POST /api/aws/email/domain/<id>/reconcile
  - Applies safe, idempotent fixes for topics/mappings and catch‑all receiving rule.
  - Does not modify DNS; use onboard for DNS outputs or provider-integrated apply.

- On Create: EmailDomain auto-audits and auto-reconciles to adopt/create resources with minimal steps.

---

## Proposed “Portal Email Manager” (High-Level Integration)

The Portal Email Manager should orchestrate common flows while hiding AWS complexity:

Responsibilities:
1) Domain Lifecycle
   - Create domain → poll audit → onboard (generate/apply DNS) → confirm verified.
   - Show DNS steps to admin if manual; auto-apply via GoDaddy when creds provided.
   - Toggle receiving: show bucket selection; enable/disable catch‑all rule.
   - Show drift report and a “Fix” button (reconcile).

2) Mailbox Management
   - Add/remove mailboxes (full address).
   - Configure async handler (from a whitelist or library).
   - Toggle inbound/outbound per mailbox.

3) Sending
   - UI to test send: select from mailbox, input subject/message or select template + context JSON.
   - Show sent messages table with status and reasons (delivery/bounce/complaint).

4) Inbound Monitoring
   - View inbox: filter by mailbox, search by subject/from/date.
   - Attachment links (S3 signed URLs if needed).
   - Processing status and error diagnostics.

5) Templates
   - Manage EmailTemplate entries (name, subject/html/text).
   - Preview rendering with JSON context; test send.

6) Webhooks and Security
   - Display current webhook endpoints and validation status.
   - Allow configuring webhook base URL (used during onboard).
   - Show SNS certificate cache info and TopicArn validations.

Core API calls required:
- Domain: POST /domain, GET /domain, GET /domain/<id>, POST /domain/<id>/onboard, .../audit, .../reconcile
- Mailbox: POST /mailbox, GET /mailbox, GET /mailbox/<id>, PUT/DELETE
- Send: POST /send
- Inbound: GET /incoming, GET /incoming/<id>
- Sent: GET /sent, GET /sent/<id>
- Templates: POST /template, GET /template, GET /template/<id>, PUT/DELETE

Suggested UX states:
- Domain Card: {status: pending|verified|error, receiving: on|off, drift: ok|drifted|conflict}
- Action Buttons: Onboard, Apply DNS (GoDaddy/manual), Audit, Reconcile, Toggle Receiving, Mailboxes, Templates
- Health Indicators: Webhook validation okay? TopicArns matched? Cert cache fresh?

---

## Security Notes

- All management endpoints require "manage_aws" permission.
- Public webhooks validate SNS signatures and allowed TopicArns; production must never enable DEBUG bypass.
- Consider S3 bucket encryption/KMS and object lifecycle policies for compliance.
- Inbound data may contain PII; handle retention and access controls accordingly.

---

## Troubleshooting

- Domain not verifying:
  - Use /domain/<id>/audit to see verification/DKIM status.
  - Confirm DNS records are applied and propagated.
- No inbound messages:
  - Ensure receiving_enabled is true, S3 bucket exists, and SNS inbound topic is subscribed to your webhook.
  - Check /domain/<id>/audit for receipt rule drift.
- SNS 403 on webhooks:
  - Confirm cert URL is reachable and signature verifies.
  - Check TopicArn matches EmailDomain topic ARN fields.
- Outbound send fails:
  - Check SentMessage.status_reason for SES error details.
  - Ensure domain is verified (or use allow_unverified for controlled testing).

---

## Appendix: DNS Reference

- SES Verification
  - TXT: _amazonses.<domain> = <token>
- DKIM (3 records):
  - CNAME: <dkim>._domainkey.<domain> → <dkim>.dkim.amazonses.com
- MAIL FROM (optional):
  - MX: feedback.<domain> → 10 feedback-smtp.<region>.amazonses.com
  - TXT: feedback.<domain> → v=spf1 include:amazonses.com ~all

---
