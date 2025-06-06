from django.http import JsonResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.conf import settings
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple, Union
import hashlib
import uuid

from ..models import FileManager, File
from ..backends import get_backend


def get_file_manager(request, file_manager_id=None, group=None):
    """
    Get FileManager instance based on ID, group, or default
    
    Args:
        request: The HTTP request
        file_manager_id: Optional ID of specific file manager
        group: Optional group to find file manager for
        
    Returns:
        FileManager instance
        
    Raises:
        ValueError: If no active file manager is available
    """
    if file_manager_id:
        return get_object_or_404(
            FileManager.objects.filter(is_active=True),
            id=file_manager_id
        )
    
    # Get user's group or use provided group
    user_group = group or getattr(request.user, 'group', None)
    
    # Try to get default file manager for the group
    if user_group:
        file_manager = FileManager.objects.filter(
            group=user_group,
            is_default=True,
            is_active=True
        ).first()
        
        if file_manager:
            return file_manager
    
    # Fall back to global default
    file_manager = FileManager.objects.filter(
        group__isnull=True,
        is_default=True,
        is_active=True
    ).first()
    
    if not file_manager:
        # If no default, get any active file manager
        file_manager = FileManager.objects.filter(is_active=True).first()
    
    if not file_manager:
        raise ValueError("No active file manager available")
    
    return file_manager


def validate_file_request(file_manager, filename, content_type, file_size=None) -> Tuple[bool, str]:
    """
    Validate a file upload request
    
    Args:
        file_manager: FileManager instance
        filename: Name of the file
        content_type: MIME type of the file
        file_size: Optional size of the file in bytes
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    # Check file manager restrictions
    can_upload, error_msg = file_manager.can_upload_file(filename, file_size)
    if not can_upload:
        return False, error_msg
    
    # Check MIME type
    if not file_manager.can_upload_mime_type(content_type):
        return False, f"MIME type '{content_type}' is not allowed"
    
    return True, "File upload request is valid"


def initiate_upload(request, data) -> Dict[str, Any]:
    """
    Initiate file upload(s) and return upload URLs and metadata
    
    Args:
        request: The HTTP request
        data: JSON data containing file information
        
    Returns:
        Dictionary with response data
        
    Example request data:
    {
        "files": [
            {
                "filename": "document.pdf",
                "content_type": "application/pdf",
                "size": 1024000
            }
        ],
        "file_manager_id": 123,  // optional
        "group_id": 456,  // optional
        "metadata": {  // optional global metadata
            "source": "web_upload",
            "category": "documents"
        }
    }
    """
    # Parse request data
    files_data = data.get('files', [])
    file_manager_id = data.get('file_manager_id')
    group_id = data.get('group_id')
    global_metadata = data.get('metadata', {})
    
    if not files_data:
        return {
            'success': False,
            'error': 'No files specified',
            'status_code': 400
        }
    
    # Get file manager
    try:
        file_manager = get_file_manager(
            request, 
            file_manager_id=file_manager_id,
            group=getattr(request.user, 'groups', None).filter(id=group_id).first() if group_id else None
        )
    except Exception as e:
        return {
            'success': False,
            'error': f'File manager error: {str(e)}',
            'status_code': 400
        }
    
    # Get storage backend
    try:
        backend = get_backend(file_manager)
    except Exception as e:
        return {
            'success': False,
            'error': f'Storage backend error: {str(e)}',
            'status_code': 500
        }
    
    # Process each file
    upload_files = []
    errors = []
    
    for file_data in files_data:
        filename = file_data.get('filename')
        content_type = file_data.get('content_type')
        file_size = file_data.get('size')
        file_metadata = file_data.get('metadata', {})
        
        if not filename or not content_type:
            errors.append({
                'filename': filename,
                'error': 'Filename and content_type are required'
            })
            continue
        
        # Validate file request
        is_valid, error_msg = validate_file_request(
            file_manager, filename, content_type, file_size
        )
        
        if not is_valid:
            errors.append({
                'filename': filename,
                'error': error_msg
            })
            continue
        
        try:
            # Create File record
            file_obj = File(
                group=file_manager.group,
                uploaded_by=request.user,
                file_manager=file_manager,
                original_filename=filename,
                content_type=content_type,
                file_size=file_size,
                upload_status=File.PENDING
            )
            
            # Combine metadata
            combined_metadata = {**global_metadata, **file_metadata}
            if combined_metadata:
                file_obj.metadata = combined_metadata
            
            # Generate file path
            file_obj.file_path = backend.generate_file_path(
                file_obj.generate_unique_filename(),
                group_id=file_manager.group.id if file_manager.group else None
            )
            
            # Save to generate upload token
            file_obj.save()
            
            # Generate upload URL if backend supports it
            upload_info = None
            if backend.supports_direct_upload():
                try:
                    upload_info = backend.generate_upload_url(
                        file_obj.file_path,
                        content_type,
                        file_size,
                        expires_in=file_manager.get_setting('upload_expires_in', 3600)
                    )
                    
                    # Update file record with upload URL and expiration
                    file_obj.upload_url = upload_info['upload_url']
                    file_obj.upload_expires_at = datetime.now() + timedelta(
                        seconds=file_manager.get_setting('upload_expires_in', 3600)
                    )
                    file_obj.save()
                    
                except Exception as e:
                    errors.append({
                        'filename': filename,
                        'error': f'Failed to generate upload URL: {str(e)}'
                    })
                    continue
            
            # Prepare response data
            file_response = {
                'id': file_obj.id,
                'filename': file_obj.filename,
                'original_filename': file_obj.original_filename,
                'upload_token': file_obj.upload_token,
                'file_path': file_obj.file_path,
                'content_type': file_obj.content_type,
                'upload_status': file_obj.upload_status
            }
            
            if upload_info:
                file_response.update({
                    'upload_url': upload_info['upload_url'],
                    'method': upload_info['method'],
                    'fields': upload_info.get('fields', {}),
                    'headers': upload_info.get('headers', {}),
                    'expires_at': file_obj.upload_expires_at.isoformat() if file_obj.upload_expires_at else None
                })
            else:
                # For backends that don't support direct upload
                file_response.update({
                    'upload_url': f'/fileman/upload/{file_obj.upload_token}/',
                    'method': 'POST',
                    'fields': {},
                    'headers': {}
                })
            
            upload_files.append(file_response)
            
        except Exception as e:
            errors.append({
                'filename': filename,
                'error': f'Failed to initiate upload: {str(e)}'
            })
    
    # Prepare response
    response_data = {
        'success': len(upload_files) > 0,
        'files': upload_files,
        'file_manager': {
            'id': file_manager.id,
            'name': file_manager.name,
            'backend_type': file_manager.backend_type,
            'supports_direct_upload': file_manager.supports_direct_upload
        }
    }
    
    if errors:
        response_data['errors'] = errors
    
    status_code = 200 if upload_files else 400
    response_data['status_code'] = status_code
    
    return response_data


def finalize_upload(request, data) -> Dict[str, Any]:
    """
    Finalize file upload and confirm successful upload
    
    Args:
        request: The HTTP request
        data: JSON data containing upload information
        
    Returns:
        Dictionary with response data
        
    Example request data:
    {
        "upload_token": "abc123...",
        "file_size": 1024000,  // optional
        "checksum": "md5:abcdef...",  // optional
        "metadata": {  // optional additional metadata
            "processing_complete": true
        }
    }
    """
    # Parse request data
    upload_token = data.get('upload_token')
    reported_size = data.get('file_size')
    reported_checksum = data.get('checksum')
    additional_metadata = data.get('metadata', {})
    
    if not upload_token:
        return {
            'success': False,
            'error': 'Upload token is required',
            'status_code': 400
        }
    
    # Get file record
    try:
        file_obj = File.objects.get(
            upload_token=upload_token,
            is_active=True
        )
    except File.DoesNotExist:
        return {
            'success': False,
            'error': 'Invalid upload token',
            'status_code': 404
        }
    
    # Check if upload has expired
    if file_obj.is_upload_expired:
        file_obj.mark_as_expired()
        return {
            'success': False,
            'error': 'Upload has expired',
            'status_code': 410
        }
    
    # Check permissions
    if file_obj.uploaded_by != request.user:
        return {
            'success': False,
            'error': 'Permission denied',
            'status_code': 403
        }
    
    # Get storage backend
    try:
        backend = get_backend(file_obj.file_manager)
    except Exception as e:
        return {
            'success': False,
            'error': f'Storage backend error: {str(e)}',
            'status_code': 500
        }
    
    try:
        # Mark as uploading
        file_obj.mark_as_uploading()
        
        # Validate upload
        is_valid, error_msg = backend.validate_upload(
            file_obj.file_path,
            upload_token,
            expected_size=reported_size,
            expected_checksum=reported_checksum
        )
        
        if not is_valid:
            file_obj.mark_as_failed(error_msg)
            return {
                'success': False,
                'error': f'Upload validation failed: {error_msg}',
                'status_code': 400
            }
        
        # Get actual file metadata from backend
        file_metadata = backend.get_file_metadata(file_obj.file_path)
        actual_size = file_metadata.get('size')
        
        # Update file record
        file_obj.file_size = actual_size or reported_size
        
        # Calculate checksum if not provided
        if not reported_checksum and backend.exists(file_obj.file_path):
            try:
                calculated_checksum = backend.get_file_checksum(file_obj.file_path)
                if calculated_checksum:
                    file_obj.checksum = f"md5:{calculated_checksum}"
            except Exception:
                pass  # Checksum calculation is optional
        else:
            file_obj.checksum = reported_checksum or ""
        
        # Add additional metadata
        if additional_metadata:
            file_obj.metadata.update(additional_metadata)
        
        # Mark as completed
        file_obj.mark_as_completed()
        
        # Generate download URL
        try:
            download_url = backend.get_url(file_obj.file_path)
        except Exception:
            download_url = f'/fileman/download/{upload_token}/'
        
        # Prepare response
        response_data = {
            'success': True,
            'file': {
                'id': file_obj.id,
                'filename': file_obj.filename,
                'original_filename': file_obj.original_filename,
                'file_path': file_obj.file_path,
                'file_size': file_obj.file_size,
                'content_type': file_obj.content_type,
                'upload_status': file_obj.upload_status,
                'upload_token': file_obj.upload_token,
                'checksum': file_obj.checksum,
                'download_url': download_url,
                'metadata': file_obj.metadata,
                'created': file_obj.created.isoformat(),
                'modified': file_obj.modified.isoformat()
            },
            'status_code': 200
        }
        
        return response_data
            
    except Exception as e:
        file_obj.mark_as_failed(str(e))
        return {
            'success': False,
            'error': f'Failed to finalize upload: {str(e)}',
            'status_code': 500
        }


def direct_upload(request, upload_token, file_data) -> Dict[str, Any]:
    """
    Handle direct file uploads for backends that don't support pre-signed URLs
    
    Args:
        request: The HTTP request
        upload_token: The upload token
        file_data: The uploaded file data
        
    Returns:
        Dictionary with response data
    """
    # Get file record
    try:
        file_obj = File.objects.get(
            upload_token=upload_token,
            is_active=True
        )
    except File.DoesNotExist:
        return {
            'success': False,
            'error': 'Invalid upload token',
            'status_code': 404
        }
    
    # Check if upload has expired
    if file_obj.is_upload_expired:
        file_obj.mark_as_expired()
        return {
            'success': False,
            'error': 'Upload has expired',
            'status_code': 410
        }
    
    # Get uploaded file
    uploaded_file = file_data
    if not uploaded_file:
        return {
            'success': False,
            'error': 'No file uploaded',
            'status_code': 400
        }
    
    # Get storage backend
    try:
        backend = get_backend(file_obj.file_manager)
    except Exception as e:
        return {
            'success': False,
            'error': f'Storage backend error: {str(e)}',
            'status_code': 500
        }
    
    try:
        # Mark as uploading
        file_obj.mark_as_uploading()
        
        # Save file using backend
        file_path = backend.save(
            uploaded_file,
            file_obj.filename,
            content_type=file_obj.content_type,
            group_id=file_obj.group.id if file_obj.group else None,
            metadata=file_obj.metadata
        )
        
        # Update file record with actual path
        file_obj.file_path = file_path
        file_obj.file_size = uploaded_file.size
        
        # Calculate checksum
        try:
            uploaded_file.seek(0)  # Reset file pointer
            md5_hash = hashlib.md5()
            for chunk in uploaded_file.chunks():
                md5_hash.update(chunk)
            file_obj.checksum = f"md5:{md5_hash.hexdigest()}"
        except Exception:
            pass  # Checksum calculation is optional
        
        # Mark as completed
        file_obj.mark_as_completed()
        
        return {
            'success': True,
            'message': 'File uploaded successfully',
            'upload_token': upload_token,
            'status_code': 200
        }
            
    except Exception as e:
        file_obj.mark_as_failed(str(e))
        return {
            'success': False,
            'error': f'Failed to upload file: {str(e)}',
            'status_code': 500
        }


def get_download_url(request, upload_token) -> Dict[str, Any]:
    """
    Generate download URL for a file
    
    Args:
        request: The HTTP request
        upload_token: The upload token
        
    Returns:
        Dictionary with download URL or error information
    """
    # Get file record
    try:
        file_obj = File.objects.get(
            upload_token=upload_token,
            is_active=True,
            upload_status=File.COMPLETED
        )
    except File.DoesNotExist:
        return {
            'success': False,
            'error': 'File not found',
            'status_code': 404
        }
    
    # Check permissions
    if not file_obj.can_be_accessed_by(request.user, getattr(request.user, 'group', None)):
        return {
            'success': False,
            'error': 'Permission denied',
            'status_code': 403
        }
    
    # Get storage backend
    try:
        backend = get_backend(file_obj.file_manager)
    except Exception as e:
        return {
            'success': False,
            'error': f'Storage backend error: {str(e)}',
            'status_code': 500
        }
    
    # Generate download URL
    try:
        download_url = backend.get_url(
            file_obj.file_path,
            expires_in=3600  # 1 hour
        )
        
        return {
            'success': True,
            'download_url': download_url,
            'file': {
                'id': file_obj.id,
                'filename': file_obj.filename,
                'original_filename': file_obj.original_filename,
                'content_type': file_obj.content_type
            },
            'status_code': 200
        }
            
    except Exception as e:
        return {
            'success': False,
            'error': f'Failed to generate download URL: {str(e)}',
            'status_code': 500
        }