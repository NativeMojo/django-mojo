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
1. `--login-link` — set **no** password of your own; see below.
2. `--password <value>` — simplest, but visible in shell history and process
   list (`ps`). The command prints a warning when used.
3. `--password-env <VAR_NAME>` — reads the password from an environment
   variable; nothing sensitive touches argv.
4. Interactive prompt (default when neither flag is given and stdin is a
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
| `--login-link` | Choose no password; print a single-use reset link instead. Requires `--email`; refuses `--password`/`--password-env` |
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

## The first admin on a freshly provisioned node — `--login-link`

A brand-new environment has a problem the flags above do not solve: there is
no terminal to type a password at (you are running over SSH from a
provisioning script), and any password you *could* pass would then exist in
argv, in shell history, and in whatever log captured the run.

`--login-link` answers that:

```bash
./manage.py create_user --email admin@example.com --superuser --login-link
```

It sets an 18-character random password using the same generator and strength
check the portal's "issue a temporary password" admin action uses, **discards
it unread**, and prints a single-use password-reset link instead:

```
Created user 'admin' (id=1) [staff] [superuser]
Login link: https://example.com/auth?flow=password_reset&token=pr:…
This link is single use and expires in one hour. It sets the account's
password; it does not reveal one.
```

Nobody, including the operator, ever holds that generated password — so the
account is never left with a weak or shared one, and the person who opens the
link chooses their own. The token is a normal `pr:` password-reset token: one
hour TTL (`PASSWORD_RESET_TOKEN_TTL`), single use.

**The link works on its first click**, including for someone with no session
who gets bounced through the challenge page on the way — see
[auth_pages.md](auth_pages.md#a-reset-link-survives-a-cold-bouncer-challenge).

Rules:

- **Requires `--email`.** A reset link is addressed to an account by email; a
  phone-only account has nowhere for one to go.
- **Refuses `--password` / `--password-env`.** Two intents in one command;
  silently honouring one of them is how an operator ends up believing an
  account has a password it does not.
- **An empty or relative `BASE_URL`** means no absolute link can be built. The
  command prints a warning and the raw token rather than something that looks
  like a link and is not. (`build_token_url` resolves the base from the user's
  org metadata, `WEBAPP_BASE_URL`, then `BASE_URL`.)

`python3 -m mojo.deploy.provision admin` is this command run for you over SSH
against node 0 of a freshly provisioned environment — see
[../deploy/provision.md](../deploy/provision.md#admin--the-first-superuser).

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
  from a script/CI with no tty requires `--password`, `--password-env`, or
  `--login-link` (which needs no password source at all and is the right
  answer when you are bootstrapping the first admin).
- **`--login-link` printed a raw `pr:` token instead of a URL** — `BASE_URL`
  (or `WEBAPP_BASE_URL`, or the org's `webapp_base_url` metadata) is unset or
  relative, so no absolute link could be built. Set it and re-issue, or paste
  the token into the reset form by hand.
- **`--login-link` refused with "cannot be combined"** — drop `--password` /
  `--password-env`; the flag sets its own throwaway password by design.
- **The account already exists and you want another link** — `create_user`
  refuses duplicates. Use the portal's Users section ("Send reset link"), which
  issues exactly the same kind of token.
