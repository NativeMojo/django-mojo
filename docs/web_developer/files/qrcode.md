# QR Code Generation

The `fileman` REST module exposes a simple QR code generator backed by a reusable helper in `mojo.helpers.qrcode`. Use it to render QR codes as PNG, SVG, or base64 JSON.

## REST Endpoint

- `GET /api/qrcode` (public)
- Required param: `data` (string payload to encode)
- Optional params:
  - `format`: `png` (default), `svg`, or `base64`
  - `size`: target image size in pixels (48–2048)
  - `border`: module border width (0–32)
  - `error_correction`: `L`, `M` (default), `Q`, or `H`
  - `color`: foreground hex (`#000000` default)
  - `background`: background hex (`#FFFFFF` default)
  - `base64_format`: when `format=base64`, choose `png` (default) or `svg`
  - `logo`: base64-encoded image to center overlay (max 512KB decoded)
  - `logo_scale`: fraction of QR size for logo (0.05–0.35, default 0.2)
  - `filename`: set download filename (PNG/SVG responses only)
  - `download`: truthy flag to force download disposition

### PNG Example

```http
POST /api/qrcode
Content-Type: application/json

{
  "data": "https://nativemojo.com",
  "format": "png",
  "size": 512,
  "color": "#0D9488",
  "background": "#FFFFFF"
}
```

PNG responses stream image bytes. Pass `download=true` or `filename` to prompt download.

### Base64 Example

```http
POST /api/qrcode
Content-Type: application/json

{
  "data": "{\"ticket\": 12345}",
  "format": "base64",
  "base64_format": "svg"
}
```

Base64 mode returns JSON:

```json
{
  "success": true,
  "format": "svg",
  "content_type": "image/svg+xml",
  "data": "PHN2ZyB4bWxucz0i..."
}
```

Decode `data` and store or embed as needed.

## vCard Endpoint

Generate a QR code that encodes a vCard (or MeCard) contact. The endpoint builds the vCard payload for you from structured fields — no need to format `BEGIN:VCARD...END:VCARD` on the client.

- `POST /api/qrcode/vcard` (public)
- Required param: `vcard` (object with contact fields)
- Optional params:
  - `vcard_format`: `vcard` (default, vCard 3.0) or `mecard`
  - All optional params from `/api/qrcode` (`format`, `size`, `border`, `error_correction`, `color`, `background`, `base64_format`, `logo`, `logo_scale`, `filename`, `download`)

### `vcard` Object Schema

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

### Defaulting Rules

- When `error_correction` is not provided → `h` (30% recovery). vCards are long and benefit from higher recovery.
- When `logo` is provided and `size` is not → `512` (better logo clarity).
- When `logo` is provided → `error_correction` is forced to `h` regardless of caller input. Logos overlay QR modules and require maximum recovery to stay scannable.

### Example

```http
POST /api/qrcode/vcard
Content-Type: application/json

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
  "size": 512
}
```

Returns PNG bytes (or JSON for `format=base64`). The encoded payload is:

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

### Notes

- vCard 3.0 is the default because all modern iOS/Android scanners support it. MeCard is more compact but has gaps in niche scanners — opt in with `vcard_format: "mecard"` only when payload size matters.
- Logo overlay (`logo` param) only applies to PNG output. When `format=svg`, the logo is ignored (same as the base `/api/qrcode` endpoint).
- Missing `vcard.name` returns a 400 error.

### Rate Limits

- `POST /api/qrcode` — 60 requests per minute per IP.
- `POST /api/qrcode/vcard` — 30 requests per minute per IP.
- `logo` payloads are capped at 512KB decoded on both endpoints.

## Helper Usage

Import `generate_qrcode` for internal use:

```python
from mojo.helpers.qrcode import generate_qrcode

payload = generate_qrcode(data="hello world", fmt="png")
```

The helper returns a `QRCodePayload` with `format`, `content`, `content_type`, and optional image dimensions. Catch `QRCodeError` to handle invalid parameters or missing dependencies.
