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
global `view_security`, `manage_security`, `security`, or `admin` grant. Its
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

The shared v1 and v2 clients renew a 401 once only for GET/HEAD. A mutation is
never replayed after an ambiguous 401. HTTP 440 may retry once after the
pre-action recent-auth ceremony. A terminal 401 tears down authenticated
chrome and preserves the exact path, query, and hash in the sign-in return.
Errors retain typed HTTP status/code but render only bounded scalar messages.

## Preview and browser proof

`bin/admin_preview --security-state STATE` supports `full`, `empty`,
`unavailable`, `view-only`, `no-access`, `partial`, `failed`, `stale`,
`expired-session`, `440`, `conflict`, and `recovery`.

The opt-in real-browser rider requires an explicit executable:

```bash
MOJO_ADMIN_CHROME=/exact/path/to/chrome \
  bin/run_tests --agent --extra slow \
  -t test_account.test_admin_security_browser
```

It creates isolated preview/CDP ports and a temporary browser profile, applies
hard deadlines, terminates both processes, and treats console/runtime errors
as failures. It never reaches a live firewall or public target.
