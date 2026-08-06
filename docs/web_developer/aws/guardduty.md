# AWS GuardDuty Finding API

One public endpoint receives signed Amazon GuardDuty findings delivered by AWS
SNS. There is no browser-facing GuardDuty API: findings surface through the
normal incident feed once ingested.

Backend reference: [docs/django_developer/aws/guardduty.md](../../django_developer/aws/guardduty.md)

---

## Endpoints

| Method | URL | Description |
|---|---|---|
| POST | `/api/aws/guardduty/sns/finding` | Public AWS SNS delivery endpoint; signature and exact-topic allowlist required |

---

## This is AWS-facing infrastructure — do not synthesize this envelope

Clients must **not** POST finding JSON themselves. SNS signs the outer envelope
and the server rejects anything unsigned, anything whose signature does not
verify against a strict AWS certificate URL, and any `TopicArn` absent from the
deployment's exact allowlist. Configure an SNS subscription and let AWS sign and
deliver.

**Authentication/permission:** no User session, JWT, or Django permission is
involved. Authorization is the SNS signature, the strict AWS certificate URL,
request-header consistency, and an exact `TopicArn` match in
`AWS_GUARDDUTY_FINDING_TOPIC_ARNS`. A missing or empty allowlist denies every
delivery.

The GuardDuty allowlist is **independent** of the CloudWatch alarm allowlist
(`AWS_CLOUDWATCH_ALARM_TOPIC_ARNS`). A topic authorized for alarms cannot
deliver findings here, and cannot confirm a subscription here.

---

## Request format

SNS posts its standard JSON envelope as the raw request body (commonly with
`Content-Type: text/plain`). `Message` is a JSON-encoded **EventBridge**
envelope — GuardDuty reaches SNS only through EventBridge — and the finding
itself is nested in `detail`:

```json
{
  "Type": "Notification",
  "MessageId": "11111111-2222-3333-4444-555555555555",
  "TopicArn": "arn:aws:sns:us-east-1:123456789012:guardduty",
  "Message": "{\"version\":\"0\",\"detail-type\":\"GuardDuty Finding\",\"source\":\"aws.guardduty\",\"account\":\"123456789012\",\"region\":\"us-east-1\",\"detail\":{\"id\":\"abcdef0123456789abcdef0123456789\",\"type\":\"UnauthorizedAccess:EC2/SSHBruteForce\",\"severity\":8,\"title\":\"SSH brute force attack\",\"description\":\"198.51.100.7 is performing SSH brute force attacks\",\"accountId\":\"123456789012\",\"region\":\"us-east-1\",\"createdAt\":\"2026-08-04T03:00:00.000Z\",\"updatedAt\":\"2026-08-04T03:05:00.000Z\",\"resource\":{\"resourceType\":\"Instance\",\"instanceDetails\":{\"instanceId\":\"i-0abc1234\"}},\"service\":{\"detectorId\":\"0123456789abcdef0123456789abcdef\",\"count\":3,\"action\":{\"actionType\":\"NETWORK_CONNECTION\",\"networkConnectionAction\":{\"connectionDirection\":\"INBOUND\",\"protocol\":\"TCP\",\"blocked\":false,\"remoteIpDetails\":{\"ipAddressV4\":\"198.51.100.7\"}}}}}}",
  "Timestamp": "2026-08-04T03:05:00.000Z",
  "SignatureVersion": "2",
  "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-example.pem",
  "Signature": "base64-signature"
}
```

The endpoint also accepts signed SNS `SubscriptionConfirmation` envelopes and
follows only a strict, non-redirecting AWS confirmation URL.

---

## What an accepted finding becomes

Accepted findings appear in the incident feed with:

- `scope`: `aws:guardduty`
- `category`: `aws:guardduty:<finding type>`
- `model_name`: `aws.guardduty.finding`
- bounded metadata for finding identity, account/region, severity, the resource
  identifiers, and a summarized action

**No finding opens an incident on its own** — not even Critical. Every severity
band maps below the auto-incident threshold, so enabling the receiver only
records events. Incidents and tickets require an explicit GuardDuty RuleSet
configured server-side.

A repeated finding accumulates on one incident occurrence while that incident
is open, escalating its priority if the severity band rises. Once the incident
is resolved or closed, a later recurrence opens a **new** incident rather than
reopening the closed one. SNS replays and out-of-order deliveries are
idempotent: they create no additional Event, Incident, Ticket, or job.

---

## Response format

An accepted notification returns the durable finding identifier, whether the
delivery was a duplicate, the mapped event level, and the occurrence count:

```json
{
  "status": true,
  "data": {
    "duplicate": false,
    "finding": 123,
    "level": 6,
    "occurrence_count": 1
  }
}
```

A confirmed subscription returns:

```json
{
  "status": true,
  "data": {
    "confirmed": true,
    "status_code": 200
  }
}
```

| Status | Meaning |
|---|---|
| `200` | Finding accepted (including an idempotent duplicate), or subscription confirmed |
| `400` | Malformed SNS envelope, malformed EventBridge/GuardDuty payload, or unsupported SNS message type |
| `403` | Signature, certificate-URL, header-consistency, or topic authorization failure |
| `405` | Method other than POST |
| `503` | Certificate fetch, confirmation, processing, or durable handler dispatch should be retried by SNS |
