import os
import subprocess
import io
import logging
import mimetypes
import tempfile
from typing import Dict, Optional, Tuple, Union, BinaryIO, List
import shutil
from PIL import Image

from mojo.apps.fileman.models import File, FileRendition
from mojo.apps.fileman.renderer.base import BaseRenderer, RenditionRole
from mojo.apps.fileman.renderer.utils import get_audio_duration

logger = logging.getLogger(__name__)

class AudioRenderer(BaseRenderer):
    """
    Renderer for audio files
    
    Creates various renditions like thumbnails, previews, and different formats
    """
    
    # Audio file categories
    supported_categories = ['audio']
    
    # Default rendition definitions with options
    default_renditions = {
        RenditionRole.AUDIO_THUMBNAIL: {
            'width': 300, 
            'height': 300,
            'format': 'jpg',
            'waveform': False,  # If true, generates a waveform image instead of using embedded artwork
        },
        RenditionRole.THUMBNAIL: {
            'width': 200, 
            'height': 200,
            'format': 'jpg',
            'waveform': False,
        },
        RenditionRole.AUDIO_PREVIEW: {
            'bitrate': '128k',
            'format': 'mp3',
            'duration': 30,  # First 30 seconds as preview
        },
        RenditionRole.AUDIO_MP3: {
            'bitrate': '192k',
            'format': 'mp3',
        },
    }
    
    def __init__(self, file: File):
        super().__init__(file)
        # Check if ffmpeg is available
        self._check_ffmpeg()
    
    def _check_ffmpeg(self):
        """Check if ffmpeg is available in the system"""
        try:
            subprocess.run(["ffmpeg", "-version"], 
                           stdout=subprocess.PIPE, 
                           stderr=subprocess.PIPE, 
                           check=True)
        except (subprocess.SubprocessError, FileNotFoundError):
            logger.warning("ffmpeg is not available. Audio rendering may not work properly.")
    
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
            with open(temp_path, 'wb') as f:
                backend.download(self.file.storage_file_path, f)
            
            return temp_path
        except Exception as e:
            logger.error(f"Failed to download original audio file: {str(e)}")
            return None
    
    def _extract_audio_cover(self, source_path: str, width: int, height: int, 
                           output_format: str) -> Tuple[str, str, int]:
        """
        Extract album artwork from audio file
        
        Args:
            source_path: Path to the source audio
            width: Target width
            height: Target height
            output_format: Output format (jpg, png)
            
        Returns:
            Tuple[str, str, int]: (Output path, mime type, file size) or (None, None, 0) if extraction failed
        """
        temp_output = self.get_temp_path(f".{output_format}")
        
        try:
            # Use ffmpeg to extract album art
            cmd = [
                "ffmpeg",
                "-y",  # Overwrite output files
                "-i", source_path,  # Input file
                "-an",  # No audio
                "-vcodec", "copy",  # Copy video codec (album art)
                temp_output  # Output file
            ]
            
            # Run command, but don't fail if artwork doesn't exist
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # Check if the output file exists and has content
            if os.path.exists(temp_output) and os.path.getsize(temp_output) > 0:
                # Resize the image to the requested dimensions
                from PIL import Image
                with Image.open(temp_output) as img:
                    img.thumbnail((width, height), Image.Resampling.LANCZOS)
                    img.save(temp_output, format=output_format.upper())
                
                # Get file size
                file_size = os.path.getsize(temp_output)
                
                # Get mime type
                mime_type = mimetypes.guess_type(f"file.{output_format}")[0]
                
                return temp_output, mime_type, file_size
            else:
                # If no artwork, create a default audio thumbnail
                return self._create_default_audio_thumbnail(width, height, output_format)
            
        except Exception as e:
            logger.error(f"Failed to extract audio cover: {str(e)}")
            return self._create_default_audio_thumbnail(width, height, output_format)
    
    def _create_default_audio_thumbnail(self, width: int, height: int, 
                                      output_format: str) -> Tuple[str, str, int]:
        """
        Create a default audio thumbnail when no album art is available
        
        Args:
            width: Target width
            height: Target height
            output_format: Output format (jpg, png)
            
        Returns:
            Tuple[str, str, int]: (Output path, mime type, file size)
        """
        temp_output = self.get_temp_path(f".{output_format}")
        
        try:
            # Create a simple gradient image with a music note icon
            from PIL import Image, ImageDraw, ImageFont
            
            # Create a gradient background
            img = Image.new('RGB', (width, height), color=(60, 60, 60))
            draw = ImageDraw.Draw(img)
            
            # Draw a music note icon or text
            try:
                # Draw a circle in the center
                circle_x, circle_y = width // 2, height // 2
                circle_radius = min(width, height) // 4
                draw.ellipse(
                    (circle_x - circle_radius, circle_y - circle_radius,
                     circle_x + circle_radius, circle_y + circle_radius),
                    fill=(100, 100, 100)
                )
                
                # Draw audio file name
                name, _ = os.path.splitext(self.file.filename)
                draw.text((width//2, height//2), name, fill=(220, 220, 220),
                          anchor="mm")
            except Exception:
                # If text drawing fails, just use the background
                pass
            
            # Save the image
            img.save(temp_output, format=output_format.upper())
            
            # Get file size
            file_size = os.path.getsize(temp_output)
            
            # Get mime type
            mime_type = mimetypes.guess_type(f"file.{output_format}")[0]
            
            return temp_output, mime_type, file_size
            
        except Exception as e:
            logger.error(f"Failed to create default audio thumbnail: {str(e)}")
            # Create a very basic fallback
            img = Image.new('RGB', (width, height), color=(80, 80, 80))
            img.save(temp_output, format=output_format.upper())
            file_size = os.path.getsize(temp_output)
            mime_type = mimetypes.guess_type(f"file.{output_format}")[0]
            return temp_output, mime_type, file_size
    
    def _create_waveform_image(self, source_path: str, width: int, height: int, 
                             output_format: str) -> Tuple[str, str, int]:
        """
        Create a waveform visualization of the audio file
        
        Args:
            source_path: Path to the source audio
            width: Target width
            height: Target height
            output_format: Output format (jpg, png)
            
        Returns:
            Tuple[str, str, int]: (Output path, mime type, file size)
        """
        temp_output = self.get_temp_path(f".{output_format}")
        
        try:
            # Use ffmpeg to generate a waveform image
            cmd = [
                "ffmpeg",
                "-y",  # Overwrite output files
                "-i", source_path,  # Input file
                "-filter_complex", f"showwavespic=s={width}x{height}:colors=white",
                "-frames:v", "1",
                temp_output  # Output file
            ]
            
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # Get file size
            file_size = os.path.getsize(temp_output)
            
            # Get mime type
            mime_type = mimetypes.guess_type(f"file.{output_format}")[0]
            
            return temp_output, mime_type, file_size
            
        except subprocess.SubprocessError as e:
            logger.error(f"Failed to create audio waveform: {str(e)}")
            return self._create_default_audio_thumbnail(width, height, output_format)
    
    def _convert_audio(self, source_path: str, options: Dict) -> Tuple[str, str, int]:
        """
        Convert audio to different format with specified options
        
        Args:
            source_path: Path to the source audio
            options: Conversion options
            
        Returns:
            Tuple[str, str, int]: (Output path, mime type, file size)
        """
        output_format = options.get('format', 'mp3')
        bitrate = options.get('bitrate', '192k')
        duration = options.get('duration')  # Optional duration limit in seconds
        
        temp_output = self.get_temp_path(f".{output_format}")
        
        try:
            # Build ffmpeg command
            cmd = [
                "ffmpeg",
                "-y",  # Overwrite output files
                "-i", source_path,  # Input file
            ]
            
            # Add duration limit if specified
            if duration:
                cmd.extend(["-t", str(duration)])
            
            # Audio settings
            cmd.extend([
                "-b:a", bitrate,
                "-ar", "44100",  # Sample rate
            ])
            
            # Specific format settings
            if output_format == 'mp3':
                cmd.extend(["-c:a", "libmp3lame"])
            elif output_format == 'ogg':
                cmd.extend(["-c:a", "libvorbis"])
            elif output_format == 'aac' or output_format == 'm4a':
                cmd.extend(["-c:a", "aac"])
            
            # Add output file
            cmd.append(temp_output)
            
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # Get file size
            file_size = os.path.getsize(temp_output)
            
            # Get mime type
            mime_type = mimetypes.guess_type(f"file.{output_format}")[0]
            
            return temp_output, mime_type, file_size
            
        except subprocess.SubprocessError as e:
            logger.error(f"Failed to convert audio: {str(e)}")
            if os.path.exists(temp_output):
                os.unlink(temp_output)
            raise
    
    def create_rendition(self, role: str, options: Dict = None) -> Optional[FileRendition]:
        """
        Create an audio rendition for the specified role
        
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
            
            # Download the original file
            source_path = self._download_original()
            if not source_path:
                return None
            
            try:
                temp_output = None
                mime_type = None
                file_size = None
                
                # Process based on role type
                if role in [RenditionRole.THUMBNAIL, RenditionRole.AUDIO_THUMBNAIL]:
                    # Create thumbnail image (either from album art or waveform)
                    width = settings.get('width', 300)
                    height = settings.get('height', 300)
                    output_format = settings.get('format', 'jpg')
                    use_waveform = settings.get('waveform', False)
                    
                    if use_waveform:
                        temp_output, mime_type, file_size = self._create_waveform_image(
                            source_path, width, height, output_format
                        )
                    else:
                        temp_output, mime_type, file_size = self._extract_audio_cover(
                            source_path, width, height, output_format
                        )
                    
                    # Set filename
                    name, _ = os.path.splitext(self.file.filename)
                    filename = f"{name}_{role}.{output_format}"
                    category = 'image'  # Thumbnails are images
                    
                else:
                    # Create audio rendition
                    temp_output, mime_type, file_size = self._convert_audio(
                        source_path, settings
                    )
                    
                    # Set filename
                    name, _ = os.path.splitext(self.file.filename)
                    output_format = settings.get('format', 'mp3')
                    filename = f"{name}_{role}.{output_format}"
                    category = 'audio'
                
                # Save to storage
                file_manager = self.file.file_manager
                backend = file_manager.backend
                storage_path = os.path.join(
                    os.path.dirname(self.file.storage_file_path),
                    filename
                )
                
                # Upload to storage
                with open(temp_output, 'rb') as f:
                    backend.save(f, storage_path, mime_type)
                
                # Create rendition record
                rendition = self._create_rendition_object(
                    role=role,
                    filename=filename,
                    storage_path=storage_path,
                    content_type=mime_type,
                    category=category,
                    file_size=file_size
                )
                
                return rendition
                
            finally:
                # Clean up temporary files
                if source_path and os.path.exists(source_path):
                    os.unlink(source_path)
                if temp_output and os.path.exists(temp_output):
                    os.unlink(temp_output)
                    
        except Exception as e:
            logger.error(f"Failed to create audio rendition '{role}': {str(e)}")
            return None