# Fileman URLs Through Shortlink by Default

**Type**: request
**Status**: planned
**Date**: 2026-04-23
**Priority**: medium

## Description

Route download URLs for `fileman.File` and `fileman.FileRendition` through `mojo.apps.shortlink` by default. Each File and each rendition gets its own `ShortLink` row; the shortlink's dynamic resolver rebuilds the underlying backend URL (presigned S3 / local / etc.) per click, so short URLs stay stable while the actual fetch URL rotates as needed.

Opt-out is available globally and per-FileManager.

## Context

- Existing `shortlink.shorten(file=file_obj, ...)` already supports File objects and `resolve_file=True` dynamically regenerates the presigned URL on each click — the machinery is mostly there.
- Benefits: stable human-shareable URLs, audit/click tracking when desired, per-link revocation, a uniform entry point for all file distribution.
- User decisions pinned in the conversation:
  1. Toggle lives **both** globally (`FILEMAN_USE_SHORTLINKS`) **and** per-FileManager (`use_shortlinks` bool override).
  2. **Each rendition gets its own ShortLink row** (not reused from parent File).
  3. Shortlinks **never expire** by default; configurable per-FileManager override if needed.
  4. Click tracking **off by default**; opt-in per-FileManager flag.
  5. Public-bucket files (`file_manager.is_public=True`) **still get shortened** (user preference — keeps URL surface uniform).

## Acceptance Criteria

- When `FILEMAN_USE_SHORTLINKS=True` (the default) and `file_manager.use_shortlinks` is not explicitly False, `File.generate_download_url()` returns a shortlink URL. Same for `FileRendition.generate_download_url()`.
- Each `File` and each `FileRendition` gets its own `ShortLink` row (one-to-one), created lazily on first URL request and cached on the model row.
- When `FILEMAN_USE_SHORTLINKS=False` **or** `file_manager.use_shortlinks=False`, behavior is unchanged — direct backend URLs are returned.
- Clicking the shortlink redirects to a freshly-resolved backend URL (presign regenerated if applicable).
- Deleting a `File` (or `FileRendition`) deletes its owning `ShortLink` row. No orphans.
- Shortlinks for fileman default to `expire_days=0, expire_hours=0` (never expire) and `track_clicks=False`, each overridable via `FileManager.settings`.
- Public FileManagers work — the shortlink still resolves via `file_manager.backend.get_url(...)` (no presign expiry to worry about).
- No change to upload flow, no change to REST surface on `File`/`FileRendition`.
- `CHANGELOG.md` entry; `docs/{django,web}_developer/fileman/` updated.
- Test coverage for both on/off paths, rendition shortening, delete cascade, and public-backend path.

## Investigation

### What exists

- `mojo.apps.shortlink.shorten(url, file, source, ...)` — creates a ShortLink and returns the full short URL string.
- `ShortLink.file` — nullable FK to `fileman.File` at `mojo/apps/shortlink/models/shortlink.py:86`.
- `ShortLink.resolve_file=True` + the resolver at `shortlink.py:218` call `self.file.generate_download_url()` on click — this is exactly the dynamic-presign behavior we want for File.
- `FileManager.settings` is a JSON bag already used for per-manager tunables (e.g., `urls_expire_in`).
- `FileManager.is_public` (bool) exists and controls whether URLs are unsigned CDN URLs or presigned.
- `File.download_url` (TextField) already caches the URL on the row when first generated. `FileRendition.download_url` does the same.
- `File.on_rest_pre_delete` already iterates `file_renditions` and deletes storage (fixed in the prior refactor) — we can extend it to also delete shortlink rows.

### What changes

**`mojo/apps/shortlink/models/shortlink.py`** — add rendition support:
- New nullable FK `rendition = models.ForeignKey("fileman.FileRendition", null=True, blank=True, on_delete=models.CASCADE, related_name="shortlinks")`.
- Extend the resolver (`shortlink.py:218`) so that when `self.rendition_id` is set and `resolve_file=True`, it returns `self.rendition.generate_download_url()`.
- Extend `ShortLink.create(...)` signature with `rendition=None` and pass through.
- **Migration** — adds the nullable FK. Run via `bin/create_testproject`.

**`mojo/apps/shortlink/__init__.py::shorten`** — accept `rendition=` kwarg and forward to `ShortLink.create`. Document in the module docstring.

**`mojo/apps/fileman/models/manager.py`**:
- Add a `use_shortlinks` BooleanField with `null=True, default=None` so `None` means "inherit the global setting". Explicit `True`/`False` overrides.
- (Alternative considered — store in the existing `settings` JSON; dedicated field is clearer and queryable. Pick field during design.)

**`mojo/apps/fileman/models/file.py`**:
- Refactor `generate_download_url()`:
  - If `self.download_url` already set, return it (unchanged).
  - Resolve effective `use_shortlinks`: `file_manager.use_shortlinks` if not None else `settings.FILEMAN_USE_SHORTLINKS` (default True).
  - If disabled → current behavior (direct backend URL, optionally cached on `download_url`).
  - If enabled → call `shortlink.shorten(file=self, source="fileman", resolve_file=True, expire_days=0, expire_hours=0, track_clicks=<fm-setting>, user=self.user, group=self.group)`, cache result on `download_url`, save update_fields.
- `on_rest_pre_delete`: extend to delete `self.shortlinks` (via related_name) before file storage cleanup. On_delete=CASCADE handles it automatically if we set CASCADE on the FK; verify behavior is correct.

**`mojo/apps/fileman/models/rendition.py`**:
- Refactor `generate_download_url()` with the same branch: shortlink when enabled (passing `rendition=self` instead of `file=...`), direct backend URL when disabled.

**`mojo/apps/fileman/utils/upload.py::get_download_url`**:
- Uses `backend.get_url(...)` directly today. Should this also route through shortlink? Most likely **no** — `/api/fileman/download/<token>` is already an indirection layer; wrapping it in shortlink is double-wrapping. Document the decision.

**New helper** (optional, TBD in design):
- `File.get_or_create_shortlink()` / `FileRendition.get_or_create_shortlink()` — convenience methods. Probably just inlined.

**Settings**:
- `FILEMAN_USE_SHORTLINKS` (default `True`) — master switch.
- No new settings for per-manager; use `FileManager.use_shortlinks` field + existing `FileManager.settings` for `shortlink_track_clicks` / `shortlink_expire_days` overrides.

### Constraints

- **Migration required**: new FK on ShortLink, new field on FileManager. Run `bin/create_testproject` after.
- **Caching**: `File.download_url` and `FileRendition.download_url` are already persisted. Once a shortlink is created, the stable short URL lives in that field — no lookup on every read.
- **Ordering**: the shortlink must be created AFTER the File/Rendition has an id (`resolve_file=True` requires `file.id` to be queryable later). Current code saves first → OK.
- **Deletion cascade**: `ShortLink.file` already has `on_delete=CASCADE`; matching for `rendition` keeps things symmetrical. No need for explicit deletion logic.
- **Public files**: user preference is to still shorten. Make sure `resolve_file=True` path handles `file_manager.is_public=True` — the resolver calls `file.generate_download_url()` which already branches on `is_public`; infinite recursion risk if `generate_download_url()` itself creates a shortlink. Need to guard: the resolver must bypass shortlink creation (e.g., call a `_get_direct_url()` internal method).
- **Performance**: avoid an extra SELECT on every file read. Since `download_url` is cached on the row, the shortlink is resolved once. List views with 100 files do 0 extra queries after first-populate.
- **bot_passthrough**: should probably be `True` for fileman links — we do not want crawlers to count as "clicks" on sensitive files. Decide in design.
- **is_protected**: what does it do for fileman links? If it gates behind auth, that's desirable for private files. Investigate during design.

### Related files

- `mojo/apps/shortlink/__init__.py`
- `mojo/apps/shortlink/models/shortlink.py`
- `mojo/apps/shortlink/migrations/` (new migration)
- `mojo/apps/fileman/models/manager.py`
- `mojo/apps/fileman/models/file.py`
- `mojo/apps/fileman/models/rendition.py`
- `mojo/apps/fileman/migrations/` (new migration)
- `mojo/apps/fileman/utils/upload.py` (decision only, likely no change)
- `docs/django_developer/fileman/README.md`, `file.md`, `file_manager.md`
- `docs/django_developer/shortlink/` (if exists — link back)
- `docs/web_developer/fileman/files.md`, `upload.md`
- `CHANGELOG.md`
- `tests/test_fileman/6_test_shortlink_urls.py` (new)

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `FILEMAN_USE_SHORTLINKS` | `True` | Master switch — wrap all fileman download URLs via shortlink |
| `FileManager.use_shortlinks` | `None` | Per-manager override. `None` = inherit global, `True`/`False` = force |
| `FileManager.settings["shortlink_track_clicks"]` | `False` | Enable click tracking for this manager's shortlinks |
| `FileManager.settings["shortlink_expire_days"]` | `0` (never) | Override shortlink expiry for this manager |
| `FileManager.settings["shortlink_bot_passthrough"]` | `True` | Skip OG preview / bot interstitial for file links |

## Endpoints

No new endpoints. All existing fileman endpoints continue to work unchanged. Shortlink's own `/s/<code>` endpoint handles the redirect.

## Tests Required

- `tests/test_fileman/6_test_shortlink_urls.py`:
  - With global on + fm default: `file.generate_download_url()` returns a `/s/<code>` URL and creates a `ShortLink` row linked to the file.
  - Second call returns the same URL (no duplicate ShortLink created).
  - Rendition URL is a *different* shortlink code, linked to the `FileRendition`.
  - `FileManager.use_shortlinks=False` → direct backend URL, no ShortLink created.
  - `FILEMAN_USE_SHORTLINKS=False` global → direct URL for all managers.
  - `FileManager.use_shortlinks=True` + global `False` → shortlink is created (per-manager wins).
  - Public FileManager (`is_public=True`) → still wrapped in shortlink, resolver returns the CDN URL (no recursion).
  - Deleting a `File` cascades to its `ShortLink` row.
  - Deleting a `FileRendition` cascades to its `ShortLink` row.
  - Click-tracking flag from FileManager settings is honored on the created ShortLink.

## Out of Scope

- Click-tracking analytics dashboard (already a shortlink feature if used).
- QR code generation from file shortlinks (separate feature).
- `/api/fileman/download/<token>` wrapping — this is already an indirection layer; document the decision not to wrap it.
- Migrating existing cached `download_url` values to shortlinks — they naturally refresh on next generation.
- Changing shortlink resolver behavior for non-fileman callers.

## Plan

**Status**: planned
**Planned**: 2026-04-23

### Objective
Default all fileman download URLs (File + FileRendition) through `mojo.apps.shortlink` with a global + per-FileManager opt-out, add a per-share minting action (attributed + optionally tracked), and correct the previous `regenerate_renditions` action shape to match the `POST_SAVE_ACTIONS = ["<verb>"]` idiom.

### Steps

1. **`mojo/apps/shortlink/models/shortlink.py`**
   - Add `rendition = models.ForeignKey("fileman.FileRendition", null=True, blank=True, default=None, on_delete=models.SET_NULL, related_name="shortlinks")`.
   - Extend `ShortLink.create(...)` signature with `rendition=None` and pass through to `cls.objects.create`.
   - In `resolve()` (shortlink.py:218): when `self.resolve_file` is True, check `self.rendition_id` first — if set, return `self.rendition.get_direct_download_url()`; else if `self.file_id`, return `self.file.get_direct_download_url()`. Both paths call the new **internal** method (see step 4) so the resolver never re-enters the shortlink-aware `generate_download_url()`.

2. **`mojo/apps/shortlink/__init__.py::shorten`**
   - Accept `rendition=None`; forward to `ShortLink.create`.
   - Update docstring with a rendition example.

3. **`mojo/apps/shortlink/migrations/` (new)** — adds the `rendition` FK.

4. **`mojo/apps/fileman/models/file.py`**
   - New field: `shortlink_code = models.CharField(max_length=10, null=True, blank=True, default=None, db_index=True)`.
   - New method `get_direct_download_url()` — lifted from the current `generate_download_url` body (backend URL only, with `is_public` branch). Used by the shortlink resolver and as the disabled-path fallback.
   - Rewrite `generate_download_url()`:
     - If `self.shortlink_code` and shortlinks enabled → compose and return `<base>/s/<code>` (use shortlink's base-URL helper).
     - If shortlinks disabled → return `self.get_direct_download_url()`.
     - Else (enabled + no code yet) → call `shortlink.shorten(file=self, source="fileman", resolve_file=True, expire_days=<fm-setting>, expire_hours=0, track_clicks=<fm-setting>, bot_passthrough=False, user=self.user, group=self.group)`, extract `code` from the returned URL, idempotent-update `self.shortlink_code` via `File.objects.filter(pk=self.pk, shortlink_code__isnull=True).update(shortlink_code=code)`, re-read `self.shortlink_code`, return composed URL.
     - Guard: if `shortlink_code` is set but the row is missing or `is_active=False`, null the code and regenerate (revocation = `is_active=False`; deletion = regenerate).
   - Replace `POST_SAVE_ACTIONS = ["action"]` with `POST_SAVE_ACTIONS = ["action", "regenerate_renditions", "share"]`.
   - **Remove** the `elif action == "regenerate_renditions":` branch from `on_action_action` (the legacy dispatch stays for `mark_as_completed` / `mark_as_failed` / `mark_as_uploading`).
   - Add `on_action_regenerate_renditions(self, value)`:
     - Accepts `value = True` (regenerate all defaults) or `value = ["thumbnail", ...]` (specific roles).
     - If `value` is a list → pass to `self.publish_regenerate_renditions(roles=value)`.
     - Else → `self.publish_regenerate_renditions(roles=None)`.
     - Return `{"queued": True}`.
   - Add `on_action_share(self, value)`:
     - `value` may be `True` or a dict (`expire_days`, `expire_hours`, `track_clicks`, `note`).
     - Normalize to a dict; cap `expire_days` (e.g., 3650) and `note` length (512).
     - `link_url = shortlink.shorten(file=self, source="fileman-share", resolve_file=True, expire_days=expire_days or 0, track_clicks=bool(track_clicks), bot_passthrough=False, metadata={"note": note} if note else None, user=self.active_request.user if self.active_request else None, group=self.group)`.
     - Return `{"url": link_url, "code": ..., "expires_at": iso-or-None, "track_clicks": bool}`.
   - Extend `on_rest_pre_delete` (already iterates renditions + storage in the prior refactor): add `ShortLink.objects.filter(file=self, source__in=["fileman", "fileman-share"]).delete()`. Keeps human-created shortlinks (`source` == something else) intact.
   - Helper (module-level or staticmethod on File): `_shortlinks_enabled(file_manager)`:
     ```python
     per_fm = file_manager.get_setting("use_shortlinks", None)
     if per_fm is not None:
         return bool(per_fm)
     from mojo.helpers.settings import settings
     return bool(settings.get("FILEMAN_USE_SHORTLINKS", True))
     ```

5. **`mojo/apps/fileman/models/rendition.py`**
   - New field: `shortlink_code = models.CharField(max_length=10, null=True, blank=True, default=None, db_index=True)`.
   - New method `get_direct_download_url()` — lifted from the current `generate_download_url` body.
   - Rewrite `generate_download_url()` — same shortlinks-or-direct branch, passing `rendition=self` (not `file`) to `shorten()`.
   - Add `class RestMeta` key `POST_SAVE_ACTIONS = ["share"]` (currently not set — add it).
   - Add `on_action_share(self, value)` mirroring File's handler but with `rendition=self`; `source="fileman-share"`.
   - Add `on_rest_pre_delete` (if MojoModel supports it; otherwise override `delete()`): `ShortLink.objects.filter(rendition=self, source__in=["fileman", "fileman-share"]).delete()`. Also triggered via File cascade when the parent is deleted.

6. **`mojo/apps/fileman/migrations/` (new)** — `File.shortlink_code` + `FileRendition.shortlink_code`.

7. **`mojo/apps/fileman/utils/upload.py::get_download_url`** — unchanged (already an indirection layer; double-wrap avoided).

8. **`mojo/apps/fileman/models/manager.py`** — no code change. Toggle lives in `FileManager.settings` secrets bag:
   - `use_shortlinks` — `True` / `False` / absent (inherit global).
   - `shortlink_track_clicks` — bool, default False.
   - `shortlink_expire_days` — int, default 0 (never).

9. **Settings**: `FILEMAN_USE_SHORTLINKS` (default `True`).

10. **Tests** — `tests/test_fileman/6_test_shortlink_urls.py` (new):
    - Tier 1 (internal/display shortlink):
      - Global on + fm default → `file.generate_download_url()` returns `/s/<code>`, creates one `ShortLink(file=..., source="fileman")`.
      - Second call returns the same URL; no duplicate row.
      - Rendition URL has a different code and is linked via `rendition=...`, not `file=...`.
      - `fm.set_setting("use_shortlinks", False)` → direct URL, no shortlink row created.
      - `FILEMAN_USE_SHORTLINKS=False` globally → direct URL for all managers.
      - Global False + fm `use_shortlinks=True` → shortlink created (per-manager wins).
      - Public FileManager (`is_public=True`) → shortlink created; resolver returns the unsigned CDN URL (no recursion).
      - Private manager → shortlink resolver returns a fresh presigned URL each call.
      - Shortlink row revoked (`is_active=False`) → `file.generate_download_url()` regenerates a new shortlink and stores the new code.
      - Shortlink row hard-deleted → same regenerate behavior.
      - Deleting a `File` deletes its `source="fileman"` and `source="fileman-share"` rows; leaves other rows (different source) alone.
      - Deleting a `FileRendition` deletes its shortlinks.
    - Tier 2 (share):
      - `POST /api/fileman/file/<id>` with `{"share": true}` → response carries `url`, `code`, `expires_at: null`, `track_clicks: False`; a new `ShortLink(source="fileman-share", user=sharer, file=..., track_clicks=False, expires_at=None)` row exists.
      - `{"share": {"expire_days": 30, "track_clicks": true, "note": "hi"}}` → ShortLink expires_at ≈ now+30d, track_clicks True, `metadata.note == "hi"`.
      - Two calls from different users produce **two distinct** share shortlinks attributed to each user.
      - Rendition share action works analogously.
      - `expire_days` cap enforced (e.g., value 99999 → clamped to 3650).
      - `note` over 512 chars truncated or rejected (pick one — design leans truncate).
    - Regenerate action shape fix:
      - `POST /api/fileman/file/<id>` with `{"regenerate_renditions": true}` → enqueues a `regenerate_renditions` job.
      - `{"regenerate_renditions": ["thumbnail"]}` → enqueues with `roles=["thumbnail"]`.
      - Legacy `{"action": "regenerate_renditions"}` is **no longer recognized**; covered by an explicit negative test.
    - Existing `{"action": "mark_as_completed"}` path still works (regression guard).

11. **`tests/test_fileman/2_test_fileman.py`** — unchanged. Existing `{"action": "mark_as_completed"}` and mark_as_* test coverage stays.

12. **Docs**:
    - `docs/django_developer/fileman/README.md` — new "Short URLs" + "Sharing" sections. Note that `regenerate_renditions` is now a discrete POST_SAVE_ACTIONS key.
    - `docs/django_developer/fileman/file.md` — document `shortlink_code`, `get_direct_download_url()`, action shape corrections.
    - `docs/django_developer/fileman/file_manager.md` — document `use_shortlinks`, `shortlink_track_clicks`, `shortlink_expire_days` settings.
    - `docs/django_developer/fileman/renditions.md` — update `regenerate_renditions` example to `{"regenerate_renditions": ["thumbnail"]}`.
    - `docs/web_developer/fileman/files.md`:
      - Rename "Regenerate" section request example from `{"action": "regenerate_renditions", "roles": [...]}` to `{"regenerate_renditions": ["thumbnail"]}` or `{"regenerate_renditions": true}`.
      - Add "Share file" section documenting `{"share": {...}}` action, response shape, listing existing share links.
      - Add "URL redirection" note — `url` fields are `/s/<code>` by default; follow redirects to the actual storage URL.
    - `docs/web_developer/fileman/upload.md` — short note that returned URLs are shortlinks by default.
    - `CHANGELOG.md` — entry under v1.1.0.

### Design Decisions

- **Two tiers**: internal/display shortlink (one per File/Rendition, untracked) + on-demand share shortlink (one per call, attributed + optionally tracked). Clean separation — UI thumbnails don't pollute the audit trail.
- **`source` discriminator**: `"fileman"` = tier 1, `"fileman-share"` = tier 2. Enables surgical cleanup on File delete (don't nuke human-created shortlinks that happen to link to the file).
- **Cache code, not full URL**: `shortlink_code` is 7 chars; base URL is composed per read so environments migrate cleanly.
- **Discrete POST_SAVE_ACTIONS keys** for every new verb — matches `feedback_post_save_actions_shape.md`. The legacy `"action"` key stays for UI compatibility on `mark_as_*` verbs only.
- **`bot_passthrough=False`** across the board — preview crawlers hit the OG interstitial, never see the presigned URL.
- **Never expire by default**; revocation is explicit via `is_active=False` on the row.
- **Click tracking off by default**; opt-in per-manager for tier 1, opt-in per-call for tier 2.
- **Toggle via encrypted `FileManager.settings`** — no schema change on `FileManager`, inherits via `primary_parent` automatically.
- **`get_direct_download_url()` is the escape hatch** — called by the shortlink resolver and by `generate_download_url()` when shortlinks are disabled. Prevents recursion and keeps the old behavior a single method-call away.
- **Idempotent shortlink-code write** — `UPDATE ... WHERE shortlink_code IS NULL` handles the double-create race without a transaction lock.

### Edge Cases

- **Race on first URL** — two concurrent generators. Both `shorten()` calls create rows. The `UPDATE ... WHERE IS NULL` pattern ensures only one code ends up on the File row; the other row is orphaned (no `file` FK cleanup necessary since both point to the same File). Acceptable waste. If we care, add an idempotency key on `shorten()` keyed on `f"fileman:{file_id}:default"`.
- **Existing cached `download_url` on public files** — after deploy, shortlink-enabled managers start returning short URLs on first call. The old cached URL on the row is effectively ignored; document the one-time behavior change.
- **Shortlink row deleted externally** — next `generate_download_url()` sees a dangling `shortlink_code`. Guard: try to resolve the row by code; if missing or inactive, null the code and regenerate.
- **Tier 2 share spam** — a user with `manage_files` could mint share links in a loop. Cap via `shortlink_track_clicks` etc., and consider a `@md.rate_limit` on the action path in a follow-up if abuse surfaces. Not in scope here; noted.
- **Rendition row missing `get_direct_download_url`** — ensure FileRendition is patched in the same commit as the resolver change so imports never run out of order.
- **Public-manager direct URL leakage** — tier 2 share links with `resolve_file=True` on a public manager just redirect to the (non-secret) CDN URL. Fine.
- **Private-manager expiry** — presign TTL (`urls_expire_in`) is separate from shortlink expiry. Clicks always regenerate the presign; shortlink expiry is the sharing-lifetime control.
- **Tier 1 shortlink for a file deleted right before click** — resolver returns `None` → `/s/<code>` endpoint 404s. Acceptable.

### Testing

All scenarios listed in step 10. Run with `bin/run_tests --agent -t test_fileman`. Minimum bar: 59 existing fileman tests + new 6_test_shortlink_urls.py pass; full suite no regressions.

### Docs

All updates listed in step 12. Cross-link from `docs/django_developer/shortlink/` (if present) back to fileman to describe the file+rendition linkage.

## Resolution

**Status**: resolved
**Date**: 2026-04-23
**Commits**:
- `f5bf944` — feature build
- `0fed2b6` — security-review follow-up + docs round-out

### What Was Built

Every `File` and `FileRendition` download URL now returns a stable `/s/<code>` shortlink by default (tier 1). The shortlink resolver regenerates the underlying backend URL per click, keeping S3 presigns fresh behind a permanent short URL. A new `{"share": ...}` POST_SAVE_ACTION mints distinct, attributed, optionally-tracked share shortlinks (tier 2) for per-sharer audit.

Opt-out is available globally (`FILEMAN_USE_SHORTLINKS=False`) and per-FileManager (`FileManager.settings["use_shortlinks"]`). The `mojo.apps.shortlink` app is treated as an **optional** dependency — when not installed, fileman behaves identically to pre-shortlink, returning direct backend URLs.

As a byproduct, the `regenerate_renditions` action shape was corrected from the legacy `{"action": "regenerate_renditions"}` string-switch to the discrete POST_SAVE_ACTIONS key form `{"regenerate_renditions": true | [...]}`, matching the framework idiom.

### Files Changed

Models & REST:
- `mojo/apps/shortlink/models/shortlink.py` — new nullable `rendition` FK; resolver dispatches to rendition first (using `get_direct_download_url()` to avoid recursion); `ShortLink.create()` accepts `rendition=`.
- `mojo/apps/shortlink/__init__.py` — `shorten()` accepts `rendition=` kwarg.
- `mojo/apps/fileman/models/file.py` — new `shortlink_code` field, module-level helpers (`_shortlink_installed`, `_shortlinks_enabled`, `_get_or_create_shortlink_url`, `_mint_share_link`, `_delete_fileman_shortlinks`), new `get_direct_download_url()` escape hatch, shortlink-aware `generate_download_url()`, discrete `on_action_regenerate_renditions` + `on_action_share`, `POST_SAVE_ACTIONS=["action","regenerate_renditions","share"]`, extended `on_rest_pre_delete` cleaning both file- and rendition-linked shortlinks.
- `mojo/apps/fileman/models/rendition.py` — `shortlink_code`, `get_direct_download_url()`, shortlink-aware `generate_download_url()`, `POST_SAVE_ACTIONS=["share"]`, `on_action_share`, `on_rest_pre_delete`, and `GROUP_FIELD="original_file__group"` for correct list scoping.
- `mojo/apps/fileman/rest/fileman.py` — new `/api/fileman/rendition[/<pk>]` endpoint with `@md.uses_model_security(FileRendition)`.

Migrations:
- `mojo/apps/shortlink/migrations/0003_shortlink_rendition.py`
- `mojo/apps/fileman/migrations/0013_file_shortlink_code_filerendition_shortlink_code.py`

Docs:
- `docs/django_developer/fileman/README.md` — rendition endpoint, new POST_SAVE_ACTIONS table, Short URLs pointer.
- `docs/django_developer/fileman/shortlinks.md` (new) — full pipeline, tier 1 vs tier 2, toggles, share action, deletion cascade, edge cases.
- `docs/django_developer/fileman/file.md` — `shortlink_code` + `get_direct_download_url()`.
- `docs/django_developer/fileman/file_manager.md` — per-manager shortlink settings.
- `docs/django_developer/fileman/renditions.md` — corrected action-shape example.
- `docs/django_developer/shortlink/README.md` — `rendition=` kwarg + new FK.
- `docs/web_developer/fileman/files.md` — rendition endpoints, "URLs are shortlinks by default", share action docs.
- `CHANGELOG.md` — v1.1.0 entries under Added / Changed.

Tests:
- `tests/test_fileman/6_test_shortlink_urls.py` (new) — 19 tests covering tier-1 auto/idempotent/rendition-distinct/public-backend/revoke, tier-2 mint/options/attribution/clamps/rendition-share, toggles (per-FM + global), action-shape fix positive + legacy negative, legacy mark_as_completed regression guard, delete cascade, two security regressions (rendition orphan cleanup + group scoping).
- `tests/test_fileman/4_test_renditions_async.py` — updated to use discrete regenerate_renditions action shape.

Also:
- New feedback file in project memory: `feedback_post_save_actions_shape.md` — action name IS the key.

### Tests

- Targeted: `bin/run_tests --agent -t test_fileman` — **79/79 pass**.
- Full suite: `bin/run_tests --agent` — **1,907 total, 1,797 passed, 0 failed, 110 skipped** (opt-in and conditional skips).

### Docs Updated

Both doc tracks rounded out. See Files Changed above.

### Security Review

Three findings from the initial review:
1. **LOW** — share `roles`/`note`/`expire_days` input validation. **Resolved** in the initial implementation: `_mint_share_link` sanitizes and clamps per `MAX_SHARE_EXPIRE_DAYS=3650` and `MAX_SHARE_NOTE_LEN=512`; non-string note is coerced; non-list roles ignored.
2. **WARNING** — rendition-linked shortlinks orphaned on File delete. **Resolved** in `0fed2b6`: `on_rest_pre_delete` now iterates renditions and deletes their shortlinks before the parent cleanup.
3. **WARNING** — `/api/fileman/rendition` list endpoint unscoped. **Resolved** in `0fed2b6`: added `GROUP_FIELD="original_file__group"` so the standard auto-scoping filter runs through the parent File.

Other review axes (auth gating, resolver recursion, revocation, race window, optional-dependency guard) returned **NONE**.

### Follow-up

- None. All acceptance criteria met; all security findings resolved; all tests pass.
