# Fileman Short URLs

Fileman integrates with `mojo.apps.shortlink` to wrap every File and FileRendition download URL in a stable `/s/<code>` short URL. The shortlink resolver rebuilds the underlying backend URL (S3 presign, public CDN, local file, etc.) per click, so short URLs stay stable while actual fetch URLs rotate as needed.

## Two tiers

### Tier 1 — internal/display shortlink
- **Automatic**, one per File, one per rendition.
- Created lazily on first `generate_download_url()` call.
- Cached in the `shortlink_code` column on the row.
- Used for everything the UI renders: `<img src>`, thumbnail URLs, admin download buttons, API `url` fields.
- `source="fileman"`.
- Not tracked, not attributed — creating 100 thumbnails in a list view should not create 100 audit rows.

### Tier 2 — share shortlink
- **Explicit**, minted via the `{"share": ...}` POST_SAVE_ACTION on File or FileRendition.
- Each call creates a **new** ShortLink row, attributed to the sharer (`request.user`).
- `source="fileman-share"`.
- Optionally click-tracked (per call), optionally expiring.
- Use cases: sending a file to an external user, embedding in an email, attribution audit ("whose link got used"), temporary access.

## Enabling / disabling

```python
# Global default (settings.py)
FILEMAN_USE_SHORTLINKS = True  # default

# Per-FileManager override (stored in FileManager.settings secrets bag)
fm.set_setting("use_shortlinks", True)   # force on
fm.set_setting("use_shortlinks", False)  # force off
fm.set_setting("use_shortlinks", None)   # inherit global (default)
```

Precedence: per-FileManager (if set) > global. **If the `mojo.apps.shortlink` app is not installed, shortlinks are always off.** Fileman falls back to direct backend URLs — behavior is identical to pre-shortlink.

## Per-FileManager tuning

All optional, stored in the FileManager settings bag:

| Key | Default | Purpose |
|---|---|---|
| `use_shortlinks` | `None` (inherit) | Force-on/off shortlink wrapping for this manager |
| `shortlink_track_clicks` | `False` | Tier-1 shortlinks log clicks when True |
| `shortlink_expire_days` | `0` (never) | Tier-1 shortlink lifetime in days |

Tier-2 shares override expiry and track_clicks per call; they do not consult these settings.

## The share action

```json
POST /api/fileman/file/123
{"share": true}

POST /api/fileman/file/123
{"share": {"expire_days": 30, "track_clicks": true, "note": "for the Q3 review"}}

POST /api/fileman/rendition/456
{"share": true}
```

Response:

```json
{
  "url": "https://app.example.com/s/Xk9mR2p",
  "shortlink_code": "Xk9mR2p",
  "expires_at": "2026-05-23T18:00:00+00:00",
  "track_clicks": true,
  "code": 200,
  "server": "host1"
}
```

Permissions: the share action is a POST_SAVE_ACTION, so it is gated by the instance's `SAVE_PERMS` like any other save. For `File` that's `manage_files`, `files`, or `owner` — the file's own uploader may mint a share link for their own file without `manage_files`/`files`. `FileRendition` has no owner token, so sharing a rendition requires `manage_files`/`files`.

### Clamps
- `expire_days` is clamped to `File.MAX_SHARE_EXPIRE_DAYS = 3650`. Values above that are silently capped.
- `note` is truncated to `File.MAX_SHARE_NOTE_LEN = 512` characters and stored in `ShortLink.metadata["note"]`.
- Non-string `note` values are coerced to `str()`.

### Listing a file's share links

Use the existing ShortLink list endpoint filtered by `source` and `file`:

```
GET /api/shortlink/shortlink?source=fileman-share&file=123
```

Each row carries `user` (sharer), `hit_count`, `expires_at`, `track_clicks`, and `metadata.note`. Per-click detail lives in `ShortLinkClick`.

## Internal API

```python
# Called by generate_download_url when shortlinks are disabled or to resolve
# a click — always returns the raw backend URL, never a short URL.
file.get_direct_download_url()
rendition.get_direct_download_url()
```

The shortlink resolver (`mojo.apps.shortlink.models.ShortLink.resolve`) calls `get_direct_download_url()` — never `generate_download_url()` — to avoid infinite recursion.

## Deletion cascade

`File.on_rest_pre_delete` deletes rows where `source__in=["fileman", "fileman-share"]` for the file and its renditions. Human-created shortlinks pointing at the file (other `source` values) are preserved — their `file` / `rendition` FK goes NULL via `SET_NULL`.

Revocation is orthogonal: set `is_active=False` on a ShortLink to revoke it. The fileman resolver detects the inactive/missing row on next read and regenerates a fresh tier-1 link. For share links, regeneration means minting a new row via the `share` action — the revoked URL stays dead.

## Edge cases

| Scenario | Behavior |
|---|---|
| Shortlink app not installed | `_shortlinks_enabled()` returns False. All URLs are direct. Share action returns `{"status": False, "error": "shortlink app is not installed"}`. |
| Tier-1 shortlink deleted externally | `generate_download_url()` detects the dangling code, nulls it, and mints a fresh one. |
| Tier-1 shortlink `is_active=False` | Same as above — treated as revoked, regenerated. |
| Public FileManager (`is_public=True`) | Shortlink is still created (for URL-surface uniformity). Resolver returns the unsigned CDN URL — no presign, no recursion. |
| Concurrent first-read race | Both concurrent readers call `shorten()`; the `UPDATE ... WHERE shortlink_code IS NULL` idempotent write ensures only one code ends up cached on the row. The orphan row is harmless. |
| File deleted with active share link | Auto-generated `source="fileman-share"` rows are deleted alongside `source="fileman"`. Non-fileman sources survive. |

## Adding to the web_developer docs

Web-consumer-facing documentation for the short-URL behavior and the `share` action lives in `docs/web_developer/fileman/files.md` and `docs/web_developer/fileman/sharing.md`.
