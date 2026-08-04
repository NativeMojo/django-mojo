# Email Subsystem — Django Developer Reference

The email subsystem lives in `mojo/apps/aws/` and is built on AWS SES.

- [Architecture & Setup](architecture.md) — Core models, domain setup, SES configuration
- [Sending Email](sending.md) — Direct send, templates, Mailbox API
- [Receiving Email](receiving.md) — Inbound email handling

Run `python manage.py aws-check --section email --check` for a non-persistent
SES/domain/DKIM/sandbox/topic/receiving audit plus system Mailbox and shipped
template checks. Apply mode can create absent SES identity/DKIM requests, SNS
topics/subscriptions and empty identity-topic mappings; any non-empty differing
mapping is preserved. It also creates missing templates and, when explicitly
given `--mailbox-email`, a missing outbound default. It does not change DNS,
request sandbox exit, send test mail, or overwrite existing SES mappings.
