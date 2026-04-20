# Assistant Metrics Tools — Django Developer Reference

The `metrics` domain lets the LLM discover, fetch, explain, and (for gauges) write metrics across every account type the framework supports.

All tools live in `mojo/apps/assistant/services/tools/metrics.py`. They are loaded via `load_tools(domain="metrics")` — the LLM sees only the domain description until a tool-call activates them.

## Permissions

| Tool class | Tool-level gate | Per-call gate |
|---|---|---|
| Read (discovery, fetch, gauge-read, slug explain, group resolve) | `view_metrics` (or `metrics` category) | `check_view_permissions(request, account)` |
| Gauge write (`set_metric_gauge`) | `write_metrics` (or `metrics` category) | `check_write_permissions(request, account)` |
| `get_system_health` | `view_admin` | — |
| `get_incident_trends` | `view_security` | — |

Per-call checks delegate to the same helpers the REST layer uses:
`mojo/apps/metrics/rest/helpers.py`. Denials raise `PermissionDeniedException`,
the tool converts to `{"error": ...}`, and a level-5 security event fires.

## Account Forms

| Form | Who can view |
|---|---|
| `public` | Anyone |
| `global` | Users with `view_metrics` or `metrics` |
| `group-<id>` | Group members with `view_metrics`/`metrics`, or system-perm users |
| `user-<id>` | The user themselves, or system-perm users |
| Custom | Determined by Redis-stored per-account view perms (`metrics.set_view_perms(...)`); supports `"public"` for open reads |

## Typical Discovery Flow

```
user: "What metrics do we track?"
 └─► list_metric_accounts               → scoped list of accounts
      └─► list_metric_categories(acct)   → categories in that account
           └─► list_metric_slugs(acct)    → slugs in the category
                └─► describe_metric_slug  → explain a mystery slug
                     └─► fetch_metrics / fetch_metric_values / get_metric_gauge
```

A user who already names a slug skips straight to `fetch_metrics`.

---

## Discovery Tools

### `list_metric_accounts`

Unions the two sources of truth:
- `metrics.list_accounts()` — accounts with configured permissions
- `metrics.list_accounts_with_data()` — accounts with any recorded slug

Users with global `view_metrics`/`metrics` see everything. Others are scoped:
`public`, `user-<self>`, every `group-<id>` they have membership-level
`view_metrics`/`metrics` on, and custom accounts whose Redis view_perms
they satisfy.

```json
{
  "accounts": ["global", "group-42", "public", "user-7"],
  "count": 4,
  "scoped": false
}
```

### `list_metric_categories`

Wraps `metrics.get_categories(account)`. Gated by `check_view_permissions`.

### `list_metric_slugs`

Lists time-series slugs on an account.

| Param | Default | Purpose |
|---|---|---|
| `account` | `"public"` | Scope |
| `category` | — | Restrict to slugs within a category |
| `prefix` | — | Client-side prefix filter (e.g. `"login_attempts:ip:"`) |
| `limit` | 500 | Max slugs returned (hard cap 2000) |

Returns `{account, category, prefix, slugs, count, total, truncated}`.

On high-cardinality accounts (thousands of dimensional slugs), always pass
a `prefix` to keep the response small.

### `list_metric_gauges`

Same shape as `list_metric_slugs` but scans the gauge keyspace
(`mets:<account>:val:*`). Returns slug names only — never values.

### `describe_metric_slug`

Greps the mojo framework source tree and `settings.BASE_DIR` for
`metrics.record(...)` call sites matching the slug. Cap: 10 hits, 200-char
snippet per hit. No permission check on the slug name itself (slug
strings are source-code literals, not secrets).

```json
{
  "slug": "sl:click:ABC123",
  "hits": [
    {
      "file": "apps/shortlink/models/shortlink.py",
      "line": 208,
      "snippet": "metrics.record(f'sl:click:{self.key}', category='shortlinks', account='global')"
    }
  ],
  "count": 1
}
```

### `resolve_group_account`

Turns a user-supplied group reference into `group-<id>`.

- Numeric input → pk lookup.
- String input → `Group.objects.filter(name__iexact=name)`.
- Ambiguous (>1 match) → `{"error": "ambiguous group name", "candidates": [...]}`.
- Zero matches → clean error.
- Access check: `group.user_has_permission(user, ["view_metrics", "metrics"])` or system-level perm.

Prevents the LLM from silently guessing when two groups share a name.

---

## Fetch Tools

### `fetch_metrics`

Time-series fetch. Accepts single slug or list.

| Param | Default | Notes |
|---|---|---|
| `slugs` | required | str or list[str] |
| `dt_start` / `dt_end` | — | ISO format; when omitted, `metrics.fetch` picks a window from granularity |
| `granularity` | auto | `minutes`, `hours`, `days`, `weeks`, `months`, `years` |
| `account` | `"public"` | |
| `with_labels` | `true` | Include bucket labels |
| `allow_empty` | `true` | Keep slugs with all-zero values |

**Auto-granularity**: when `granularity` is omitted, picks based on the range
delta: <=3h → `minutes`, <=3d → `hours`, else `days`.

**Retention note**: when `dt_start` predates the granularity's TTL
(`GRANULARITY_EXPIRES_DAYS` in `mojo/apps/metrics/utils.py`), the response
includes `"retention_note": "..."` so the LLM can explain why older buckets
return 0. `hours` retains ~3 days; `days`/`weeks` retain ~360; `minutes`
retain ~1 day.

**Response metadata**: every successful fetch echoes
`{account, granularity, dt_start, dt_end, slug_count}` so the LLM can
describe what it fetched.

### `fetch_metric_values`

Point-in-time snapshot for many slugs at one timestamp.

```json
{
  "data": {"jobs.channel.webhook.failed": 12, "jobs.channel.webhook.completed": 48},
  "slugs": [...],
  "when": "2026-04-19T00:00:00",
  "granularity": "hours",
  "account": "global"
}
```

### `fetch_metrics_by_category`

Fetch every slug in a category at once. Cap: `max_slugs=50` (default),
max 200. Truncation is surfaced:

```json
{
  "category": "bouncer",
  "data": {...},
  "slug_count": 50,
  "total_slugs": 127,
  "truncated": true,
  "account": "global",
  "granularity": "days"
}
```

### `get_metric_gauge`

Read one or more gauge (non-time-series) values. Accepts `slug` (single),
`slugs` (list or comma-separated string), and a `default` for missing keys.

```json
{
  "account": "public",
  "data": {"maintenance_mode": "off", "signup_disabled": "false"},
  "slugs": ["maintenance_mode", "signup_disabled"]
}
```

---

## Write Tool

### `set_metric_gauge` (mutates)

The single write tool in this domain. Designed for operational toggles —
maintenance mode, feature flags, rate-limit overrides — not metric
corrections. (Counter corrections are tracked as a separate follow-up
request.)

```json
{"slug": "maintenance_mode", "value": "on", "account": "public"}
```

Gating:
- Tool-level: `write_metrics` or `metrics` category.
- Per-call: `check_write_permissions(request, account)`.
- LLM description instructs the model to present an `action` block and wait
  for user confirmation before executing.

On success:
- Wraps `metrics.set_value(slug, str(value), account=account)`.
- Writes a `logit.Log` entry with kind `assistant:metric:gauge_set`.
  The payload records `slug` and `account` only — **never the value** (audit
  logs are long-lived; gauge values may be sensitive).
- Agent loop emits an `assistant:tool:set_metric_gauge` event (level 5) per
  the standard mutating-tool event pattern.

---

## System Aggregates (retained)

### `get_system_health`

Unchanged cross-domain roll-up. Permission: `view_admin`. Reads directly
from the ORM (users, incidents, events, jobs) rather than the metrics app.

### `get_incident_trends`

Unchanged incident/event trend aggregate. Permission: `view_security`.

---

## Implementation Notes

- **Synthetic request**: every per-call permission check uses a synthetic
  `objict` request built by `_build_request(user, request_meta=request_meta)`.
  When the assistant is invoked from an HTTP path, the originating IP flows
  through `request_meta.ip` so security events record the real source.
- **Error handling**: `PermissionDeniedException` is caught and converted to
  an error dict; the agent loop never sees a bare raise. Redis-down errors
  return `{"error": "Metrics backend unavailable"}`.
- **Colon-separated slugs** (`sl:click:ABC123`, `login_attempts:ip:1.2.3.4`)
  flow through untouched. The underlying `metrics` module normalizes
  internally.
- **Registration collision**: earlier versions of `discovery.py` registered
  `list_metric_categories` and `list_metric_slugs`. They now live here with
  proper per-account gating; the discovery module no longer registers them.

## See Also

- [docs/django_developer/metrics/recording.md](../metrics/recording.md)
- [docs/django_developer/metrics/fetching.md](../metrics/fetching.md)
- [docs/django_developer/metrics/permissions.md](../metrics/permissions.md)
- [mojo/apps/assistant/services/tools/metrics.py](../../../mojo/apps/assistant/services/tools/metrics.py) — source
- [tests/test_assistant/28_test_metrics_tools.py](../../../tests/test_assistant/28_test_metrics_tools.py) — tests
