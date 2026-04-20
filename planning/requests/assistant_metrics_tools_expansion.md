# Assistant metrics tools — full discovery, gauges, and per-account enforcement

**Type**: request
**Status**: planned
**Date**: 2026-04-19
**Priority**: high

## Description

Expand `mojo/apps/assistant/services/tools/metrics.py` from a 3-tool shim into a complete metrics domain that lets the LLM:

1. **Discover what exists** — list accounts (both configured and data-inferred), categories, slugs, and gauges for an account, so it can answer "what are we tracking?" without the user having to name a slug.
2. **Fetch time-series data** at any granularity (`minutes`, `hours`, `days`, `weeks`, `months`, `years`) across any account type (`public`, `global`, `group-<id>`, `user-<id>`, custom), with retention warnings when the window exceeds the granularity's TTL.
3. **Read and write gauges** — simple key/value slugs recorded via `metrics.set_value` / `get_value`. Reads for status checks; writes for operational toggles like `maintenance_mode`, feature flags, rate-limit overrides.
4. **Explain a slug** — grep the codebase for `metrics.record(...)` call sites that mention the slug so the LLM can tell the user what a mystery slug like `sl:click:ABC123` or `login_attempts:ip:1.2.3.4` actually represents.
5. **Resolve group-name → group-id account** — accept a friendly group name or id and produce the correct `group-<id>` account string, so the LLM does not have to guess.
6. **Enforce per-account permissions** on every call, not just the tool-level `view_admin` gate. Metrics functions themselves do **not** check permissions — the REST layer does, via `mojo/apps/metrics/rest/helpers.py:check_view_permissions` / `check_write_permissions`. The assistant tool must call those same helpers so a user with only group-scoped metric access cannot read `global` or another group's metrics via the LLM, and cannot flip a gauge on an account they can't write to.

This is the most used assistant domain. Metrics are recorded in endpoint decorators, model hooks, jobs, auth flows, shortlinks, content guard — everywhere. The LLM needs to be a first-class explorer of what's been tracked, not just a fetcher of slugs the user already knows.

## Context

Today `mojo/apps/assistant/services/tools/metrics.py` exposes three tools:

- `fetch_metrics(slugs, dt_start, dt_end, granularity, account)` — requires the caller to already know the slug names.
- `get_system_health()` — bespoke Django-ORM roll-up, not a metrics-app wrapper.
- `get_incident_trends()` — same, incident-specific.

Problems:

- **No discovery**: LLM cannot answer "what metrics do we track for group 42?" without manually guessing slug names.
- **No gauges**: no tool for `metrics.get_value` (simple KV — used for maintenance flags, counters that don't need time buckets).
- **No category-aware fetching**: no wrapper around `metrics.fetch_by_category` or `metrics.get_category_slugs`.
- **No point-in-time multi-slug fetch**: no wrapper around `metrics.fetch_values`.
- **Permissions loophole**: the tool only checks `user.has_permission("view_admin")` at dispatch. After that, an admin could read `group-42`'s metrics even though group-42's `VIEW_PERMS` might require being a member. This mismatches the REST endpoint behavior where `check_view_permissions(request, account)` is called on every read.
- **Slug meanings opaque**: developers record metrics inline with `metrics.record("foo_bar", category="baz")` throughout the codebase. The LLM has no way to answer "what does `foo_bar` mean?" — it has to guess from the slug name alone.
- **Account format not documented to LLM**: the current tool description says `"public, global, group-<id>"` but doesn't help the LLM convert "group 42" or "the NativeMojo group" into `group-42`.
- **Granularity hard-coded**: the tool lists "hours, days, months" but the underlying system supports `minutes`, `hours`, `days`, `weeks`, `months`, `years`. LLM can't pick minute granularity for real-time questions.

The underlying `mojo/apps/metrics` functions (`record`, `fetch`, `fetch_values`, `fetch_by_category`, `get_categories`, `get_category_slugs`, `get_account_slugs`, `list_accounts`, `get_value`, `set_value`) are all solid — they just need tool-shaped wrappers with permission gating and good descriptions.

## Acceptance Criteria

- The LLM, with only `view_metrics` (or `metrics` category) permission, can:
  - List every account that has metrics data/config.
  - List every category on a given account.
  - List every slug on a given account, optionally filtered by category.
  - Fetch time-series for one or many slugs over any `dt_start`/`dt_end` at any supported granularity (including `minutes` and `weeks`).
  - Fetch a multi-slug point-in-time snapshot (gauge-style) via `fetch_values`.
  - Fetch a simple KV gauge via `get_value`.
  - Fetch every slug in a category at once via `fetch_by_category`.
  - Resolve a group name/id to `group-<id>`.
  - Grep the codebase for `metrics.record(...)` call sites matching a slug, so it can explain what the metric tracks.
- Every read tool calls `mojo.apps.metrics.rest.helpers.check_view_permissions(synthetic_request, account)` before returning data.
  - Permission denials are converted to clean `{"error": "..."}` returns (not raised exceptions) so the LLM can explain the problem to the user.
  - Each denial is reported via `_report_security_event(...)` at level 5 (same level as model VIEW_PERMS denials).
- **Tool-level `permission` is `view_metrics`** (decision confirmed 2026-04-19) — the `metrics` category permission implicitly grants access via `user.has_permission(["view_metrics", "metrics"])`, matching REST layer semantics. `view_admin` is **not** required to dispatch these tools. `get_system_health` and `get_incident_trends` stay at their existing gate.
- All tools accept `account` with the same format the REST API uses: `public | global | group-<id> | user-<id> | <custom>`. Default account is `public` for safety.
- Tool descriptions:
  - Explicitly list all six granularities.
  - Describe each account scope and when to use it.
  - Tell the LLM to call `list_metric_slugs` / `list_metric_categories` **before** `fetch_metrics` if the user didn't name a specific slug.
  - Tell the LLM to use `describe_metric_slug` when the user asks what a slug means or when the slug name isn't self-explanatory.
- `get_system_health` and `get_incident_trends` are **retained** (they're convenience aggregates, not metrics-app tools). Their permission level is unchanged (`view_admin`).
- All tools respect `core=False` and live in `domain="metrics"` — they're loaded via `load_tools(domain="metrics")`.
- **One write tool — `set_metric_gauge`** — ships in this request for operational toggles (maintenance mode, feature flags, rate-limit overrides). Gated by `write_metrics` at the tool level and `check_write_permissions(request, account)` per call. Writes a `logit.Log` audit entry every time. The LLM is instructed to confirm with the user via an `action` block before executing. Counter writes (`metrics.record`), deletes (`delete_category`), and permission management (`set_view_perms`, `set_write_perms`, `add_account`, `delete_account`) remain out of scope — those stay REST-only.

## Investigation

### What exists

- `mojo/apps/metrics/__init__.py` — public API: `record`, `fetch`, `fetch_values`, `get_categories`, `fetch_by_category`, `get_category_slugs`, `delete_category`, `get_view_perms`, `get_write_perms`, `set_view_perms`, `set_write_perms`, `list_accounts`, `add_account`, `delete_account`, `get_accounts_with_permissions`, `set_value`, `get_value`.
- `mojo/apps/metrics/redis_metrics.py:get_account_slugs(account)` — lists every slug on an account. Already callable from Python; not re-exported in `__init__.py` (we should add it).
- `mojo/apps/metrics/rest/helpers.py` — `check_view_permissions(request, account)` / `check_write_permissions(request, account)`. Raises `PermissionDeniedException`. Handles `global`, `group-<id>`, `user-<id>`, `public`, and custom accounts (looks up per-account perms from Redis when custom).
- `mojo/apps/metrics/utils.py` — granularity constants (`GRANULARITIES = ['minutes', 'hours', 'days', 'weeks', 'months', 'years']`), slug normalization (`normalize_slug` replaces `:` with `|`), and `get_date_range` which auto-picks a sensible default window per granularity.
- `mojo/apps/assistant/services/tools/metrics.py` — current 3-tool file; uses `permission="view_admin"`, hand-rolls account regex instead of importing `check_view_permissions`.
- `mojo/apps/assistant/services/tools/models.py` — template for the synthetic-request pattern (`_build_request(user, ...)`), `_report_security_event`, `_resolve_model` helpers. The new metrics tools should mirror this shape.
- `mojo/apps/assistant/__init__.py:register_tool` — `permission` gate at the tool-call dispatch level. `view_metrics` is the right fit for the expanded tools; admin-only aggregates stay `view_admin`.
- `mojo/apps/assistant/services/tools/discovery.py` — `load_tools` already advertises `metrics` as a domain with description "Fetch time-series metrics, system health, and incident trends." Description should be updated to reflect the expanded capabilities.

### What changes

- **`mojo/apps/metrics/__init__.py`** — export `get_account_slugs` alongside existing names.
- **`mojo/apps/assistant/services/tools/metrics.py`** — full rewrite into a ~10-tool module. Keep `get_system_health` and `get_incident_trends` as-is at the bottom.
- **`mojo/apps/assistant/services/tools/discovery.py`** — update `metrics` domain description in `DOMAIN_DESCRIPTIONS` (mojo/apps/assistant/__init__.py:30) to something like: "Discover, list, and fetch time-series metrics and gauges across accounts, groups, and users. Explain what tracked slugs represent."
- **`docs/django_developer/assistant/`** and **`docs/web_developer/assistant/`** — document the expanded metrics tool surface (one section per tool, with examples).
- **`CHANGELOG.md`** — entry describing the new tools and the permission-model tightening.
- **Tests** — new `tests/test_assistant/28_test_metrics_tools.py` (next sequential number after `27_test_save_model_tool.py`).

### Constraints

- **Permission enforcement must match REST behavior exactly.** The REST layer calls `check_view_permissions(request, account)` on every read (`mojo/apps/metrics/rest/helpers.py:37`). The helper already handles all five account types: `public` (open), `global` (requires `view_metrics`/`metrics`), `group-<id>` (group-member perm OR system-level), `user-<id>` (self OR system-level), custom (Redis-configured per-account perms). The assistant tool builds a synthetic request via `_build_request(user, ...)` and delegates to this helper unchanged — **never roll our own perm logic**.
- **`check_view_permissions` raises `PermissionDeniedException`** — wrap the call in try/except, convert to `{"error": "..."}`, report via `_report_security_event` at level 5, return to the LLM.
- **Group-name ambiguity policy** (question resolved — re-stated concretely): users will type things like "show me metrics for the Acme group" rather than "group-42". `resolve_group_account` accepts a `name_or_id`:
  - If the input is numeric (`"42"` or `42`), treat as pk → return `"group-42"` after verifying user access.
  - If the input is a string, do a case-insensitive exact match: `Group.objects.filter(name__iexact=name_or_id)`.
  - **Ambiguous case**: if the filter returns 2+ rows (e.g. two different groups both named "Acme"), we cannot guess. Return `{"error": "ambiguous group name", "candidates": [{"pk": 42, "name": "Acme"}, {"pk": 57, "name": "Acme"}]}` so the LLM shows the list to the user and asks them to pick a pk. No "best effort" pick — that would silently fetch the wrong data.
  - **Zero matches**: `{"error": "no group found for 'Foo'"}`.
  - **Access denied**: if the user can't access the resolved group (no membership + no system perm), return `{"error": "no access to group-42"}` instead of the account string.
- **Codebase search scope for `describe_metric_slug`** (question resolved — re-stated concretely): the LLM calls this tool when the user asks "what does slug `bouncer:pre_screen_blocks` mean?" or "where is `login_attempts` tracked?". The tool needs to `grep` for the slug inside Python source. Two candidate roots:
  1. The **mojo framework source tree** (this repo: `django-mojo/mojo/**`) — where framework-level metrics live (`api_calls`, `jobs.completed`, `bouncer:*`, `shortlink:click`, etc.).
  2. The **host Django project root** — wherever django-mojo is installed into (`settings.BASE_DIR`) — where the consuming app defines its own slugs.
  Proposal: scan **both**. Walk `*.py` files under each root, match `metrics.record(` lines containing the slug as a literal or inside an f-string prefix. Cap at 10 hits, 200-char snippet per hit. Return `{"slug": ..., "hits": [{"file": relpath, "line": n, "snippet": ...}], "count": N}`. No redaction — slug strings are not secrets; they're the keys developers wrote into source.
- **Dimensional slugs are the norm, not the exception** (question resolved — re-stated concretely): a grep of the current mojo codebase confirms 50+ `metrics.record(...)` call sites across 16 files, with heavy use of dimensional f-strings. Examples found:
  - `mojo/decorators/limits.py:341,352` — `@endpoint_metrics` decorator auto-generates `<slug>:ip:<ip>`, `<slug>:duid:<device_uuid>`, `<slug>:api_key:<key_pk>`, `<slug>:user:<user_pk>` per request.
  - `mojo/apps/account/models/user.py:291` — `user_activity:<user_pk>`.
  - `mojo/apps/account/models/member.py:157` — `member_activity:<member_pk>`.
  - `mojo/apps/account/models/login_event.py:130-132` — `login:country:<cc>`, `login:region:<cc>:<region>`.
  - `mojo/apps/account/models/geolocated_ip.py:350` — `firewall:blocks:country:<cc>`.
  - `mojo/apps/account/rest/bouncer/assess.py:169` — `bouncer:blocks:country:<cc>`.
  - `mojo/apps/incident/models/event.py:288,298` — `incident_events:country:<cc>`, `incident:country:<cc>`.
  - `mojo/apps/jobs/job_engine.py:669,746` — `jobs.channel.<channel>.completed`, `jobs.channel.<channel>.failed`.
  - `mojo/apps/shortlink/models/shortlink.py:201,208` — `shortlink:click` and per-key dimensional variants.
  Conclusion: **dimensional suffixes embed PII-adjacent data (IPs, device IDs, user/member PKs)** but they are already protected by the account-scope permission model — an IP-dimensional slug like `login_attempts:ip:1.2.3.4` is recorded under `group-<id>` or `global`, and `check_view_permissions` governs who can read that account. **No tool-level redaction.** The existing permission model is the protection; redacting at the tool layer would break the core "list what we track" use case. If a deployment wants IPs redacted, that's a separate setting-driven request.
- **Slug patterns contain colons** that become `|` in Redis keys (see `utils.normalize_slug`). The tool must accept the original colon-separated form the developer writes in code (`login_attempts:ip:1.2.3.4`) and pass it through unchanged — `metrics.fetch` handles normalization internally.
- **Default granularity defaults**: if the user asks for a big date range (>90 days) and doesn't specify granularity, default to `days`; if range is <3 days, default to `hours`; if <3 hours, `minutes`. LLM description must mention this so it doesn't fight the default.
- **Week labels** come back from `utils.format_week_label` as human-readable ranges — don't re-process.
- **Custom accounts** (anything not matching `public|global|group-<id>|user-<id>`) are supported. `check_view_permissions` looks them up from Redis and applies per-account perms.

### Related files

- `mojo/apps/assistant/services/tools/metrics.py` (rewrite)
- `mojo/apps/assistant/services/tools/models.py` (template reference)
- `mojo/apps/assistant/services/tools/discovery.py` (domain description)
- `mojo/apps/assistant/__init__.py:30` (DOMAIN_DESCRIPTIONS)
- `mojo/apps/metrics/__init__.py` (export `get_account_slugs`)
- `mojo/apps/metrics/redis_metrics.py` (read-only — understand behavior)
- `mojo/apps/metrics/rest/helpers.py` (delegate to `check_view_permissions`)
- `mojo/apps/metrics/utils.py` (granularity constants)
- `docs/django_developer/metrics/*` (reference, do not modify)
- `docs/django_developer/assistant/` + `docs/web_developer/assistant/` (add tool docs)
- `tests/test_assistant/28_test_metrics_tools.py` (new)
- `CHANGELOG.md`

## Tools to Add / Modify

All live in `mojo/apps/assistant/services/tools/metrics.py`, domain `metrics`, permission `view_metrics` (unless marked otherwise), `core=False`.

| Tool | Purpose | Key params | Notes |
|---|---|---|---|
| `list_metric_accounts` | List all accounts with metrics data or permission configs. | — | Unions `metrics.list_accounts()` (configured perms) with `scan_iter("{mets:*}:mets:*:slugs")` (data-inferred). Users with global `view_metrics`/`metrics` see everything. Others see only `public`, `user-<self>`, accessible `group-<id>`s, and custom accounts with matching view_perms. |
| `list_metric_categories` | List all categories on a given account. | `account` | Wraps `metrics.get_categories(account)`. `check_view_permissions(request, account)`. |
| `list_metric_slugs` | List slugs on an account, optionally filtered by category or prefix. | `account`, `category` (optional), `prefix` (optional), `limit` (default 500, max 2000) | Wraps `metrics.get_account_slugs(account)` or `metrics.get_category_slugs(category, account)`. Filters client-side by `prefix` (e.g. `"login_attempts:ip:"`). Returns `{truncated: true}` when over `limit`. `check_view_permissions`. |
| `list_metric_gauges` *(new)* | List gauge keys stored via `set_value`. | `account`, `prefix` (optional), `limit` (default 500) | Scans `mets:<account>:val:*` via `scan_iter`. Returns slug names only (not values). `check_view_permissions`. |
| `describe_metric_slug` | Explain what a slug means by grepping the codebase for `metrics.record` call sites. | `slug`, `search_paths` (optional list of roots) | No permission check on slug itself (slug names are not sensitive). Scans django-mojo source tree + `settings.BASE_DIR`. Caps output at 10 hits, 200-char snippet each. |
| `fetch_metrics` (rewritten) | Time-series fetch for one or many slugs. | `slugs`, `dt_start`, `dt_end`, `granularity` (auto if omitted), `account`, `with_labels` (default True), `allow_empty` (default True) | `check_view_permissions`. Auto-picks granularity from range when omitted: <3h→minutes, <3d→hours, <90d→days, else days. Response echoes `{account, granularity, dt_start, dt_end, slug_count}` + `retention_note` when `dt_start` predates the granularity's TTL (`GRANULARITY_EXPIRES_DAYS`). |
| `fetch_metric_values` | Point-in-time multi-slug snapshot. | `slugs` (list or comma string), `when` (datetime, optional — defaults to now), `granularity`, `account` | Wraps `metrics.fetch_values`. `check_view_permissions`. |
| `fetch_metrics_by_category` | Every slug in a category fetched at once. | `category`, `account`, `dt_start`, `dt_end`, `granularity`, `with_labels` (default True), `max_slugs` (default 50) | Wraps `metrics.fetch_by_category`. Caps to `max_slugs`; returns `{truncated: true, slug_count, total_slugs}` when exceeded. `check_view_permissions`. |
| `get_metric_gauge` | Fetch a simple KV gauge (non-time-series). | `slug` or `slugs`, `account`, `default` | Wraps `metrics.get_value`. Supports single slug or list for batch fetch. `check_view_permissions`. |
| `set_metric_gauge` *(new, mutates)* | Set a simple KV gauge. | `slug`, `value`, `account` | **`mutates=True`**, tool-level permission `write_metrics`. Wraps `metrics.set_value`. `check_write_permissions(request, account)` per call. Writes `logit.Log` audit entry (`assistant:metric:gauge_set`, fields include `slug`, `account`, no value). LLM description tells it to confirm with the user via an `action` block before firing. |
| `resolve_group_account` | Resolve a group name/id to `group-<id>` account string. | `name_or_id` | Uses `account.Group` lookup: int → pk; string → `iexact` name. Ambiguous → return candidates list. Verifies the user can access that group (`group.user_has_permission(user, ["view_metrics","metrics"])` or system-level perm). |
| `get_system_health` (unchanged) | Cross-domain health aggregate. | — | Stays `view_admin`. |
| `get_incident_trends` (unchanged) | Incident/event trends. | — | Stays `view_security`. |

## Tests Required

New file: `tests/test_assistant/28_test_metrics_tools.py`, one `@th.django_unit_test()` per case. Setup must clean up test metric data before inserting (tests run on long-lived DB).

**Discovery tools:**
- `test_list_metric_accounts_returns_configured_accounts` — seed two accounts with perms, assert both appear.
- `test_list_metric_accounts_includes_data_inferred` — record a metric on `group-99` with no perm config, assert `group-99` appears in the result.
- `test_list_metric_accounts_respects_user_scope` — non-global user sees only their user-`<id>` and accessible groups plus `public`.
- `test_list_metric_categories_for_account` — seed slugs in categories, assert list matches.
- `test_list_metric_slugs_no_category` — all slugs on account.
- `test_list_metric_slugs_in_category` — filtered to category.
- `test_list_metric_slugs_prefix_filter` — `prefix="login_attempts:ip:"` returns only matching slugs.
- `test_list_metric_slugs_truncates_at_limit` — seed 600 slugs, default limit 500 returns `truncated: true`.
- `test_list_metric_gauges_returns_keys` — set three gauges, assert names returned (no values).
- `test_list_metric_gauges_prefix_filter` — prefix filter narrows the scan.

**Fetch tools:**
- `test_fetch_metrics_single_slug_hours` — record, then fetch, values match.
- `test_fetch_metrics_multi_slug_with_labels` — dict shape with `labels` + `data`.
- `test_fetch_metrics_minutes_granularity` — verify minute granularity actually works (was missing from tool before).
- `test_fetch_metrics_weeks_granularity` — week labels come back human-readable.
- `test_fetch_metric_values_point_in_time` — multi-slug snapshot returns dict keyed by slug.
- `test_fetch_metrics_by_category` — category roll-up returns one series per slug.
- `test_get_metric_gauge_single` — `set_value` then `get_metric_gauge` returns it.
- `test_get_metric_gauge_batch` — list input returns dict.
- `test_get_metric_gauge_default_for_missing` — default returned when slug absent.
- `test_fetch_metrics_by_category_caps_slugs` — seed 60 slugs in a category, `max_slugs=50` returns 50 with `truncated: true`.
- `test_fetch_metrics_retention_note_for_old_range` — request 30 days of `hours` data → response has `retention_note` mentioning the 3-day hours TTL.
- `test_fetch_metrics_no_retention_note_within_window` — request 2 days of `hours` data → no `retention_note`.
- `test_fetch_metrics_response_includes_metadata` — every fetch echoes `{account, granularity, dt_start, dt_end, slug_count}`.

**Gauge writes (`set_metric_gauge`):**
- `test_set_metric_gauge_writes_value` — user with `write_metrics` sets `maintenance_mode=on`, `get_value` confirms.
- `test_set_metric_gauge_tool_level_requires_write_metrics` — user with only `view_metrics` cannot dispatch the tool.
- `test_set_metric_gauge_per_account_denied` — user with tool-level `write_metrics` but no access to `group-42` gets `{error: ...}`, security event reported at level 5.
- `test_set_metric_gauge_writes_logit_audit` — successful write emits a `logit.Log` entry with kind `assistant:metric:gauge_set` and metadata containing `slug` and `account`.
- `test_set_metric_gauge_fires_mutation_event` — agent-level `assistant:tool:set_metric_gauge` event fires on success (confirms `mutates=True` wiring).

**Permission enforcement:**
- `test_fetch_metrics_denied_for_global_without_perm` — user lacking `view_metrics`/`metrics` cannot read `global`, gets `{"error": ...}`, security event reported at level 5.
- `test_fetch_metrics_group_account_allowed_for_member` — group member with group-scoped `view_metrics` can fetch `group-<id>`.
- `test_fetch_metrics_group_account_denied_for_non_member` — non-member without global perm denied.
- `test_fetch_metrics_user_account_allowed_for_self` — user reads `user-<own-id>` without any perm.
- `test_fetch_metrics_user_account_denied_for_other` — user without `metrics` cannot read another user's account.
- `test_fetch_metrics_custom_account_respects_redis_perms` — set view perms on custom account, user with that perm allowed.
- `test_fetch_metrics_custom_account_public_perms` — setting `"public"` allows unauthenticated-equivalent reads.
- `test_fetch_metrics_tool_level_view_metrics_required` — user without `view_metrics` / `metrics` cannot dispatch the tool at all (registry-level gate).

**Slug explanation:**
- `test_describe_metric_slug_finds_record_call` — seed a test file containing `metrics.record("my_slug")`, assert tool returns the file/line.
- `test_describe_metric_slug_no_match` — slug with no code hits returns clean `{"hits": [], "message": "No call sites found"}`.
- `test_describe_metric_slug_caps_results` — slug used in 50 places returns ≤10 hits.

**Group resolution:**
- `test_resolve_group_account_by_id` — numeric input → `group-<id>`.
- `test_resolve_group_account_by_name` — string matches one group → `group-<id>`.
- `test_resolve_group_account_ambiguous_name` — two groups named "Acme" → returns candidates list, tool returns `{"error": "ambiguous", "candidates": [...]}`.
- `test_resolve_group_account_denies_access` — user without membership/perm gets denied on group they're not in.

**Defaults + edge cases:**
- `test_fetch_metrics_auto_granularity_large_range` — 6-month range with no granularity → `days`.
- `test_fetch_metrics_auto_granularity_short_range` — 1-hour range → `minutes`.
- `test_fetch_metrics_empty_slug_returns_error` — empty list/string rejected.
- `test_invalid_granularity_rejected` — "decades" → clean error.
- `test_colon_slug_passthrough` — `login_attempts:ip:1.2.3.4` fetched unchanged.

## Settings

No new settings. `METRICS_TIMEZONE` / `METRICS_DEFAULT_MIN_GRANULARITY` / `METRICS_DEFAULT_MAX_GRANULARITY` already exist and continue to apply.

## Docs

- `docs/django_developer/assistant/metrics_tools.md` (new) — one section per tool with parameters, examples, and permission expectations. Cross-link to `docs/django_developer/metrics/fetching.md` and `permissions.md`.
- `docs/web_developer/assistant/` — if a tool-listing doc exists, add entries.
- `docs/django_developer/README.md` — if there's a tool index, update it.
- `CHANGELOG.md` — new entry describing expanded metrics tools, tightened per-account permission gating, and new capabilities (minutes granularity, gauges, slug explanation, group resolution).

## Resolved Questions (2026-04-19)

1. **Tool-level permission** — ✅ `view_metrics`. Kept at the `view_metrics` / `metrics` category level, matching REST semantics. `check_view_permissions` enforces per-account scope on every call.
2. **Group-name disambiguation** — ✅ Return candidates list on ambiguity. See "Constraints → Group-name ambiguity policy" above for the concrete rules.
3. **Codebase search scope** — ✅ Scan both the mojo framework source tree and `settings.BASE_DIR` (the host project). 10-hit cap, 200-char snippet, `*.py` only. No redaction.
4. **Dimensional slug exposure** — ✅ No tool-level redaction. Confirmed via codebase scan that dimensional suffixes (`:ip:`, `:duid:`, `:country:`, `:user:<pk>`, `:channel:`) are the dominant pattern (50+ call sites, 16 files). Per-account permissions are the protection; redacting at the tool layer would break discovery.
5. **Write tools** — ✅ Out of scope here. Deferred to a separate request (see "Follow-up" below).

## Follow-up Request (Tracked Separately)

**Counter correction tools** — flagged for a later, separate request. The use case is "fix bad metrics" — e.g. backfill a missed increment, correct a wrong account, reset a slug. This is distinct from `set_metric_gauge` (simple operational toggle) because it requires:

- Using `query_model` / `aggregate_model` (from `mojo/apps/assistant/services/tools/models.py`) to compute the **correct** total from the underlying ORM data.
- Comparing against the Redis-stored metric count.
- Offering a diff and asking the user to approve the correction.
- Applying via `metrics.record(..., count=<delta>)` across multiple granularities.

Same permission and audit pattern as `set_metric_gauge`, but the computation is the hard part. File once the read tools land and we see how operators actually want to correct metrics in practice.

## Out of Scope

- **Counter writes** (`metrics.record`) — lumped with counter correction (see Follow-up). The only write tool in this request is `set_metric_gauge`.
- **Deletes & permission management**: no `delete_category`, `delete_account`, `set_view_perms`, `set_write_perms`, `add_account`, `delete_metrics_slug`. These stay REST-only.

## Plan

**Status**: planned
**Planned**: 2026-04-19

### Objective

Rewrite `mojo/apps/assistant/services/tools/metrics.py` into an 11-tool domain (10 read + 1 write gauge) with per-account `check_view_permissions` / `check_write_permissions` gating, data-inferred account discovery, dimensional-slug prefix filtering, retention warnings, auto-granularity, group-name resolution, and a codebase grep for slug explanation. Migrate two duplicate tools out of `discovery.py`. Retain `get_system_health` and `get_incident_trends` unchanged.

### Steps

1. **`mojo/apps/metrics/__init__.py`** — add `get_account_slugs` to the re-export list so the assistant tool can enumerate slugs without a category.

2. **`mojo/apps/metrics/redis_metrics.py`** — add two helpers used by the new discovery tools (keeps Redis key knowledge out of the assistant layer):
   - `list_accounts_with_data(redis_con=None)` — returns a set of accounts discovered by `scan_iter("{mets:*}:mets:*:slugs")`, parsing the hash-tag segment to extract the account name.
   - `list_gauge_slugs(account, prefix=None, limit=500, redis_con=None)` — returns `{slugs: [...], truncated: bool, count: int}` by scanning `{mets:<account>}:mets:<account>:val:*` via `scan_iter`, optionally prefix-filtered.
   - Export both from `mojo/apps/metrics/__init__.py`.

3. **`mojo/apps/assistant/services/tools/discovery.py`** — delete the two duplicate tool registrations (`list_metric_categories` at lines 132–162 and `list_metric_slugs` at lines 165–199) plus their intro comment block. They will be re-homed in `metrics.py`. The `load_tools` / `list_tools` / `list_job_channels` / `list_event_categories` / `list_permissions` registrations stay.

4. **`mojo/apps/assistant/__init__.py`** — update `DOMAIN_DESCRIPTIONS["metrics"]` at line 30 to: `"Discover, fetch, and explain time-series metrics and gauges across accounts, groups, and users. Covers API traffic, jobs, bouncer, logins, shortlinks, and any slug recorded via metrics.record(). Includes one write tool for gauge toggles (maintenance_mode, feature flags)."`.

5. **`mojo/apps/assistant/services/tools/metrics.py`** — full rewrite. File order:
   - Imports (`re`, `pathlib`, `mojo.apps.assistant.tool`, `mojo.apps.metrics`, `mojo.apps.metrics.rest.helpers`, `mojo.helpers.{dates,logit}`, `mojo.errors`, `mojo.apps.metrics.utils.GRANULARITY_EXPIRES_DAYS`).
   - Module-level constants: `VALID_GRANULARITIES`, `DEFAULT_SLUG_LIMIT=500`, `MAX_SLUG_LIMIT=2000`, `DEFAULT_CATEGORY_MAX_SLUGS=50`, `DESCRIBE_MAX_HITS=10`, `DESCRIBE_SNIPPET_LEN=200`.
   - Shared helpers (private, underscore-prefixed):
     - `_build_request(user, request_meta=None, method="GET", path="/assistant/metrics")` — copy the pattern from `tools/models.py:139` (`req.user`, `req.ip`, `req.META`, api_key).
     - `_check_account_view(request, account)` — wraps `check_view_permissions`; on `PermissionDeniedException` logs + `_report_security_event(level=5)` + returns `{"error": f"Permission denied for account '{account}'"}`; returns `None` on success.
     - `_check_account_write(request, account)` — same pattern with `check_write_permissions`.
     - `_report_security_event(...)` — mirror from `tools/models.py:114`; thread `request_meta.ip` into the event.
     - `_audit_log(user, kind, message, request=None, conversation=None, payload=None)` — thin wrapper over `logit.Log.logit(...)` copied from `tools/models.py:80` (gauge writes only).
     - `_validate_granularity(granularity)` — returns `(value, error_dict_or_none)`.
     - `_auto_granularity(dt_start, dt_end)` — returns `minutes`/`hours`/`days` based on window delta; None-safe.
     - `_retention_note(granularity, dt_start)` — returns a string like `"hours granularity retains ~3 days of data; buckets before 2026-04-16 return 0"` when `dt_start` predates the TTL window; else `None`.
     - `_resolve_user_accessible_accounts(user)` — returns the set of accounts the user is allowed to see when they lack global `view_metrics`/`metrics`: `{"public", f"user-{user.pk}"}` ∪ every `group-<id>` the user is a member of with `view_metrics`/`metrics` via `group.user_has_permission`, filtered against the configured + data-inferred accounts list.
     - `_echo_meta(account, granularity=None, dt_start=None, dt_end=None, slug_count=None)` — dict helper that fetch tools add to their response.
   - Tool registrations in this order (helps reading): `list_metric_accounts`, `list_metric_categories`, `list_metric_slugs`, `list_metric_gauges`, `describe_metric_slug`, `resolve_group_account`, `fetch_metrics`, `fetch_metric_values`, `fetch_metrics_by_category`, `get_metric_gauge`, `set_metric_gauge`, `get_system_health` (unchanged), `get_incident_trends` (unchanged).
   - Each tool signature is `(params, user, *, request_meta=None, conversation=None)` — `_call_handler` (agent.py:45) inspects the signature and passes only what's declared.
   - Every tool description:
     - One-sentence purpose.
     - When to call (LLM hint).
     - Which sibling tool to reach for next.
     - Account format reminder (`public | global | group-<id> | user-<id> | custom`).

6. **`set_metric_gauge` handler specifics** (only write tool):
   - `@tool(..., mutates=True, permission="write_metrics", core=False)`.
   - Validate `slug` (non-empty, no control chars), `value` (coerce to string — Redis stores strings), `account` (format-check).
   - `_check_account_write(request, account)` — return error on denial.
   - Call `metrics.set_value(slug, value, account=account)`.
   - `_audit_log(user, "assistant:metric:gauge_set", f"set {slug} on {account}", request=request, conversation=conversation, payload={"slug": slug, "account": account})` — **no value in payload** (same no-values-in-audit rule as `save_model_instance`).
   - Description: prefix with "OPERATIONAL TOGGLE — confirm with the user via an `action` block before calling. Example uses: maintenance_mode, feature flags, rate-limit overrides. Never call without explicit user approval."
   - Return `{"ok": True, "slug": slug, "account": account}`.

7. **`describe_metric_slug` handler specifics**:
   - Roots: `Path(mojo.__file__).parent` and `Path(settings.BASE_DIR)` (via `from mojo.helpers.settings import settings`). De-dupe if nested.
   - Glob each root for `**/*.py`, open, regex `re.compile(r"metrics\.record\([fF]?['\"][^'\"]*" + re.escape(slug_or_prefix) + r"[^'\"]*['\"]")` — match slug as literal or as part of an f-string.
   - Collect up to `DESCRIBE_MAX_HITS=10` hits. Each hit: `{"file": str(path.relative_to(root)), "line": n, "snippet": line.strip()[:200]}`.
   - Empty result: `{"slug": slug, "hits": [], "count": 0, "message": "No metrics.record() call sites found for this slug. May be recorded dynamically or in an external app."}`.
   - No permission check (slug names are not secrets).

8. **`resolve_group_account` handler specifics**:
   - Accept `name_or_id` as int or string.
   - If coercible to int → `Group.objects.filter(pk=int_val).first()`; if not found return `{"error": f"no group with pk={n}"}`.
   - Else → `qs = Group.objects.filter(name__iexact=name_or_id)`. `qs.count()`:
     - 0 → `{"error": f"no group found for '{name_or_id}'"}`.
     - 1 → resolved group.
     - ≥2 → `{"error": "ambiguous group name", "candidates": [{"pk": g.pk, "name": g.name} for g in qs[:10]]}`.
   - Access check on resolved group: `group.user_has_permission(user, ["view_metrics", "metrics"])`. If False → `{"error": f"no access to group-{group.pk}"}`.
   - Return `{"account": f"group-{group.pk}", "group": {"pk": group.pk, "name": group.name}}`.

9. **`list_metric_accounts` handler specifics**:
   - Union: `set(metrics.list_accounts()) | set(metrics.list_accounts_with_data())` (both Redis-scanning; wrap in try/except for Redis-down).
   - Ensure `"public"` and `"global"` are always included.
   - If `user.has_permission(["view_metrics", "metrics"])` → return the full set sorted.
   - Else → intersect with `_resolve_user_accessible_accounts(user)`.
   - Return `{"accounts": sorted([...]), "count": N, "scoped": bool}` where `scoped=True` means the list was filtered to accessible-only.

10. **`fetch_metrics` handler specifics**:
    - Accept `slugs` as list or single string. Reject empty.
    - `_check_account_view(request, account)` — return error on denial.
    - If `granularity` is omitted: `granularity = _auto_granularity(dt_start, dt_end)`.
    - Validate granularity. Parse `dt_start` / `dt_end` via `mojo.helpers.dates.parse`.
    - Call `metrics.fetch(slugs, dt_start, dt_end, granularity=granularity, account=account, with_labels=with_labels, allow_empty=allow_empty)`.
    - Build `retention_note = _retention_note(granularity, dt_start)`.
    - Return `{"data": ..., **_echo_meta(account, granularity, dt_start, dt_end, slug_count=len(slugs)), "retention_note": retention_note}`.

11. **`list_metric_slugs` handler specifics**:
    - `_check_account_view`.
    - If `category` present: `slugs = sorted(metrics.get_category_slugs(category, account))`.
    - Else: `slugs = sorted(metrics.get_account_slugs(account))`.
    - If `prefix`: `slugs = [s for s in slugs if s.startswith(prefix)]`.
    - `limit = min(params.get("limit", DEFAULT_SLUG_LIMIT), MAX_SLUG_LIMIT)`.
    - Return `{"account": account, "category": category, "prefix": prefix, "slugs": slugs[:limit], "count": len(slugs[:limit]), "total": len(slugs), "truncated": len(slugs) > limit}`.

12. **`list_metric_gauges` handler specifics**: same shape as `list_metric_slugs`, calls `metrics.list_gauge_slugs(account, prefix, limit)`.

13. **`fetch_metrics_by_category` handler specifics**:
    - `_check_account_view`.
    - `slugs = sorted(metrics.get_category_slugs(category, account))`.
    - `max_slugs = min(params.get("max_slugs", DEFAULT_CATEGORY_MAX_SLUGS), 200)`.
    - If `len(slugs) > max_slugs`: truncate, set `truncated=True`, keep `total_slugs`.
    - Call `metrics.fetch(slugs[:max_slugs], dt_start, dt_end, granularity, account, with_labels)`.
    - Return enriched response with `_echo_meta` + truncation flags + `retention_note`.

14. **`fetch_metric_values` handler specifics**: wrap `metrics.fetch_values` after `_check_account_view`, return with `_echo_meta`.

15. **`get_metric_gauge` handler specifics**:
    - Accept `slug` (single) or `slugs` (list or comma-string). After `_check_account_view`.
    - For each slug → `metrics.get_value(slug, account=account, default=default)`.
    - Return `{"account": account, "data": {slug: value}}`.

16. **Preserve `get_system_health` and `get_incident_trends`** verbatim at the bottom of the file, permissions unchanged.

17. **`docs/django_developer/assistant/README.md`** — replace the Metrics Domain table (lines 205–212) with the expanded 11-tool table. Link to the new `metrics_tools.md`.

18. **`docs/django_developer/assistant/metrics_tools.md`** (new) — per-tool reference:
    - One section per tool: purpose, parameters table, example LLM prompt that triggers it, example response shape, permission behavior.
    - Top-level "Typical discovery flow" section with the step-by-step tree from plan-mode scenario walkthrough.
    - Cross-link to `docs/django_developer/metrics/{fetching,recording,permissions}.md`.

19. **`docs/web_developer/assistant/README.md`** — if it enumerates tools, mirror the expanded list. If not, skip (no change needed on the frontend side).

20. **`CHANGELOG.md`** — entry under the next unreleased version: "Assistant metrics domain expanded from 3 to 11 tools (discovery, gauge read/write, group resolution, slug explanation). Per-account permissions now enforced on every call. First assistant write tool for gauges: `set_metric_gauge` (operational toggles like maintenance_mode)."

21. **`tests/test_assistant/28_test_metrics_tools.py`** (new) — every test scenario from the Tests Required section above. Setup cleans Redis state (`metrics.delete_account`, `metrics.delete_category`, direct `redis.delete` for `mets:<acct>:val:*`) and test Groups before inserting. Use `th.server_settings` for any Django-setting toggles. Import `from mojo.apps.assistant import get_registry` to reach handlers directly (the fast path used by `7_test_model_tools.py`).

### Design Decisions

- **Delete duplicate discovery tools, don't alias**: `discovery.py`'s `list_metric_categories` / `list_metric_slugs` are registered with `view_admin` and no per-account check. Re-homing in `metrics.py` with `view_metrics` + `check_view_permissions` requires the old registrations be removed — duplicate `register_tool` raises `ValueError`. Names and behavior are preserved for LLM continuity.
- **Signature `(params, user, *, request_meta, conversation)`**: matches the pattern already supported by `agent._call_handler` (agent.py:45). Existing `(params, user)` tools keep working; new tools opt in by declaring the kwargs.
- **`_check_account_view` / `_check_account_write` wrap `PermissionDeniedException` into error dicts**: avoids exception unwinding inside the agent loop. Security events fire at level 5 (matching model VIEW_PERMS denials).
- **Data-inferred account discovery** via `scan_iter("{mets:*}:mets:*:slugs")`: solves the gap where `metrics.list_accounts()` only returns accounts with configured perms. Added in `redis_metrics.py` (not in the assistant layer) so other callers can benefit too.
- **Prefix + limit on slug listing**: prevents dimensional-slug explosions (`login_attempts:ip:*` can be tens of thousands) from blowing token budgets. Default 500, max 2000.
- **`retention_note` is advisory, not blocking**: the fetch still runs; the LLM sees the note and surfaces it in narrative. Using `GRANULARITY_EXPIRES_DAYS` from `mojo/apps/metrics/utils.py:17` keeps the TTL policy in one place.
- **Auto-granularity**: <3h→minutes, <3d→hours, <90d→days, else days. Gives sane defaults when the LLM doesn't pass `granularity` — most natural-language queries ("last week", "this month") don't specify.
- **`set_metric_gauge` ships now, counter correction defers**: the user's "maintenance mode" use case is trivial (set a string value). Counter corrections require ORM reconciliation + delta math and are a separate, later problem.
- **No value in gauge audit payload**: same rule as `save_model_instance` — record field names and account, never the value. A maintenance_mode flag is low-risk, but a generic `set_metric_gauge` could write a sensitive flag value, and the audit log is long-lived.
- **`describe_metric_slug` has no permission check**: slug *names* are not secrets; they're source-code literals. Greps are bounded (10 hits, 200 chars). Values recorded under those slugs are protected separately via account perms.
- **`resolve_group_account` access check uses `view_metrics`/`metrics`**: if the user can't view the group's metrics, they shouldn't even learn its pk through this tool. Prevents info leak to low-privilege users.
- **Describe handler does source scan, not memoized index**: 50 `metrics.record` call sites across a mojo repo scans in well under a second with regex. Building an index would drift.

### Edge Cases

- **Redis down**: every tool wraps `metrics.*` calls that scan Redis in try/except, returning `{"error": "Metrics backend unavailable"}` rather than raising into the agent loop.
- **Unknown account format**: `check_view_permissions` handles this via the custom-account branch (Redis-looked-up perms). If no per-account perms exist, denial is returned. Tool does not try to pre-filter.
- **`scan_iter` over a large Redis**: `list_metric_accounts` and `list_metric_gauges` could scan millions of keys on a huge deployment. Acceptable — discovery tools are called by humans, not in hot paths. If proven slow, a cached account-index can be added later.
- **Empty slug list to `fetch_metrics`**: returns `{"error": "At least one slug is required"}` before hitting Redis.
- **`dt_start > dt_end`**: `metrics.fetch` / `utils.get_date_range` handle this today; don't add extra validation.
- **Very long `dt_start` against fine granularity**: `retention_note` warns; zeros returned. LLM explains.
- **`resolve_group_account` with an integer that matches a Group pk but user has no access**: returns access-denied error, not "group not found" — we've already proven existence via pk lookup, so the honest answer is "you can't see it".
- **`set_metric_gauge` with an empty string value**: allowed (used to "clear" flags like `maintenance_mode=""`). Explicit None/delete is not supported here (stays REST).
- **Custom account whose view_perms are set to `"public"`**: `check_view_permissions` returns success — tool allows the read. Matches REST.
- **A user with global `view_metrics` but no `write_metrics` calls `set_metric_gauge`**: tool-level gate blocks dispatch with a clean permission-denied message.
- **Conversation ID missing on WS path**: audit payload stores `None` for `conversation_id` — fine.
- **Slug with regex-special chars in `describe_metric_slug`** (e.g. `jobs.completed`): `re.escape(slug)` handles.
- **A tool gets called before `load_tools(domain="metrics")`** — the agent's registry gate at `agent.py:597` returns `"Unknown tool"` because the tool isn't in the filtered list. Acceptable; the LLM will call `load_tools` and retry.

### Testing

All scenarios in `tests/test_assistant/28_test_metrics_tools.py` per the "Tests Required" section above. Tests call handlers directly via `get_registry()[tool_name]["handler"]` to bypass the agent loop (same pattern as `7_test_model_tools.py`).

Setup helpers in the test file:
- `_clean_metrics_account(account)` — deletes Redis keys for the account (slug set, categories, time-series keys via `delete_metrics_slug`, gauge `val` keys, perm keys).
- `_clean_test_groups(name_prefix)` — deletes test `Group` rows to avoid pk collisions across reruns.
- `_seed_dimensional_slugs(account, base, dimension, count)` — records `base:<dimension>:<i>` for i in range(count) so truncation tests have enough data.

### Docs

- `docs/django_developer/assistant/README.md` — rewrite Metrics Domain section (lines 205–212).
- `docs/django_developer/assistant/metrics_tools.md` (new) — per-tool reference with discovery flow.
- `docs/django_developer/metrics/fetching.md` / `recording.md` / `permissions.md` — unchanged (cross-linked, not edited).
- `CHANGELOG.md` — one entry covering the full expansion + new write tool.
- `docs/web_developer/assistant/README.md` — mirror the tool table only if it exists and enumerates tools.
- **Chart/visualization rendering**: the tool returns numeric series; formatting for display is the UI's job.
- **Cross-account aggregation**: no tool that sums metrics across multiple accounts at once. The LLM can fetch each account separately and add them up if needed.
- **Rewriting `get_system_health` / `get_incident_trends`** to use the metrics app instead of direct ORM queries. They're convenience aggregates; keeping them unchanged is a deliberate simplification.
- **Per-slug retention/expiry inspection**: no tool to read TTL or `GRANULARITY_EXPIRES_DAYS` per slug. Defer until a user asks for it.
- **Alerting / thresholds**: no "alert if this metric exceeds X" — belongs in a dedicated alerts domain.
