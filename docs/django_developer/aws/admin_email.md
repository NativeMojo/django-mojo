# Admin Email Management

The built-in Admin portal's Email surface: a Dashboard row that says in plain
words whether email works, a management page (`messaging-email`) listing SES
domains and mailboxes, a live re-check, a test send that never 500s, and one
locked writer for the "exactly one system default mailbox" invariant.

Nothing about the email data model changed. Everything reads what
`audit_email_domain(persist=True)` already writes onto `EmailDomain`
(`status`, `can_send`, `can_recv`).

## Services

### `mojo.apps.aws.services.email_admin`

| Function | What it does |
|---|---|
| `email_posture()` | The default-sender / templates posture dict. **The one source of truth** — the admin-settings `email_posture` resolver delegates here, and the summary and Dashboard source reuse it. |
| `summarize()` | Domains (≤100), mailboxes (≤200), counts, and posture for the management page. Never emits credential material, not even the masked forms. |
| `dashboard_source()` | The Dashboard's email row. `unconfigured` (no domains, renders absent), `degraded` (named reasons: `no_sendable_domain`, `default_sender_conflict`, `no_default_sender`, `templates_missing`), or `healthy`. **Never `unhealthy`** — a broken email setup is not an outage, and `email` is deliberately not in `AVAILABILITY_SOURCES`. |
| `test_send(from_email, to, subject=None, body_text=None, body_html=None)` | Sends one email via the regular `email.send_email` path and **never raises**: `MailboxNotFound`, `OutboundNotAllowed`, `DomainNotVerified`, `ValueError`, and environment failures each come back as `{"sent": False, "error", "error_code"}`. An SES-side refusal comes back as `{"sent": True, "status": "failed", "status_reason"}`. |

It makes **zero AWS calls** — the live re-check stays on the existing
`GET /api/aws/email/domain/<pk>/audit` endpoint, whose `persist=True` side
effect is what keeps these readings fresh. Nothing re-audits on a schedule.

### `mojo.apps.aws.services.mailbox_defaults`

The **only supported way to set a default mailbox**:

```python
from mojo.apps.aws.services import mailbox_defaults

mailbox_defaults.claim_system_default(mailbox)   # single system-wide default
mailbox_defaults.claim_domain_default(mailbox)   # single default per domain
```

Each helper locks every current holder (`select_for_update`) inside one
transaction before collapsing it, so two concurrent claims serialize instead
of leaving two defaults behind. All three call sites use it:

- `Mailbox.on_rest_saved` (a REST save with `is_system_default` /
  `is_domain_default` set true),
- `aws_setup.configure_email` (System Setup's sender import),
- `POST /api/aws/email/mailbox-default` (the portal endpoint).

Do not write the flags directly — an unlocked write recreates the
two-defaults race this module exists to close. A pre-existing conflict
(two defaults already in the table) reads as `degraded` on the Dashboard and
is collapsed by the next claim through any path.

## Audit trail

`Mailbox.on_rest_saved` now files an admin audit event
(`category="admin_platform"`, action `mailbox_default`) via
`admin_platform.audit_after_commit` whenever a default flag changes, naming
the **real acting user** (`self.active_user`). The portal endpoint audits its
own writes the same way with a `scope:email` target.

## Portal wiring

- Bootstrap capability: `email` = `has(["manage_aws", "comms", "admin"])` —
  one tier; no read-only email permission exists in this repo and this
  surface does not invent one.
- Feature provider `admin_features/email.py` publishes a `{view, manage}`
  pair (both the same predicate today).
- Dashboard collector `_dashboard_email` is registered in the dashboard's
  `_section_map` under that same tier and delegates to
  `email_admin.dashboard_source()`.
- Frontend feature `assets/features/email/` (route `messaging-email`, nav
  section `Messaging`, `order: 51` — adjacent to SMS's 50 so the two group
  under one header).
- `bin/admin_preview` serves the feature with deterministic fixtures
  (`bin/admin_preview_support/features/email.py`): a healthy domain, a
  sandbox-only domain, a receiving-half-configured domain, and every
  test-send failure branch.

## Endpoints

See [web developer docs](../../web_developer/aws/admin_email.md) for the
request/response contracts of `GET /api/aws/email/summary`,
`POST /api/aws/email/test`, and `POST /api/aws/email/mailbox-default`.
