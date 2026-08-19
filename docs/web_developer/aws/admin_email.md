# Admin Email Management API

Three endpoints behind the built-in Admin portal's Email page. One permission
tier for all of them: `manage_aws` OR `comms` OR `admin`. All three refuse
key-backed sessions; the two POSTs additionally require recent authentication
when `FRESH_AUTH_WINDOW` is configured (same step-up as the capacity and
maintenance applies — expect `440 reauth_required` on a stale session).

The live re-check is **not** here: `GET /api/aws/email/domain/<pk>/audit`
(already documented with the email endpoints) runs the SES audit and persists
fresh `status` / `can_send` / `can_recv` onto the domain as a side effect.

## GET /api/aws/email/summary

Domains, mailboxes, and sending posture. Read-only, no AWS calls, never
returns credential material (not even masked forms).

```json
{
  "status": true,
  "data": {
    "schema_version": 1,
    "posture": {
      "default_sender_configured": true,
      "default_sender_conflict": false,
      "templates_installed": true,
      "missing_template_count": 0
    },
    "domain_count": 1,
    "mailbox_count": 2,
    "domains": [
      {"id": 1, "name": "example.com", "region": "us-east-1",
       "status": "ready", "receiving_enabled": true,
       "can_send": true, "can_recv": true, "dns_mode": "route53",
       "checked_at": "2026-08-18T09:14:00+00:00"}
    ],
    "mailboxes": [
      {"id": 11, "email": "support@example.com", "domain": 1,
       "domain_name": "example.com", "allow_inbound": true,
       "allow_outbound": true, "is_system_default": true,
       "is_domain_default": true}
    ]
  }
}
```

`domains` is capped at 100 rows and `mailboxes` at 200; `domain_count` /
`mailbox_count` carry the true totals. `checked_at` is when the persisted
readiness was last written (the domain row's `modified`).

## POST /api/aws/email/test

Send one test email. **Always answers HTTP 200** — every foreseeable failure
is a structured error, never a 500.

Request:

```json
{"from_email": "support@example.com", "to": "you@example.org",
 "subject": "Test", "body_text": "Hello"}
```

`body_html` is also accepted. `from_email` must match a configured Mailbox.

Success (the message was handed to SES):

```json
{"status": true, "data": {"sent": true, "message_id": "0100018...",
                          "status": "sending", "status_reason": null}}
```

SES refused the message (the call ran; the SentMessage records the refusal):

```json
{"status": true, "data": {"sent": true, "message_id": null,
                          "status": "failed",
                          "status_reason": "MessageRejected: ..."}}
```

Failure before any send was attempted:

```json
{"status": true, "data": {"sent": false, "error_code": "mailbox_not_found",
                          "error": "No mailbox is configured for ..."}}
```

`error_code` vocabulary:

| Code | Meaning |
|---|---|
| `invalid_request` | Missing `from_email`, empty `to`, or no subject/body at all |
| `mailbox_not_found` | `from_email` matches no configured Mailbox |
| `outbound_not_allowed` | The mailbox has `allow_outbound=false` |
| `domain_not_verified` | The mailbox's domain is not verified for sending — run the domain audit to see what is missing |
| `configuration_error` | The environment cannot attempt a send at all (e.g. no AWS credentials anywhere) |

## POST /api/aws/email/mailbox-default

Set the system-wide or per-domain default mailbox. Goes through the same
locked writer as System Setup and REST Mailbox saves, so the end state is
always exactly one system default (and at most one default per domain).

Request:

```json
{"mailbox": 11, "scope": "system"}
```

`scope` is `"system"` (default) or `"domain"`. Refused with 400 when the
mailbox has outbound disabled, 404 when it does not exist.

Response:

```json
{"status": true, "data": {"mailbox": 11, "email": "support@example.com",
                          "scope": "system", "is_system_default": true,
                          "is_domain_default": true}}
```

Every default change (through this endpoint or a plain Mailbox save) files an
admin audit event naming the acting user.

## Dashboard

`GET /api/account/admin/dashboard` includes a `sources.email` envelope for
callers holding the tier — persisted evidence only, statuses `unconfigured`
(no SES domains; the portal renders no row), `degraded` (reason names the
problem: `no_sendable_domain`, `default_sender_conflict`,
`no_default_sender`, `templates_missing`), or `healthy`. It is **not** an
availability source: a broken email setup never turns the headline red.

The portal bootstrap gains `capabilities.email` and a `features.email`
provider entry (`{view, manage}`, both the same predicate today).
