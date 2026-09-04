# Packaged Admin Security client

Admin v2 exposes Security at `#/security-operations` with Overview, Cases,
Incidents & events, Rules, Firewall & IPSets, and Recommendations. This is the
seventh packaged feature destination. Admin v1 is compatible and still keeps its
original Activity surface; v2 Activity now contains only tickets and logs.

Use `features.security` from `GET /api/account/admin/bootstrap` to disclose the
destination. `enabled` plus `capabilities.view` is required to route there;
`capabilities.manage` controls mutation affordances. A missing/malformed block,
an absent Incident app, or an unauthorized legacy Activity hash must not render
Security.

All content comes from the version-2 Admin Security envelope. Every panel must
honor its own `status`, `cutoff`, `window`, and `truncated` values. Display exact
and sampled metrics separately. An unavailable, partial, missing, or stale
receipt is not success and is not an empty result.

RuleSet forms and governed actions are generated from `sections=schemas`.
Bind actions to `modified`, require the advertised typed confirmation, and send
them once. On 409, reread authoritative state, discard/rebase the draft, and
ask for confirmation again. Never replay automatically.

For authentication recovery, retry a 401 once only for GET/HEAD. Do not replay
a POST after a 401. A 440 indicates the server refused the action before it ran;
perform recent authentication and retry once. If the session cannot be renewed,
remove authenticated chrome and send the user to sign-in with the exact current
path/query/hash as `redirect`.
