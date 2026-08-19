# Admin SMS Management — Django Developer Reference

`mojo/apps/phonehub/services/admin_sms.py` backs the Admin portal's
**Text messages** page (`GET/POST /api/account/admin/messaging-sms…`, see the
web-developer doc for wire shapes). It manages the **system** `PhoneConfig`
row (`group=None`) — the row every OTP/MFA/magic-login/password-reset SMS
routes through, because none of those senders pass a `group`.

Everything imports phonehub models lazily and answers a clean
`not_installed` envelope when `mojo.apps.phonehub` is not in INSTALLED_APPS.

## Service functions

| Function | What it does |
|---|---|
| `summary()` | System config (secret **presence** only, never values), active group overrides (bounded), persisted verify state, settings fallback facts |
| `test_connection(config_id=None)` | Runs `PhoneConfig.test_connection()` verbatim and post-processes through `diagnose()` — raw provider exception text never reaches the browser; `test_mode` stays a distinct third state |
| `send_test(actor, to_number)` | One `SMS.send()` through the effective config; `+1555…` recipients short-circuit before any provider call |
| `save_config(actor, data)` | Normalize → **mandatory verification** → `provider_setup.save_messaging_system_config(...)` |
| `diagnose(code)` | Fixed browser-facing vocabulary for stable error codes |

## The system-row write gate (security-critical)

Writes to the system row require a **literal superuser**, checked in the REST
body (`requires_global_perms` composes with OR and cannot express AND) and
re-proved under the installation lock inside
`provider_setup.save_messaging_system_config()`. Rationale: a
`manage_phone_config` holder who can point the system row at a
`mojo`-provider URL they control receives every second-factor code on the
installation — verification proves a target *answers*, not that it is the
right target. Group-scoped rows keep the broader tier; editing them is out of
scope for this page.

Verification rules for `save_config`:

- Runs the real per-provider check (`_test_twilio` / `_test_aws` /
  `_test_mojo`) on an **unsaved in-memory candidate** — never
  `test_connection()`, whose `test_mode` short-circuit must not vouch for a
  system-row write. A failed verification refuses the save with nothing
  written.
- Omitted credentials merge from the stored row; a stored `mojo_api_key` is
  reused **only when the submitted URL equals the stored URL**, so the stored
  credential is never replayed to a new host.
- `clear_api_key` / `clear_*_credentials` short-circuit verification the same
  way the Settings-page editor does.

## `provider_setup.save_system_config(...)`

The Settings page's SMS writer was extracted into this parameterized writer
(`mojo/apps/account/services/provider_setup.py`). It keeps the original
row-selection precedence verbatim — first active system row, else first
`provider="mojo"` row, else create — plus the deactivate-other-active-rows
update and `select_for_update`. `_save_sms_config()` is now a thin caller
passing `provider="mojo"`, `name="Mojo Remote SMS"`, so the Settings page's
contract is unchanged.

`bump_revision=True` (default) writes a fresh
`ADMIN_PROVIDER_SETUP_REVISION` inside the same transaction; `apply()` alone
passes `False` because it writes its own S3-bound revision immediately after.
Both editors therefore invalidate the other's open `expected_revision`
instead of silently clobbering.

`save_messaging_system_config(actor, *, provider, name, expected_revision,
verify_result, **fields)` wraps the writer in the full Settings safeguard
stack: installation lock, live superuser re-check, revision-token compare,
audit event (`topic="sms"`), and the verified result persisted via the shared
verify-state record the Dashboard row reads.

## Dashboard source

`admin_platform._dashboard_sms()` feeds the Dashboard's Software-section
"Text messages" row: provider, active state, last verification outcome.
Not in `AVAILABILITY_SOURCES` — an unconfigured provider is absent, not red;
a failed last verification is amber.
