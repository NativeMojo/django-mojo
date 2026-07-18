---
id:
type: feature
title: "Admin impersonation — one-time handoff code to sign in as another user (audited, marked, restricted)"
priority: P2
effort:
owner:
opened: 2026-07-16
depends_on: []
related: []
links: []
---

# Admin impersonation — one-time handoff code to sign in as another user

## What & Why

Admins supporting users frequently need to *see what the user sees with the
user's permissions* — menu gating, group scoping, data filtering — which no
client-side "simulate permissions" hack can do faithfully. web-mojo's admin
UserView shipped an `onActionImpersonate` handler calling
`POST /api/auth/impersonate`, but **no such route exists anywhere in
django-mojo** — the frontend handler is being deleted as dead code
(web-mojo `WM-027`), and this item is the real backend feature it needed.

Filed from web-mojo scoping (2026-07-16, Ian-approved direction). The
frontend counterpart is web-mojo
`planning/inbox/userview-impersonate-view-as-user.md`, which `depends_on`
this item.

**Proposed design (for /scope to confirm):** reuse the existing one-time
handoff-code primitives (`POST /api/auth/handoff` → code,
`POST /api/auth/exchange` → JWT; `mojo/apps/account/rest/user.py:209`,
`:230`) rather than inventing a new token flow. New admin-tier endpoint —
suggest `POST /api/auth/manage/impersonate` to match the existing
`auth/manage/*` admin namespace (`throttle`, `clear_rate_limit`,
`generate_api_key`):

1. Caller must hold `manage_users` as a **global** grant
   (`@md.requires_global_perms`, same as the other `auth/manage/*` routes).
2. Body: `{ "user_id": N }` (or `username`).
3. Issues a **single-use, short-lived (≤60s) handoff code** bound to the
   *target* user, redeemable at the existing `/api/auth/exchange`.
4. The resulting JWT is **marked**: short TTL (e.g. 15–60 min, no refresh),
   an `impersonated_by: <admin_id>` claim, `source: "impersonate"` in the
   login-event trail.
5. **Restrictions:** cannot impersonate superusers (and possibly staff)
   unless the caller is a superuser; cannot impersonate yourself (no-op);
   target must be `is_active`.
6. **Audit:** `report_incident` on the target ("<admin> started
   impersonation of <user>", e.g. `user:impersonated`) + log on the actor,
   mirroring the `sessions:revoked` pattern
   (`mojo/apps/account/models/user.py:1011`).
7. While an `impersonated_by` claim is present, the backend should refuse
   the sensitive account actions on the target (password/email/username
   change, `revoke_sessions`, TOTP changes) — impersonation is for
   *viewing as*, not account takeover. Exact deny-list is a /scope call.

**Frontend session-isolation context** (why handoff-code, not a direct JWT
response): web-mojo stores its JWT in `localStorage` (per-origin, shared
across tabs), so returning a JWT directly would clobber the admin's own
session in every tab. A one-time code lets the frontend open the
impersonated session in an isolated context — v1 is "copy link, open in a
private window" (`/portal?auth_code=...`, exchanged by the existing
web-mojo auth_code bootstrap); a later v2 may use a sessionStorage-scoped
tab. JS cannot open incognito windows, so isolation is the frontend's
problem — the backend just needs the single-use code + marked JWT.

## Acceptance Criteria

- [ ] Admin with global `manage_users` can obtain a single-use, short-lived
      code for an eligible target user; redeeming it at
      `/api/auth/exchange` yields a working JWT for that user.
- [ ] The impersonated JWT carries an `impersonated_by` claim and a
      reduced TTL; `source` in the login-event trail identifies
      impersonation.
- [ ] Non-admin callers get a 403; ineligible targets (superuser without
      superuser caller, inactive user, self) are refused with a clear
      error.
- [ ] Impersonation start is incident-logged on the target and logged on
      the actor.
- [ ] Sensitive account mutations are refused while impersonating (list
      decided in /scope).
- [ ] The code is single-use and expires (≤60s); replay fails.
- [ ] Documented in `docs/web_developer/account/` (likely a new
      `impersonation.md` + a row in `user.md`'s endpoint table).

## Repro — bugs only

n/a (feature)

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

_Write a complete, self-contained design here — enough that a fresh session can
`/build` it cold, without re-deriving anything. Fill every subsection._

### Goal
[One sentence.]

### Context — what exists
[The recon a builder would otherwise redo: relevant files with paths and
`file:line` refs, current behavior, key snippets, helpers/patterns to reuse.]

### Changes — what to do
1. `path` — [exact change and why]
2. `path` — [...]

### Design decisions
- [decision] — [rationale; alternatives rejected]

### Edge cases & risks
- [case] — [how it's handled]

### Tests
- [scenario] -> `test file`   (for a bug: the regression test to add)

### Docs
- `doc` — [what changes]

### Open questions
- Exact deny-list of account actions while impersonating.
- Endpoint name (`auth/manage/impersonate` suggested for namespace
  consistency) and whether staff (not just superusers) are protected
  targets.
- Whether the impersonated JWT should be refreshable (suggest: no).

## Notes

Cross-repo pair: web-mojo
`planning/inbox/userview-impersonate-view-as-user.md` (UI) depends on this
item. When /scope assigns this a DM id, update that file's `depends_on`
to `[nativemojo/django-mojo#DM-xxx]`.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
