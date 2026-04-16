# S3 backend fails when bucket region differs from default and endpoint_url is unset

**Type**: bug
**Status**: resolved
**Date**: 2026-04-16
**Severity**: high

## Description
The `S3StorageBackend` does not construct a regional S3 endpoint. When a FileManager is configured for a bucket in a non-us-east-1 region (e.g. `mojoware-media` in `eu-north-1`) without an explicit `endpoint_url` setting, boto3's client defaults to `s3.amazonaws.com` (us-east-1).

SigV4 requires the signing region to match the bucket's actual region:
- If `aws_region` is misconfigured (e.g. `us-east-1` while bucket lives in `eu-north-1`), S3 rejects signed requests with `AuthorizationHeaderMalformed`.
- Even when `aws_region` is correct (`eu-north-1`), boto3 still hits the global `s3.amazonaws.com` endpoint unless `endpoint_url` is set, causing `PermanentRedirect`. PUTs cannot follow redirects once the body has been streamed, so direct uploads fail.

## Context
Affects any FileManager pointing at a bucket outside us-east-1 that relies on the default endpoint behavior. Blocks file uploads end-to-end (server-side PUT, pre-signed PUT/POST, bucket policy operations). Currently unblockable only by manually setting `endpoint_url` in the FileManager's settings JSON.

## Acceptance Criteria
- S3 client and resource construction default `endpoint_url` to `https://s3.{region_name}.amazonaws.com` when no explicit `endpoint_url` setting is provided.
- Existing installations that explicitly set `endpoint_url` (e.g. S3-compatible services, MinIO) continue to work unchanged.
- Pre-signed upload URLs generated for a bucket in a non-us-east-1 region succeed against AWS S3.
- `test_connection()` / `validate_configuration()` pass against buckets in any AWS region.

## Investigation
**Likely root cause**: [mojo/apps/fileman/backends/s3.py:75-79](mojo/apps/fileman/backends/s3.py:75) and [mojo/apps/fileman/backends/s3.py:93-96](mojo/apps/fileman/backends/s3.py:93) pass `endpoint_url=self.endpoint_url` which is `None` unless explicitly set. boto3 then falls back to the global endpoint regardless of `region_name`.
**Confidence**: confirmed (code analysis + AWS SigV4 behavior)
**Code path**:
- [mojo/apps/fileman/backends/s3.py:34](mojo/apps/fileman/backends/s3.py:34) — `endpoint_url` read from settings, defaults to `None`
- [mojo/apps/fileman/backends/s3.py:56-79](mojo/apps/fileman/backends/s3.py:56) — client built with `endpoint_url=None`
- [mojo/apps/fileman/backends/s3.py:87-96](mojo/apps/fileman/backends/s3.py:87) — resource built with `endpoint_url=None`
**Regression test**: not feasible — requires a real S3 bucket in a non-us-east-1 region, or a moto mock configured to emulate regional endpoint enforcement.
**Related files**:
- [mojo/apps/fileman/backends/s3.py](mojo/apps/fileman/backends/s3.py)

## Proposed Fix (for planning)
Default `self.endpoint_url` to `f"https://s3.{self.region_name}.amazonaws.com"` when the setting is unset. Works for any AWS region and leaves S3-compatible overrides intact.

**Workaround (no code change)**: add `endpoint_url: "https://s3.<region>.amazonaws.com"` to the FileManager's settings JSON.
