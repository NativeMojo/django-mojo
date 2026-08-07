"""Upload and download helpers for token-gated fileman endpoints.

Only two functions are live here:
  - direct_upload:    body/multipart upload to a token URL (local backends)
  - get_download_url: generate a signed download URL for a completed file

The former orchestration helpers (initiate_upload, finalize_upload,
get_file_manager, validate_file_request) were removed — they referenced
File fields that no longer exist (uploaded_by, original_filename, file_path,
upload_expires_at, is_upload_expired). Upload initiation now flows through
the purpose-built `/upload/initiate` endpoint in rest/upload.py, which uses
the current `File` model directly.
"""

import hashlib
import magic

from ..backends import get_backend
from ..models import File


class BoundedUpload:
    """Count, hash and sample bytes while a backend consumes a file."""

    SAMPLE_SIZE = 8192

    def __init__(self, source, limit, content_type):
        self.source = source
        self.limit = limit
        self.content_type = content_type
        self.size = 0
        self.sample = bytearray()
        self.hash = hashlib.md5()

    def read(self, size=-1):
        data = self.source.read(size)
        if not data:
            return data
        self.size += len(data)
        if self.limit is not None and self.size > self.limit:
            raise ValueError("Upload exceeds its declared or configured size")
        self.hash.update(data)
        if len(self.sample) < self.SAMPLE_SIZE:
            remaining = self.SAMPLE_SIZE - len(self.sample)
            self.sample.extend(data[:remaining])
        return data

    @property
    def checksum(self):
        return f"md5:{self.hash.hexdigest()}"

    @property
    def detected_content_type(self):
        if not self.sample:
            return "application/x-empty"
        return magic.from_buffer(bytes(self.sample), mime=True)


def direct_upload(request, upload_token, file_data):
    """Handle direct file uploads for backends without presigned URLs.

    Args:
        request: The HTTP request
        upload_token: The upload token
        file_data: The uploaded file data (UploadedFile or RawUploadFile)

    Returns:
        Dict with status, message, and status_code.
    """
    try:
        file_obj = File.objects.select_related("file_manager").get(
            upload_token=upload_token,
            is_active=True,
            upload_status=File.UPLOADING,
        )
    except File.DoesNotExist:
        return {
            'success': False,
            'error': 'Invalid upload token',
            'status_code': 404,
        }

    if not file_data:
        return {
            'success': False,
            'error': 'No file uploaded',
            'status_code': 400,
        }

    try:
        backend = get_backend(file_obj.file_manager)
    except Exception:
        return {
            'success': False,
            'error': 'Storage backend error',
            'status_code': 500,
        }

    try:
        transfer_type = file_obj.file_manager.normalize_upload_mime_type(
            getattr(file_data, "content_type", "application/octet-stream"))
        if transfer_type != file_obj.content_type:
            raise ValueError("Upload content type does not match initiation")
        if request.method == "PUT":
            content_length = request.META.get("CONTENT_LENGTH")
            if content_length in (None, ""):
                raise ValueError("Content-Length is required")
            try:
                content_length = int(content_length)
            except (TypeError, ValueError):
                raise ValueError("Invalid Content-Length")
            if content_length < 0 or content_length != file_obj.file_size:
                raise ValueError("Upload size does not match initiation")
        max_size = file_obj.file_size
        if file_obj.file_manager.max_file_size > 0:
            max_size = min(max_size, file_obj.file_manager.max_file_size)
        bounded = BoundedUpload(file_data, max_size, transfer_type)
        backend.save(bounded, file_obj.storage_file_path, file_obj.content_type)
        if bounded.size != file_obj.file_size:
            raise ValueError("Upload size does not match initiation")
        if bounded.size:
            actual_type = file_obj.file_manager.normalize_upload_mime_type(
                bounded.detected_content_type)
            # Browsers derive File.type from the filename, so a valid payload
            # can legitimately sniff differently (for example, a PNG named
            # ``logo.svg``).  The declaration was already policy-checked at
            # initiation and matched against the transfer header above; the
            # sniffed type must independently satisfy the manager policy, but
            # need not equal that browser-supplied declaration.
            if not file_obj.file_manager.can_upload_mime_type(actual_type):
                raise ValueError("Uploaded content type is not allowed")
        # Transfer and completion are deliberately separate. Keep the token and
        # UPLOADING state retryable until the documented completion action wins.
        file_obj.file_size = bounded.size
        file_obj.checksum = bounded.checksum
        file_obj.save(update_fields=["file_size", "checksum", "modified"])

        return {
            'success': True,
            'message': 'File transferred; completion confirmation is required',
            'file': file_obj.lifecycle_dict(),
            'status_code': 200,
        }
    except Exception:
        try:
            backend.delete(file_obj.storage_file_path)
        except Exception:
            pass
        file_obj.upload_token = ""
        file_obj.mark_as_failed(commit=True)
        return {
            'success': False,
            'error': 'Upload failed validation',
            'status_code': 400,
        }


def get_download_url(request, upload_token):
    """Generate a download URL for a completed file by upload token."""
    try:
        file_obj = File.objects.get(
            upload_token=upload_token,
            is_active=True,
            upload_status=File.COMPLETED,
        )
    except File.DoesNotExist:
        return {
            'success': False,
            'error': 'File not found',
            'status_code': 404,
        }

    if not file_obj.can_be_accessed_by(request.user, getattr(request.user, 'group', None)):
        return {
            'success': False,
            'error': 'Permission denied',
            'status_code': 403,
        }

    try:
        backend = get_backend(file_obj.file_manager)
    except Exception as e:
        return {
            'success': False,
            'error': f'Storage backend error: {str(e)}',
            'status_code': 500,
        }

    try:
        download_url = backend.get_url(file_obj.storage_file_path, expires_in=3600)
        return {
            'success': True,
            'download_url': download_url,
            'file': {
                'id': file_obj.id,
                'filename': file_obj.filename,
                'content_type': file_obj.content_type,
            },
            'status_code': 200,
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'Failed to generate download URL: {str(e)}',
            'status_code': 500,
        }
