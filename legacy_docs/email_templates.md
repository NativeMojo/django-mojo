# Email Templates (DB) — Names & Context (KISS)

This document is a lightweight reference for **database-backed email templates** used via `send_template_email(...)` and the **context fields** they receive.

Scope:
- Templates rendered from the `EmailTemplate` model (DB templates), sent via `Mailbox.send_template_email(...)`.
- This is **not** about AWS SES “managed templates”.

---

## How template names are resolved (important)

When you call `User.send_template_email(...)` or `Group.send_template_email(...)`, the template name may be rewritten before the DB template is loaded.

### 1) Optional prefix override (User-level helper)
When sending via `User.send_template_email(template_name="X", template_prefix=...)`:

- If `template_prefix` is provided (or inferred from `user.org.metadata["email_template"]`),
- The system will try: `"{template_prefix}_{X}"`
- If a DB `EmailTemplate` exists with that name, it will be used instead of `"X"`.

Example:
- Base: `invite`
- Prefixed: `acme_invite`

### 2) Optional domain override (Mailbox-level helper)
When sending via `Mailbox.send_template_email(template_name="X")`:

- If the mailbox has a domain name, the system will try: `"{domain}.{X}"`
- If a DB `EmailTemplate` exists with that name, it will be used instead of `"X"`.

Example:
- Base: `invite`
- Domain override: `example.com.invite`

### Combined behavior
If both apply, the effective lookup can become:

1. (optional) prefix rewrite: `prefix_X`
2. (optional) domain rewrite: `domain.tld.prefix_X`

---

## Context injection rules (what’s added automatically)

### `User.send_template_email(...)`
Guarantees:
- If `context["user"]` is missing, it is auto-added as `user.to_dict("basic")`.

So every template sent via `User.send_template_email(...)` can assume:
- `user` (dict) is present.

### `Group.send_template_email(...)`
Guarantees:
- If `context["group"]` is missing, it is auto-added as the `Group` instance (`self`).

So every template sent via `Group.send_template_email(...)` can assume:
- `group` is present (usually a model instance unless caller sets a dict).

---

## Templates in use (names + context fields)

### 1) `group_invite`
Used by: group membership invitation flow.

Context:
- `group` (dict): `group.to_dict("basic")` is explicitly provided by the caller
- `user` (dict): auto-added by `User.send_template_email(...)` if not present

Minimum keys to expect:
- `group`
- `user`

Possible overrides:
- Prefix override: `"{template_prefix}_group_invite"` (if exists in DB)
- Domain override: `"{domain}.{final_name}"` (if exists in DB)

---

### 2) `invite`
Used by: user invitation flow.

Context:
- `user` (dict): explicitly provided by the caller (`user.to_dict("basic")`)
- `token` (string): invitation token
- Optional: additional keys from `kwargs` if they are:
  - objects with `to_dict()` → stored as `value.to_dict("basic")`
  - primitive `str|int|float`

Minimum keys to expect:
- `user`
- `token`

Possible overrides:
- Prefix override: `"{template_prefix}_invite"` (if exists in DB)
- Domain override: `"{domain}.{final_name}"` (if exists in DB)

---

### 3) `password_reset_code`
Used by: password reset flow when reset method is `"code"`.

Context:
- `code` (string): 6-character reset code (generated server-side)
- `user` (dict): auto-added by `User.send_template_email(...)` if not present

Minimum keys to expect:
- `code`
- `user`

Possible overrides:
- Prefix override: `"{template_prefix}_password_reset_code"` (if exists in DB)
- Domain override: `"{domain}.{final_name}"` (if exists in DB)

---

### 4) `password_reset_link`
Used by: password reset flow when reset method is `"link"` or `"email"`.

Context:
- `token` (string): password reset token (generated server-side)
- `user` (dict): auto-added by `User.send_template_email(...)` if not present

Minimum keys to expect:
- `token`
- `user`

Possible overrides:
- Prefix override: `"{template_prefix}_password_reset_link"` (if exists in DB)
- Domain override: `"{domain}.{final_name}"` (if exists in DB)

---

## Notes / Gotchas (keep in mind)

- Context shapes:
  - `user` is generally a **dict** (`to_dict("basic")`).
  - `group` may be a **dict** or a **model instance**, depending on the call path.
- If you add a new template, keep the context minimal and explicit at the call site.
- If you rely on organization/domain-specific overrides, ensure the corresponding `EmailTemplate.name` exists in the DB.

---