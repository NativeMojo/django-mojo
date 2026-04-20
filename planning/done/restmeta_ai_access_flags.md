# RestMeta `AI_*` access flags — opt-out per model per verb

**Type**: request
**Status**: planned
**Date**: 2026-04-19
**Priority**: medium

## Description

Add per-model opt-out flags in `RestMeta` that let model authors deny the assistant's model tools (`query_model`, `aggregate_model`, `export_data`, `delete_model_instance`, future `save_model_instance`) on a per-verb basis — even when the calling user has the underlying REST permissions.

Proposed flags (all default `False`, i.e. allowed when REST perms permit):

- `DENY_AI_VIEW` — blocks `query_model`, `aggregate_model`, `export_data`.
- `DENY_AI_CREATE` — blocks `save_model_instance` create path.
- `DENY_AI_UPDATE` — blocks `save_model_instance` update path.
- `DENY_AI_DELETE` — blocks `delete_model_instance`.

Optional shorthand: `DENY_AI = True` denies all four.

This is a **defense-in-depth** layer on top of the existing REST permission chain — REST perms still apply, the AI flag is checked first and short-circuits with a clear "this model is not available to the assistant" error.

## Context

While scoping the assistant `save_model_instance` tool ([assistant_save_model_tool.md](assistant_save_model_tool.md)), the question came up: are there models a `view_admin` user should be able to touch via REST but **not** via the assistant? Likely yes for some — e.g.:

- `account.User` self-modification through the assistant could enable subtle privilege escalation flows that the human admin wouldn't perform deliberately.
- `account.Group` membership changes are sensitive enough to want the human-in-the-loop guarantee that the admin UI gives, even when the assistant has confirmation prompts.
- Append-only audit-style models (`LoginEvent`, `Click`) — already getting `CAN_UPDATE = False` per [can_update_rest_meta_flag.md](can_update_rest_meta_flag.md), but model authors may want to explicitly mark them as "never AI."

`NO_REST = True` already exists as a blanket "exclude from REST entirely" flag, and the assistant tools honor it via `_resolve_model`. The `DENY_AI_*` flags fill the gap between "exposed via REST to humans" and "exposed via REST to the LLM."

This is the right place for the policy: model authors know their models best, and putting the gate in `RestMeta` keeps it next to the existing `CAN_CREATE` / `CAN_DELETE` / `VIEW_PERMS` config rather than maintaining a separate denylist in the assistant package.

## Acceptance Criteria

- Four new RestMeta flags (`DENY_AI_VIEW`, `DENY_AI_CREATE`, `DENY_AI_UPDATE`, `DENY_AI_DELETE`) plus optional `DENY_AI` shorthand. All default `False`.
- A single helper in `mojo/apps/assistant/services/tools/models.py` (e.g. `_check_ai_access(model, verb)`) used by every assistant model tool.
- Each existing tool calls the helper after `_resolve_model` and before the REST permission check:
  - `query_model`, `aggregate_model`, `export_data` → check `DENY_AI_VIEW`.
  - `delete_model_instance` → check `DENY_AI_DELETE`.
  - `save_model_instance` (when added) → check `DENY_AI_CREATE` or `DENY_AI_UPDATE` based on whether `pk` is present.
- Denial returns a clear, non-leaky error: `"{model_label} is not available to the assistant"` — not "permission denied" (which would falsely suggest a perm fix would help).
- Denial is logged as a security event via `_report_security_event(...)` (level 4 — informational, not a real attack, but worth recording).
- Documented in `docs/django_developer/rest/permissions.md` (or wherever RestMeta flags live) and in the assistant tools docs.
- `CHANGELOG.md` entry describing the flags and noting that no existing model behavior changes (defaults preserve current access).

## Investigation

**What exists**:
- `mojo/apps/assistant/services/tools/models.py:_resolve_model` — the natural injection point; already centralizes "is this model addressable by the assistant at all?" via `NO_REST` and `MojoModel` checks.
- `_report_security_event` helper — already used for permission denials.
- RestMeta flag pattern from `CAN_CREATE` / `CAN_DELETE` — `model.get_rest_meta_prop("FLAG_NAME", default)`.

**What changes**:
- `mojo/apps/assistant/services/tools/models.py` — add `_check_ai_access(model, verb, user)` helper; call it from each `_tool_*` function.
- No `mojo/models/rest.py` change — the flags are read by the assistant package only, not enforced at the REST layer (REST already has its own auth).
- Docs.

**Constraints**:
- Defaults must preserve today's behavior — no existing model becomes inaccessible.
- The flags are advisory, not authoritative: the REST perm chain remains the security gate. `DENY_AI_*` is a "this model isn't a good fit for AI access" signal from the model author, not "this user shouldn't have this perm."
- Naming: `DENY_AI_*` follows the convention that flags default to the safe state (`False` here = "not denied"). Alternative `AI_VIEW = True` (allow-list) would require touching every model — rejected.

**Related files**:
- `mojo/apps/assistant/services/tools/models.py`
- `docs/django_developer/rest/permissions.md`

## Tests Required

- Each flag set to `True` blocks the corresponding tool; user with full REST perms still gets denied.
- Each flag unset (default) allows the tool through (subject to REST perms).
- `DENY_AI = True` shorthand blocks all four verbs.
- Denial response message does not leak whether the user has REST perms — same shape regardless.
- Security event recorded on each AI denial.

## Out of Scope

- Adding `DENY_AI_*` to specific existing models — file as separate small requests once the framework is in place. Candidates worth discussing: `account.User`, `account.Group`, `account.UserApiKey`, `account.OAuth`.
- Per-field AI access controls — too granular for this pass; rely on existing `_is_sensitive_field` substring filter and `on_rest_save_field` gates.
- Allow-list mode (`AI_ENABLED = True` per model) — rejected; would force every model author to opt in.
- Centralized denylist outside RestMeta — rejected; keeps policy with the model.

## Plan

**Status**: planned
**Planned**: 2026-04-19

### Objective

Add four per-model `DENY_AI_*` RestMeta flags (plus a `DENY_AI` shorthand) that let model authors block specific assistant verbs independent of REST permissions, enforced by a single helper in the assistant models tool module.

### Steps

1. **`mojo/apps/assistant/services/tools/models.py`** — add `_check_ai_access(model, verb, user, request=None)` helper after `_resolve_model`:
   - Signature: `verb ∈ {"view", "create", "update", "delete"}`.
   - Reads `model.get_rest_meta_prop("DENY_AI", False)` first — if True, deny regardless of verb.
   - Then reads the verb-specific flag: `DENY_AI_VIEW` / `DENY_AI_CREATE` / `DENY_AI_UPDATE` / `DENY_AI_DELETE`.
   - On deny: log via `logger.info` (not warning — this isn't a security incident, it's expected policy), call `_report_security_event("assistant_ai_denied", 4, details, user, model_name=model_label, request=request)`, return `{"error": f"{model_label} is not available to the assistant"}`.
   - On allow: return None.
   - Also export a mapping `_VERB_TO_FLAG = {"view": "DENY_AI_VIEW", ...}` so the helper is driven by a single table.

2. **`mojo/apps/assistant/services/tools/models.py:_tool_describe_model`** — call `_check_ai_access(model, "view", user)` right after `_resolve_model`. Returning schema info for a DENY_AI_VIEW model partly defeats the gate.

3. **`_tool_query_model`** — call `_check_ai_access(model, "view", user, request)` right after `_resolve_model` (line ~404), before the `rest_check_permission` call. Pass the synthetic request for ip propagation into the event.

4. **`_tool_aggregate_model`** — same placement, `verb="view"`.

5. **`_tool_export_data`** — same placement, `verb="view"`.

6. **`_tool_delete_model_instance`** — same placement, `verb="delete"`. Call before `CAN_DELETE` check.

7. **`_tool_save_model_instance`** — same placement, but pick the verb based on `is_create`: `verb = "create" if is_create else "update"`. Call before `CAN_CREATE` / instance lookup so a blocked model fails fast without a DB hit.

8. **`tests/test_assistant/29_test_ai_access_flags.py`** (new) — single file, one module setup that monkey-patches `RestMeta` flags onto existing test-safe models (e.g. `incident.Event`, `assistant.Skill`) and removes them in teardown.

9. **`docs/django_developer/rest/permissions.md`** — new section documenting the four flags + `DENY_AI` shorthand, their defaults, and that they are enforced only by assistant tools, not REST itself.

10. **`docs/django_developer/assistant/README.md`** — Models Domain section gains a note: "Each model tool honors per-model `DENY_AI_*` flags; denied models return `{model} is not available to the assistant`, distinct from permission errors."

11. **`CHANGELOG.md`** — entry noting the flags default False (no behavior change for existing models) and describing the new opt-out capability.

### Design Decisions

- **Helper placement immediately after `_resolve_model`**: fail fast before any permission work or DB lookup. The AI gate is a policy check, not a permission check — it belongs at the outer layer.
- **`describe_model` gated too** (not explicitly in the request but included here): leaking full field/graph metadata for a DENY_AI_VIEW model undermines the opt-out. Trivial to undo if a user wants looser behavior.
- **Verb naming `view/create/update/delete`** instead of tool names: decouples the flag semantic from the specific tool names. Future tools can pick the existing verb.
- **`DENY_AI` as shorthand, not only**: some models (`User`, audit-only rows) benefit from a single flag; others want fine-grained control (e.g. allow view, deny update). Keep both.
- **Error message distinct from permission denied**: `"{model} is not available to the assistant"` — signals to the user that this is policy, not a permission misconfiguration. No "try asking an admin" framing.
- **Level 4 security event, not 5 or 6**: these denials are *expected* when a user's question hits a DENY_AI model. Not an attack, not a probe. Log so operators can see "LLM tried to touch X, got correctly blocked" but don't burn incident budget on it.
- **No REST-layer change**: the flags are advisory to the assistant only. `DENY_AI_DELETE = True` + REST `CAN_DELETE = True` is legal: REST deletes continue to work for humans via the UI; only the LLM is blocked. This is the whole point.
- **Monkey-patch in tests, not test-only models**: adding test-only RestMeta flags via `setattr(Model.RestMeta, "DENY_AI_VIEW", True)` in setUp + `delattr` in teardown avoids a migration. Follows the pattern of other behavior-flag tests in the codebase.
- **No allow-list mode**: rejected in the request. A `DENY_AI_*` omission means "AI access governed by REST perms" — matches how the rest of `RestMeta` works.

### Edge Cases

- **`DENY_AI = True` plus `DENY_AI_VIEW = False`**: the shorthand wins (denies everything). Documented as "`DENY_AI` overrides per-verb flags."
- **Save with pk missing AND `DENY_AI_CREATE = False` but `DENY_AI_UPDATE = True`**: create path proceeds; a subsequent update on the new pk would be blocked in a future call. Expected.
- **Non-MojoModel / NO_REST model reaches `_check_ai_access`**: impossible — `_resolve_model` rejects first. Helper assumes a valid model.
- **Legacy model without `RestMeta`**: `_resolve_model` already returns an error. `_check_ai_access` is never called.
- **Flag value isn't a bool** (e.g. a truthy string): `get_rest_meta_prop` returns the raw value; helper does `if flag:` so any truthy value denies. Acceptable — model authors set `True`/`False`.
- **Security event firehose when a user asks "list all models"**: LLM typically calls `describe_model` per model on demand, not in bulk. If abuse arises, the level-4 event threshold is low enough that dedup/ratelimit rules can handle it downstream.
- **`save_model_instance` action-only call**: `data` may contain only action keys with no field changes. `_check_ai_access` runs before that logic; verb is still "update" when `pk` is set. Correct.

### Testing

All in `tests/test_assistant/29_test_ai_access_flags.py`:

- `test_deny_ai_view_blocks_query_model` — set `DENY_AI_VIEW=True` on a test model; admin with full REST perms gets `{error: "... not available to the assistant"}`.
- `test_deny_ai_view_blocks_describe_model` — same for describe.
- `test_deny_ai_view_blocks_aggregate_model` — same.
- `test_deny_ai_view_blocks_export_data` — same.
- `test_deny_ai_delete_blocks_delete_tool` — set flag; delete returns denial even with CAN_DELETE=True and perms.
- `test_deny_ai_create_blocks_save_create` — save without pk denied when `DENY_AI_CREATE=True`; update with pk still allowed when `DENY_AI_UPDATE=False`.
- `test_deny_ai_update_blocks_save_update` — save with pk denied; create without pk allowed.
- `test_deny_ai_shorthand_blocks_all_verbs` — `DENY_AI=True` blocks view/create/update/delete regardless of verb-specific flags.
- `test_deny_ai_false_allows_normal_access` — default state (flag absent) preserves existing behavior.
- `test_deny_ai_denial_reports_security_event` — a single deny emits exactly one `assistant_ai_denied` event at level 4.
- `test_deny_ai_denial_message_is_distinct` — error text does not contain "Permission denied" (so users don't chase perm fixes).
- `test_deny_ai_denial_fires_before_permission_check` — user without REST perms on a DENY_AI model gets the AI-denial message, not the perm-denied message. Confirms ordering.

### Docs

- `docs/django_developer/rest/permissions.md` — new section "Assistant Access Flags" after CAN_CREATE / CAN_DELETE description.
- `docs/django_developer/assistant/README.md` — Models Domain section: short note + link to the RestMeta doc.
- `CHANGELOG.md` — one entry under `v1.1.0 - (current)` > Added.

## Resolution

**Status**: resolved
**Date**: 2026-04-19
**Commits**: 8a04206 (main implementation) + a96fd4c (security-review fixes)

### What Was Built

Per-model opt-out for the assistant's model tools via four `DENY_AI_*` RestMeta flags plus a `DENY_AI` shorthand. All default `False`, so no existing model changes behavior. Flags are assistant-layer only — REST continues to operate unchanged for human-driven requests.

| Flag | Blocks |
|---|---|
| `DENY_AI_VIEW` | `describe_model`, `query_model`, `aggregate_model`, `export_data` |
| `DENY_AI_CREATE` | `save_model_instance` when `pk` is omitted |
| `DENY_AI_UPDATE` | `save_model_instance` when `pk` is present |
| `DENY_AI_DELETE` | `delete_model_instance` |
| `DENY_AI` | all four (overrides per-verb flags even when explicitly `False`) |

### Files Changed

- `mojo/apps/assistant/services/tools/models.py` — new `_check_ai_access(model, verb, user, request=None)` helper and `_VERB_TO_FLAG` map. Wired into six tool handlers immediately after `_resolve_model` and before any permission check or DB work. Unknown verbs fail closed.
- `tests/test_assistant/29_test_ai_access_flags.py` — new, 17 scenarios.
- `docs/django_developer/rest/permissions.md` — flags added to the properties table plus a new "Assistant Access Flags" section with usage patterns.
- `docs/django_developer/assistant/README.md` — `DENY_AI_*` subsection in Models Domain with cross-link.
- `docs/web_developer/assistant/README.md` — note that `DENY_AI_*` produces a distinct error string so API consumers don't chase a permission fix.
- `CHANGELOG.md` — v1.1.0 Added entry.

### Tests

- `tests/test_assistant/29_test_ai_access_flags.py` — 17 scenarios: helper with no flags / specific flag / shorthand, each verb gate (describe/query/aggregate/export/delete/create/update), shorthand overriding explicit `False`, unknown verb fail-closed, default state allows, security event emitted at level 4, denial message is distinct from "Permission denied", and ordering (AI gate fires before the REST permission check). Tests monkey-patch flags via `setattr`/`delattr` so no migrations are needed.
- Run: `bin/run_tests -t test_assistant.29_test_ai_access_flags`
- Full suite post-commit: 1672 passed, 0 regressions from this change.

### Docs Updated

- `docs/django_developer/rest/permissions.md` — flags added to RestMeta properties table + new "Assistant Access Flags" section.
- `docs/django_developer/assistant/README.md` — Models Domain gained a `DENY_AI_*` subsection.
- `docs/web_developer/assistant/README.md` — Models Domain top note on distinct error string for API consumers.
- `CHANGELOG.md` — v1.1.0 Added entry.

### Security Review

Two actionable findings, both resolved in a96fd4c:

- **WARNING (resolved)** — Dead `return model, None` left over from a copy-paste in `_check_ai_access`. Unreachable today, but would turn every model into a silent AI-deny if the preceding `return` ever moved. Deleted.
- **MEDIUM (resolved)** — Unknown-verb path was fail-open. Now fails closed: an unrecognized verb denies, logs a warning, and emits the same level-4 event. Protects against future handler bugs silently bypassing the gate.
- **LOW (resolved)** — Added the missing "shorthand overrides explicit False" test case plus an unknown-verb test.

All other focus areas passed: gate ordering (verified across all six handlers), error-message distinctness, flag-reading correctness, security event payload (no secrets), and `save_model_instance` verb selection firing before the instance lookup.

### Follow-up

- **Applying `DENY_AI_*` to specific models** — out of scope here per the original AC. Candidates worth separate small requests: `account.User` (update/delete from AI likely unwise), `account.Group` (membership changes), `account.UserApiKey` (credential-adjacent), `account.OAuth`. File individually as operators identify concrete risks.
- **Per-field AI access** — still explicitly out of scope; relies on existing `_is_sensitive_field` substring filter and `on_rest_save_field` gates.
