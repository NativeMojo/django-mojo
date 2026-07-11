---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-033
type: bug
title: fileman initiated uploads can't be completed (or FK-attached) by the uploading user
priority: P1
effort: S
owner: backend
opened: 2026-07-10
depends_on: []
related: []
links: []
---

# fileman initiated uploads can't be completed (or FK-attached) by the uploading user

## What & Why
`POST /api/fileman/upload/initiate` only requires auth (`@md.requires_auth()`),
so any member can start an upload — but the documented completion step
(`docs/web_developer/fileman/upload.md` step 3: `POST /api/fileman/file/<id>`
`{"action": "mark_as_completed"}`) runs through the File save path gated by
`File.RestMeta.SAVE_PERMS = ["manage_files", "files"]`
(`mojo/apps/fileman/models/file.py` ~line 250). A plain group member therefore
initiates an upload they can never finalize: 403
`group_member_permission_denied`. The bytes land in storage fine (presigned
PUT), but the File row is stuck `uploading` and renditions never build.

Second symptom of the same gap: the generic FK view-gate in
`on_rest_save_related_field` (`mojo/models/rest.py` ~1450) **silently drops**
any `fileman.File` FK a member assigns (e.g. `note.media`, `avatar`), because
`File.VIEW_PERMS = ["view_fileman", "manage_files", "files"]` and the member
holds none — the save succeeds with the FK missing, only an
`fk_attach_denied` incident records why.

The permission evaluator already supports an `"owner"` token
(`_evaluate_permission`, `mojo/models/rest.py` ~280: `OWNER_FIELD` defaults to
`user`, matches `request.user.id`), and `upload/initiate` stamps
`file.user = request.user` — so the minimal fix is adding `"owner"` to
`File.SAVE_PERMS` and `VIEW_PERMS`.

This bites real consumers today:
- **maestro** works around both (owner-scoped `POST /api/maestro/upload/complete`
  + `BoardItemNote.NO_FK_VIEW_CHECK_FIELDS = ["media"]` with an own-upload
  check) — see maestro `docs/AttentionNeeded.md` 2026-07-10; those workarounds
  retire when this lands.
- **uru-webapp** hit the same 403 and simply skips completion
  (`uru-webapp/src/api/endpoints.ts` ~429: "returns 403 for non-admins —
  skipped"), leaving files permanently `uploading` with no renditions.

## Acceptance Criteria
- [ ] The user who initiated an upload can complete it via the documented
      `mark_as_completed` action without `manage_files`/`files` perms.
- [ ] The uploading user can FK-attach their own completed File (e.g.
      `avatar`, `note.media`) — no silent drop.
- [ ] Users still cannot complete or attach files they don't own (absent
      `manage_files`/`files`/superuser).
- [ ] `docs/web_developer/fileman/upload.md` step 3 documents who may call
      completion.
- [ ] Regression tests: owner completes (200), non-owner completes (403),
      owner FK-attach survives, foreign FK-attach still denied/dropped.

## Repro — bugs only
1. As a plain group member (no `manage_files`): `POST /api/fileman/upload/initiate`
   `{filename, content_type, file_size}` → 200 with `{id, upload_url}`.
2. PUT bytes to `upload_url` → 200 (object lands in storage).
3. `POST /api/fileman/file/<id>` `{"action": "mark_as_completed"}`
- Expected: 200, file `completed`, renditions queued.
- Actual: 403 `{"error": "Permission denied: group_member_permission_denied"}`;
  file stuck `uploading`. (Hit live on maestromojo.com 2026-07-10.)

## Plan

### Goal
Add the `"owner"` permission token to `File.RestMeta` so the user who initiated
an upload can complete it (`mark_as_completed`) and FK-attach it, matching the
framework's documented default pattern and the filevault precedent.

### Context — what exists
- **Initiate** — `mojo/apps/fileman/rest/upload.py:44-84`: `@md.POST('upload/initiate')`
  + `@md.requires_auth()` (auth-only, no model perm gate). Stamps
  `user=request.user` (line 79) and `group=file_manager.group` (line 78 — NOT the
  request group). So any member creates a File they own.
- **File.RestMeta** — `mojo/apps/fileman/models/file.py:251-291`:
  ```python
  CAN_CREATE = True
  CAN_DELETE = True
  VIEW_PERMS = ["view_fileman", "manage_files", "files"]   # line 255
  SAVE_PERMS = ["manage_files", "files"]                   # line 256
  POST_SAVE_ACTIONS = ["action", "regenerate_renditions", "share"]
  ```
  No `DELETE_PERMS`, no `OWNER_FIELD` (defaults to `"user"` — File has a `user`
  FK at file.py:~325, SET_NULL, nullable). No `check_view_permission` hook.
- **Completion path** — `POST /api/fileman/file/<id>` `{"action": "mark_as_completed"}`
  (endpoint `mojo/apps/fileman/rest/fileman.py:11-15`, `@md.uses_model_security(File)`)
  → `on_rest_handle_save` (`mojo/models/rest.py:462`) gates on
  `["SAVE_PERMS", "VIEW_PERMS"]` **before** action dispatch — this is the 403.
  Then `on_action_action` (file.py:596-604) → `mark_as_completed(commit=True)`
  (file.py:713-730), which verifies the bytes landed via
  `self.file_manager.backend.exists(self.storage_file_path)` (file.py:719 — missing
  bytes → `FAILED`, so no phantom completes) and queues renditions
  (`publish_renditions()`, file.py:725, async job on channel `"renditions"`).
- **Owner token semantics** — `_evaluate_permission`, `mojo/models/rest.py:280-285`:
  with an instance, `"owner"` matches `getattr(instance, OWNER_FIELD)` (default
  `"user"`) against `request.user.id`, guarded by `owner is not None`. With no
  instance the token contributes nothing (falls through to group/global perms).
- **List behavior** — `on_rest_handle_list`, `mojo/models/rest.py:499-513`: when the
  bare perm check fails but `"owner"` is in `VIEW_PERMS`, the list is
  **auto-filtered** to `cls.objects.filter(user=request.user)` — a permissionless
  owner sees only their own rows, never everything.
- **FK view-gate** — `on_rest_save_related_field`, `mojo/models/rest.py:1500-1521`
  (dict-branch 1469-1473): evaluates the related model's `VIEW_PERMS` **against the
  related instance**, so the owner token applies to the FK'd File itself. On deny:
  silent drop + `fk_attach_denied` incident (`_report_fk_attach_denied`,
  rest.py:1437-1459). `NO_FK_VIEW_CHECK_FIELDS` is the per-model exemption list.
- **DELETE fallback** — `on_rest_handle_delete` (`mojo/models/rest.py:485`) checks
  `["DELETE_PERMS", "SAVE_PERMS", "VIEW_PERMS"]`; with no `DELETE_PERMS` on File,
  whatever lands in `SAVE_PERMS` also governs delete (`CAN_DELETE = True`).
- **Precedent** — `mojo/apps/filevault/models/file.py:15-17` (fileman's near-clone):
  `VIEW_PERMS = ["view_vault","manage_vault","files","owner"]`,
  `SAVE_PERMS = ["manage_vault","files","owner"]`,
  `DELETE_PERMS = ["manage_vault","owner"]`. Same pattern across ~12 other models
  (totp, pkey, user_api_key, chat room, notification, shortlink, …).
  `docs/django_developer/README.md:52-58` documents `["manage_x", "owner"]` as the
  canonical RestMeta pattern.
- **FK target for the regression test** — `account.User.avatar`
  (`mojo/apps/account/models/user.py:134`) is a `fileman.File` FK; User's RestMeta
  has `"owner"` with `OWNER_FIELD = "self"` (user.py:159-161), so a plain member
  can always save their own user row — ideal for exercising the FK gate.
- **Test infra** — `tests/test_fileman/2_test_fileman.py` covers the full upload
  flow but with a privileged user (`["view_fileman","manage_files"]`, line 34) —
  the plain-member path is untested. `tests/test_fileman/7_test_fm_owner_stamp.py:39-61`
  shows the local-backend setup: `FileManager(backend_type="file", backend_url="file://")`
  + `fm.set_setting("base_path", tempfile.mkdtemp())`; `_write_dummy_file(tmpdir,
  storage_file_path)` (lines 13-18) writes real bytes so `backend.exists()` passes.
  REST-client pattern: `opts.client.login(...)` then `opts.client.post(...)`
  (2_test_fileman.py:196).

### Changes — what to do
1. `mojo/apps/fileman/models/file.py:255-256` — add `"owner"` to both lists and add
   an explicit `DELETE_PERMS`:
   ```python
   VIEW_PERMS = ["view_fileman", "manage_files", "files", "owner"]
   SAVE_PERMS = ["manage_files", "files", "owner"]
   DELETE_PERMS = ["manage_files", "files", "owner"]
   ```
   (This is the entire code fix — the evaluator, list filter, and FK gate all
   already honor the token.)
2. `tests/test_fileman/10_test_owner_upload_complete.py` — new regression test
   (scenarios below).
3. Docs — see Docs section.
4. `CHANGELOG.md` — behavior-change entry.

### Design decisions
- **Use the `"owner"` token, not a custom completion endpoint or per-action
  override** — framework-native, one-line fix, matches the documented canonical
  pattern and filevault; retires the maestro and uru-webapp workarounds.
- **Explicit `DELETE_PERMS` including `"owner"`** — without it, the delete
  fallback (rest.py:485) would grant owner-delete implicitly via SAVE_PERMS
  anyway; making it explicit is discoverable and matches filevault
  (`DELETE_PERMS = ["manage_vault","owner"]`). Uploaders deleting their own file
  is reasonable owner semantics. *(Flagged at scope time; if the user objects,
  set `DELETE_PERMS = ["manage_files", "files"]` instead — the rest of the plan
  is unchanged.)*
- **No in-action ownership check added** — the outer
  `["SAVE_PERMS","VIEW_PERMS"]` gate (rest.py:462) is the enforcement point, and
  `mark_as_completed` already verifies the bytes exist (file.py:719).
- **Group stamping (`file.group` from FileManager vs request group)** — out of
  scope; file as a separate item if wanted (noted in the original request).

### Edge cases & risks
- **Non-owner without perms** — owner branch fails (`file.user != request.user`),
  group/global perms fail → still 403 / FK-drop. Fail-closed preserved; covered
  by tests.
- **`file.user` is None** (user FK is SET_NULL) — evaluator guards
  `owner is not None` (rest.py:284) → denied. Safe.
- **List exposure** — `"owner"` in VIEW_PERMS gives permissionless users a list
  auto-filtered to `user=request.user` (rest.py:508-513), never other users'
  files. Intended; document it.
- **Owner can now edit other File fields** (filename, metadata) via the save
  path — standard owner semantics, same as filevault; `upload_status` transitions
  remain action-driven.
- **Owner-delete** — newly granted via explicit DELETE_PERMS (see decision above).

### Tests
New file `tests/test_fileman/10_test_owner_upload_complete.py` (testit,
`@th.django_unit_test()`, REST client; setup mirrors 2_test_fileman.py +
7_test_fm_owner_stamp.py — local `file://` backend at a tempdir; setup deletes
its users/files before creating them, per testing rules):
- **Owner completes (regression)** — plain member (NO perms): `POST
  /api/fileman/upload/initiate` → write dummy bytes at `storage_file_path` →
  `POST /api/fileman/file/<id>` `{"action": "mark_as_completed"}` → 200,
  `upload_status == "completed"`. (Fails with 403 before the fix.)
- **Non-owner denied** — second permissionless member POSTs `mark_as_completed`
  on the first user's file → 403; status unchanged.
- **Owner FK-attach (regression)** — owner saves own user record with
  `{"avatar": <file_id>}` → avatar set. (Silently dropped before the fix.)
- **Foreign FK-attach still dropped** — second user sets `avatar` to the first
  user's file → avatar remains unset.
- **Owner list scoping** — `GET /api/fileman/file` as permissionless owner →
  only their own rows returned.

### Docs
- `docs/web_developer/fileman/upload.md` — step 3 (lines 73-83): note who may
  complete (the initiating user, or holders of `manage_files`/`files`).
- `docs/django_developer/fileman/file.md:160-172` — RestMeta block currently
  shows stale perms with no `"owner"` and no SAVE_PERMS; sync to the new values.
- `docs/django_developer/core/permissions.md:262-264` (matrix) and `:307`
  (summary) — update the File rows with `"owner"`.
- `CHANGELOG.md` — entry: file uploader (owner) can now view/complete/attach/
  delete their own File.

### Open questions
- none — two decisions were resolved at scope time and flagged for sign-off:
  (a) owner-delete is included via explicit `DELETE_PERMS` (filevault precedent;
  the fallback would grant it implicitly anyway); (b) group-stamping on initiate
  is deferred to a separate item.

## Notes
Maestro-side verification exists and can be ported: the flow is exercised
end-to-end over HTTP in maestro `apps/tests/test_boards/4_test_attachments.py`.

### Build baseline (2026-07-10, before first edit)
Default `--agent` suite (var/test_failures.json): total 2417, passed 2361,
skipped 56, **failed 0**. All-green baseline → any failure after this change is
mine to fix. (Pre-existing uncommitted change in tree: `api_key.py` adds an
`is_superuser` property — unrelated to ITEM-033, left untouched, not staged.)

### Build discovery — the FK premise in ## Plan was wrong (corrected in build)
The plan assumed the generic FK view-gate (`on_rest_save_related_field`) honors
the owner token for `fileman.File` FK attach. **It does not.** Every File FK
attach (avatar, note.media, any model) routes through `File.on_rest_related_save`
(`file.py:817`), which the caller dispatches to at `rest.py:1475` **before** the
scalar-pk VIEW_PERMS gate — and that method's int branch (`file.py:856-859`) did
`File.objects.get(id=...)` + `setattr` with **no permission check at all**.
Empirically proven by the new test pre-fix: owner-attaches-own PASSED (so
acceptance #2 already worked — the "silent drop" premise was false) and
non-owner-attaches-foreign FAILED (a plain member attached another user's File by
id — cross-user/cross-tenant). So the owner token in `VIEW_PERMS` is **inert** on
the File FK-attach path; it only fixes completion (#1) + list scoping.

Owner ruling (build-time, 2026-07-10): **fix the ungated attach in-scope** (not a
separate item). Second change added: `on_rest_save_related_field` now runs the
same VIEW_PERMS gate on the `on_rest_related_save` branch when the value is an
integer pk (an "attach existing"), honoring `NO_FK_VIEW_CHECK_FIELDS`; string /
base64 / data-URL payloads are an inline CREATE the caller owns and skip the gate.
This is what makes acceptance #3 hold, and the File owner token (#1's change) is
exactly what makes the gate PASS for the file's own uploader. Fixes the hole for
every model with a custom `on_rest_related_save` (only File today).

Note: maestro's `BoardItemNote.NO_FK_VIEW_CHECK_FIELDS = ["media"]` workaround was
inert against the *old* code (the exemption is read in the scalar-pk branch, which
File FKs never reached); with this fix it now genuinely exempts `media`, so their
own own-upload check keeps enforcing and nothing breaks. They can retire the
workaround if they want the framework gate instead.

### Post-implementation full suite (2026-07-10)
Default `--agent` suite: total 2422 (+5 new fileman tests), all green. The lone
transient failure — `test_assistant.6_test_docs_tools::404_error_no_url_leak` —
is a flaky external-network test (fetches `raw.githubusercontent.com`); it PASSED
on immediate re-run and touches no code path this change modifies. Zero
regressions vs. baseline.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
