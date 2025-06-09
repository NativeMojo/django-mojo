import os
import io
from typing import Dict, Optional, Tuple, Union, BinaryIO
from PIL import Image, ImageOps
import mimetypes
import logging

from mojo.apps.fileman.models import File, FileRendition
from mojo.apps.fileman.renderer.base import BaseRenderer, RenditionRole

logger = logging.getLogger(__name__)

class ImageRenderer(BaseRenderer):
    """
    Renderer for image files

    Creates various renditions like thumbnails, square crops, and resized versions
    """

    # Image file categories
    supported_categories = ['image']

    # Default rendition definitions with sizes and options
    default_renditions = {
        RenditionRole.THUMBNAIL: {'width': 150, 'height': 150, 'mode': 'contain'},
        RenditionRole.THUMBNAIL_SM: {'width': 32, 'height': 32, 'mode': 'contain'},
        RenditionRole.THUMBNAIL_MD: {'width': 64, 'height': 64, 'mode': 'contain'},
        RenditionRole.THUMBNAIL_LG: {'width': 300, 'height': 300, 'mode': 'contain'},
        RenditionRole.SQUARE_SM: {'width': 100, 'height': 100, 'mode': 'crop'}
    }

    # Default output format settings
    default_format = 'JPEG'
    default_quality = 85

    def __init__(self, file: File):
        super().__init__(file)
        # Map of file extensions to PIL format names
        self.format_map = {
            '.jpg': 'JPEG',
            '.jpeg': 'JPEG',
            '.png': 'PNG',
            '.gif': 'GIF',
            '.bmp': 'BMP',
            '.webp': 'WEBP',
            '.tiff': 'TIFF',
        }

    def _download_original(self) -> Union[str, None]:
        """
        Download the original file to a temporary location

        Returns:
            str: Path to the downloaded file, or None if download failed
        """
        try:
            file_manager = self.file.file_manager
            backend = file_manager.backend

            # Get file extension
            _, ext = os.path.splitext(self.file.filename)
            temp_path = self.get_temp_path(ext)

            # Download file from storage
            backend.download(self.file.storage_file_path, temp_path)
            return temp_path
        except Exception as e:
            logger.error(f"Failed to download original file: {str(e)}")
            return None

    def _get_output_format(self, source_path: str, options: Dict = None) -> Tuple[str, str]:
        """
        Determine the output format and file extension

        Args:
            source_path: Path to the source image
            options: Additional options for format selection

        Returns:
            Tuple[str, str]: (PIL format name, file extension)
        """
        _, ext = os.path.splitext(source_path.lower())

        # Use specified format if provided
        if options and 'format' in options:
            format_name = options['format'].upper()
            if format_name == 'JPEG':
                return format_name, '.jpg'
            return format_name, f".{options['format'].lower()}"

        # Use original format if supported
        if ext in self.format_map:
            return self.format_map[ext], ext

        # Default to JPEG
        return self.default_format, '.jpg'

    def _process_image(self, source_path: str, width: int, height: int,
                      mode: str = 'contain', options: Dict = None) -> Tuple[BinaryIO, str, str]:
        """
        Process image to create a rendition

        Args:
            source_path: Path to the source image
            width: Target width
            height: Target height
            mode: Resize mode ('contain', 'crop', 'stretch')
            options: Additional processing options

        Returns:
            Tuple[BinaryIO, str, str]: (Image data, format, mime type)
        """
        options = options or {}
        quality = options.get('quality', self.default_quality)

        try:
            # Open the image
            with Image.open(source_path) as img:
                # Convert to RGB if RGBA (unless PNG or format with alpha support)
                if img.mode == 'RGBA' and options.get('format', '').upper() != 'PNG':
                    img = img.convert('RGB')

                # Process based on mode
                if mode == 'crop':
                    # Square crop (centered)
                    img = ImageOps.fit(img, (width, height), Image.Resampling.LANCZOS)
                elif mode == 'contain':
                    # Resize to fit within dimensions while maintaining aspect ratio
                    img.thumbnail((width, height), Image.Resampling.LANCZOS)
                elif mode == 'stretch':
                    # Stretch to fill dimensions
                    img = img.resize((width, height), Image.Resampling.LANCZOS)

                # Determine output format
                format_name, extension = self._get_output_format(source_path, options)

                # Save to buffer
                buffer = io.BytesIO()
                if format_name == 'JPEG':
                    img.save(buffer, format=format_name, quality=quality, optimize=True)
                else:
                    img.save(buffer, format=format_name)

                buffer.seek(0)
                mime_type = mimetypes.guess_type(f"file{extension}")[0]

                return buffer, extension, mime_type
        except Exception as e:
            logger.error(f"Image processing error: {str(e)}")
            raise

    def create_rendition(self, role: str, options: Dict = None) -> Optional[FileRendition]:
        """
        Create an image rendition for the specified role

        Args:
            role: The role of the rendition
            options: Additional options for creating the rendition

        Returns:
            FileRendition: The created rendition, or None if creation failed
        """
        try:
            # Get rendition settings
            settings = self.default_renditions.get(role, {})
            if options:
                settings.update(options)

            # Default dimensions and mode
            width = settings.get('width', 150)
            height = settings.get('height', 150)
            mode = settings.get('mode', 'contain')

            # Download the original file
            source_path = self._download_original()
            if not source_path:
                return None

            try:
                # Process the image
                buffer, extension, mime_type = self._process_image(
                    source_path, width, height, mode, settings
                )

                # Generate output filename
                name, _ = os.path.splitext(self.file.storage_filename)
                filename = f"{name}_renditions/{role}{extension}"

                # Save to storage
                file_manager = self.file.file_manager
                backend = file_manager.backend
                storage_path = os.path.join(
                    os.path.dirname(self.file.storage_file_path),
                    filename
                )

                # Upload to storage
                # print(storage_path)
                backend.save(buffer, storage_path, mime_type)

                # Get file size
                file_size = buffer.getbuffer().nbytes

                # Create rendition record
                rendition = self._create_rendition_object(
                    role=role,
                    filename=filename,
                    storage_path=storage_path,
                    content_type=mime_type,
                    category='image',
                    file_size=file_size
                )

                return rendition
            finally:
                # Clean up temporary file
                if os.path.exists(source_path):
                    os.unlink(source_path)

        except Exception as e:
            logger.error(f"Failed to create image rendition '{role}': {str(e)}")
            return None
