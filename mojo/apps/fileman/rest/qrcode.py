from django.http import HttpResponse
from django.shortcuts import render

from mojo import JsonResponse
from mojo import decorators as md
from mojo.helpers import logit
from mojo.helpers.qrcode import QRCodeError, build_vcard, generate_qrcode


@md.URL("/qrcode/builder")
@md.URL("qrcode/builder")
@md.public_endpoint("QR code builder UI for developers/admins")
def on_qrcode_builder(request):
    """
    Render the interactive QR code builder page.
    """
    return render(request, "fileman/qrcode_builder.html", {})


@md.URL("/api/qrcode")
@md.URL("qrcode")
@md.public_endpoint("we allow this to be a public endpoint")
@md.rate_limit("qrcode", ip_limit=60, ip_window=60)
@md.requires_params("data")
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
        return _error_response(str(exc), status_code)
    except Exception:  # pragma: no cover - unexpected failure
        logit.exception("mojo.apps.fileman.rest.qrcode", "QR code generation failed")
        return _error_response("Unable to generate QR code.", 500)

    return _build_response(request, payload, fmt)


@md.URL("/api/qrcode/vcard")
@md.URL("qrcode/vcard")
@md.public_endpoint("we allow this to be a public endpoint")
@md.rate_limit("qrcode_vcard", ip_limit=30, ip_window=60)
@md.requires_params("vcard")
def on_qrcode_vcard(request):
    """
    Generate a QR code encoding a vCard (or MeCard) from structured contact fields.
    """
    fmt = (request.DATA.get("format") or "png").lower()
    vcard_fields = request.DATA.get("vcard")
    vcard_format = (request.DATA.get("vcard_format") or "vcard").lower()
    logo = request.DATA.get("logo")

    error_correction = request.DATA.get("error_correction")
    if logo:
        # Logos cover modules — force H recovery regardless of caller input.
        error_correction = "h"
    elif not error_correction:
        error_correction = "h"

    size = request.DATA.get("size")
    if logo and not size:
        size = 512

    try:
        data = build_vcard(vcard_fields, fmt=vcard_format)
        payload = generate_qrcode(
            data=data,
            fmt=fmt,
            size=size,
            border=request.DATA.get("border"),
            error_correction=error_correction,
            color=request.DATA.get("color"),
            background=request.DATA.get("background"),
            base64_format=request.DATA.get("base64_format"),
            logo=logo,
            logo_scale=request.DATA.get("logo_scale"),
        )
    except QRCodeError as exc:
        status_code = getattr(exc, "status", 400)
        return _error_response(str(exc), status_code)
    except Exception:  # pragma: no cover - unexpected failure
        logit.exception("mojo.apps.fileman.rest.qrcode", "vCard QR code generation failed")
        return _error_response("Unable to generate QR code.", 500)

    return _build_response(request, payload, fmt)


def _error_response(message, status):
    return JsonResponse({"success": False, "status": False, "error": message}, status=status)


def _build_response(request, payload, fmt):
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
