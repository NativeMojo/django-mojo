import os
import shutil
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timedelta
from urllib.parse import urljoin
import uuid

from .base import StorageBackend


class FileSystemStorageBackend(StorageBackend):
    """
    Local file system storage backend implementation
    """

    def __init__(self, file_manager, **kwargs):
        super().__init__(file_manager, **kwargs)

        # File system configuration
        self.base_path = self.get_setting('base_path', '/tmp/fileman')
        self.base_url = self.get_setting('base_url', '/media/')
        self.create_directories = self.get_setting('create_directories', True)
        self.permissions = self.get_setting('permissions', 0o644)
        self.directory_permissions = self.get_setting('directory_permissions', 0o755)

        # Upload configuration
        self.upload_expires_in = self.get_setting('upload_expires_in', 3600)  # 1 hour
        self.temp_upload_path = self.get_setting('temp_upload_path', os.path.join(self.base_path, 'uploads'))

        # Ensure base paths exist
        if self.create_directories:
            os.makedirs(self.base_path, mode=self.directory_permissions, exist_ok=True)
            os.makedirs(self.temp_upload_path, mode=self.directory_permissions, exist_ok=True)

    def _get_full_path(self, file_path: str) -> str:
        """Get the full file system path for a file"""
        # Normalize the path to prevent directory traversal
        normalized_path = os.path.normpath(file_path.lstrip('/'))

        # Ensure the path doesn't escape the base directory
        full_path = os.path.join(self.base_path, normalized_path)
        if not full_path.startswith(self.base_path):
            raise ValueError(f"Invalid file path: {file_path}")

        return full_path

    def _ensure_directory(self, file_path: str):
        """Ensure the directory for a file path exists"""
        directory = os.path.dirname(file_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, mode=self.directory_permissions, exist_ok=True)

    def save(self, file_obj, file_path: str, content_type: Optional[str] = None, metadata: Optional[dict] = None) -> str:
        """Save a file to the local file system"""
        try:
            # Generate file path
            file_path = self.generate_file_path(file_path)
            full_path = self._get_full_path(file_path)

            # Ensure directory exists
            self._ensure_directory(full_path)

            # Save the file
            with open(full_path, 'wb') as dest:
                if hasattr(file_obj, 'read'):
                    # File-like object
                    for chunk in iter(lambda: file_obj.read(4096), b''):
                        dest.write(chunk)
                else:
                    # Bytes data
                    dest.write(file_obj)

            # Set file permissions
            os.chmod(full_path, self.permissions)

            return file_path

        except Exception as e:
            raise Exception(f"Failed to save file to filesystem: {e}")

    def delete(self, file_path: str) -> bool:
        """Delete a file from the file system"""
        try:
            full_path = self._get_full_path(file_path)
            if os.path.exists(full_path):
                os.remove(full_path)

                # Try to remove empty parent directories
                parent_dir = os.path.dirname(full_path)
                while parent_dir != self.base_path:
                    try:
                        if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                            os.rmdir(parent_dir)
                            parent_dir = os.path.dirname(parent_dir)
                        else:
                            break
                    except OSError:
                        break

                return True
            return False
        except Exception:
            return False

    def exists(self, file_path: str) -> bool:
        """Check if a file exists in the file system"""
        try:
            full_path = self._get_full_path(file_path)
            return os.path.isfile(full_path)
        except Exception:
            return False

    def get_file_size(self, file_path: str) -> Optional[int]:
        """Get the size of a file in bytes"""
        try:
            full_path = self._get_full_path(file_path)
            if os.path.isfile(full_path):
                return os.path.getsize(full_path)
            return None
        except Exception:
            return None

    def get_url(self, file_path: str, expires_in: Optional[int] = None) -> str:
        """Get a URL to access the file"""
        # For file system, we just return a static URL
        # In a real implementation, you might want to generate signed URLs
        # or check permissions here
        return urljoin(self.base_url, file_path)

    def generate_upload_url(self, file_path: str, content_type: str,
                           file_size: Optional[int] = None,
                           expires_in: int = 3600) -> Dict[str, Any]:
        """
        Generate an upload URL for file system backend
        Note: File system doesn't natively support pre-signed URLs like S3,
        so this creates a temporary upload token that can be used with a custom endpoint
        """
        try:
            # Generate upload token
            upload_token = hashlib.sha256(f"{file_path}{uuid.uuid4()}{datetime.now()}".encode()).hexdigest()[:32]

            # Create temporary upload directory if needed
            temp_path = os.path.join(self.temp_upload_path, upload_token)
            if self.create_directories:
                os.makedirs(temp_path, mode=self.directory_permissions, exist_ok=True)

            # Store upload metadata in a temporary file
            metadata = {
                'file_path': file_path,
                'content_type': content_type,
                'file_size': file_size,
                'expires_at': (datetime.now() + timedelta(seconds=expires_in)).isoformat(),
                'created_at': datetime.now().isoformat()
            }

            metadata_path = os.path.join(temp_path, 'metadata.json')
            import json
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f)

            # Return upload information
            # The upload_url would point to a custom Django view that handles the upload
            return {
                'upload_url': f'/fileman/upload/{upload_token}/',
                'method': 'POST',
                'fields': {
                    'upload_token': upload_token,
                    'content_type': content_type
                },
                'headers': {
                    'Content-Type': content_type
                }
            }

        except Exception as e:
            raise Exception(f"Failed to generate upload URL: {e}")

    def validate_upload_token(self, upload_token: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Validate an upload token and return metadata"""
        try:
            temp_path = os.path.join(self.temp_upload_path, upload_token)
            metadata_path = os.path.join(temp_path, 'metadata.json')

            if not os.path.exists(metadata_path):
                return False, None

            import json
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)

            # Check if expired
            expires_at = datetime.fromisoformat(metadata['expires_at'])
            if datetime.now() > expires_at:
                # Clean up expired token
                shutil.rmtree(temp_path, ignore_errors=True)
                return False, None

            return True, metadata

        except Exception:
            return False, None

    def finalize_upload(self, upload_token: str, uploaded_file_path: str) -> bool:
        """Move uploaded file from temp location to final location"""
        try:
            is_valid, metadata = self.validate_upload_token(upload_token)
            if not is_valid or not metadata:
                return False

            temp_path = os.path.join(self.temp_upload_path, upload_token)
            temp_file_path = os.path.join(temp_path, 'uploaded_file')

            if not os.path.exists(temp_file_path):
                return False

            # Move file to final location
            final_path = self._get_full_path(metadata['file_path'])
            self._ensure_directory(final_path)

            shutil.move(temp_file_path, final_path)
            os.chmod(final_path, self.permissions)

            # Clean up temp directory
            shutil.rmtree(temp_path, ignore_errors=True)

            return True

        except Exception:
            return False

    def open(self, file_path: str, mode: str = 'rb'):
        """Open a file from the file system"""
        full_path = self._get_full_path(file_path)
        return open(full_path, mode)

    def list_files(self, path_prefix: str = "", limit: int = 1000) -> List[str]:
        """List files in the file system with optional path prefix"""
        try:
            search_path = self._get_full_path(path_prefix) if path_prefix else self.base_path

            files = []
            for root, dirs, filenames in os.walk(search_path):
                for filename in filenames:
                    if len(files) >= limit:
                        break

                    full_path = os.path.join(root, filename)
                    # Get relative path from base_path
                    rel_path = os.path.relpath(full_path, self.base_path)
                    files.append(rel_path.replace(os.sep, '/'))  # Use forward slashes

                if len(files) >= limit:
                    break

            return files[:limit]

        except Exception:
            return []

    def copy_file(self, source_path: str, dest_path: str) -> bool:
        """Copy a file within the file system"""
        try:
            source_full_path = self._get_full_path(source_path)
            dest_full_path = self._get_full_path(dest_path)

            if not os.path.exists(source_full_path):
                return False

            self._ensure_directory(dest_full_path)
            shutil.copy2(source_full_path, dest_full_path)
            os.chmod(dest_full_path, self.permissions)

            return True

        except Exception:
            return False

    def move_file(self, source_path: str, dest_path: str) -> bool:
        """Move a file within the file system"""
        try:
            source_full_path = self._get_full_path(source_path)
            dest_full_path = self._get_full_path(dest_path)

            if not os.path.exists(source_full_path):
                return False

            self._ensure_directory(dest_full_path)
            shutil.move(source_full_path, dest_full_path)
            os.chmod(dest_full_path, self.permissions)

            return True

        except Exception:
            return False

    def get_file_metadata(self, file_path: str) -> Dict[str, Any]:
        """Get comprehensive metadata for a file"""
        try:
            full_path = self._get_full_path(file_path)

            if not os.path.exists(full_path):
                return {'exists': False, 'path': file_path}

            stat = os.stat(full_path)

            metadata = {
                'exists': True,
                'path': file_path,
                'size': stat.st_size,
                'last_modified': datetime.fromtimestamp(stat.st_mtime),
                'created': datetime.fromtimestamp(stat.st_ctime),
                'permissions': oct(stat.st_mode)[-3:],
                'is_file': os.path.isfile(full_path),
                'is_directory': os.path.isdir(full_path)
            }

            return metadata

        except Exception:
            return {'exists': False, 'path': file_path}

    def cleanup_expired_uploads(self, before_date: Optional[datetime] = None):
        """Clean up expired upload tokens and temporary files"""
        if before_date is None:
            before_date = datetime.now() - timedelta(hours=1)

        try:
            if not os.path.exists(self.temp_upload_path):
                return

            for token_dir in os.listdir(self.temp_upload_path):
                token_path = os.path.join(self.temp_upload_path, token_dir)

                if not os.path.isdir(token_path):
                    continue

                metadata_path = os.path.join(token_path, 'metadata.json')

                try:
                    if os.path.exists(metadata_path):
                        import json
                        with open(metadata_path, 'r') as f:
                            metadata = json.load(f)

                        expires_at = datetime.fromisoformat(metadata['expires_at'])
                        if expires_at < before_date:
                            shutil.rmtree(token_path, ignore_errors=True)
                    else:
                        # If no metadata file, check directory modification time
                        stat = os.stat(token_path)
                        if datetime.fromtimestamp(stat.st_mtime) < before_date:
                            shutil.rmtree(token_path, ignore_errors=True)

                except Exception:
                    # If we can't process the directory, skip it
                    continue

        except Exception:
            pass  # Silently ignore cleanup errors

    def get_available_space(self) -> Optional[int]:
        """Get available disk space in bytes"""
        try:
            statvfs = os.statvfs(self.base_path)
            return statvfs.f_frsize * statvfs.f_bavail
        except Exception:
            return None

    def validate_configuration(self) -> Tuple[bool, List[str]]:
        """Validate file system configuration"""
        errors = []

        if not self.base_path:
            errors.append("Base path is required for file system backend")

        try:
            # Check if base path is accessible
            if not os.path.exists(self.base_path):
                if self.create_directories:
                    os.makedirs(self.base_path, mode=self.directory_permissions)
                else:
                    errors.append(f"Base path does not exist: {self.base_path}")

            # Check write permissions
            if os.path.exists(self.base_path):
                if not os.access(self.base_path, os.W_OK):
                    errors.append(f"No write permission for base path: {self.base_path}")

                if not os.access(self.base_path, os.R_OK):
                    errors.append(f"No read permission for base path: {self.base_path}")

        except Exception as e:
            errors.append(f"Error accessing base path: {e}")

        return len(errors) == 0, errors
