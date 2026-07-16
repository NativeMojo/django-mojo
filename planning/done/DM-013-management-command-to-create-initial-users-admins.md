---
# id is assigned by /scope on pickup — leave it blank
id: DM-013
type: feature          # feature | bug | chore
title: Management command to create initial users/admins
priority: P2           # P0 (drop everything) | P1 | P2 | P3
effort: S              # XS | S | M | L | XL
owner: backend         # team or person
opened: 2026-07-05
depends_on: []         # hard blockers: [DM-003, org/other-repo#ITEM-007]
related: []            # soft links: [DM-009]
links: []              # external URLs
---

# Management command to create initial users/admins

## What & Why
There is currently no supported way to bootstrap the first admin user on a fresh
django-mojo deployment. Django's built-in `createsuperuser` is **broken** against
this framework's custom `account.User` model, and django-mojo ships no
replacement command. Anyone standing up a new instance has to hand-write a
Python snippet (as the tests do) to create their first user and grant
permissions — there's no documented, repeatable path.

## Acceptance Criteria
- [ ] A `manage.py` command exists to create a user (interactive prompts and/or
      flags for username/email/phone, password, display name)
- [ ] The command can grant admin-level access — at minimum `is_staff` /
      `is_superuser`, and ideally one or more permission keys via
      `user.add_permission(...)`
- [ ] Command works with either email-based or phone-only user creation (the
      model supports both — see Investigation)
- [ ] Django's built-in `createsuperuser` is either fixed to work with this
      User model, or explicitly documented as unsupported in favor of the new
      command
- [ ] `docs/django_developer/README.md` (or `docs/django_developer/account/README.md`)
      documents how to create the first admin user for a fresh deployment
- [ ] Docs include a permission-key reference table matching web-mojo's admin
      portal sections, and explicitly warn that `permissions.admin=true` is a
      **frontend-only** wildcard, not a backend one (see Investigation) — so
      nobody uses it expecting real API access

## Investigation
**What exists:**
- Custom user model: [`mojo/apps/account/models/user.py`](mojo/apps/account/models/user.py)
  — `User(MojoSecrets, MojoAuthMixin, AbstractBaseUser, MojoModel)`, with
  `USERNAME_FIELD = 'username'` (line 137), `is_staff`/`is_superuser` boolean
  fields (lines 123-124), and a JSON-based `permissions` field (line 115)
  instead of Django's `contrib.auth` permission/group tables.
- `CustomUserManager` (lines 70-87) defines `create_user(self, email,
  password=None, **extra_fields)` and `create_superuser(self, email,
  password=None, **extra_fields)`.
- Permission grants happen via `user.add_permission(perm_key, value=True,
  commit=True)` (around line 508) and `user.has_permission(perm_key)` — no
  Django Group/Permission model involved.
- No existing management command creates users. `mojo/apps/account/management/commands/`
  only has `model_permissions.py`, `serializer_admin.py`, `status.py` — none
  create/seed users.
- Tests bootstrap users by hand in Python (e.g.
  `tests/test_realtime/basic.py`, `tests/test_docit/docit_core.py`):
  instantiate `User(...)`, `.save()`, `.save_password(...)`,
  `.remove_all_permissions()`, then set `is_staff`/`is_superuser` and re-save.
  This is the pattern a new command should mirror/wrap.

**Confirmed root cause — `createsuperuser` is broken, not just unofficial:**
- `USERNAME_FIELD = 'username'` and no `REQUIRED_FIELDS` is declared, so
  Django's built-in `createsuperuser` prompts for `username` + `password` only,
  then calls the manager as `create_superuser(username=<val>, password=<val>)`.
- But `CustomUserManager.create_superuser`/`create_user` take `email` as the
  first (keyword-or-positional) argument and build the user via
  `self.model(email=email, **extra_fields)` — `username` is **never assigned**,
  it just falls into `**extra_fields` and gets set as an attribute pre-save
  (works by luck via `**extra_fields` on the model constructor), but `email`
  itself has no default and isn't supplied by Django's prompt flow, so the
  call raises `TypeError: create_superuser() missing 1 required positional
  argument: 'email'`.
- Net effect: `./manage.py createsuperuser` does not work today against this
  User model.

**Constraints for the new command:**
- Must handle the model's dual identity modes: email-based users (`email`
  unique, nullable) and phone-only users (`phone_number` unique, nullable) —
  see comment at user.py:99-103.
- Must go through `save_password(...)` (not raw `set_password`) if that's the
  framework's supported path — confirm during `/scope` by reading
  `MojoAuthMixin`/`MojoSecrets`.
- Should use `add_permission(...)` rather than writing directly to the
  `permissions` JSON field, to stay consistent with the rest of the framework.
- Keep the command non-interactive-friendly (flags) so it can be scripted in
  deploy/bootstrap tooling, but support prompting when flags are omitted
  (matches Django convention for `createsuperuser`).

**Frontend/backend permission mismatch (checked against `web-mojo`):**
- web-mojo's `hasPermission()` (`web-mojo/src/core/models/User.js:13-42`) treats
  `permissions.admin = true` as a wildcard: `if (this._hasPermission('admin'))
  return true;` runs for *every* permission check, so `{"admin": true}` alone
  unlocks the entire portal nav/pages on the frontend.
- The backend's `User.has_permission` (`mojo/apps/account/models/user.py:492-503`)
  has **no such wildcard** — only `is_superuser`, the literal strings
  `"all"`/`"authenticated"`, or an exact key match in the `permissions` dict.
- Net effect: granting only `{"admin": true}` gets a user into the admin portal
  UI but every real REST call behind it (e.g. anything gated by
  `manage_users`) still 403s server-side. **The new command must not offer
  this as a "lighter than superuser" option** — only `--superuser` (full
  `is_superuser=True`, which both sides special-case correctly) or explicit
  `--permission <key>` grants of the actual keys a section needs.
- Portal section → required permission key(s), from `web-mojo/src/admin.js`
  `registerPage(...)` calls (used to build the docs table):
  `system/dashboard` → `security`; `system/users` → `view_users`/`manage_users`;
  `system/groups` → `view_groups`/`manage_groups`; `system/jobs/*` →
  `view_jobs`/`manage_jobs`; `system/logs` → `view_logs`; `system/incidents`,
  `system/events`, `system/security/blocked-ips` → `view_security`;
  `system/rulesets`, `system/tickets` → `manage_security`; `system/s3buckets`,
  `system/email/*`, `system/cloudwatch` → `manage_aws`; `system/files`,
  `system/filemanagers` → `manage_files`/`view_fileman`; `system/push/*` →
  `manage_notifications`/`view_notifications`/`manage_push_config`;
  `system/settings` → `manage_settings`.

## Plan

### Goal
Add a `manage.py` command that creates a `User` (email-based or phone-only)
and grants admin access through the framework's own APIs, and document
Django's built-in `createsuperuser` as unsupported in favor of it.

### Context — what exists
- User model: [`mojo/apps/account/models/user.py`](mojo/apps/account/models/user.py)
  — `AbstractBaseUser` subclass, `USERNAME_FIELD = 'username'` (line 137), JSON
  `permissions` field (line 115), no Django `contrib.auth` Group/Permission
  tables.
- **Confirmed root cause of the `createsuperuser` breakage**: Django's built-in
  command calls the manager as `create_superuser(username=<val>,
  password=<val>)` (derived from `USERNAME_FIELD`; no `REQUIRED_FIELDS`
  declared), but `CustomUserManager.create_superuser(self, email,
  password=None, **extra_fields)` (user.py:80) requires `email` — raises
  `TypeError: missing 1 required positional argument: 'email'`. The manager
  itself is fine for direct callers that supply both `username=` and `email=`
  explicitly (as dozens of existing tests do) — only Django's generic
  prompt-and-call convention is incompatible with it.
- **Direct `.save()` skips all REST-path validation.** `on_rest_pre_save`
  (user.py:823) — where username format, email format, and permission
  protections live — only runs via `on_rest_save()` inside the REST
  request-handling path (confirmed by the comment at
  `mojo/apps/account/rest/user.py:497`: *"on_rest_pre_save / on_rest_created
  don't fire on direct .save()"*). `MojoSecrets.save()`
  (`mojo/models/secrets.py:68`) only handles secret-field encryption.
- **Proven non-REST user-creation pattern to mirror**: `on_register`
  (`mojo/apps/account/rest/user.py:257`), direct-construction block at
  lines 470-514:
  ```python
  with transaction.atomic():
      user = User(email=email or None)
      if first_name: user.first_name = first_name
      if last_name: user.last_name = last_name
      if phone: user.phone_number = phone
      if identity_field == "email" and email:
          user.username = user.generate_username_from_email()
      else:
          user.username = user.generate_username_from_names(fallback=phone)
      user.set_password(password)  # or set_unusable_password()
      user.infer_names_from_email()
      if not user.display_name:
          user.display_name = user.generate_display_name()
      user.validate_name_fields({}, created=True)
      user.save()
  ```
  `validate_username` (~user.py:600), `validate_email` (user.py:563), and
  `validate_name_fields` all guard on `if self.active_request:`, so they're
  safe to call with no active request (confirmed: they don't crash when
  `active_request`/`active_user` are `None`, which is what they resolve to
  outside a request — `mojo/models/rest.py:62-73`).
- Password: `save_password(value)` (user.py:549) = `set_password()` +
  `.save()`. Strength policy lives in `check_password_strength(password)`
  (user.py:619) — raises `merrors.ValueException` if score < 2 (12+ chars, or
  10+ with mixed case/digit/special) — **not enforced automatically**; only
  `set_new_password()` calls it, so the command must call it explicitly
  before setting the password.
- Permissions: `add_permission(perm_key, value=True, commit=True)`
  (user.py:508) accepts a string or list/set.
- **Bootstrap chicken-and-egg**: `set_is_staff`/`set_is_superuser`
  (user.py:461-469) each raise `PermissionDeniedException` unless
  `self.active_user.is_superuser` is already true. `active_user` is `None`
  outside a request, so these setters can *never* succeed from a management
  command. **The command must set `user.is_staff` / `user.is_superuser` via
  plain attribute assignment** (they're just `BooleanField`s) — bypassing the
  setter methods is correct here, not a security hole, since there is no REST
  caller to gate against.
- `org` (Group FK, user.py:108) is optional (`null=True, blank=True`) — a
  superuser can exist with `org=None`.
- **Frontend/backend permission mismatch** (see Investigation): web-mojo's
  `hasPermission()` treats `permissions.admin = true` as a full wildcard;
  the backend's `has_permission()` does not. The command must not offer an
  `{"admin": true}` shortcut as if it were a lighter-weight superuser — it
  would pass the frontend gate and still 403 on the backend.
- Command conventions: `mojo/apps/account/management/commands/status.py`,
  `model_permissions.py` — plain `BaseCommand`, `add_arguments(self, parser)`,
  `handle(self, *args, **options)`, `self.stdout.write(self.style.SUCCESS(...))`
  / `self.style.ERROR(...)`. No prior password-prompt command exists in this
  app; use stdlib `getpass.getpass()`.

### Changes — what to do
1. `mojo/apps/account/management/commands/create_user.py` (new) —
   `Command(BaseCommand)`:
   - **Args**: `--username`, `--email`, `--phone`, `--first-name`,
     `--last-name`, `--password`, `--password-env` (env var name to read the
     password from), `--staff` (store_true), `--superuser` (store_true),
     `--permission` (`action="append"`, repeatable), `--org` (int Group id).
   - Require at least one of `--email`/`--phone`; raise `CommandError` if
     neither given.
   - Pre-check existence: `User.objects.filter(username=...)`.exists()` /
     `email=` / `phone_number=` up front for any value actually supplied, and
     raise a clean `CommandError` naming the conflicting field (instead of a
     raw `IntegrityError` traceback from the DB unique constraint).
   - Resolve the password, in order: `--password` (print a
     `self.style.WARNING` to stderr that it's visible in shell
     history/process list) → `--password-env` (read `os.environ[name]`,
     `CommandError` if unset/empty) → interactive: if
     `sys.stdin.isatty()`, prompt via `getpass.getpass("Password: ")` +
     `getpass.getpass("Password (again): ")` and require they match
     (`CommandError` if not); if not a tty and neither flag given,
     `CommandError` telling the operator to pass `--password` or
     `--password-env`.
   - Build the user mirroring `on_register`'s block (mojo/apps/account/rest/user.py:470-514),
     inside `transaction.atomic()`:
     1. `user = User(email=email or None)`
     2. set `first_name`/`last_name` if given
     3. set phone via `user.set_phone_number(phone)` if given (normalizes +
        raises `ValueException` on bad format)
     4. resolve `username`: use `--username` verbatim if given, else
        `user.generate_username_from_email()` (email path) or
        `user.generate_username_from_names(fallback=phone)` (phone path)
     5. `user.check_password_strength(password)` then `user.set_password(password)`
     6. `user.infer_names_from_email()`
     7. `if not user.display_name: user.display_name = user.generate_display_name()`
     8. `user.validate_username()`; `user.validate_email()` if email set;
        `user.validate_name_fields({}, created=True)`
     9. `user.is_staff = options["staff"] or options["superuser"]`;
        `user.is_superuser = options["superuser"]` (plain attribute
        assignment — see Design decisions)
     10. `user.save()`
   - After save (still inside the atomic block): if `--permission` values
     given, `user.add_permission(options["permission"], commit=True)`; if
     `--org` given, `user.org = Group.objects.get(pk=options["org"])`
     (`CommandError` if `Group.DoesNotExist`) and `user.save()` again.
   - `self.stdout.write(self.style.SUCCESS(...))` summarizing username, id,
     staff/superuser flags, and permissions granted.
2. `docs/django_developer/account/bootstrap.md` (new) — covers: why
   `./manage.py createsuperuser` doesn't work here (root cause above); how to
   use `create_user` for email-based and phone-only admins; the
   password-input options and their tradeoffs; a portal-section →
   permission-key reference table (from the Investigation list above) with an
   explicit callout that `{"admin": true}` is frontend-only and not a backend
   equivalent to `is_superuser`; troubleshooting (duplicate user, weak
   password, non-tty with no password source).
3. `docs/django_developer/account/README.md` — add one line linking to
   `bootstrap.md`.
4. `CHANGELOG.md` — note the new command and that `createsuperuser` is
   unsupported.

### Design decisions
- **Mirror `on_register`'s direct-construction pattern rather than calling
  `CustomUserManager.create_user`.** The manager requires `email` truthy, so
  it can't create phone-only users; `on_register` already has a proven branch
  for both identities. One code path for both modes avoids maintaining
  divergent manager-based (email) vs. manual (phone) logic.
- **Leave `CustomUserManager`/Django's `createsuperuser` untouched; document
  as unsupported** rather than patching the manager to be
  `createsuperuser`-compatible. The manager already works for direct
  programmatic callers; building a second, half-fixed admin-creation path
  would just duplicate what the new command does properly.
- **Bypass `set_is_staff`/`set_is_superuser` via plain attribute assignment.**
  They assume a REST caller (`active_user`) already exists, which is false
  for the very first admin. Add a code comment explaining why, so this isn't
  "fixed" later into a check that makes bootstrap impossible again.
- **No `{"admin": true}` shortcut.** It only satisfies the frontend's
  wildcard, not the backend's `has_permission`. Offering it as a
  "lighter-weight admin" option would be a footgun — document `--superuser`
  as the only true full-access grant, and `--permission <key>` for scoped
  access.
- **`--password-env` over a bare `--password` for the primary non-interactive
  path** — avoids putting the plaintext in argv (visible via `ps`/shell
  history), consistent with the repo's fail-closed security default.

### Edge cases & risks
- Duplicate username/email/phone → clean `CommandError` pre-check, not a DB
  traceback; the atomic block still protects against a race.
- Weak password → `CommandError` from `check_password_strength`'s
  `ValueException`, before any DB write.
- Invalid phone format → `set_phone_number` raises `ValueException` →
  surfaced as `CommandError`.
- Non-interactive shell with no `--password`/`--password-env` → explicit
  `CommandError`, not a hanging `getpass` prompt.
- `--org` pointing at a nonexistent Group id → `CommandError`, not a silent
  `None`.
- Granting `--permission admin` (the frontend wildcard key) without
  `--superuser` → allowed (it's just another permission key to the backend),
  but the docs must call out that it does **not** grant backend API access,
  to prevent operators assuming it's a lighter superuser.

### Tests
Testit, `tests/test_account/test_create_user_command.py` — clean up any
pre-existing test users by username first (per `.claude/rules/testing.md`),
then via `from django.core.management import call_command`:
- creates an email-based user with `--superuser`; assert `is_staff`/
  `is_superuser` are `True` and `user.check_password(...)` verifies the given
  password.
- creates a phone-only user (`--phone`, no `--email`); assert `email is None`,
  `phone_number` is normalized, and a username was auto-generated.
- `--permission manage_users` on a non-superuser `--staff` user; assert
  `user.has_permission("manage_users")` is `True` and
  `user.has_permission("view_logs")` is `False` (no wildcard leak).
- duplicate `--username` → asserts `CommandError` is raised via
  `self.assertRaises(CommandError, call_command, ...)` and no extra user row
  was created.
- weak `--password` → asserts `CommandError` raised, no user row created.

### Docs
- `docs/django_developer/account/bootstrap.md` (new)
- `docs/django_developer/account/README.md` (link)
- `CHANGELOG.md`

### Open questions
None — ready to build.

## Notes
**Baseline (before first edit)**: `bin/run_tests --agent` — 2282 total, 2226
passed, 0 failed, 56 skipped (plus 325 opt-in `--full`-only tests in
`test_incident`/`test_security`, not run by default). All-green baseline; no
pre-existing failures to attribute away.

**After implementation**: `test_account` module — 179/179 passed (174
baseline + 5 new tests in `test_create_user_command.py`), 0 failed.

One test-fixture false positive found and fixed during implementation (not a
command bug): the auto-generated username for the `--staff` test fixture
email happened to consonant-skeleton-match a blocked term in
`content_guard.check_username`'s fuzzy matching (`"cucmd_staff"` → skeleton
matches `"stfu"`). Renamed the fixture to `cucmd_scoped@test.com`.

## Resolution
- closed: 2026-07-05
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/account/README.md,docs/django_developer/account/bootstrap.md,mojo/apps/account/management/commands/create_user.py,planning/.next_id,planning/in_progress/DM-013-management-command-to-create-initial-users-admins.md,tests/test_account/test_create_user_command.py,uv.lock
- tests added: tests/test_account/test_create_user_command.py (5 tests: email superuser,
  phone-only user, staff + scoped permission, duplicate rejection, weak-password rejection)
