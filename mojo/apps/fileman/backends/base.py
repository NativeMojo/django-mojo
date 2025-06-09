from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timedelta
import os


class StorageBackend(ABC):
    """
    Abstract base class for all storage backends
    """

    def __init__(self, file_manager, **kwargs):
        """
        Initialize the storage backend with a FileManager instance

        Args:
            file_manager: FileManager instance with configuration
            **kwargs: Additional backend-specific configuration
        """
        self.file_manager = file_manager
        self.settings = file_manager.primary_settings
        self.backend_url = file_manager.backend_url
        self.config = kwargs

    @abstractmethod
    def save(self, file_obj, file_path: str, content_type: Optional[str] = None, metadata: Optional[dict] = None) -> str:
        """
        Save a file to the storage backend

        Args:
            file_obj: File-like object to save
            filename: Name to save the file as
            **kwargs: Additional save options

        Returns:
            str: Full path to the saved file
        """
        pass

    @abstractmethod
    def delete(self, file_path: str) -> bool:
        """
        Delete a file from the storage backend

        Args:
            file_path: Path to the file to delete

        Returns:
            bool: True if deletion was successful
        """
        pass

    @abstractmethod
    def exists(self, file_path: str) -> bool:
        """
        Check if a file exists in the storage backend

        Args:
            file_path: Path to check

        Returns:
            bool: True if file exists
        """
        pass

    @abstractmethod
    def get_file_size(self, file_path: str) -> Optional[int]:
        """
        Get the size of a file in bytes

        Args:
            file_path: Path to the file

        Returns:
            Optional[int]: File size in bytes, None if file doesn't exist
        """
        pass

    @abstractmethod
    def get_url(self, file_path: str, expires_in: Optional[int] = None) -> str:
        """
        Get a URL to access the file

        Args:
            file_path: Path to the file
            expires_in: Optional expiration time in seconds

        Returns:
            str: URL to access the file
        """
        pass

    @abstractmethod
    def generate_upload_url(self, file_path: str, content_type: str,
                           file_size: Optional[int] = None,
                           expires_in: int = 3600) -> Dict[str, Any]:
        """
        Generate a pre-signed URL for direct upload

        Args:
            file_path: Path where the file will be stored
            content_type: MIME type of the file
            file_size: Expected file size in bytes
            expires_in: URL expiration time in seconds

        Returns:
            Dict containing:
                - upload_url: Pre-signed upload URL
                - method: HTTP method to use (POST, PUT, etc.)
                - fields: Additional form fields (if any)
                - headers: Required headers (if any)
        """
        pass

    def supports_direct_upload(self) -> bool:
        """
        Check if this backend supports direct uploads

        Returns:
            bool: True if direct uploads are supported
        """
        return self.file_manager.supports_direct_upload

    def validate_upload(self, file_path: str, upload_token: Optional[str] = None,
                       expected_size: Optional[int] = None,
                       expected_checksum: Optional[str] = None) -> Tuple[bool, str]:
        """
        Validate that an uploaded file matches expectations

        Args:
            file_path: Path to the uploaded file
            upload_token: Token used for the upload
            expected_size: Expected file size
            expected_checksum: Expected file checksum

        Returns:
            Tuple[bool, str]: (is_valid, error_message)
        """
        if not self.exists(file_path):
            return False, "File does not exist after upload"

        if expected_size:
            actual_size = self.get_file_size(file_path)
            if actual_size != expected_size:
                return False, f"File size mismatch: expected {expected_size}, got {actual_size}"

        if expected_checksum:
            actual_checksum = self.get_file_checksum(file_path)
            if actual_checksum != expected_checksum:
                return False, f"Checksum mismatch: expected {expected_checksum}, got {actual_checksum}"

        return True, "Upload validation successful"

    def get_file_checksum(self, file_path: str, algorithm: str = 'md5') -> Optional[str]:
        """
        Calculate checksum of a file

        Args:
            file_path: Path to the file
            algorithm: Hash algorithm to use (md5, sha256, etc.)

        Returns:
            Optional[str]: File checksum, None if calculation fails
        """
        # Default implementation - backends can override for efficiency
        try:
            import hashlib
            hash_obj = hashlib.new(algorithm)

            with self.open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_obj.update(chunk)

            return hash_obj.hexdigest()
        except Exception:
            return None

    def open(self, file_path: str, mode: str = 'rb'):
        """
        Open a file from the storage backend

        Args:
            file_path: Path to the file
            mode: File open mode

        Returns:
            File-like object
        """
        raise NotImplementedError("Backend does not support file opening")

    def generate_file_path(self, filename: str, group_id: Optional[int] = None) -> str:
        """
        Generate a storage path for a file

        Args:
            filename: Original filename
            group_id: Optional group ID for organization

        Returns:
            str: Generated file path
        """
        # Default implementation - backends can override
        parts = []

        if group_id:
            parts.append(f"group_{group_id}")

        # Add date-based organization
        now = datetime.now()
        parts.extend([
            str(now.year),
            f"{now.month:02d}",
            f"{now.day:02d}"
        ])

        parts.append(filename)
        return "/".join(parts)

    def get_available_space(self) -> Optional[int]:
        """
        Get available storage space in bytes

        Returns:
            Optional[int]: Available space in bytes, None if unlimited/unknown
        """
        return None

    def cleanup_expired_uploads(self, before_date: Optional[datetime] = None):
        """
        Clean up expired upload URLs and temporary files

        Args:
            before_date: Clean up uploads before this date (default: now)
        """
        # Default implementation does nothing - backends can override
        pass

    def get_file_metadata(self, file_path: str) -> Dict[str, Any]:
        """
        Get metadata for a file

        Args:
            file_path: Path to the file

        Returns:
            Dict containing file metadata
        """
        metadata = {}

        if self.exists(file_path):
            metadata['exists'] = True
            metadata['size'] = self.get_file_size(file_path)
            metadata['path'] = file_path
        else:
            metadata['exists'] = False

        return metadata

    def copy_file(self, source_path: str, dest_path: str) -> bool:
        """
        Copy a file within the storage backend

        Args:
            source_path: Source file path
            dest_path: Destination file path

        Returns:
            bool: True if copy was successful
        """
        try:
            with self.open(source_path, 'rb') as source:
                return self.save(source, dest_path) is not None
        except Exception:
            return False

    def move_file(self, source_path: str, dest_path: str) -> bool:
        """
        Move a file within the storage backend

        Args:
            source_path: Source file path
            dest_path: Destination file path

        Returns:
            bool: True if move was successful
        """
        if self.copy_file(source_path, dest_path):
            return self.delete(source_path)
        return False

    def list_files(self, path_prefix: str = "", limit: int = 1000) -> List[str]:
        """
        List files with optional path prefix

        Args:
            path_prefix: Optional path prefix to filter by
            limit: Maximum number of files to return

        Returns:
            List[str]: List of file paths
        """
        # Default implementation returns empty list - backends should override
        return []

    def get_setting(self, key: str, default: Any = None) -> Any:
        """
        Get a setting value from the file manager configuration

        Args:
            key: Setting key
            default: Default value if key not found

        Returns:
            Setting value
        """
        return self.settings.get(key, default)

    def make_path_public(self):
        return

    def make_path_private(self):
        return

    def __str__(self):
        return f"{self.__class__.__name__}({self.backend_url})"
