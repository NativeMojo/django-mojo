# Reporting Events — REST API Reference

Use this endpoint to report security or application events from a web page or client. Events feed into the incident system and can trigger alerts, rule processing, and incident creation.

## Endpoint

**POST** `/api/incident/event`

No authentication is required — the endpoint is open to all (`CREATE_PERMS = ["all"]`).

## Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `category` | string | yes | Dot-namespaced event type, e.g. `auth:failed`, `xss:attempt` |
| `level` | integer | no | Severity 0–15. 0–3 informational, 4–7 warning, 8–15 critical. Default `0` |
| `scope` | string | no | Logical grouping, e.g. `global`, `account`, `payment`. Default `global` |
| `title` | string | no | Short human-readable summary |
| `details` | string | no | Full description or stack trace |
| `source_ip` | string | no | Client IP (server will use request IP if omitted) |
| `hostname` | string | no | Originating hostname |
| `uid` | integer | no | User ID associated with the event |
| `model_name` | string | no | Related model, e.g. `account.User` |
| `model_id` | integer | no | Related model instance ID |
| `metadata` | object | no | Arbitrary key/value data — include any custom fields here, they are available for rule matching |

## Custom Metadata

The `metadata` field accepts any JSON object. Use it to attach whatever context is relevant — there are no restrictions on keys or structure. Everything in `metadata` is available for server-side rule matching.

```json
{
  "category": "auth:failed",
  "level": 5,
  "metadata": {
    "username": "bob@example.com",
    "attempt_count": 7,
    "auth_method": "password",
    "screen": "login"
  }
}
```

A server-side rule with `field_name="attempt_count"` and `comparator=">="` and `value="5"` would match this event and can trigger alerts, tickets, or other automated responses.

## Level Reference

| Range | Meaning |
|---|---|
| 0–3 | Informational / low importance |
| 4–7 | Warning / potential issue |
| 8–15 | Increasing severity — 15 is critical |

Events with `level >= 7` (configurable via `INCIDENT_LEVEL_THRESHOLD`) automatically create or escalate an incident.

## Examples

### Report a failed login attempt

```js
await fetch('/api/incident/event', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    category: 'auth:failed',
    level: 5,
    scope: 'account',
    title: 'Failed login attempt',
    details: 'User attempted to log in with an invalid password.',
    uid: 42,
  }),
});
```

### Report a suspicious client-side action

```js
await fetch('/api/incident/event', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    category: 'xss:attempt',
    level: 8,
    scope: 'global',
    title: 'Possible XSS detected',
    details: document.location.href,
    metadata: {
      user_agent: navigator.userAgent,
      referrer: document.referrer,
    },
  }),
});
```

### Report a payment error

```js
await fetch('/api/incident/event', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    category: 'payment:error',
    level: 6,
    scope: 'payment',
    title: 'Checkout failed',
    model_name: 'billing.Order',
    model_id: 1099,
    details: 'Card declined — insufficient funds.',
  }),
});
```

## Response

```json
{
  "status": true,
  "data": {
    "id": 4821,
    "created": "2026-02-24T12:00:00Z",
    "level": 5,
    "scope": "account",
    "category": "auth:failed",
    "title": "Failed login attempt"
  }
}
```

## How Events Become Incidents

1. The event is saved and metrics are recorded.
2. The system looks for a matching `RuleSet` by `scope` then `category`.
3. If a matching rule set exists, or `level >= INCIDENT_LEVEL_THRESHOLD` (default 7), an incident is created or updated.
4. If the rule set defines `min_count` or `window_minutes`, events stay in `pending` status until the threshold is met, then transition to `new` and handlers fire.

## Tips

- Use consistent `category` naming (e.g. `auth:failed`, `auth:locked`) so rule sets can match reliably.
- Pass `uid` when the event is tied to a logged-in user — it helps correlate incidents.
- Keep `level` proportional to actual severity. Overusing high levels reduces signal quality.
- Use `metadata` for any extra context you want visible in the incident dashboard without needing dedicated fields.
