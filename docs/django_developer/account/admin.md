# Packaged Admin v2

The packaged Admin v2 has exactly seven feature destinations: Home, Apps,
Infrastructure, Domains, Access, Security, and Settings. Admin v1 remains
packaged with its original four-lane Activity surface. In v2, incidents and
events live only at `#/security-operations`; Activity retains tickets and logs.
Authorized old
`#/activity?tab=incidents` and `tab=events` links are canonicalized to Security.
If Security is unavailable or unreadable, those links fail closed to Home.

## Security feature admission

The ordinary Admin source-session gate still requires global Admin access and
denies key-backed sessions. After that gate, the `security` bootstrap provider
is enabled only when `mojo.apps.incident` is installed and the caller has a
global `view_security`, `manage_security`, or `security` grant. A literal
`admin` grant admits the portal but is not a fine-grained Security wildcard. Its
capabilities are independent:

```json
{"id":"security","enabled":true,"capabilities":{"view":true,"manage":false}}
```

Reads use `view_security|manage_security|security`; writes use
`manage_security|security` and recent authentication. Activity's ticket
capabilities are also Incident-aware. Do not infer either provider from a
generic Admin grant in a client.

## Browser boundary

Security consumes only `/api/incident/admin/security` schema version 2 and its
governed action endpoint. It renders server-curated fields, action schemas,
cutoff/window metadata, and captured checked-host summaries. It never reads
the generic Incident/Event endpoints, serializes raw rows, or reconstructs
policy/enforcement rules in JavaScript.

Schema version alone is not trusted. The v2 client validates each requested
section's envelope, window, bounded row shape, policy/action schemas, and
firewall host lists before rendering it. A missing, malformed, contradictory,
or oversized value fails the requested view with a contract error; it is never
coerced into an empty table and never enables a governed action.

The shared v1 and v2 clients renew a 401 once only for GET/HEAD. A mutation is
never replayed after an ambiguous 401. HTTP 440 may retry once after the
pre-action recent-auth ceremony. A terminal 401 tears down authenticated
chrome and preserves the exact path, query, and hash in the sign-in return.
Errors retain typed status/code but render only bounded scalar messages. When
legacy `MOJO_APP_STATUS_200_ON_ERROR` folds a failure onto HTTP 200, the error
envelope's validated `error_status` remains authoritative, so folded 401, 409,
and 440 responses follow the same state machine as native HTTP statuses.

## Preview and browser proof

`bin/admin_preview --security-state STATE` supports `full`, `empty`,
`unavailable`, `view-only`, `no-access`, `partial`, `failed`, `stale`,
`expired-session`, `440`, `conflict`, `recovery`, and `malformed`.

The opt-in real-browser rider requires an explicit executable:

```bash
MOJO_ADMIN_CHROME=/exact/path/to/chrome \
  bin/run_tests --agent --extra slow \
  -t test_account.test_admin_security_browser
```

It creates isolated preview/CDP ports and a temporary browser profile, applies
hard deadlines, terminates both processes, and treats console/runtime errors
as failures. It never reaches a live firewall or public target.

For non-destructive acceptance against a real installation, bridge the local
packaged source to that installation and open Admin v2:

```bash
bin/admin_preview --port 8766 --upstream https://api.example.com
# open http://localhost:8766/admin/v2/#/security-operations
```

Prefer an operator with global `view_admin` and `view_security` but no
`manage_security`. Verify admission, all six tabs, schema-v2 status/cutoff
rendering, redaction, legacy Activity links, and session recovery using reads
only. Do not submit any RuleSet, recommendation, or IPSet confirmation: those
requests target the real installation and may change policy or fleet state.
