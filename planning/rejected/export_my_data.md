# Rejected: Export My Data

## Request

`POST /api/account/export` — GDPR data portability. Kicks off a background job,
notifies the user when the export is ready to download.

## Status

**Rejected — out of scope for the framework layer.**

## Reason

Data export is inherently product-specific. The framework has no knowledge of
what data a downstream project stores on behalf of a user — custom models,
app-specific relationships, third-party integrations, and project-level
retention policies all vary. A generic framework export would either be
useless (only exporting User row fields) or require every downstream project
to register exportable models, which adds significant framework complexity for
marginal gain.

The infrastructure pieces already exist individually:

- Background jobs: `mojo.apps.jobs`
- Secure file delivery: `mojo/apps/filevault`
- User notification on completion: `mojo/apps/account/models/notification.py`

A downstream project that needs GDPR data portability should wire these
together themselves, scoped to their own data model.

## Revisit Conditions

If a lightweight, opt-in registration mechanism (`EXPORT_MODELS = [...]`) can
be designed that covers the common case without baking in product assumptions,
this is worth revisiting as a framework feature. Needs a design spike first.