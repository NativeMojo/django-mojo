# django-mojo/docs/future/email.md

# AWS SES Email System — Design Plan

Status: Draft (Future work)
Owner: AWS app maintainers
Scope: Add first-class email sending and receiving support in `mojo/apps/aws` using AWS SES, SNS, and S3, with optional DNS automation (Route 53 / GoDaddy).

## Goals

- One-step domain onboarding via REST API:
  - Verify domain in SES for sending.
  - Optionally configure MAIL FROM domain and DKIM.
  - Optionally enable receiving with SES Receipt Rules, writing raw emails to a user-assigned S3 bucket.
  - Configure SNS topics/subscriptions for bounces, complaints, deliveries, and inbound notifications.
  - Provide DNS automation when credentials are available (GoDaddy, Route 53), or provide a manual workflow (return required records).

- Simple per-address mailboxes:
  - Send and receive per mailbox (email address).
  - Incoming email stored in Django models (headers, body, attachments).
  - Async processing hook via our Tasks system (string `module:function`).

- Sending:
  - Send simple emails (plain/HTML) directly.
  - Send templated emails using `EmailTemplate` model + Django template rendering.
  - Track sent message status via SNS (delivered, bounced, complained).

- Clean REST APIs to configure, operate, and observe everything with least AWS complexity exposed.

- Security by default, permissions via `RestMeta` on each model, and SNS signature validation on webhooks.

## Non-goals

- Non-AWS email providers.
- Custom MTA. We rely on SES for SMTP/HTTP email infrastructure.

---

## High-level Architecture

- Domain onboarding:
  1. Create `EmailDomain` via API.
  2. Backend uses AWS SES to request domain verification and DKIM.
  3. System emits required DNS records:
     - Manual mode: returns records to the caller to apply.
     - Automated mode: applies DNS via GoDaddy API or AWS Route 53 (if configured).
  4. Optional MAIL FROM domain setup.
  5. Optional SES receiving setup:
     - Create or reuse a Receipt Rule Set.
     - Add rules per domain that match recipient(s) → store to S3 and publish to SNS.

- Inbound flow:
  - Domain-level catch-all SES Receipt Rule writes all incoming mail for the domain to the configured S3 bucket/prefix.
  - SNS notifies our inbound webhook for each message.
  - We validate SNS, fetch the S3 object, parse MIME, and store in `IncomingEmail` and `EmailAttachment`.
  - If a `Mailbox` exists for any recipient address on the message, associate the message to that mailbox and enqueue its `async_handler`; otherwise, leave it unassigned (or attach to a default mailbox if configured).
  - Attachments are stored in the same inbound S3 bucket under the inbound prefix.

- Outbound flow:
  - API call to send a message from a `Mailbox` (only Mailbox-owned addresses can send; must have `allow_outbound` true).
  - Source (envelope MAIL FROM) uses the `Mailbox.email` address; the SES domain identity must be verified. Optional SES MAIL FROM domain configuration can be enabled per domain but is not required for sending.
  - Build body from `EmailTemplate` if requested; send via SES (using existing helper).
  - Save `SentMessage` with SES `MessageId`.
  - SNS delivery/bounce/complaint webhooks update `SentMessage.status` and reason fields.

---

## Data Model (Proposed)

Each model in its own file under `mojo/apps/aws/models`.

- `EmailDomain`
  - `id` (UUID)
  - `name` (domain name, unique)
  - `region` (SES region; default `settings.AWS_REGION`)
  - `status` (enum: pending, verified, error)
  - `dkim_tokens` (Array or JSON)
  - `mail_from_subdomain` (optional, default `feedback`)
  - `s3_inbound_bucket` (string: bucket name; optional)
  - `s3_inbound_prefix` (string; default `inbound/<domain>/`)
  - `sns_topic_bounce_arn` (string; optional)
  - `sns_topic_complaint_arn` (string; optional)
  - `sns_topic_delivery_arn` (string; optional)
  - `sns_topic_inbound_arn` (string; optional)
  - `dns_mode` (enum: manual, route53, godaddy)
  - `dns_provider_ref` (FK to `DnsProviderCredential`, optional)
  - `created_at`, `updated_at`

- `DnsProviderCredential`
  - `id` (UUID)
  - `provider` (enum: route53, godaddy)
  - `name` (display name)
  - `config` (JSON: for GoDaddy: key/secret; for Route 53: hosted zone id or autodetect)
  - `created_at`, `updated_at`

- `Mailbox`
  - `id` (UUID)
  - `domain` (FK to `EmailDomain`)
  - `email` (full email, unique)
  - `allow_inbound` (bool)
  - `allow_outbound` (bool)
  - `async_handler` (string: `package.module:function`, optional)
  - `metadata` (JSON, optional)
  - `created_at`, `updated_at`

- `IncomingEmail`
  - `id` (UUID)
  - `mailbox` (FK to `Mailbox`, nullable if unmatched)
  - `s3_object_url` (string: s3://bucket/key)
  - `message_id` (SMTP Message-ID header)
  - `from_address`
  - `to_addresses` (array/json)
  - `cc_addresses` (array/json)
  - `subject`
  - `date_header` (datetime parsed from headers)
  - `headers` (JSON)
  - `text_body` (text)
  - `html_body` (text)
  - `size_bytes` (int)
  - `received_at` (datetime)
  - `processed` (bool)
  - `process_status` (enum: pending, success, error)
  - `process_error` (text)
  - `created_at`, `updated_at`

- `EmailAttachment`
  - `id` (UUID)
  - `incoming_email` (FK to `IncomingEmail`)
  - `filename`
  - `content_type`
  - `size_bytes`
  - `stored_as` (string: s3://bucket/key or internal storage ref)
  - `created_at`

- `SentMessage`
  - `id` (UUID)
  - `mailbox` (FK to `Mailbox`)
  - `ses_message_id` (string)
  - `to_addresses` (array/json)
  - `cc_addresses` (array/json)
  - `bcc_addresses` (array/json)
  - `subject`
  - `body_text`
  - `body_html`
  - `template` (FK to `EmailTemplate`, nullable)
  - `template_context` (JSON, nullable)
  - `status` (enum: queued, sending, delivered, bounced, complained, failed, unknown)
  - `status_reason` (text/json with bounce/complaint details)
  - `created_at`, `updated_at`

- `EmailTemplate`
  - `id` (UUID)
  - `name` (unique)
  - `subject_template` (django template string)
  - `html_template` (django template string, optional)
  - `text_template` (django template string, optional)
  - `created_at`, `updated_at`

Note: We will use `RestMeta` on each model to define permissions, graphs, and default API behaviors.

Example skeleton (illustrative only):

```/dev/null/mojo/apps/aws/models/email_domain.py#L1-120
from mojo.models import MojoModel, fields

class EmailDomain(MojoModel):
    name = fields.CharField(unique=True)
    region = fields.CharField(default='us-east-1')
    status = fields.CharField(default='pending')
    dkim_tokens = fields.JSONField(default=list)
    mail_from_subdomain = fields.CharField(default='feedback')
    s3_inbound_bucket = fields.CharField(null=True, blank=True)
    s3_inbound_prefix = fields.CharField(default='')
    sns_topic_bounce_arn = fields.CharField(null=True, blank=True)
    sns_topic_complaint_arn = fields.CharField(null=True, blank=True)
    sns_topic_delivery_arn = fields.CharField(null=True, blank=True)
    sns_topic_inbound_arn = fields.CharField(null=True, blank=True)
    dns_mode = fields.CharField(default='manual')
    dns_provider_ref = fields.ForeignKey('DnsProviderCredential', null=True, blank=True, on_delete=fields.SET_NULL)

    class RestMeta:
        VIEW_PERMS = ['manage_aws']
        SAVE_PERMS = ['manage_aws']
        DELETE_PERMS = ['manage_aws']
        GRAPHS = {
            'base': ['id', 'name', 'status', 'region'],
            'detail': ['id', 'name', 'status', 'region', 's3_inbound_bucket',
                       's3_inbound_prefix', 'dns_mode', 'sns_topic_bounce_arn',
                       'sns_topic_complaint_arn', 'sns_topic_delivery_arn', 'sns_topic_inbound_arn']
        }
```

---

## AWS Resources and Configuration

Per domain:

- SES Identity:
  - `VerifyDomainIdentity`, `VerifyDomainDkim`, result in:
    - TXT record for domain verification: `_amazonses.<domain> = <token>`
    - DKIM CNAME records: `<token>._domainkey.<domain> → <token>.dkim.amazonses.com`
- Optional MAIL FROM:
  - `SetIdentityMailFromDomain` with subdomain (default `feedback.<domain>`).
  - Adds:
    - MX record: `10 feedback-smtp.<region>.amazonses.com`
    - SPF record (TXT): `v=spf1 include:amazonses.com ~all`
- SNS Topics:
  - `ses-<domain>-bounce`, `ses-<domain>-complaint`, `ses-<domain>-delivery`, `ses-<domain>-inbound`
  - Subscriptions:
    - HTTPS to our webhook endpoints.
- SES Notification Mapping:
  - `SetIdentityNotificationTopic` for bounce/complaint/delivery.
- SES Receiving (optional):
  - Create or ensure a Rule Set (e.g., `mojo-default-receiving`).
  - Create a domain-level catch-all Rule that matches the domain (no recipient-specific matching):
    - Actions:
      - S3 action → the assigned inbound bucket and prefix (catch-all for the domain).
      - SNS action → `ses-<domain>-inbound`.
    - TLS policy as needed.
  - Note: SES receiving availability varies by region; default to `settings.AWS_REGION`, override per domain if needed.

S3 layout for inbound:
- Bucket: user-provided (must exist; we can create via our S3 API if allowed).
- Prefix: `inbound/<domain>/<YYYY>/<MM>/<DD>/<message-id or uuid>.eml`

---

## DNS Automation Options

- Manual mode:
  - API returns a list of records to add for verification, DKIM, and MAIL FROM.
  - Client applies changes manually; we poll SES to update status.

- Route 53 mode:
  - If hosted zone is known or discoverable, we create/UPSERT the needed records.
  - Optionally verify hosted zone ownership by matching domain suffix.

- GoDaddy mode:
  - Use existing `mojo/helpers/dns/godaddy.py` (`DNSManager`) with provided API credentials to add/patch records.
  - For verification records:
    - TXT `_amazonses`
    - CNAME DKIM tokens
    - MAIL FROM MX and TXT when enabled

---

## REST API Design (Proposed)

New endpoints under `mojo/apps/aws/rest/email.py` (and submodules). All require `manage_aws` permission unless otherwise specified.

- POST `/aws/email/domain/`
  - Create `EmailDomain`, kick off SES verification and DKIM.
  - Body:
    - `name` (domain)
    - `region` (optional)
    - `dns_mode` (manual|route53|godaddy)
    - `dns_provider_id` (when needed)
    - `enable_receiving` (bool, optional)
    - `s3_inbound_bucket` (required if receiving)
    - `s3_inbound_prefix` (optional)
    - `enable_mail_from` (bool, optional)
  - Response:
    - Domain object (detail graph)
    - `dns_records_required` (list) when manual or when provider not yet applied

- GET `/aws/email/domain/<id or domain>/`
- POST `/aws/email/domain/<id>/audit/` - Inspect SES/SNS/S3/DNS and return a drift report (current vs desired), including potential conflicts and a proposed fix plan.
- POST `/aws/email/domain/<id>/reconcile/` - Attempt safe, idempotent fixes for detected drift; if irreconcilable conflicts are found, return explicit manual steps (e.g., which resource to remove in AWS Console).
  - Returns domain with `status` and verification flags.

- POST `/aws/email/domain/<id>/apply-dns/`
  - For automated DNS (GoDaddy/Route 53), apply missing records.

- POST `/aws/email/domain/<id>/toggle-receiving/`
  - Body: `{ "enabled": true, "s3_inbound_bucket": "...", "s3_inbound_prefix": "..." }`
  - Creates/removes SES receipt rules accordingly.

- POST `/aws/email/domain/<id>/set-mail-from/`
  - Body: `{ "enabled": true, "subdomain": "feedback" }`
  - Sets SES mail from domain and returns required DNS.

- POST `/aws/email/mailbox/`
  - Create `Mailbox` (per address).
  - Body:
    - `domain` (FK or domain name)
    - `local_part` or `email`
    - `allow_inbound`, `allow_outbound`
    - `async_handler` (optional)
  - Receiving is domain-level catch-all; no SES rule changes are required when adding mailboxes.

- GET `/aws/email/mailbox/<id>/`
  - Return mailbox details.

- POST `/aws/email/send/`
  - Body:
    - `from_mailbox` (id or email)
    - `to`, `cc`, `bcc`
    - `subject` (optional if template)
    - `text`, `html` (optional)
    - `template_name` (optional)
    - `template_context` (json, optional)
    - `attachments` (optional: references to S3 objects or upload flow TBD)
  - Only allowed when `from_mailbox` exists and `allow_outbound` is true. Source/envelope MAIL FROM uses the mailbox email. Sends via SES; creates `SentMessage`.

- GET `/aws/email/incoming/` and `/aws/email/incoming/<id>/`
  - List and detail for incoming messages.

- GET `/aws/email/sent/` and `/aws/email/sent/<id>/`
  - List and detail for sent messages.

- Webhooks (Public, signature-validated):
  - POST `/aws/email/sns/bounce/`
  - POST `/aws/email/sns/complaint/`
  - POST `/aws/email/sns/delivery/`
  - POST `/aws/email/sns/inbound/`
  - Handle SubscriptionConfirmation and Notification.
  - Update `SentMessage.status` for bounce/complaint/delivery.
  - For inbound: download raw email from S3 and create `IncomingEmail`.

Example REST handler skeleton:

```/dev/null/mojo/apps/aws/rest/email.py#L1-120
from mojo import decorators as md
from mojo import JsonResponse
from mojo.apps.aws.models import EmailDomain

@md.URL('aws/email/domain')
@md.URL('aws/email/domain/<str:pk>')
@md.requires_perms("manage_aws")
def on_email_domain(request, pk=None):
    if request.method == 'POST' and pk is None:
        # create and kick off verification
        # call helpers to setup SES + DNS
        return JsonResponse({"status": True})
    if request.method == 'GET' and pk:
        # return domain info
        return JsonResponse({"data": {}, "status": True})
    return JsonResponse({"error": "Unsupported"}, status=405)
```

---

## Services and Helpers

Extend `mojo/helpers/aws` where possible (SES/SNS/S3/IAM already present). Add:

- `mojo/helpers/aws/ses_domain.py`
  - High-level orchestrations:
    - create/verify domain
    - generate required DNS records
    - set MAIL FROM
    - set notification topics
    - create receipt rules
  - Use existing `EmailSender` for send operations.

- `mojo/helpers/dns`:
  - We already have GoDaddy. Add Route 53 helper if needed (or implement inside SES orchestration using boto3 route53 client).

- Parsing inbound emails:
  - Utility to parse raw MIME from S3 to structured parts (subject, body_text, body_html, attachments).
  - Store attachments to S3 (project bucket/prefix) and record `EmailAttachment` rows.

Example orchestration outline:

```/dev/null/mojo/helpers/aws/ses_domain.py#L1-160
from mojo.helpers.aws.ses import EmailSender
from mojo.helpers.aws.sns import SNSTopic, SNSSubscription
from mojo.helpers.aws.s3 import S3
import boto3

def onboard_domain(domain_name, region, dns_strategy, dns_credentials, enable_receiving, s3_bucket, s3_prefix, enable_mail_from):
    # 1) Verify domain + DKIM
    # 2) Produce/apply DNS records
    # 3) Create SNS topics and map SES notifications
    # 4) Optionally enable receiving with rule set + rules (S3 action + SNS action)
    # Returns: dict with resources and pending steps (if manual DNS)
    pass
```

---

## Task System Integration (Async)

Use our Tasks library (see `docs/tasks.md`) to stitch async processing:

- Tasks (not exhaustive):
  - `email.tasks.poll_domain_verification(domain_id)` — periodically poll SES verification status; stop when Verified/Error.
  - `email.tasks.process_inbound(incoming_email_id)` — run the `async_handler` for the `Mailbox`.
  - `email.tasks.retry_send(sent_message_id)` — if needed.
  - `email.tasks.audit_and_reconcile(domain_id)` — validate SES/SNS/S3/DNS and optionally repair drift for the domain.

`async_handler` contract:
- String `"package.module:function_name"`.
- Signature: `def handler(task_data)` where `task_data` includes `incoming_email_id` and selected fields.
- The REST onboarding ensures handler importability and log errors if not found.

Example task dispatch:

```/dev/null/mojo/apps/aws/tasks/email_tasks.py#L1-80
from taskit.manager import TaskManager
task_manager = TaskManager(['email'])

def queue_inbound_processing(incoming_email_id, handler_path):
    task_manager.publish(
        'email.handlers.process_incoming',
        {'incoming_email_id': incoming_email_id, 'handler': handler_path},
        channel='email'
    )
```

---

## Template Rendering

- `EmailTemplate` stores Django template strings for subject, text, and HTML.
- Rendering pipeline:
  1. Load template by name.
  2. Render with `template_context`.
  3. Populate `subject`, `body_text`, `body_html` for sending via SES.

- Fallback: Provide `subject`, `text`, `html` directly (no template).
- Consider caching compiled templates.

---

## Security

- Webhooks:
  - Validate SNS signatures for SubscriptionConfirmation and Notification.
  - Confirm subscription by calling `SubscribeURL` when appropriate.
  - Only accept known topic ARNs configured in `EmailDomain`.

- Permissions:
  - All configuration endpoints require `manage_aws`.
  - Data read endpoints restrict by perms; do not leak other tenants’ domains/mailboxes/emails.
  - Enforce owner/group perms via `RestMeta` patterns.

- PII/Compliance:
  - Incoming emails may contain PII; store minimal necessary.
  - Optionally provide a retention policy and a redaction path for attachments/bodies.
  - Support S3 bucket encryption and KMS policies if configured.

---

## Observability

- Logging:
  - Orchestration steps (SES calls, DNS changes, SNS subscriptions).
  - Webhook events and outcomes (delivery/bounce/complaint).
  - Inbound processing success/failure.

- Metrics:
  - Domains verified vs pending.
  - Sent volumes, delivery rate, bounce/complaint rates.
  - Inbound counts, processing latency.

---

## Audit and Reconciliation (Sanity checks)

Why: Users may change resources in the AWS Console or pre-exist resources from other systems, which can cause collisions or drift.

Mechanism:
- Inspect current state:
  - SES: domain identity, DKIM status, notification topics, MAIL FROM settings, receipt rule set and rules.
  - SNS: topics and HTTPS subscriptions (and confirmation status).
  - S3: inbound bucket existence, permissions, CORS (optional), prefix health.
  - DNS: required TXT/CNAME/MX/SPF records (either fetch via DNS provider API, or return expected set for manual mode).
- Compute desired state from `EmailDomain` + `Mailbox` definitions.
- Produce a drift report with:
  - status: ok | drifted | conflict
  - items: list of resources with current vs desired, and suggested action
- Reconcile:
  - Apply only safe, idempotent operations automatically (create missing, update mismatched where safe, subscribe missing webhooks).
  - For conflicts (e.g., another rule set targeting a different bucket), return explicit manual steps and mark as `conflict`.
- Endpoints:
  - POST `/aws/email/domain/<id>/audit/` — read-only drift report.
  - POST `/aws/email/domain/<id>/reconcile/` — attempt safe fixes; returns updated drift report.

Notes:
- Receiving is domain-level catch-all. Mailbox changes do not mutate SES rules; routing happens in our app.
- Attachments remain in the same inbound S3 bucket; ensure bucket policies and encryption align with project defaults.
- MAIL FROM:
  - Sending always uses the `Mailbox.email` as the envelope MAIL FROM.
  - Optional SES MAIL FROM domain configuration can be enabled at the domain, but is not required.

## Rollout Plan

Phase 1: Sending + Domain Verification
- Models: `EmailDomain`, `Mailbox`, `SentMessage`, `EmailTemplate`
- REST: domain create, mailbox create, send endpoint
- SES: Verify domain, DKIM, set SNS topics for bounce/complaint/delivery
- Webhooks for bounce/complaint/delivery
- Manual DNS flow + GoDaddy automation

Phase 2: Receiving
- Models: `IncomingEmail`, `EmailAttachment`
- REST: list/detail for incoming
- SES: Receipt rule set + rules (S3 + SNS)
- Webhook: inbound handler, S3 fetch, MIME parse, store, async dispatch

Phase 3: Route 53 + Management UX polish
- Route 53 automation
- Toggle endpoints (receiving on/off, MAIL FROM on/off)
- Rule reconciliation on mailbox changes

Phase 4: Hardening and Compliance
- SNS signature validation + retries
- KMS encryption options
- Retention and purge jobs

---

## Example Payloads

Create domain (manual DNS):

```/dev/null/example.create-domain.json#L1-40
{
  "name": "example.com",
  "region": "us-east-1",
  "dns_mode": "manual",
  "enable_receiving": true,
  "s3_inbound_bucket": "my-inbound-bucket",
  "s3_inbound_prefix": "inbound/example.com/",
  "enable_mail_from": true
}
```

Create mailbox:

```/dev/null/example.create-mailbox.json#L1-40
{
  "domain": "example.com",
  "email": "support@example.com",
  "allow_inbound": true,
  "allow_outbound": true,
  "async_handler": "myapp.handlers.email.process_support_inbox"
}
```

Send email (with template):

```/dev/null/example.send-email.json#L1-80
{
  "from_mailbox": "support@example.com",
  "to": ["user@example.org"],
  "template_name": "welcome",
  "template_context": {
    "user": {"first_name": "Ada"},
    "cta_url": "https://app.example.com/get-started"
  }
}
```

---

## MIME Parsing Strategy

- Use Python `email` package to parse stored `.eml` from S3.
- Extract text and html parts; walk attachments and write each to the same inbound S3 bucket under `<prefix>attachments/<incoming_id>/...`.
- Guard against zip bombs and large payloads with size caps; store truncated flag if necessary.

---

## DNS Records (Reference)

- SES domain verification:
  - TXT: `_amazonses.<domain> = <token>`
- DKIM:
  - CNAME: `<dkim1>._domainkey.<domain> → <dkim1>.dkim.amazonses.com` (3 tokens)
- MAIL FROM (if enabled):
  - MX: `feedback.<domain> → 10 feedback-smtp.<region>.amazonses.com`
  - TXT: `feedback.<domain> → v=spf1 include:amazonses.com ~all`

---

## Open Questions

- Multi-tenant isolation: will we namespace S3 prefixes per tenant automatically? Proposed: `inbound/<tenant>/<domain>/...`.
- Attachment storage location: use the same inbound bucket or a dedicated bucket?
- Template internationalization: keep per-template locale variants?
- Handling multiple SES regions per project: store per-domain region (yes, already planned).

---

## Work Items Checklist

- Models and migrations for all entities (Phase 1 + 2).
- REST handlers with `@md.URL` + `RestMeta` graphs.
- SES orchestration helper with idempotent operations.
- DNS automation for GoDaddy (existing helper) + optional Route 53.
- Webhook endpoints with SNS signature verification.
- MIME parser and attachment management.
- Taskit jobs for verification polling and inbound processing.
- Tests:
  - Unit tests for helpers (mock boto3, requests).
  - REST tests for domain/mailbox lifecycle.
  - Webhook tests for SNS flows.
- Docs for REST users (later in `docs/rest_api/`).
- Admin commands (optional) for ops/backfill.

---

## References

- Existing helpers:
  - `mojo/helpers/aws/ses.py` — send API, verify domain, identities.
  - `mojo/helpers/aws/sns.py` — topics and subscriptions.
  - `mojo/helpers/aws/s3.py` — bucket and object helpers.
  - `mojo/helpers/dns/godaddy.py` — simple DNS manager (GoDaddy).
- Tasks: `docs/tasks.md` — channels, publishing, monitoring.
