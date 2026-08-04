# Email Subsystem — Django Developer Reference

The email subsystem lives in `mojo/apps/aws/` and is built on AWS SES.

- [Architecture & Setup](architecture.md) — Core models, domain setup, SES configuration
- [Sending Email](sending.md) — Direct send, templates, Mailbox API
- [Receiving Email](receiving.md) — Inbound email handling

Run `python manage.py aws-check --section email --check` for a non-persistent
SES/domain/DKIM/sandbox/topic/receiving audit plus system Mailbox and shipped
template checks. Apply mode can create absent SES identity/DKIM requests, SNS
topics/subscriptions and empty identity-topic mappings; any non-empty differing
mapping is preserved. HTTPS subscriptions are created only when the matching
`<kind>_endpoint` or `sns_<kind>_endpoint` is already present in the domain's
metadata. Inbound topics are considered only when receiving is enabled. Apply
also creates missing templates and, when explicitly given `--mailbox-email`, a
missing outbound default. It does not change DNS, request sandbox exit, send
test mail, or overwrite existing SES mappings.

Create-missing topic names are stable: `ses-<domain>-<kind>`, where kind is
`bounce`, `complaint`, `delivery`, or (when enabled) `inbound`. New topics are
tagged with `managed-by=django-mojo`, `purpose=ses-notifications`, and exact
deployment/domain values. A same-name topic with different or missing ownership
tags is a conflict and is never adopted automatically. Existing non-empty SES
mappings remain authoritative and are recorded instead of rewired.
