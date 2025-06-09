import logging
from typing import Dict, List, Optional, Tuple, Any, Union

# Import renderer classes
from mojo.apps.fileman.models import File, FileRendition
from mojo.apps.fileman.renderer.base import BaseRenderer, RenditionRole
from mojo.apps.fileman.renderer.image import ImageRenderer
from mojo.apps.fileman.renderer.video import VideoRenderer
from mojo.apps.fileman.renderer.document import DocumentRenderer
from mojo.apps.fileman.renderer.audio import AudioRenderer

logger = logging.getLogger(__name__)

# Register renderers in order of preference
RENDERERS = [
    ImageRenderer,
    VideoRenderer,
    DocumentRenderer,
    AudioRenderer,
]

__all__ = [
    'BaseRenderer', 'RenditionRole', 'ImageRenderer', 'VideoRenderer', 'DocumentRenderer', 'AudioRenderer',
    'get_renderer_for_file', 'create_rendition', 'create_all_renditions',
    'get_rendition', 'get_or_create_rendition'
]

def get_renderer_for_file(file: File) -> Optional[BaseRenderer]:
    """
    Get the appropriate renderer for a file based on its category

    Args:
        file: The file to get a renderer for

    Returns:
        BaseRenderer: The renderer instance, or None if no renderer supports the file
    """
    for renderer_class in RENDERERS:
        if renderer_class.supports_file(file):
            return renderer_class(file)
    return None

def create_rendition(file: File, role: str, options: Dict = None) -> Optional[FileRendition]:
    """
    Create a rendition for a file

    Args:
        file: The file to create a rendition for
        role: The role of the rendition (e.g., 'thumbnail', 'preview')
        options: Additional options for creating the rendition

    Returns:
        FileRendition: The created rendition, or None if creation failed
    """
    renderer = get_renderer_for_file(file)
    if not renderer:
        logger.warning(f"No renderer available for file {file.id} ({file.filename}, {file.category})")
        return None

    return renderer.create_rendition(role, options)

def create_all_renditions(file: File) -> List[FileRendition]:
    """
    Create all default renditions for a file

    Args:
        file: The file to create renditions for

    Returns:
        List[FileRendition]: List of created renditions
    """
    renderer = get_renderer_for_file(file)
    if not renderer:
        logger.warning(f"No renderer available for file {file.id} ({file.filename}, {file.category})")
        return []

    return renderer.create_all_renditions()

def get_rendition(file: File, role: str) -> Optional[FileRendition]:
    """
    Get an existing rendition for a file

    Args:
        file: The file to get a rendition for
        role: The role of the rendition

    Returns:
        FileRendition: The rendition, or None if not found
    """
    try:
        return FileRendition.objects.get(original_file=file, role=role)
    except FileRendition.DoesNotExist:
        return None

def get_or_create_rendition(file: File, role: str, options: Dict = None) -> Optional[FileRendition]:
    """
    Get an existing rendition or create a new one if it doesn't exist

    Args:
        file: The file to get or create a rendition for
        role: The role of the rendition
        options: Additional options for creating the rendition

    Returns:
        FileRendition: The rendition, or None if not found and creation failed
    """
    rendition = get_rendition(file, role)
    if rendition:
        return rendition

    return create_rendition(file, role, options)
