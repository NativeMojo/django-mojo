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
  - `logo`: base64-encoded image to center overlay
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

## Helper Usage

Import `generate_qrcode` for internal use:

```python
from mojo.helpers.qrcode import generate_qrcode

payload = generate_qrcode(data="hello world", fmt="png")
```

The helper returns a `QRCodePayload` with `format`, `content`, `content_type`, and optional image dimensions. Catch `QRCodeError` to handle invalid parameters or missing dependencies.
