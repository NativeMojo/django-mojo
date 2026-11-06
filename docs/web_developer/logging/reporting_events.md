# Reporting Events — REST API Reference

Two routes feed the incident system. Pick by what you are reporting:

| Route | For | Sign-in | Receipt |
|---|---|---|---|
| `POST /api/incident/event/client` | browser crash and debug reports | not needed | `{"status": true}` |
| `POST /api/incident/event` | security events from a signed-in client | required | `{"status": true, "data": {"id": n}}` |

Both run each accepted report through the RuleSet pipeline once. In both, the
server records who sent it (`uid`), from where (`source_ip`, request details)
and for which group. A client can never set those.

---

## Crash reports: `POST /api/incident/event/client`

Works before sign-in. With a Bearer token the report is filed under the
signed-in person.

**Limits:** 60 requests per minute per IP and 20 per minute per cookie
(`429` beyond that — drop the report, do not retry in a loop); bodies over
64 KB get `413`. The same report (category, message, first stack line, and
the sender) is filed once per 5 minutes; repeats get the same receipt. While
the server's Redis is down, crash reports are dropped.

### Fields

| Field | Rule |
|---|---|
| `category` | required; `frontend:` plus `a-z 0-9 _ . : -`, at most 64 characters. Anything else is `400` |
| `severity` / `level` | `debug` 1, `info` 2, `warning` 4, `error` 5, `fatal` 6, or an integer. Capped below the incident threshold (at most 6); missing or bad is 5. A sent value that differs from the stored one is kept as `claimed_level` |
| `message` | at most 2000 characters (`title` / `details` are accepted as aliases); the title is the first 200 |
| `metadata.stack_trace` | at most 16 KB |
| `metadata.component_stack` | at most 8 KB |
| `metadata.app`, `environment`, `build` | at most 64 each |
| `metadata.reference` | at most 128 — your correlation key |
| `metadata.page_path`, `filename` | at most 512; query string and fragment stripped |
| `metadata.lineno`, `colno` | integers 0–10,000,000, otherwise dropped |

`scope` is always `web`. Every other field and metadata key is dropped. A
`?group=` the sender may not use is kept only as `claimed_group_id`.

### Receipt

```json
{"status": true}
```

There is no event id: an accepted and a suppressed report look the same.
Refusals: `400 {"status": false, "error": "..."}`, `413`, or `429`. A
malformed `group` (not an integer) is a `400` like any other refusal: it counts
against the same limit and files no event.

See [Frontend starter §7](../frontend_starter.md#7-client-error--event-reporting-incidents)
for copyable `error`, `unhandledrejection` and React examples.

---

## Security events: `POST /api/incident/event`

Requires a signed-in user, API key or group token. Anonymous callers get `401`
(`403` with `?group=`).

**Strictly rate limited** (240/min per IP, 120/min per session — DM-042),
including when the caller authenticates with an ApiKey. Sample and dedupe
repeated errors — see
[Rate Limits & Client Backoff](../security/rate_limits.md#never-report-one-telemetry-event-per-failure).

### Fields

| Field | Required | Treatment |
|---|---|---|
| `category` | yes | string, 1–124 characters |
| `level` | no | integer, default `0`. Kept as sent: it is **your claimed severity**, not a trusted one |
| `scope` | no | string, at most 64, default `global` |
| `title`, `details` | no | strings; `details` defaults to `title` or `category` |
| `hostname` | no | cut to 16 characters |
| `model_name`, `model_id` | no | kept as sent |
| `metadata` | no | an object |

**Server-owned** — a value you send is kept only as a claim:

| You send | Stored as |
|---|---|
| `uid` | `metadata.claimed_uid` (the row's `uid` is you, or your API key's linked member) |
| `source_ip` | `metadata.claimed_source_ip` (the row's `source_ip` is the request's) |
| `country_code` | `metadata.claimed_country_code` |
| `incident` | `metadata.claimed_incident_id` |
| a `?group=` you may not use | `metadata.claimed_group_id` |
| a reserved metadata key (`request_ip`, `http_*`, `user_*`, `group_id`, `level`, `reporter`, …) | `metadata.claimed_<key>` |

Every submitted row carries `metadata.reporter` (the credential kind and
user/key ids the server saw). RuleSets can match on the claims
(`claimed_uid`, `claimed_source_ip`, `claimed_group_id`, or any flat metadata
key). Submitted rows never appear in anyone's account security history.

### Example

```js
await fetch('/api/incident/event', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${token}`,
  },
  body: JSON.stringify({
    category: 'payment:error',
    level: 6,
    scope: 'payment',
    title: 'Checkout failed',
    model_name: 'billing.Order',
    model_id: 1099,
    details: 'Card declined — insufficient funds.',
    metadata: { attempt_count: 3, screen: 'checkout' },
  }),
});
```

### Receipt

```json
{"status": true, "data": {"id": 4821}}
```

Only the id. Operators read the full row with `GET /api/incident/event/<id>`
(`view_security`).

---

## How Events Become Incidents

1. The event is saved and metrics are recorded.
2. The system looks for a matching `RuleSet` by `scope`, then `category`, then
   the `*` catch-all.
3. A matching rule set creates or updates an incident. Without one, a
   **security event** at `level >= INCIDENT_LEVEL_THRESHOLD` (default 7) still
   opens an incident, but never starts automatic LLM triage: the level is a
   claim. A **crash report** is capped below the threshold.
4. If the rule set defines `trigger_count` / `trigger_window`, events stay
   `pending` until the threshold is met, then move to `new` and handlers fire.
   Handlers act on the server-stamped `source_ip` and `uid`, never a claim.

## Tips

- Use consistent `category` names (`auth:failed`, `frontend:error`) so rule
  sets match reliably.
- Never put passwords, tokens or other secrets in messages, stacks, paths or
  metadata.
- Keep `level` proportional; it is treated as a claim.
