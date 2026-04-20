# REST auto-owner should respect body-provided user

**Type**: request
**Status**: planned
**Date**: 2026-04-20
**Priority**: medium

## Description

Change the framework's create-time auto-owner behavior so that when the request body already provides a value for the owner field (default `user`), the framework does not overwrite it. Only auto-stamp `request.user` when the owner field is unset. This mirrors how `group` is already handled, removing an asymmetric (and surprising) default.

## Context

Currently in `mojo/models/rest.py:on_rest_save`, every create unconditionally clobbers the owner field with `request.user`:

```python
owner_field = self.get_rest_meta_prop("CREATED_BY_OWNER_FIELD", "user")
if request.user.is_authenticated and self.get_model_field(owner_field):
    setattr(self, owner_field, request.user)   # always overwrites
```

Immediately below, `group` uses the opposite pattern — only set if the body didn't provide one:

```python
if request.group and self.get_model_field("group"):
    if getattr(self, "group", None) is None:
        self.group = request.group
```

This asymmetry:

- Makes "admin creates record for another user" silently broken for any model with a plain `user` FK. Callers pass `{"user": 7, ...}`, the body is discarded, and the row is created against the logged-in user. When there's a uniqueness constraint, Postgres raises and a raw error leaks to the client.
- Forces per-model opt-outs via `CREATED_BY_OWNER_FIELD = None` plus a custom `on_rest_pre_save` that re-reads the body — boilerplate that the framework should handle.
- Surprises developers who reasonably assume "I sent `user` in the body, it will be used."

Surfaced by a consumer project reporting `POST /api/routes/operator` ignoring the `user` field when an admin tries to enrol another group member. The diagnosis traced cleanly to the framework default.

## Acceptance Criteria

- On create, if the body provides a value for the owner field, the framework uses that value instead of auto-stamping `request.user`.
- On create, if the body omits the owner field (or it is null), existing behavior is preserved: auto-stamp `request.user` when authenticated.
- Behavior for `group` is unchanged (already correct).
- Behavior on update is unchanged.
- Existing self-owned models (models with `"owner"` in SAVE_PERMS, or that rely on implicit self-ownership) continue to pass their current tests.
- An opt-in `CREATED_BY_OWNER_STRICT` RestMeta flag is available for models that want the old clobber behavior (defaults to `False`).
- `CHANGELOG.md` documents the behavior change with migration guidance.
- `docs/django_developer/` explains the new semantics, including the opt-in strict flag and the security implications for models that accept a `user` field in the body.

## Investigation

**What exists**:
- `mojo/models/rest.py:1012-1024` — the create-vs-update owner stamping block. `group` already uses the "only if unset" pattern; `user` does not.
- `CREATED_BY_OWNER_FIELD` RestMeta prop (default `"user"`) controls which field is stamped. Setting it to `None` currently opts out entirely because `get_model_field(None)` returns `None` and the guard short-circuits.
- 32 models across `mojo/apps/*` have a `user` FK. Some already opt out explicitly (e.g. `docit/models/{book,asset,page}.py`, `account/models/member.py` — "we do this to protect user"). Most rely on the default.
- `RestMeta` permission model: `"owner"` in SAVE_PERMS auto-scopes writes to the owner. This gates *update/delete* but does not help on *create*, since the row has no owner yet — which is exactly why the framework clobbers today.

**What changes**:
- `mojo/models/rest.py:1012-1017` — change the create branch to only set the owner field when the current value is `None`, unless `CREATED_BY_OWNER_STRICT` is True.
- `mojo/models/rest.py` — add `CREATED_BY_OWNER_STRICT` reading via `get_rest_meta_prop(..., False)`.
- `docs/django_developer/rest/permissions.md` (or the closest existing doc) — document the new default and the opt-in.
- `docs/django_developer/rest/restmeta.md` (or wherever `CREATED_BY_OWNER_FIELD` is currently documented) — add `CREATED_BY_OWNER_STRICT`.
- `CHANGELOG.md` — entry under next version describing the change, why, and the strict opt-in.
- Tests in `tests/test_rest/` (or wherever create-time owner behavior is tested) covering both default and strict paths.

**Constraints**:
- **Security** — any model that previously relied on the clobber as implicit create-time authorization now becomes exploitable if the caller can sneak a `user` field into the body. Before merging, audit the 32 models with `user` FKs and classify:
  - Self-owned (expects `request.user`): confirm SAVE_PERMS + pre-save logic prevents forgery on create. If it doesn't, set `CREATED_BY_OWNER_STRICT = True`.
  - Admin-managed (e.g. Operator-style): benefits from the new default.
- **Backwards compatibility** — this is a behavior change. Consumer apps must be notified via CHANGELOG and release notes. `CREATED_BY_OWNER_STRICT = True` is the explicit escape hatch for any model that depends on the old behavior.
- **No framework type hints** (per `core.md`).

**Related files**:
- `mojo/models/rest.py` (core change)
- `mojo/apps/docit/models/{book,asset,page}.py` (existing opt-out pattern)
- `mojo/apps/account/models/member.py` (existing opt-out pattern)
- `docs/django_developer/rest/*.md`
- `CHANGELOG.md`
- All 32 files listed above under "What exists" for the security audit.

## Endpoints

No new endpoints. Framework-wide behavior change affects every `POST` to a RestMeta endpoint on a model whose owner field has a matching body value.

## Settings

Per-model RestMeta flags (no global settings):

| Flag | Default | Purpose |
|---|---|---|
| `CREATED_BY_OWNER_FIELD` | `"user"` | Existing. Name of the field that gets auto-stamped on create. `None` disables stamping entirely. |
| `CREATED_BY_OWNER_STRICT` | `False` | New. When `True`, auto-stamp always overwrites the body value (old behavior). When `False`, body wins if provided. |

## Tests Required

In a `tests/test_rest/` (or suitable) module, using `testit`:

- **Default (non-strict), body omits `user`**: authenticated POST creates record with `user = request.user`. (Self-signup regression.)
- **Default (non-strict), body provides `user` matching caller**: record is created with the body value (no change observable).
- **Default (non-strict), body provides `user` for another user**: record is created with the body's `user`. (New behavior.)
- **Strict mode, body provides `user` for another user**: record is created with `request.user`, body value ignored. (Old behavior preserved under opt-in.)
- **`CREATED_BY_OWNER_FIELD = None`, body provides `user`**: record is created with body value, no auto-stamp. (Existing opt-out unchanged.)
- **Update path, body provides `user`**: behavior unchanged — no auto-stamp on update (current `UPDATED_BY_OWNER_FIELD` logic still runs for `modified_by`).
- **Group behavior unchanged**: body-provided `group` still wins, omitted `group` still auto-fills from `request.group`.

All asserts include descriptive failure messages per `testing.md`.

## Out of Scope

- Changing `UPDATED_BY_OWNER_FIELD` / update-path owner stamping.
- Changing `group` auto-assignment behavior.
- Changing SAVE_PERMS / VIEW_PERMS semantics.
- Per-model permission policies for "admin creates for another user" (e.g. the Operator-specific `manage_routes` + membership checks in the SERVER-026 report). Those remain the responsibility of the consuming model's `on_rest_pre_save`.
- Auditing and adjusting consumer apps outside django-mojo. Downstream projects must read the CHANGELOG and decide per model whether to opt into `CREATED_BY_OWNER_STRICT`.

## Plan

**Status**: planned
**Planned**: 2026-04-20

### Objective
Make the create-time auto-owner stamp mirror `group`'s "only set when unset" semantics so body-provided `user` values are respected. No new RestMeta flag — `CREATED_BY_OWNER_FIELD = None` remains the escape hatch.

### Steps
1. `mojo/models/rest.py:1014-1017` — In the `if created:` branch, wrap the `setattr` in `if getattr(self, owner_field, None) is None:`. No other change to the block. Update the method docstring to note the new semantics.
2. `docs/django_developer/rest/permissions.md` — Add a short section ("Create-time owner stamping") explaining: (a) the framework auto-stamps `CREATED_BY_OWNER_FIELD` (default `user`) with `request.user` only when the body omits it, (b) body-provided values win, (c) models that want strict self-ownership must set `CREATED_BY_OWNER_FIELD = None` and re-stamp in `on_rest_pre_save`, or validate the incoming `user` via a `set_user`/`on_rest_pre_save` gate, (d) callers who want to forbid admins setting `user` on behalf of others must enforce it per-model.
3. `CHANGELOG.md` — Add entry under `## v1.1.0 - (current)` → `### Changed` describing the behavior change, the motivation (admin-creates-for-user flows were silently clobbered), and migration guidance (opt-out with `CREATED_BY_OWNER_FIELD = None` + manual stamp).
4. `tests/test_models/owner_stamp.py` — New testit module covering all scenarios below.

### Design Decisions
- **Mirror `group` instead of adding a flag (Option B)**. Symmetric, minimal surface. Existing `CREATED_BY_OWNER_FIELD = None` already covers models that need to fully opt out.
- **Only the `if created:` branch changes**. Update-path `UPDATED_BY_OWNER_FIELD = "modified_by"` continues to always overwrite — "last modifier" is an actor fact, not a body fact.
- **Check `getattr(self, owner_field, None) is None`** rather than `owner_field in data_dict`. The loop above has already applied body values via `on_rest_save_related_field`, which coerces `user: null/0/""` to `None`. Using `getattr` correctly handles both "field omitted" and "field explicitly cleared" by auto-stamping in both cases.
- **No opt-in strict flag**. Per user decision. Keeps the surface minimal.

### Edge Cases
- **Body omits `user`** → `self.user is None` → auto-stamp runs → unchanged.
- **Body `user: <self_id>`** → set by loop → non-None → kept. Same value, no behavior observable.
- **Body `user: <other_id>`** → set by loop → non-None → kept. **New behavior.**
- **Body `user: null`/`0`/`""`** → coerced to None in `on_rest_save_related_field:1121` → auto-stamp kicks in. User cannot "create with no owner" by accident.
- **Unauthenticated create** → `request.user.is_authenticated` False → auto-stamp skipped → body value kept if provided, else None. Unchanged.
- **`CREATED_BY_OWNER_FIELD = None`** → `get_model_field(None)` returns None → whole block short-circuits → unchanged (existing opt-out).
- **Custom `CREATED_BY_OWNER_FIELD = "created_by"`** (docit/member pattern) → check runs on `created_by`, not `user` → body's `user` never touched by this block anyway → unchanged.
- **Custom `set_user` setter** → runs in the loop before the owner-stamp block → can still reject or transform → unchanged. New check only fires after.
- **Security**: callers who allow arbitrary users to spoof `user` in the body gain a new attack surface. Mitigation: documentation, CHANGELOG warning, and the existing `CREATED_BY_OWNER_FIELD = None` pattern. The framework cannot enforce per-model "who may create for whom" policies — that remains `on_rest_pre_save` / `SAVE_PERMS` territory.

### Testing
Tests go in `tests/test_models/owner_stamp.py`, using `testit` + `@th.django_unit_test()`. Use an existing simple model with a `user` FK and default `CREATED_BY_OWNER_FIELD` for most cases. Use `docit.Book` (CREATED_BY_OWNER_FIELD = 'created_by') to confirm unchanged behavior. Pick the model during implementation by scanning for one with no custom `on_rest_pre_save` interference and a permissive SAVE_PERMS.

- Body omits `user`, authed POST → record's user == request.user. Regression for self-signup.
- Body `user: <self_id>`, authed POST → record's user == request.user (same id). No-op behavior.
- Body `user: <other_active_user_id>`, authed POST with SAVE_PERMS → record's user == other user. **New behavior.**
- Body `user: null`, authed POST → record's user == request.user. Null coerced, auto-stamp fires.
- Unauthenticated create where permitted (or simulate by sending no auth and confirming the existing behavior) → no auto-stamp.
- Model with `CREATED_BY_OWNER_FIELD = None`, body `user: <id>` → record's user == body value (existing opt-out unchanged).
- Model with `CREATED_BY_OWNER_FIELD = "created_by"`, body provides `user` and omits `created_by` → `created_by == request.user`, `user` == body value. (Existing docit pattern still works.)
- Update path: PUT with body `user: <other>` → field **is** saved by the loop (no owner-stamp on update), `modified_by` set to request.user. This was already true; documented for clarity.

All asserts include descriptive failure messages per `.claude/rules/testing.md`. Every test sets up by deleting any records it creates.

### Docs
- `docs/django_developer/rest/permissions.md` — new "Create-time owner stamping" section explaining the default, how to opt out, and the security implication.
- `CHANGELOG.md` — `### Changed` entry under the current version.
