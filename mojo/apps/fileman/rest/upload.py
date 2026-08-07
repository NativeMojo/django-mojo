import hashlib
import json
import re

from django.db import transaction

from mojo import JsonResponse
from mojo import decorators as md
import mojo.errors
from mojo.apps.fileman.models import File, FileManager, UploadInitiation
from mojo.apps.fileman.utils.upload import get_download_url


class RawUploadStream:
    """File-like adapter over Django's already-spooled request stream."""

    def __init__(self, request, content_type):
        self.request = request
        self.content_type = content_type

    def read(self, size=-1):
        return self.request.read(size)


IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def _idempotency_material(request, file_manager, filename, content_type, file_size):
    key = request.DATA.get("idempotency_key")
    if key is None:
        return None, None
    if not isinstance(key, str) or not IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise mojo.errors.ValueException(
            "idempotency_key must be 1-128 ASCII letters, digits, '.', '_', ':' or '-'"
        )
    payload = {
        "filename": filename,
        "content_type": content_type,
        "file_size": file_size,
        "file_manager_id": file_manager.id,
        "group_id": file_manager.group_id,
        "use": file_manager.use,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return (
        hashlib.sha256(key.encode("utf-8")).hexdigest(),
        hashlib.sha256(encoded).hexdigest(),
    )


def _upload_response(file, include_target):
    response = file.lifecycle_dict()
    if include_target:
        target = file.request_upload_target()
        if target:
            response.update(target)
    return response


def _new_upload_file(actor, file_manager, filename, content_type, file_size):
    file = File(
        filename=filename,
        content_type=content_type,
        file_size=file_size,
        file_manager=file_manager,
        group=file_manager.group,
        user=actor,
    )
    file.on_rest_pre_save({}, True)
    file.mark_as_uploading()
    file.save()
    return file


@md.POST("upload/initiate")
@md.requires_auth()
def on_upload_initiate(request):
    """Initiate one upload from a flat singular request body.

    Required: filename, content_type and file_size. Optional selectors are
    file_manager, group and use. idempotency_key enables same-File recovery.
    """
    file_manager = FileManager.resolve_for_upload(request)
    try:
        filename = request.DATA["filename"]
        content_type = request.DATA["content_type"]
        file_size = request.DATA["file_size"]
    except KeyError:
        raise mojo.errors.ValueException("filename, content_type and file_size are required")
    filename = FileManager.normalize_upload_filename(filename)
    content_type = FileManager.normalize_upload_mime_type(content_type)
    file_size = FileManager.normalize_upload_size(file_size)
    allowed, reason = file_manager.can_upload_file(filename, file_size)
    if not allowed:
        raise mojo.errors.ValueException(reason)
    if not file_manager.can_upload_mime_type(content_type):
        raise mojo.errors.ValueException("content_type is not allowed")

    actor = request.user
    key_digest, fingerprint = _idempotency_material(
        request, file_manager, filename, content_type, file_size)
    if key_digest is None:
        file = _new_upload_file(actor, file_manager, filename, content_type, file_size)
        return _upload_response(file, include_target=True)

    with transaction.atomic():
        type(actor).objects.select_for_update().get(pk=actor.pk)
        attempt = UploadInitiation.objects.select_related("file", "file__file_manager").filter(
            actor=actor, key_digest=key_digest).first()
        if attempt is not None:
            if attempt.fingerprint != fingerprint:
                return JsonResponse({
                    "status": False,
                    "code": 409,
                    "error": "idempotency_key was already used for a different upload",
                }, status=409)
            file = attempt.file
            return _upload_response(file, include_target=file.upload_status == File.UPLOADING)
        file = _new_upload_file(actor, file_manager, filename, content_type, file_size)
        UploadInitiation.objects.create(
            actor=actor,
            file=file,
            key_digest=key_digest,
            fingerprint=fingerprint,
        )
        return _upload_response(file, include_target=True)


@md.POST("upload/<str:upload_token>")
@md.PUT("upload/<str:upload_token>")
@md.custom_security("requires upload token")
def on_direct_upload(request, upload_token):
    """Receive a local upload as multipart POST or bounded raw PUT."""
    from mojo.apps.fileman.utils.upload import direct_upload

    if request.method == "PUT":
        content_type = request.META.get("CONTENT_TYPE", "application/octet-stream")
        content_type = content_type.split(";", 1)[0].strip()
        file_data = RawUploadStream(request, content_type)
    else:
        if not request.FILES or "file" not in request.FILES:
            return JsonResponse({"success": False, "error": "No file provided"}, status=400)
        file_data = request.FILES["file"]

    response_data = direct_upload(request, upload_token, file_data)
    status_code = response_data.pop("status_code", 200)
    return JsonResponse(response_data, status=status_code)


@md.GET("download/<str:download_token>")
@md.custom_security("requires download token")
def on_download(request, download_token):
    """Get a download URL for a completed file."""
    response_data = get_download_url(request, download_token)
    if response_data.get("success") and "download_url" in response_data:
        return JsonResponse({
            "success": True,
            "download_url": response_data["download_url"],
            "file": response_data.get("file", {}),
        })
    status_code = response_data.pop("status_code", 200)
    return JsonResponse(response_data, status=status_code)
