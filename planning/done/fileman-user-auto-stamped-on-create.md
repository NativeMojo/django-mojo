# FileManager `user` is auto-stamped to the caller on REST create

**Type**: issue
**Status**: resolved
**Date**: 2026-05-21
**Severity**: medium

## Description
When a `FileManager` is created through the REST flow without a `user` field in
the request body, the framework silently sets `user` to the requesting user.
The expectation is that an omitted `user` stays `None`, allowing group-scoped
and system-scoped file managers to be created via REST.

The same clobbering also happens when the body *explicitly* sends `user: null`
or `user: 0` — those are coerced to `None` and then overwritten with the caller.

## Context
`FileManager` supports three ownership scopes:
- user-owned (`user` set, `group` null)
- group-owned (`user` null, `group` set)
- system default (`user` null, `group` null)

`FileManager.get_for_group()` and `get_for_user()` rely on `user=None` to find
group/system managers (e.g. `manager.py:709`, `manager.py:666`). When the REST
create path stamps `user`, a manager that was meant to be group- or
system-scoped becomes user-scoped and is no longer discoverable by those
lookups — leading to duplicate managers and "default manager not found"
behavior. There is no REST-only workaround today; the caller cannot create a
non-user-owned manager.

## Acceptance Criteria
- Creating a `FileManager` via REST with no `user` in the body leaves `user = None`.
- Creating a `FileManager` via REST with `user: null` / `user: 0` leaves `user = None`.
- Creating a `FileManager` via REST with an explicit `user: <id>` still sets that user.
- Group auto-stamping (`group` from `request.group`) is unaffected.

## Investigation
**Likely root cause**: The generic REST save path auto-stamps the owner field
on create. In `MojoModel.on_rest_save` ([mojo/models/rest.py:1237](mojo/models/rest.py:1237)):

```python
if created:
    owner_field = self.get_rest_meta_prop("CREATED_BY_OWNER_FIELD", "user")
    if request.user.is_authenticated and self.get_model_field(owner_field):
        if getattr(self, owner_field, None) is None:
            setattr(self, owner_field, request.user)
```

`CREATED_BY_OWNER_FIELD` defaults to `"user"`. `FileManager.RestMeta`
([mojo/apps/fileman/models/manager.py:14](mojo/apps/fileman/models/manager.py:14))
does **not** define `CREATED_BY_OWNER_FIELD`, so the default applies and the
`user` field is auto-stamped whenever the body does not provide a non-null
value.

This is intentional framework behavior for self-signup models (see
[tests/test_models/owner_stamp.py:84](tests/test_models/owner_stamp.py:84)), but
it is wrong for `FileManager`, which legitimately needs `user=None` managers.

The documented opt-out is `CREATED_BY_OWNER_FIELD = None` in `RestMeta`
(see the comment at [mojo/models/rest.py:1241](mojo/models/rest.py:1241) and the
covering test `test_owner_field_none_skips_auto_stamp` at
[tests/test_models/owner_stamp.py:154](tests/test_models/owner_stamp.py:154)).
Precedent: `account/member.py:48`, `docit/models/{asset,book,page}.py` set a
non-default `CREATED_BY_OWNER_FIELD`.

Note the explicit-null edge case: the body value is processed first by
`on_rest_save_related_field` ([mojo/models/rest.py:1372](mojo/models/rest.py:1372)),
which coerces `null`/`0` to `None`. The later auto-stamp check then cannot tell
"body said None" from "body said nothing", so an explicit `user: null` is also
clobbered. Setting `CREATED_BY_OWNER_FIELD = None` fixes both cases at once.

**Confidence**: confirmed
**Code path**:
- [mojo/models/rest.py:1235-1248](mojo/models/rest.py:1235) — owner auto-stamp on create
- [mojo/apps/fileman/models/manager.py:14-46](mojo/apps/fileman/models/manager.py:14) — `FileManager.RestMeta` (no `CREATED_BY_OWNER_FIELD`)
- [mojo/apps/fileman/models/manager.py:76-84](mojo/apps/fileman/models/manager.py:76) — `user` FK (nullable, default None)

**Regression test**: not written this session — feasible. Model it on
`test_owner_field_none_skips_auto_stamp` in
[tests/test_models/owner_stamp.py:154](tests/test_models/owner_stamp.py:154),
but target `FileManager` directly (e.g. a `file`-backend manager) so the test
verifies the opt-out is wired on the real model, not toggled at runtime. Watch
out for `on_rest_saved` side effects — it calls `backend.make_path_public()`
([manager.py:357](mojo/apps/fileman/models/manager.py:357)) — so use the `file`
backend or a manager config that does not require live S3.

**Related files**:
- `mojo/apps/fileman/models/manager.py` — likely the only change: add `CREATED_BY_OWNER_FIELD = None` to `RestMeta`
- `tests/test_fileman/` — new regression test
- `docs/django_developer/*` and `docs/web_developer/*` — note that REST-created file managers are not auto-owned

## Resolution

**Status**: resolved
**Date**: 2026-05-21

### What Was Built
`FileManager.RestMeta` now sets `CREATED_BY_OWNER_FIELD = None`, disabling the
generic REST create-time owner auto-stamp. A create request that omits `user`
(or sends `user: null`) now leaves `user = None` — group-scoped managers can be
created via REST. An explicit `user` in the body is still honored; `group`
auto-fill from `request.group` is unchanged.

A security review of the change found that the opt-out also made it possible for
any caller with system-level `manage_files`/`files` to create a *system-scoped*
manager (`user` and `group` both unset) — which can become the system default
that `get_for_user`/`get_for_group` derive every manager from. To close that
gap, `FileManager.on_rest_pre_save` now raises `PermissionDeniedException` (403)
when a non-superuser creates a system-scoped manager via REST. Direct ORM
creation (bootstrap, `get_for_*` provisioning) does not go through
`on_rest_pre_save` and is unaffected.

### Files Changed
- `mojo/apps/fileman/models/manager.py` — `RestMeta.CREATED_BY_OWNER_FIELD = None`; `on_rest_pre_save` superuser guard for system-scoped creation; `from mojo import errors as me` import
- `tests/test_fileman/7_test_fm_owner_stamp.py` — new regression test (5 tests)
- `CHANGELOG.md` — fileman entry under current section
- `docs/django_developer/fileman/file_manager.md` — Ownership and Scoping section + system-scope guard
- `docs/web_developer/fileman/manager.md` — new endpoint reference (created by docs-updater) + system-scope note
- `docs/web_developer/fileman/README.md` — index entry for `manager.md`

### Tests
- `tests/test_fileman/7_test_fm_owner_stamp.py` — group manager omits `user` → `None`; `user: null` → `None`; explicit `user` honored; system-scope create blocked for non-superuser (403); system-scope create allowed for superuser
- Run: `bin/run_tests -t test_fileman.7_test_fm_owner_stamp` (full `test_fileman`: 84/84 pass; `test_models`: 58/58 pass)

### Docs Updated
- `docs/django_developer/fileman/file_manager.md` — ownership scopes + superuser-only system-scope guard
- `docs/web_developer/fileman/manager.md` (new) + `README.md` index — `user` field behavior + system-scope 403 note

### Security Review
Initial review flagged the system-scoped creation gap (a `manage_files` user
could create/replace the system default). Resolved by the superuser-only guard
in `on_rest_pre_save`. No other concerns.

### Follow-up
- None. (Pre-existing: `CHANGELOG.md` top section is mislabeled `v1.2.21` while `v1.2.22` is already released — left for the maintainer's release process.)
