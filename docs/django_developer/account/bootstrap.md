# Bootstrapping Users & Admins

How to create the first admin user (and any subsequent user) for a fresh
django-mojo deployment.

## Why `./manage.py createsuperuser` doesn't work

Django's built-in `createsuperuser` prompts for `USERNAME_FIELD` (`username`
on this project's `account.User`) and calls the user manager as
`create_superuser(username=..., password=...)`. But
`CustomUserManager.create_superuser`/`create_user`
(`mojo/apps/account/models/user.py`) require `email` as their first argument —
so the built-in command raises `TypeError: create_superuser() missing 1
required positional argument: 'email'`. This is not a configuration problem;
it's a fundamental mismatch between Django's generic username-first flow and
this project's email/phone-based identity model. Use `create_user` instead.

## The `create_user` management command

```bash
# Full admin (bypasses every permission check, on both frontend and backend)
./manage.py create_user --email admin@example.com --superuser

# Phone-only account, no admin access
./manage.py create_user --phone +15551234567 --first-name Ada --last-name Lovelace

# Non-superuser admin scoped to specific portal sections
./manage.py create_user --email ops@example.com --staff \
    --permission manage_users --permission view_logs
```

Provide at least one of `--email` / `--phone`. If `--username` is omitted, one
is generated from the email local-part or from first/last name (falling back
to the phone number).

**Password input** (in priority order):
1. `--password <value>` — simplest, but visible in shell history and process
   list (`ps`). The command prints a warning when used.
2. `--password-env <VAR_NAME>` — reads the password from an environment
   variable; nothing sensitive touches argv.
3. Interactive prompt (default when neither flag is given and stdin is a
   tty) — prompts twice and requires a match.

Password strength is enforced (same policy as changing a password via the
API): 12+ characters, or 10+ with a mix of case/digits/special characters.

### Flags

| Flag | Purpose |
|---|---|
| `--username` | Explicit username; auto-generated if omitted |
| `--email` | Email address |
| `--phone` | Phone number (phone-only accounts) |
| `--first-name`, `--last-name` | Profile name fields |
| `--password`, `--password-env` | Password source (see above) |
| `--staff` | Sets `is_staff` |
| `--superuser` | Sets `is_staff` + `is_superuser` — full access, frontend and backend |
| `--permission <key>` | Grant a permission key (repeatable) |
| `--org <group_id>` | Assign the user's default organization (`account.Group`) |

## `--superuser` vs. `--permission` — there is no partial-admin shortcut

The web-mojo frontend's `hasPermission()` treats `permissions.admin = true`
as a wildcard that satisfies every check — nav visibility, page gating,
everything. **The backend does not.** `User.has_permission()`
(`mojo/apps/account/models/user.py`) only checks `is_superuser`, the literal
strings `"all"`/`"authenticated"`, or an exact key match in the `permissions`
dict — it has no `"admin"` wildcard.

So granting only `{"admin": true}` would get someone into the portal admin
navigation, while every real REST call behind it (anything gated by, say,
`manage_users`) still returns 403. **`create_user` intentionally does not
offer this as a lighter-weight admin option.** There are exactly two sound
choices:

- **`--superuser`** — `is_superuser=True` bypasses checks correctly on both
  sides. Use for genuine full-access admins.
- **`--permission <key>`** (repeatable) — grant the exact keys the desired
  portal sections need. These behave identically on both sides since both
  read the same JSON dict literally.

## Portal section → permission key reference

From web-mojo's `src/admin.js` route registrations:

| Portal section | Required permission key(s) |
|---|---|
| Dashboard | `security` |
| Users | `view_users`, `manage_users` |
| Groups / Members | `view_groups`/`manage_groups`, `view_members`/`manage_groups` |
| Jobs | `view_jobs`, `manage_jobs` |
| Logs | `view_logs` |
| Incidents / Events / Blocked IPs | `view_security` |
| Rulesets / Tickets | `manage_security` |
| S3 Buckets / Email / CloudWatch | `manage_aws` |
| Files / File Managers | `manage_files`, `view_fileman` |
| Push (dashboard/templates/deliveries/devices) | `manage_notifications`, `view_notifications`, `manage_push_config` |
| Settings | `manage_settings` |

Any of these can be granted individually with `--permission <key>` for a
non-superuser admin scoped to just those sections.

## Troubleshooting

- **"A user with email/username/phone already exists"** — the command
  pre-checks for conflicts and refuses rather than raising a raw database
  error; pick a different identity or update the existing user through the
  portal/REST API instead.
- **"Password is too weak..."** — use a longer password or mix in
  uppercase/lowercase/digits/special characters.
- **"No password source available in a non-interactive shell"** — running
  from a script/CI with no tty requires `--password` or `--password-env`.
