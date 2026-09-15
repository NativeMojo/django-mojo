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

The receiving path is **SES → S3 → SNS → inbox ingestion**. The enabled
domain catch-all rule stores the MIME message in `S3Action.BucketName` under
`ObjectKeyPrefix`. Set the inbound SNS topic on **`S3Action.TopicArn`**: its
notification contains `receipt.action.bucketName` and `objectKey`, which the
consumer needs. A separate `SNSAction` does not replace that notification.
Subscribe `POST /api/aws/email/sns/inbound` to the topic and confirm the HTTPS
subscription. The endpoint verifies signed SNS envelopes against configured
domain topic ARNs.

`ensure_receiving_catch_all()` returns the rule-set and rule names only after
reading back the requested rule and confirming that its set is active.
Create/update, policy and activation failures propagate to the caller. Another
active set is a conflict; reconciliation does not switch away from it. Audit
requires the enabled rule, domain recipient, expected bucket/prefix and the
S3 action's inbound topic. An inactive or disabled rule cannot pass receiving
checks.

### Credentials, storage permissions and encryption

Receiving setup uses the shared AWS client factory for SES, S3, STS and KMS.
Explicit domain/settings credentials are carried through bucket setup; absent
static keys leave the normal AWS credential chain available, including instance
roles. See [AWS credentials](../aws/credentials.md). The running inbox consumer
also needs permission to read its objects and decrypt bucket SSE-KMS data.

Reconciliation merges a stable per-rule S3 policy statement granting SES
`s3:PutObject` for the configured prefix, constrained by `AWS:SourceAccount`
and the exact receipt-rule `AWS:SourceArn`. For an enabled customer-managed
bucket SSE-KMS key it merges the corresponding `kms:GenerateDataKey` and
`kms:Decrypt` grant. The caller needs policy read/write permissions, bucket
encryption inspection (`s3:GetEncryptionConfiguration`), and, when applicable,
`kms:DescribeKey`, `kms:GetKeyPolicy` and `kms:PutKeyPolicy`.

Malformed policies and unsupported encryption configurations fail setup.
Unrelated fetched policy statements and bucket CORS are preserved, and written
policies are read back. Policy writes have no compare-and-swap guard: a
concurrent external edit can still be overwritten, so avoid simultaneous
policy writers. A later failure can leave earlier successful changes applied.

SES message encryption (`S3Action.KmsKeyArn`) requires a client-side decryptor
that inbox ingestion does not implement. Existing rules using that field or
an explicit `IamRoleArn` require manual reconciliation; setup refuses to remove
them silently. Bucket SSE-KMS is a separate, supported storage setting.

### Receiving DNS and delivery proof

Receiving onboarding returns the apex MX record
`10 inbound-smtp.<region>.amazonaws.com` alongside the sending records.
`email_ops.reconcile_email_domain()` applies only this receiving MX when the
EmailDomain uses managed DNS (`route53` or `godaddy`), through its active
dnsman domain and provider credentials. Missing or inactive managed domains and
provider failures stop reconciliation. GoDaddy receives separate priority/data
fields and complete grouped record sets. Manual mode returns the exact MX
record and TTL in `notes` for the operator to apply.

Configuration readback and mocked tests do not establish live delivery. For
staging verification, send a uniquely identifiable message to an inbound-enabled
mailbox and correlate the stored S3 object, SNS notification and persisted
`IncomingEmail` with the expected mailbox. Verify the handler outcome when
configured. A successful webhook response alone is insufficient: the current
consumer can log processing failures while acknowledging the notification.

Inbound subscription errors also fail reconciliation with the AWS operation and required IAM action. A pending HTTPS subscription must be confirmed by the webhook before a retry can report successful reconciliation.
