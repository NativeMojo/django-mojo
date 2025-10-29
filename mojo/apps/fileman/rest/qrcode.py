from django.http import HttpResponse

from mojo import JsonResponse
from mojo import decorators as md
from mojo.helpers import logit
from mojo.helpers.qrcode import QRCodeError, generate_qrcode


@md.URL("qrcode")
@md.public_endpoint("we allow this to be a public endpoint")
@md.requires_params(["data"])
def on_qrcode(request):
    """
    Generate a QR code image in PNG, SVG, or base64-encoded form.
    """
    fmt = (request.DATA.get("format") or "png").lower()

    try:
        payload = generate_qrcode(
            data=request.DATA.get("data", ""),
            fmt=fmt,
            size=request.DATA.get("size"),
            border=request.DATA.get("border"),
            error_correction=request.DATA.get("error_correction"),
            color=request.DATA.get("color"),
            background=request.DATA.get("background"),
            base64_format=request.DATA.get("base64_format"),
            logo=request.DATA.get("logo"),
            logo_scale=request.DATA.get("logo_scale"),
        )
    except QRCodeError as exc:
        status_code = getattr(exc, "status", 400)
        return md.response_error(str(exc), status=status_code)
    except Exception:  # pragma: no cover - unexpected failure
        logit.error("mojo.apps.fileman.rest.qrcode", "QR code generation failed", exc_info=True)
        return md.response_error("Unable to generate QR code.", status=500)

    if fmt == "base64":
        return JsonResponse(
            {
                "success": True,
                "format": payload.format,
                "data": payload.content,
                "content_type": payload.content_type,
                "width": payload.width,
                "height": payload.height,
            }
        )

    response = HttpResponse(payload.content, content_type=payload.content_type)
    default_name = "qrcode.svg" if payload.format == "svg" else "qrcode.png"
    _apply_filename(request, response, default_name=default_name)
    return response


def _apply_filename(request, response, default_name):
    filename = request.DATA.get("filename")
    if filename:
        disposition = f'attachment; filename="{filename}"'
    elif _is_truthy(request.DATA.get("download")):
        disposition = f'attachment; filename="{default_name}"'
    else:
        return
    response["Content-Disposition"] = disposition


def _is_truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False
