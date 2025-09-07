# File Manager: S3 Direct Uploads and CORS

This document explains how our S3 backend manages CORS to support direct browser uploads using presigned URLs. It covers how to check the current configuration, update it programmatically, and how this aligns with our presigned upload method (PUT or POST depending on SSE). It also highlights important security considerations.

## Summary

- CORS is configured at the S3 bucket level, not per-prefix or folder.
- Our system can:
  - Inspect current CORS config
  - Validate it against required rules for direct uploads
  - Update it to support our upload method (aligned to your presigned type)
- Strict input: You must provide at least one allowed origin. There is no safe default.
- Multiple origins are supported (list or comma-separated string).
- Upload method alignment:
  - Without SSE: presigned PUT is used → require ["PUT", "HEAD"]
  - With SSE: presigned POST is used → require ["POST", "HEAD"]

## Where this lives

- Backend: `mojo/apps/fileman/backends/s3.py`
  - `get_cors_configuration()`
  - `check_cors_configuration_for_direct_upload(...)`
  - `update_cors_configuration_for_direct_upload(...)`
  - `ensure_cors_for_direct_upload(...)`

- Manager: `mojo/apps/fileman/models/manager.py`
  - Actions:
    - `on_action_check_cors(value)`
    - `on_action_fix_cors(value)`
  - Helpers:
    - `_resolve_allowed_origins_from_value_or_settings(value)`
    - `check_cors_config(allowed_origins, ...)`
    - `update_cors(allowed_origins, ...)`

## How origins are provided

You can pass origins via the action payload using one of these keys:

- `origins`
- `allowed_origins`
- `domains`
- `list_of_domains`

Each can be either:
- A list of origins: `["http://localhost:3000", "https://app.example.com"]`
- A comma-separated string: `"http://localhost:3000, https://app.example.com"`

We also look at settings (if you don’t pass them in the payload):

- `CORS_ALLOWED_ORIGINS`, `ALLOWED_ORIGINS` (list or comma-separated string)
- `FRONTEND_ORIGIN`, `FRONTEND_URL`, `SITE_URL`, `BASE_URL` (single origin strings)

Strict behavior: If we cannot resolve at least one origin from the payload and/or settings, we raise an error.

Notes:
- Origins should be exact (scheme + host + optional port), no trailing slash.
  - Example: `https://app.example.com`, `http://localhost:3000`.

### Examples: Origins in action payloads

```/dev/null/payload.json#L1-40
{
  "origins": [
    "http://localhost:3000",
    "https://app.example.com"
  ]
}

{
  "allowed_origins": "http://localhost:3000, https://app.example.com"
}

{
  "domains": [
    "https://staging.example.com",
    "https://app.example.com"
  ]
}

{
  "list_of_domains": "https://staging.example.com, https://app.example.com"
}
```

### Examples: Origins from settings

```/dev/null/settings.py#L1-60
# Either a list:
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "https://app.example.com",
]

# Or a comma-separated string:
ALLOWED_ORIGINS = "http://localhost:3000, https://app.example.com"

# Single-origin fallbacks (first present one will be used if list settings are not provided):
FRONTEND_ORIGIN = "https://app.example.com"
# FRONTEND_URL = "https://app.example.com"
# SITE_URL = "https://app.example.com"
# BASE_URL = "https://app.example.com"
```

## Alignment with presigned upload method

- If server-side encryption (SSE) is configured in the backend, we generate a presigned POST for uploads. Required CORS methods: `["POST", "HEAD"]`. Required headers for checks: `[]` (POST form fields carry data).
- If SSE is not configured, we generate a presigned PUT for uploads. Required CORS methods: `["PUT", "HEAD"]`. Required headers for checks: `["content-type"]`.

The update helpers automatically align to the correct method set based on whether SSE is set.

## What gets set in CORS

- AllowedMethods:
  - SSE: `["POST", "HEAD"]`
  - No SSE: `["PUT", "HEAD"]`
- AllowedHeaders: `["*"]` (ensures compatibility with `Content-Type` and `x-amz-*` headers)
- ExposeHeaders: `["ETag", "x-amz-request-id", "x-amz-id-2", "x-amz-version-id"]`
- MaxAgeSeconds: `3000`
- AllowedOrigins: the origins you provide

By default we merge your rule into any existing rules (`merge=True`). You can set `merge=False` to replace the entire CORS configuration.

## Security model

- CORS is not authorization. It only controls which browser origins can make cross-origin requests.
- Access is enforced by:
  - Presigned URLs: the signature binds the exact Key, method, headers, and expiry.
  - IAM/bucket policies: broader permissions.
- Folder/prefix scope:
  - S3 CORS cannot be constrained to a folder/prefix. That’s okay because your presigned URLs and IAM policies still restrict where uploads can go.
- Required AWS permissions for the service principal that manages CORS:
  - `s3:GetBucketCORS`, `s3:PutBucketCORS`
  - If also using `make_path_public`/`make_path_private`: `s3:GetBucketPolicy`, `s3:PutBucketPolicy`

## Actions

### on_action_check_cors

Checks the current bucket CORS and validates it against the required rules for direct uploads (aligned to your presigned upload type).

Input (payload):
- One of: `origins`, `allowed_origins`, `domains`, or `list_of_domains` (list or comma-separated string)
- Or ensure settings provide at least one origin

Output:
- `{"status": true, "result": {"ok": bool, "issues": [..], "config": {... or None}}}`

Example (Django shell):

```/dev/null/shell.py#L1-80
# Get a FileManager (S3-backed)
fm = FileManager.objects.get(id=123)

# Via action payload (list)
res = fm.on_action_check_cors({
    "origins": ["http://localhost:3000", "https://app.example.com"]
})
print(res)
# => {"status": true, "result": {"ok": true/false, "issues": [...], "config": {... or None}}}

# Via action payload (comma-separated string)
res = fm.on_action_check_cors({
    "allowed_origins": "http://localhost:3000, https://app.example.com"
})
print(res)

# If you rely on settings, call without payload; must still resolve to ≥ 1 origin
res = fm.on_action_check_cors({})
print(res)
```

### on_action_fix_cors

Updates CORS as needed to support direct uploads from the provided origins. It merges with existing config by default and re-verifies.

Input (payload):
- One of: `origins`, `allowed_origins`, `domains`, or `list_of_domains`
- Or ensure settings provide at least one origin

Output:
- `{"status": true, "result": {"changed": bool, "applied": {...}, "verified": bool, "post_update_issues": [...]}}`

Example:

```/dev/null/shell.py#L82-140
fm = FileManager.objects.get(id=123)

# Merge rule into existing CORS config
res = fm.on_action_fix_cors({
    "origins": ["http://localhost:3000", "https://app.example.com"]
})
print(res)
# => {"status": true, "result": {"changed": true/false, "applied": {...}, "verified": true/false, "post_update_issues": [...]}}

# If you need to replace instead of merge, call programmatically:
res = fm.update_cors(
    allowed_origins=["http://localhost:3000", "https://app.example.com"],
    merge=False  # replace entire CORS config
)
print(res)
```

## Programmatic usage (manager)

- Check current config:

```/dev/null/shell.py#L142-190
fm = FileManager.objects.get(id=123)
res = fm.check_cors_config(
    allowed_origins=["http://localhost:3000", "https://app.example.com"]
)
print(res)
# {'ok': True/False, 'issues': [...], 'config': {... or None}}
```

- Update config (merge):

```/dev/null/shell.py#L192-240
res = fm.update_cors(
    allowed_origins=["http://localhost:3000", "https://app.example.com"],
    merge=True  # default
)
print(res)
# {'changed': True/False, 'applied': {...}, 'verified': True/False, 'post_update_issues': [...]}
```

- Update config (replace):

```/dev/null/shell.py#L242-280
res = fm.update_cors(
    allowed_origins=["http://localhost:3000", "https://app.example.com"],
    merge=False
)
print(res)
```

## Programmatic usage (backend)

- Check + update with backend utilities if you’re directly interacting with the backend instance:

```/dev/null/shell.py#L282-360
backend = fm.backend  # S3StorageBackend

# Check
ok, issues, current = backend.check_cors_configuration_for_direct_upload(
    allowed_origins=["http://localhost:3000", "https://app.example.com"]
)
print(ok, issues, current)

# Update (merge)
result = backend.update_cors_configuration_for_direct_upload(
    allowed_origins=["http://localhost:3000", "https://app.example.com"],
    merge=True
)
print(result)

# Ensure (check + update + re-check)
result = backend.ensure_cors_for_direct_upload(
    allowed_origins=["http://localhost:3000", "https://app.example.com"]
)
print(result)
```

## Behavior after saving FileManager

`RestMeta.POST_SAVE_ACTIONS` includes `"fix_cors"`. That means after saving a `FileManager`, the system will attempt to fix CORS automatically. With the strict behavior (no default origins), this will error unless at least one origin is resolvable from settings. Options:

- Ensure you have at least one origin configured in settings:
  - `CORS_ALLOWED_ORIGINS` or `ALLOWED_ORIGINS` (list or comma-separated)
  - Or one of `FRONTEND_ORIGIN`, `FRONTEND_URL`, `SITE_URL`, `BASE_URL`
- Or remove `"fix_cors"` from `POST_SAVE_ACTIONS` and run it explicitly via action with a payload.

## Common errors and troubleshooting

- “No allowed origins provided. Please pass at least one origin.”
  - Provide origins in the action payload or configure them in settings.
- “No CORS configuration set on this bucket.”
  - Normal for a new bucket; run `on_action_fix_cors` or `update_cors`.
- “Access denied” or AWS `ClientError` on `get_bucket_cors`/`put_bucket_cors`
  - Ensure the service principal has `s3:GetBucketCORS` and `s3:PutBucketCORS`.
- Verifications failing for missing methods/headers
  - If you use SSE, expect required methods to be `POST, HEAD` (not PUT).
  - If you don’t use SSE, expect `PUT, HEAD` and require `content-type` header for checks.
  - You can explicitly pass `allowed_methods` and `allowed_headers` if you need to customize beyond defaults.

## Notes for S3-compatible services

We honor a custom `endpoint_url` in secrets. The same rules and methods apply for compatible services like MinIO; just ensure your credentials and endpoint are set correctly.

## Audit recommendations

- Log every change made to CORS, including the user, old config, and new config.
- Consider adding a dry-run mode (check only) in UI to review the merged config that would be applied.

## Quick checklist

- [ ] Decide your frontend origins and add them to settings or pass via action
- [ ] Ensure IAM permissions for `GetBucketCORS` and `PutBucketCORS`
- [ ] Run `on_action_check_cors` to see current status
- [ ] Run `on_action_fix_cors` to apply changes (merge vs replace)
- [ ] Verify uploads work from each origin (PUT or POST aligned with your SSE setting)