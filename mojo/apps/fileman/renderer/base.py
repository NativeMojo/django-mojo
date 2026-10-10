import os
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple, Any, Union
from mojo.helpers.settings import settings
from mojo.helpers import logit
from mojo.apps.fileman.models import File, FileRendition
from mojo.apps.fileman.renderer.process import MAX_ERROR_LENGTH

logger = logit.get_logger(__name__, "fileman.log")

class RenditionRole:
    """
    Predefined roles for file renditions
    """
    # Common roles
    ORIGINAL = 'original'
    THUMBNAIL = 'thumbnail'
    PREVIEW = 'preview'
    
    # Image-specific roles
    THUMBNAIL_SM = 'thumbnail_sm'
    THUMBNAIL_MD = 'thumbnail_md'
    THUMBNAIL_LG = 'thumbnail_lg'
    SQUARE_SM = 'square_sm'
    SQUARE_MD = 'square_md'
    SQUARE_LG = 'square_lg'
    
    # Video-specific roles
    VIDEO_THUMBNAIL = 'video_thumbnail'
    VIDEO_PREVIEW = 'video_preview'
    VIDEO_MP4 = 'video_mp4'
    VIDEO_WEBM = 'video_webm'
    
    # Document-specific roles
    DOCUMENT_THUMBNAIL = 'document_thumbnail'
    DOCUMENT_PREVIEW = 'document_preview'
    DOCUMENT_PDF = 'document_pdf'
    
    # Audio-specific roles
    AUDIO_THUMBNAIL = 'audio_thumbnail'
    AUDIO_PREVIEW = 'audio_preview'
    AUDIO_MP3 = 'audio_mp3'


class RenditionBatchError(RuntimeError):
    """One or more requested renditions failed in a diagnosable way."""


class BaseRenderer(ABC):
    """
    Base class for file renderers
    
    A renderer creates different versions (renditions) of a file based on
    predefined roles. Each renderer supports specific file categories and
    provides implementations for creating renditions.
    """
    
    # The file categories this renderer supports
    supported_categories = []
    
    # Default rendition definitions: 
    # mapping of role -> (width, height, options)
    default_renditions = {}

    # None means every declared rendition is automatic. Renderers with
    # expensive opt-in roles may provide an explicit tuple.
    automatic_rendition_roles = None

    # Which FILEMAN_RENDITIONS_* setting may override this renderer's options
    # (see renderer/config.py). None means the class defaults are final.
    config_category = None

    def __init__(self, file: File):
        """
        Initialize renderer with a file

        Args:
            file: The original file to create renditions from
        """
        self.file = file
        self.renditions = {}
        self.failures = {}
        self._rendition_config = None
        self._load_existing_renditions()

    def _load_existing_renditions(self):
        """Load existing renditions for this file"""
        for rendition in FileRendition.objects.filter(
                original_file=self.file).order_by("created"):
            self.renditions[rendition.role] = rendition

    # ------------------------------------------------------------------
    # Rendition options: class defaults, then the admin override on top.
    # Every renderer reads its options through the two instance methods
    # below; nothing else consults default_renditions directly.
    # ------------------------------------------------------------------

    @classmethod
    def default_automatic_roles(cls):
        if cls.automatic_rendition_roles is None:
            return tuple(cls.default_renditions.keys())
        return tuple(cls.automatic_rendition_roles)

    @classmethod
    def default_rendition_options(cls, role):
        if role not in cls.default_renditions:
            raise ValueError("unsupported rendition role: %s" % role)
        return dict(cls.default_renditions[role])

    def _config_group(self):
        group = getattr(self.file, "group", None)
        if group is None:
            manager = getattr(self.file, "file_manager", None)
            group = getattr(manager, "group", None)
        return group

    def _merged_rendition_config(self):
        """(options_by_role, automatic_roles) with the override applied,
        resolved once per renderer instance."""
        if self._rendition_config is None:
            from mojo.apps.fileman.renderer import config
            override = {}
            if self.config_category:
                override = config.load(self.config_category, group=self._config_group())
            self._rendition_config = config.merge(
                self.default_renditions, self.default_automatic_roles(), override)
        return self._rendition_config

    def get_automatic_rendition_roles(self):
        return self._merged_rendition_config()[1]

    def get_rendition_options(self, role):
        options = self._merged_rendition_config()[0]
        if role not in options:
            raise ValueError("unsupported rendition role: %s" % role)
        return dict(options[role])
    
    @classmethod
    def supports_file(cls, file: File) -> bool:
        """
        Check if this renderer supports the given file
        
        Args:
            file: The file to check
            
        Returns:
            bool: True if this renderer supports the file, False otherwise
        """
        return file.category in cls.supported_categories
    
    @abstractmethod
    def create_rendition(self, role: str, options: Dict = None) -> Optional[FileRendition]:
        """
        Create a rendition for the specified role
        
        Args:
            role: The role of the rendition (e.g., 'thumbnail', 'preview')
            options: Additional options for creating the rendition
            
        Returns:
            FileRendition: The created rendition, or None if creation failed
        """
        pass
    
    def get_rendition(self, role: str, create_if_missing: bool = True) -> Optional[FileRendition]:
        """
        Get a rendition for the specified role
        
        Args:
            role: The role of the rendition
            create_if_missing: Whether to create the rendition if it doesn't exist
            
        Returns:
            FileRendition: The rendition, or None if not found and not created
        """
        existing = self.renditions.get(role)
        if existing and existing.upload_status == FileRendition.COMPLETED:
            return existing
        
        if create_if_missing:
            options = self.get_rendition_options(role)
            rendition = self.create_rendition(role, options)
            if rendition:
                self.renditions[role] = rendition
                return rendition
        
        return None
    
    def create_all_renditions(self) -> List[FileRendition]:
        """
        Create all default renditions for this file
        
        Returns:
            List[FileRendition]: List of created renditions
        """
        results = []
        for role in self.get_automatic_rendition_roles():
            rendition = self.get_rendition(role)
            if rendition:
                results.append(rendition)
        self.raise_for_failures()
        return results

    def record_failure(self, role, error):
        """Persist a safe failed rendition result and remember it for the job."""
        message = str(error).strip()[:MAX_ERROR_LENGTH] or "rendition failed"
        self.failures[role] = message

        rendition = FileRendition.objects.filter(
            original_file=self.file,
            role=role,
        ).order_by("-created").first()
        if rendition is None:
            name, _ = os.path.splitext(self.file.filename)
            rendition = FileRendition(
                original_file=self.file,
                role=role,
                filename="%s_%s.failed" % (name, role),
                storage_path="",
                content_type="application/octet-stream",
                category=self.file.category or "unknown",
            )
        rendition.upload_status = FileRendition.FAILED
        rendition.error_message = message
        rendition.file_size = None
        rendition.save()
        self.renditions[role] = rendition
        return rendition

    def raise_for_failures(self):
        if not self.failures:
            return
        detail = "; ".join(
            "%s: %s" % (role, message)
            for role, message in sorted(self.failures.items())
        )[:500]
        raise RenditionBatchError("rendition processing failed: %s" % detail)
    
    def cleanup_renditions(self):
        """
        Remove all renditions for this file
        """
        FileRendition.objects.filter(original_file=self.file).delete()
        self.renditions = {}

    def _create_rendition_object(self, role: str, filename: str, storage_path: str, 
                                content_type: str, category: str, file_size: int = None) -> FileRendition:
        """
        Create a FileRendition object in the database
        
        Args:
            role: The role of the rendition
            filename: The filename of the rendition
            storage_path: The storage path of the rendition
            content_type: The MIME type of the rendition
            category: The category of the rendition
            file_size: The size of the rendition in bytes
            
        Returns:
            FileRendition: The created rendition object
        """
        FileRendition.objects.filter(
            original_file=self.file,
            role=role,
            upload_status=FileRendition.FAILED,
        ).delete()
        rendition = FileRendition(
            original_file=self.file,
            role=role,
            filename=filename,
            storage_path=storage_path,
            content_type=content_type,
            category=category,
            file_size=file_size,
            upload_status=FileRendition.COMPLETED,
            error_message="",
        )
        rendition.save()
        return rendition
    
    def get_temp_path(self, suffix: str = '') -> str:
        """
        Get a temporary file path for processing
        
        Args:
            suffix: Optional suffix for the temp file (e.g., '.jpg')
            
        Returns:
            str: Path to a temporary file
        """
        import tempfile
        temp_dir = getattr(settings, 'MOJO_TEMP_DIR', None)
        if temp_dir:
            os.makedirs(temp_dir, exist_ok=True)
            return os.path.join(temp_dir, f"{self.file.id}_{suffix}")
        return tempfile.mktemp(suffix=suffix)
    
    @staticmethod
    def get_renderer_for_file(file: File) -> Optional['BaseRenderer']:
        """
        Get the appropriate renderer for a file
        
        Args:
            file: The file to get a renderer for
            
        Returns:
            BaseRenderer: The renderer instance, or None if no renderer supports the file
        """
        from mojo.apps.fileman.renderer import get_renderer_for_file
        return get_renderer_for_file(file)
