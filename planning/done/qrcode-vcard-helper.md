# QR Code vCard Helper

**Type**: request
**Status**: planned
**Date**: 2026-04-22
**Priority**: medium

## Description
Add a `vcard` parameter to the QR code endpoint that accepts a structured object (name, phone, email, etc.) and formats it into a valid vCard 3.0 string server-side, then encodes it into the QR code. Optionally support MeCard format as an opt-in variant. Auto-tune error correction and size when a logo is combined with a vCard payload so the resulting QR remains scannable.

## Context
The existing `/api/qrcode` endpoint accepts `data` as an opaque string and will encode whatever is passed — so vCard technically works today if the client builds the `BEGIN:VCARD...END:VCARD` payload themselves. This pushes RFC 6350 escaping rules (commas, semicolons, backslashes, newlines) onto every client, which is error-prone.

Callers want to pass structured contact fields and get a scannable contact-card QR back without caring about vCard syntax. This is the most common real-world use case for contact QR codes (business cards, email signatures, event badges).

vCard 3.0 is chosen as the default over MeCard because it is universally supported by modern iOS/Android scanners, while MeCard has gaps on older/niche scanners. MeCard's only advantage — smaller payload — is exposed as an opt-in for users who need it.

## Acceptance Criteria
- `POST /api/qrcode` accepts a `vcard` object parameter and returns a QR code encoding a valid vCard string.
- `vcard` and `data` are mutually exclusive; passing both returns a 400 error.
- `name` is required inside `vcard`; all other fields are optional.
- `phone` and `email` accept either a string or an array of strings (people commonly have work + personal).
- `vcard_format` accepts `"vcard"` (default) or `"mecard"`.
- All vCard field values are escaped per RFC 6350 (`,`, `;`, `\`, newlines).
- When `vcard` is provided and `error_correction` is not explicitly set, default to `"h"` (30% recovery).
- When `vcard` + `logo` are both provided and `size` is not explicitly set, default to `512` for logo clarity.
- When `vcard` + `logo` are both provided and `error_correction` is not explicitly set, force `"h"` (logos cover modules, long payloads need max recovery).
- Output of the vCard builder is scannable by iOS Camera, Android default scanner, and Google Lens for a representative contact record.
- Docs updated in both `docs/django_developer/` and `docs/web_developer/` with request/response examples.

## Investigation
**What exists**:
- [mojo/apps/fileman/rest/qrcode.py](mojo/apps/fileman/rest/qrcode.py) — public endpoint, reads params from `request.DATA`, delegates to helper.
- [mojo/helpers/qrcode.py](mojo/helpers/qrcode.py) — `generate_qrcode()` helper that accepts a raw `data` string and supports PNG/SVG/base64 output plus logo overlay (PNG only, `logo_scale` 0.05–0.35).
- Logo overlay in `_overlay_logo()` is already implemented and works for PNG/base64-PNG output.

**What changes**:
- `mojo/helpers/qrcode.py` — add `build_vcard(fields, format="vcard")` pure string builder. Handles RFC 6350 escaping and MeCard output variant. No external dependencies.
- `mojo/apps/fileman/rest/qrcode.py` — read `request.DATA.get("vcard")`, validate mutual exclusion with `data`, call `build_vcard`, apply defaulting rules for `error_correction` and `size`, then pass result as `data` into `generate_qrcode`.
- Tests in `tests/test_helpers/` for the builder (escaping, formats, MeCard) and `tests/test_fileman/` for the endpoint (param plumbing, mutual exclusion, default tuning).
- Docs: add a vCard section to existing QR code docs in both doc tracks.

**Constraints**:
- Logo overlay only works for PNG and base64-PNG output — SVG path skips `_overlay_logo`. Document this limitation when `vcard` + `logo` are used with SVG.
- Endpoint is `@md.public_endpoint` today; no permission changes. vCard input must still be validated to avoid encoding oversized or malformed payloads.
- Must stay KISS — no third-party vCard library. Plain string builder + escaping.
- No Python type hints (per core rules).

**Related files**:
- [mojo/apps/fileman/rest/qrcode.py](mojo/apps/fileman/rest/qrcode.py)
- [mojo/helpers/qrcode.py](mojo/helpers/qrcode.py)
- `tests/test_helpers/` (new or existing qrcode test module)
- `tests/test_fileman/` (new or existing endpoint test module)
- `docs/django_developer/` (QR code helper docs)
- `docs/web_developer/` (QR code API docs)

## Endpoints
| Method | Path | Description | Permission |
|---|---|---|---|
| POST | `/api/qrcode` | Existing endpoint — adds `vcard` input object | public (unchanged) |

### New `vcard` object schema
| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Maps to `FN:` and structured `N:` fields |
| `org` | string | no | Company / organization |
| `title` | string | no | Job title |
| `phone` | string or array | no | One or more phone numbers |
| `email` | string or array | no | One or more email addresses |
| `url` | string or array | no | Website(s) |
| `address` | string | no | Free-form street address (single line) |
| `note` | string | no | Free-form note |

### New top-level params
| Param | Type | Default | Notes |
|---|---|---|---|
| `vcard` | object | — | Mutually exclusive with `data` |
| `vcard_format` | string | `"vcard"` | `vcard` or `mecard` |

### Defaulting rules
- `vcard` present, `error_correction` unset → `"h"`
- `vcard` + `logo` present, `size` unset → `512`
- `vcard` + `logo` present, `error_correction` unset → forced `"h"`

## Example

**Request**:
```json
POST /api/qrcode
{
  "vcard": {
    "name": "Jane Doe",
    "org": "Acme Inc",
    "title": "Engineer",
    "phone": ["+15551234567", "+15557654321"],
    "email": "jane@acme.com",
    "url": "https://acme.com"
  },
  "format": "png",
  "logo": "<base64 png>",
  "logo_scale": 0.2
}
```

**Encoded payload inside QR**:
```
BEGIN:VCARD
VERSION:3.0
FN:Jane Doe
N:Doe;Jane;;;
ORG:Acme Inc
TITLE:Engineer
TEL:+15551234567
TEL:+15557654321
EMAIL:jane@acme.com
URL:https://acme.com
END:VCARD
```

## Tests Required
- `build_vcard` produces valid vCard 3.0 output for minimal (`name` only) and full field sets.
- `build_vcard` escapes `,`, `;`, `\`, and newlines per RFC 6350.
- `build_vcard` emits repeated `TEL:` / `EMAIL:` lines when arrays are passed.
- `build_vcard` with `format="mecard"` produces valid MeCard output.
- Endpoint returns 400 when both `vcard` and `data` are passed.
- Endpoint returns 400 when `vcard` is provided without `name`.
- Endpoint defaults `error_correction` to `"h"` when `vcard` is provided and level is unset.
- Endpoint defaults `size` to `512` when `vcard` + `logo` are present and size is unset.
- Endpoint respects caller-provided overrides for `error_correction` and `size` even when `vcard` + `logo` are present (except the `logo` → `h` force, which is intentional — document this).
- Smoke test: generate PNG with `vcard` + logo, confirm non-empty bytes and correct content type.

## Out of Scope
- Hosted vCard link mode (returning a URL that serves `.vcf`) — separate future request.
- vCard 4.0 output — stick with 3.0 for broadest scanner compatibility.
- Photo embedding inside the vCard (`PHOTO:` field) — adds significant payload size; revisit if requested.
- Validation of phone/email format — pass through as provided.
- Tracking / analytics on scanned vCards.
- SVG logo overlay support — existing limitation, not introduced here.

## Plan

**Status**: planned
**Planned**: 2026-04-22

### Objective
Add a dedicated `/api/qrcode/vcard` endpoint that accepts structured contact fields, formats them into a vCard 3.0 (or MeCard) string via a new helper, and encodes the result as a QR code with auto-tuned defaults when a logo is present.

### Steps
1. `mojo/helpers/qrcode.py` — add `build_vcard(fields, fmt="vcard")` pure string builder. Supports vCard 3.0 and MeCard output. Escapes `\`, `;`, `,`, and newlines per RFC 6350 for vCard; escapes `\`, `;`, `:`, `,` for MeCard. Normalizes string-or-array inputs for `phone`, `email`, `url`. Splits `name` on last space into `N:family;given;;;` plus `FN:<name>`; single-token names emit `FN:` only with `N:<token>;;;;`. Raises `QRCodeError` for missing `name` or unknown `fmt`. No new dependencies.
2. `mojo/apps/fileman/rest/qrcode.py` — add new handler `on_qrcode_vcard` wired to `@md.URL("/api/qrcode/vcard")` and `@md.URL("qrcode/vcard")` with `@md.public_endpoint(...)` and `@md.requires_params(["vcard"])`. Reads `vcard` (object) and `vcard_format` (default `"vcard"`). Applies defaulting rules:
   - If `error_correction` unset → `"h"`.
   - If `logo` present and `size` unset → `512`.
   - If `logo` present → force `error_correction="h"` regardless of caller value.
   Calls `build_vcard(...)`, then delegates to `generate_qrcode(data=<built>, ...)`.
3. `mojo/apps/fileman/rest/qrcode.py` — factor response shaping (PNG/SVG/base64 branching + `_apply_filename`) into a small private helper `_build_response(request, payload)` shared between `on_qrcode` and `on_qrcode_vcard`. Leave existing `on_qrcode` behavior unchanged.
4. `tests/test_fileman/1_test_qrcode.py` — add `build_vcard` unit tests (minimal name-only, full field set, array phone/email, RFC escaping, mecard variant, missing-name raises) and endpoint tests via `opts.client` (defaulting rules, `logo` forces `h`, smoke test with vcard + logo producing non-empty PNG bytes).
5. `docs/web_developer/files/qrcode.md` — add "vCard Endpoint" section documenting `/api/qrcode/vcard`, the `vcard` object schema, `vcard_format`, defaulting rules, SVG-logo limitation, request/response example.
6. `docs/django_developer/helpers/other.md` — document `build_vcard()` signature, formats, and error behavior.
7. `CHANGELOG.md` — one-line entry describing the new endpoint and helper.

### Design Decisions
- **New endpoint over param on existing**: `/api/qrcode` stays single-purpose (raw string → QR); `/api/qrcode/vcard` owns its own contract, defaults, and docs. Avoids mutual-exclusion error paths.
- **vCard 3.0 default**: broadest scanner support; MeCard opt-in via `vcard_format`.
- **Pure string builder, no lib**: RFC 6350 escaping is small and deterministic; KISS.
- **Logo forces `h`**: logos cover modules; `h` (30% recovery) is the only reliable level for logo + long payload.
- **`size` default `512` only when logo present**: non-logo callers keep the existing `256` default from `generate_qrcode`.
- **Arrays for `phone`/`email`/`url`**: common real case — emit repeated `TEL:` / `EMAIL:` / `URL:` lines.
- **No type hints** per core rules; no changes to `generate_qrcode` signature.

### Edge Cases
- Missing `name` inside `vcard` → `QRCodeError` → 400 from endpoint.
- Non-string field values → coerce to `str` before escaping.
- Oversized vCard payload → existing `DataOverflowError` → `QRCodeError` → 400.
- `vcard_format="mecard"` + `logo` → same defaulting rules apply.
- SVG format + `logo` + `vcard` → logo silently skipped (existing helper behavior); documented.
- Empty array for `phone`/`email` → emit zero lines for that field, not an empty `TEL:` line.
- `vcard` passed as non-object (string) → `QRCodeError` with clear message.

### Testing
- `build_vcard` minimal (`name` only) → `tests/test_fileman/1_test_qrcode.py`
- `build_vcard` full field set with arrays → `tests/test_fileman/1_test_qrcode.py`
- `build_vcard` RFC 6350 escaping of `,` `;` `\` and newlines → `tests/test_fileman/1_test_qrcode.py`
- `build_vcard` MeCard variant output shape → `tests/test_fileman/1_test_qrcode.py`
- `build_vcard` missing `name` raises `QRCodeError` → `tests/test_fileman/1_test_qrcode.py`
- Endpoint `/api/qrcode/vcard` returns PNG with default `h` error correction → `tests/test_fileman/1_test_qrcode.py`
- Endpoint with `logo` defaults `size` to 512 and forces `h` even when caller passes `l` → `tests/test_fileman/1_test_qrcode.py`
- Endpoint smoke test: `vcard` + `logo` produces non-empty image/png bytes → `tests/test_fileman/1_test_qrcode.py`
- Endpoint missing `vcard` → 400 (via `@md.requires_params`) → `tests/test_fileman/1_test_qrcode.py`

### Docs
- `docs/web_developer/files/qrcode.md` — new vCard Endpoint section with schema table, defaulting rules, example request/response, SVG-logo note.
- `docs/django_developer/helpers/other.md` — `build_vcard()` signature and usage.
- `CHANGELOG.md` — one-line entry.

## Resolution

**Status**: resolved
**Date**: 2026-04-22

### What Was Built
New `POST /api/qrcode/vcard` endpoint that accepts a structured `vcard` object (`name`, `org`, `title`, `phone`, `email`, `url`, `address`, `note`) and encodes it as a QR code. Supports vCard 3.0 (default) and MeCard via `vcard_format`. Auto-defaults `error_correction` to `h`; when `logo` is supplied, forces `h` and bumps `size` to 512 for scannability. New `mojo.helpers.qrcode.build_vcard()` helper performs RFC 6350 escaping and is reusable outside the endpoint. Also fixed a latent bug in `/api/qrcode` where error-path responses crashed calling nonexistent `md.response_error`. Hardening: 512KB cap on decoded logo payload (both endpoints) and `@md.rate_limit` (60/min on base, 30/min on vcard).

### Files Changed
- `mojo/helpers/qrcode.py` — added `build_vcard()`, `_build_vcard_30`, `_build_mecard`, escape helpers, `MAX_LOGO_BYTES` cap enforced in `_decode_base64`.
- `mojo/apps/fileman/rest/qrcode.py` — added `on_qrcode_vcard` handler, factored `_build_response` + `_error_response` shared helpers, applied `@md.rate_limit` to both endpoints, fixed `md.response_error` error path.
- `tests/test_fileman/1_test_qrcode.py` — added 8 builder unit tests, 5 endpoint tests, oversized-logo DoS test.
- `docs/web_developer/files/qrcode.md` — new vCard Endpoint section, rate limits, logo cap note.
- `docs/web_developer/account/user_self_management.md` — quick reference row for `/api/qrcode/vcard`.
- `docs/django_developer/helpers/other.md` — `build_vcard()` subsection.
- `CHANGELOG.md` — entries for feature + hardening.

### Tests
- `tests/test_fileman/1_test_qrcode.py` — builder correctness (minimal, full, escaping, mecard, missing name, unknown format, empty array), endpoint behavior (PNG minimal, missing param, missing name 400, base64 JSON, mecard variant), oversized logo rejection.
- Run: `bin/run_tests --agent -t test_fileman.1_test_qrcode`
- Full suite: 1761 passed, 0 failed (113s).

### Docs Updated
- `docs/web_developer/files/qrcode.md` — vCard Endpoint section + rate limits.
- `docs/web_developer/account/user_self_management.md` — quick reference row.
- `docs/django_developer/helpers/other.md` — `build_vcard()` usage.
- `CHANGELOG.md` — feature and hardening entries.

### Security Review
Two DoS concerns flagged: oversized `logo` base64 and unbounded vcard field sizes. **Logo cap enforced (512KB)** and **rate limits applied** in follow-up commit `14ef0fc`. Per-field vcard size caps deferred — rate limit combined with QR library's own size ceiling (QR v40 ~2953 bytes) bounds the per-request work; revisit if abuse patterns emerge.

### Follow-up
- Consider adding per-field length caps on vcard inputs (note/address/arrays) if rate limit proves insufficient under load.
