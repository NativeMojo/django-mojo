# Assistant tool: create and update model instances

**Type**: request
**Status**: planned
**Date**: 2026-04-19
**Priority**: medium

## Description

Add a single `save_model_instance` tool to `mojo/apps/assistant/services/tools/models.py` that lets the LLM create or update any MojoModel instance, gated by the same RestMeta permission chain the REST framework enforces (`CREATE_PERMS` / `SAVE_PERMS` / `VIEW_PERMS`, plus the `CAN_CREATE` flag for creates).

One tool, not two: pass `pk` to update an existing row, omit `pk` to create a new one. The tool description must make this distinction unambiguous to the LLM.

Mirrors the pattern already used by `delete_model_instance`: synthetic request, full permission gating, mutation flag, audit-trail logging, security-event reporting on denial, and "always confirm with user before executing" guidance in the description.

## Context

`mojo/apps/assistant/services/tools/models.py` currently exposes `describe_model`, `query_model`, `aggregate_model`, `export_data`, and `delete_model_instance`. The LLM can read, summarize, export, and delete — but cannot create or modify rows. This blocks a class of admin-style flows (e.g. "create a Skill named X", "set this Conversation's title to Y") where the user would otherwise have to leave the chat and use the admin UI.

The REST framework already enforces a complete permission model for saves via `on_rest_save` / `on_rest_post` / `on_rest_save_field` / `on_rest_save_related_field`. The tool should delegate to that machinery rather than reimplementing it, so the assistant inherits all existing field-level guards, FK-by-pk handling, and post-save hooks for free.

## Acceptance Criteria

- A new `save_model_instance` tool registered in `mojo/apps/assistant/services/tools/models.py` with `mutates=True` and `permission="view_admin"`.
- Single tool handles both create (no `pk`) and update (`pk` present). Tool description states this clearly with examples.
- Permission gating exactly matches the REST layer:
  - Create path: `CAN_CREATE` flag (defaults True) **and** `rest_check_permission(["CREATE_PERMS", "SAVE_PERMS", "VIEW_PERMS"])`.
  - Update path: `rest_check_permission(["SAVE_PERMS", "VIEW_PERMS"], instance)`.
  - On denial: log + `_report_security_event(...)` (level 6, same as delete).
- Saves are executed by calling the model's own `on_rest_save(request, data_dict)` (or `on_rest_post`) so all existing field/FK/related-field/post-save hooks run unchanged.
- FK fields can be set by pk in the data dict — relies on existing `on_rest_save_related_field` handling.
- Sensitive-field guard is **not** applied to input keys (per user: trust `on_rest_save_field`'s own gates).
- Successful save writes a user audit entry via `request.user.log(message, kind)` (the established pattern — see `mojo/apps/account/rest/user_api_key.py:23` and `mojo/apps/account/rest/user.py:699`). Suggested kinds: `"assistant:model:created"` and `"assistant:model:updated"`. Message includes model label and pk. `logger.info` continues to write the debug-log line as well.
- Same pattern applied to `delete_model_instance` for consistency: `kind="assistant:model:deleted"`.
- Errors from the save layer are logged in full server-side; the LLM receives a sanitized message (same pattern as delete).
- Tool description includes the same explicit "confirm with the user before executing" language as `delete_model_instance`.

## Investigation

**What exists**:
- `mojo/models/rest.py:415` — `CAN_CREATE` flag check (defaults True) before create.
- `mojo/models/rest.py:317` — update permission chain (`SAVE_PERMS` > `VIEW_PERMS`).
- `mojo/models/rest.py:418` — create permission chain (`CREATE_PERMS` > `SAVE_PERMS` > `VIEW_PERMS`).
- `mojo/models/rest.py:951` — `on_rest_save(request, data_dict)` is the canonical entry point; handles per-field saves, related FKs, files, and post-save hooks.
- `mojo/models/rest.py:1071` — `on_rest_save_related_field` already resolves FKs by pk and runs `rest_check_permission` on the related model.
- `mojo/apps/assistant/services/tools/models.py:472` — `_tool_delete_model_instance` is the template to follow (resolve_model → CAN_x flag → instance lookup → permission chain → delegate to model's on_rest_* → parse JsonResponse → sanitized error/audit log).
- `_resolve_model`, `_build_request`, `_report_security_event` helpers are already in the file and reusable as-is.

**What changes**:
- `mojo/apps/assistant/services/tools/models.py` — add `_tool_save_model_instance` plus its `@tool(...)` registration. No other files need to change.
- `docs/django_developer/` and `docs/web_developer/` — document the new tool alongside the existing assistant model tools.
- `CHANGELOG.md` — note the new tool.

**Constraints**:
- **No `CAN_SAVE` flag exists today**. `CAN_SAVE` appears in some app RestMetas (filevault, fileman) but is **not gated anywhere in `mojo/models/rest.py`** — it is a phantom flag. Decision needed: (a) leave updates gated only by the perm chain (matches REST today), or (b) introduce real `CAN_SAVE` gating in `rest.py` as a separate change so the assistant tool can honor it. Recommendation: (a) for this request, file (b) as a follow-up if desired — keeps this change scoped to "matches REST framework behavior exactly."
- `CAN_CREATE` defaults to **True** in `rest.py`, so most models are creatable by default. This is consistent with REST behavior and should not be changed here.
- Per user, no input field whitelist — but the user flagged "we might want to restrict some models" as an open question. Suggest: defer model-level restriction to a follow-up; rely on `RestMeta.NO_REST` and the existing `_resolve_model` checks for now.
- Audit trail: `logger.info` is debug-only per project conventions. For real audit, consider also writing to `logit.Log` or `incident.report_event` on every successful mutation. Decision needed (see Open Questions).

**Related files**:
- `mojo/apps/assistant/services/tools/models.py`
- `mojo/models/rest.py`
- `docs/django_developer/assistant/` (if exists) and `docs/web_developer/`

## Tests Required

- Create with sufficient permissions succeeds; row exists with expected field values.
- Create without `CREATE_PERMS` denied; security event reported.
- Create blocked when `CAN_CREATE = False` on the target model.
- Update existing instance with `SAVE_PERMS` succeeds; field changes persist; `on_rest_saved` post-save hook fires.
- Update without `SAVE_PERMS` denied; security event reported.
- Update with non-existent `pk` returns clean "not found" error.
- FK field set by pk on create and on update — value persists; related-model permission check fires.
- Owner-scoped models: user without owner relation cannot update another user's row.
- Sensitive field in data dict (e.g. `password`) is rejected by the underlying `on_rest_save_field`, not by the tool.
- Errors from `on_rest_save` (validation, integrity) are logged in full server-side and returned to the LLM as a sanitized message.

## Open Questions

1. ~~**Audit trail strength**~~ — resolved: every successful mutation calls `request.user.log(message, kind)` for the user audit trail, in addition to the `logger.info` debug line. Also extend `delete_model_instance` to do the same.
2. ~~**Model allowlist/denylist**~~ — filed separately as [restmeta_ai_access_flags.md](restmeta_ai_access_flags.md). Adds `DENY_AI_CREATE` / `DENY_AI_UPDATE` / `DENY_AI_DELETE` / `DENY_AI_VIEW` RestMeta flags. The save tool will honor the create/update flags once that lands.
3. ~~**Phantom `CAN_SAVE` flag**~~ — filed separately as [can_update_rest_meta_flag.md](can_update_rest_meta_flag.md). Once that lands, the assistant tool inherits the gate automatically via `on_rest_save`.

## Plan

**Status**: planned
**Planned**: 2026-04-19

### Objective

Add `save_model_instance` to the assistant model tools, gated by the REST permission chain, executed atomically, and audited per-mutation via `user.log`. Extend the tool dispatcher so handlers can see the originating request and conversation. Retrofit `delete_model_instance` for audit consistency.

### Steps

1. `mojo/apps/assistant/services/agent.py` — extend tool handler contract:
   - `run_assistant(user, message, conversation_id=None, on_event=None, request=None)` accepts the originating Django request.
   - Build `request_meta = objict(ip, user_agent, path, method)` from `request` (or `None` for WS / non-HTTP entry).
   - `_execute_tool(...)` passes `request_meta=request_meta, conversation=conversation` as kwargs to `tool_entry["handler"]`.
   - Handler call site: `tool_entry["handler"](tool_input, user, request_meta=request_meta, conversation=conversation)`.
   - All existing handlers continue to work — they accept `(params, user)` positionally; the new kwargs are silently dropped via `**_` in handlers that don't care, OR by inspecting handler signature with `inspect.signature` and passing only what the handler accepts. Pick the inspect-signature approach so existing handlers need zero changes.

2. `mojo/apps/assistant/rest/assistant.py:32` — pass `request=request` through to `run_assistant(...)`.

3. `mojo/apps/assistant/services/tools/models.py` — extend `_build_request` to optionally consume `request_meta`:
   - New signature: `_build_request(user, filters=None, method="GET", path="/assistant/query_model", request_meta=None)`.
   - When `request_meta` is provided, copy `ip`, `META["HTTP_USER_AGENT"]`, etc. into the synthetic request so downstream `incident.report_event(request=req)` calls record the real source IP.
   - Default fallback (`ip="assistant"`) preserved when `request_meta` is None.

4. `mojo/apps/assistant/services/tools/models.py` — add `_audit_user_log(user, kind, message, conversation, model_label, pk, fields=None)` helper:
   - Calls `user.log(message, kind, model_name=model_label, model_id=pk, conversation_id=conversation.pk if conversation else None, fields=fields)`.
   - Single place to format audit messages and metadata.

5. `mojo/apps/assistant/services/tools/models.py` — add `_tool_save_model_instance(params, user, *, request_meta=None, conversation=None)`:
   - Required params: `app_name`, `model_name`, `data` (dict). Optional: `pk` (int).
   - Resolve model via `_resolve_model`. Return error if not found / `NO_REST` / non-MojoModel.
   - Build synthetic request via `_build_request(user, filters=data, method="POST" if pk is None else "PUT", path=..., request_meta=request_meta)`.
   - **Create path** (no pk):
     - Check `model.get_rest_meta_prop("CAN_CREATE", True)` — return error if False.
     - `rest_check_permission(request, ["CREATE_PERMS", "SAVE_PERMS", "VIEW_PERMS"])` — on failure, log + `_report_security_event` at level 6, return sanitized error.
     - Instantiate `instance = model()`.
   - **Update path** (pk provided):
     - `instance = model.objects.filter(pk=pk).first()` — return clean "not found" if None.
     - `rest_check_permission(request, ["SAVE_PERMS", "VIEW_PERMS"], instance)` — on failure, log + `_report_security_event` level 6, sanitized error.
   - Capture changed-field names: `list(data.keys())` minus `NO_SAVE_FIELDS`. Used for audit only.
   - **Atomic execution**: `with transaction.atomic(): instance.on_rest_save(request, data)`. All-or-nothing; partial failures roll back.
   - On success: `_audit_user_log(user, "assistant:model:created" if pk is None else "assistant:model:updated", message, conversation, model_label, instance.pk, fields=changed_field_names)`.
   - On exception during save: log full server-side via `logger.exception`, `_audit_user_log(user, "assistant:model:save_failed", ..., fields=...)`, return sanitized `{"error": "Save failed for {model_label}"}`. No incident event — failed validation isn't a security event.
   - Return `{"ok": True, "model": model_label, "pk": instance.pk, "created": pk is None}`.
   - Register with `@tool(name="save_model_instance", domain="models", permission="view_admin", core=False, mutates=True, ...)` — clear description differentiating create (no pk) from update (pk present), with examples for each, and the same "always confirm with user before executing" language as `delete_model_instance`.

6. `mojo/apps/assistant/services/tools/models.py` — retrofit `_tool_delete_model_instance`:
   - Add `*, request_meta=None, conversation=None` to signature.
   - Use `request_meta` in `_build_request`.
   - On successful delete, call `_audit_user_log(user, "assistant:model:deleted", ..., model_label, pk)`.
   - No other behavior changes.

7. `docs/django_developer/assistant/tools.md` (or wherever assistant tools are documented) — add `save_model_instance` section, document the `request_meta` / `conversation` kwargs handlers can opt into.

8. `docs/web_developer/` — note the new tool exists alongside delete; flag the audit/incident behavior so consumers know mutations are tracked.

9. `CHANGELOG.md` — add entry: new `save_model_instance` tool; tool dispatch now threads originating request and conversation; `delete_model_instance` mutations now audited via `user.log`.

### Design Decisions

- **Single tool, not two**: pk-presence dispatches create vs update. Mirrors REST's `on_rest_save`. Description must make this unambiguous to the LLM.
- **Atomic wrapper**: `on_rest_save` is NOT atomic today (`rest.py:951` loops field saves with no transaction; `atomic_save()` at `rest.py:1233` is just a commit). Wrapping in `transaction.atomic()` at the tool boundary is the minimal correct fix without changing core REST behavior. A separate request can address the broader REST atomicity gap if desired.
- **Inspect-signature for handler dispatch**: lets existing handlers keep `(params, user)` signature without `**_`. New tools opt into the kwargs they want. Zero changes to other tool files.
- **Slim `request_meta` objict, not Django request**: handlers stay decoupled from HTTP. Only the bits `incident.report_event` needs.
- **Audit via `user.log` only (option (i))**: agent-level generic mutation event already fires at `agent.py:606`. Adding a model-specific incident event would duplicate. `user.log` (writes to `logit.Log`) provides the forensic detail; the existing agent event covers rule-engine / realtime needs.
- **Field NAMES in audit, never values**: prevents PII / secret leakage into long-lived audit table.
- **`save_failed` is audit, not incident**: validation errors are user mistakes, not security signals. They go to `user.log` with kind `assistant:model:save_failed`, error class name only.
- **`core=False` for the save tool**: it's destructive and shouldn't auto-load. Users opt into `models` domain to get it (matches `delete_model_instance` pattern of being non-core).

### Edge Cases

- **Atomic rollback aborts the audit**: if we record audit *inside* the `transaction.atomic()` block, a rollback would also wipe the audit. Solution: capture intent before the save, write the audit *after* the `with` block exits successfully. On exception, write `save_failed` audit outside the atomic block.
- **Related-field permission denial mid-save**: `on_rest_save_related_field` (`rest.py:1071`) checks `rest_check_permission` on the related model and may raise. Atomic wrapper rolls back the parent save; tool catches and returns sanitized error.
- **`actions_only` request with no real fields**: `on_rest_save` handles this via post-save actions. If `data` only contains action keys and no pk, the create path still triggers (since `created or not actions_only`). Tool should accept this — useful for triggering actions on new instances.
- **`POST_SAVE_ACTIONS` returning custom response**: `on_rest_save` returns `action_resp` (a JsonResponse). Tool should detect this and either inline the JSON body into the result or just report `{"ok": True, "action_response": "..."}`. Pick inline.
- **Sensitive field in `data`**: per acceptance criteria, no input whitelist — trust `on_rest_save_field`'s gates. But the audit's `fields=[...]` list could leak the *name* "password" if the LLM sends one. That's acceptable — names are not values, and recording attempts to set sensitive fields is a feature, not a leak.
- **`_resolve_model` race**: model could be uninstalled between resolve and save. `apps.get_model` throws `LookupError` cleanly; tool returns clean error.
- **Conversation None**: WS / non-HTTP path. Audit metadata records `conversation_id=None` — fine.
- **Existing `delete_model_instance` tests must not break**: signature change is additive (kwargs with defaults). Existing callers untouched.
- **`DENY_AI_*` flags from sister request not yet landed**: tool ships without that gate. When [restmeta_ai_access_flags.md](restmeta_ai_access_flags.md) lands, helper added there is wired in. Save tool checks `DENY_AI_CREATE` for creates, `DENY_AI_UPDATE` for updates.
- **`CAN_UPDATE` flag from sister request not yet landed**: tool ships honoring only the perm chain for updates. When [can_update_rest_meta_flag.md](can_update_rest_meta_flag.md) lands, the gate is enforced inside `on_rest_save` itself — tool inherits for free, no change.

### Testing

All in `tests/test_assistant/test_tools_save_model.py` (new file):

- `test_create_with_perms_succeeds` — row exists, audit log entry written, agent-level mutation event fires.
- `test_create_blocked_by_can_create_false` — `CAN_CREATE = False` returns clean error, no row created, no audit.
- `test_create_without_create_perms_denied` — security event at level 6, sanitized error.
- `test_update_with_save_perms_succeeds` — fields persist, `on_rest_saved` post-save hook fired, audit written.
- `test_update_without_save_perms_denied` — security event level 6, no mutation.
- `test_update_pk_not_found` — clean "not found" error, no audit.
- `test_fk_set_by_pk` — create + update both resolve FK from pk, related-model permission check fires.
- `test_owner_scoped_update_blocked_for_non_owner` — user without owner relation cannot update other user's row.
- `test_atomic_rollback_on_partial_failure` — mid-save exception (e.g. invalid value on field 3 of 5) rolls back fields 1–2 and any related instances; audit records `save_failed`, not success.
- `test_audit_records_field_names_not_values` — update with email change → audit log message contains `fields=[email]`, no value substring.
- `test_failed_validation_writes_save_failed_audit` — bad data → `assistant:model:save_failed` user.log entry, no incident event, sanitized error to LLM.
- `test_request_meta_threads_real_ip` — when `request_meta.ip="1.2.3.4"` provided, `incident.report_event` called on permission denial records `1.2.3.4` (not `"assistant"`).
- `test_conversation_id_in_audit_metadata` — audit log entry has `conversation_id` set when conversation provided.

Plus `tests/test_assistant/test_tools_delete_model.py` additions:
- `test_delete_writes_audit_log` — successful delete now writes `assistant:model:deleted` user.log entry.
- `test_delete_request_meta_threads_ip` — same IP threading verification.

Plus `tests/test_assistant/test_agent_dispatch.py` additions:
- `test_handler_receives_request_meta_when_accepted` — handler with `request_meta` kwarg gets it; handler without it is unaffected.
- `test_handler_receives_conversation_when_accepted` — same for `conversation`.
- `test_existing_handler_signature_unchanged` — `(params, user)` handlers continue to work.

### Docs

- `docs/django_developer/assistant/tools.md` (or similar) — new `save_model_instance` section; document handler kwargs.
- `docs/django_developer/assistant/audit.md` (new or existing) — describe the two-tier audit (agent-level event + per-mutation `user.log`), kinds, what's included/excluded.
- `docs/web_developer/` — note the new tool, document mutation auditing for API consumers.
- `CHANGELOG.md` — entry per Step 9.

## Out of Scope

- Bulk create/update (one instance per call only).
- Adding a `CAN_SAVE` gate in `mojo/models/rest.py` (separate request if desired).
- Model-level allowlist / denylist for the assistant (open question, defer).
- File uploads via the assistant — `on_rest_save_files` requires multipart handling that doesn't fit the tool-call shape.
- Changes to `delete_model_instance`, `query_model`, `describe_model`, `aggregate_model`, or `export_data`.
