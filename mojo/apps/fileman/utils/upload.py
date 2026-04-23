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
from typing import Any, Dict

from ..backends import get_backend
from ..models import File


def direct_upload(request, upload_token, file_data) -> Dict[str, Any]:
    """Handle direct file uploads for backends without presigned URLs.

    Args:
        request: The HTTP request
        upload_token: The upload token
        file_data: The uploaded file data (UploadedFile or RawUploadFile)

    Returns:
        Dict with status, message, and status_code.
    """
    try:
        file_obj = File.objects.get(upload_token=upload_token, is_active=True)
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
    except Exception as e:
        return {
            'success': False,
            'error': f'Storage backend error: {str(e)}',
            'status_code': 500,
        }

    try:
        file_obj.mark_as_uploading()

        backend.save(file_data, file_obj.storage_file_path, file_obj.content_type)
        file_obj.file_size = file_data.size

        # Best-effort checksum.
        try:
            file_data.seek(0)
            md5_hash = hashlib.md5()
            for chunk in file_data.chunks():
                md5_hash.update(chunk)
            file_obj.checksum = f"md5:{md5_hash.hexdigest()}"
        except Exception:
            pass

        file_obj.mark_as_completed(commit=True)

        return {
            'success': True,
            'message': 'File uploaded successfully',
            'upload_token': upload_token,
            'status_code': 200,
        }
    except Exception as e:
        file_obj.mark_as_failed(str(e))
        return {
            'success': False,
            'error': f'Failed to upload file: {str(e)}',
            'status_code': 500,
        }


def get_download_url(request, upload_token) -> Dict[str, Any]:
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
