import os
import io
import subprocess
import tempfile
import logging
import mimetypes
import shutil
from typing import Dict, Optional, Tuple, Union, BinaryIO, List

from mojo.apps.fileman.models import File, FileRendition
from mojo.apps.fileman.renderer.base import BaseRenderer, RenditionRole

logger = logging.getLogger(__name__)

class VideoRenderer(BaseRenderer):
    """
    Renderer for video files
    
    Creates various renditions like thumbnails, previews, and different formats using ffmpeg
    """
    
    # Video file categories
    supported_categories = ['video']
    
    # Default rendition definitions with options
    default_renditions = {
        RenditionRole.VIDEO_THUMBNAIL: {
            'width': 300, 
            'height': 169, 
            'time_offset': '00:00:03',
            'format': 'jpg'
        },
        RenditionRole.THUMBNAIL: {
            'width': 300, 
            'height': 169, 
            'time_offset': '00:00:03',
            'format': 'jpg'
        },
        RenditionRole.VIDEO_PREVIEW: {
            'width': 640,
            'height': 360,
            'bitrate': '500k',
            'duration': 10,
            'format': 'mp4',
            'audio': True,
        },
        RenditionRole.VIDEO_MP4: {
            'width': 1280,
            'height': 720,
            'bitrate': '2000k',
            'format': 'mp4',
            'audio': True,
        },
        RenditionRole.VIDEO_WEBM: {
            'width': 1280,
            'height': 720,
            'bitrate': '2000k',
            'format': 'webm',
            'audio': True,
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
            logger.warning("ffmpeg is not available. Video rendering may not work properly.")
    
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
            logger.error(f"Failed to download original video file: {str(e)}")
            return None
    
    def _create_thumbnail(self, source_path: str, width: int, height: int, 
                        time_offset: str, output_format: str) -> Tuple[str, str, int]:
        """
        Create a thumbnail from a video at specified time offset
        
        Args:
            source_path: Path to the source video
            width: Target width
            height: Target height
            time_offset: Time offset for thumbnail (format: HH:MM:SS)
            output_format: Output format (jpg, png)
            
        Returns:
            Tuple[str, str, int]: (Output path, mime type, file size)
        """
        temp_output = self.get_temp_path(f".{output_format}")
        
        try:
            # Use ffmpeg to extract a frame
            cmd = [
                "ffmpeg",
                "-y",  # Overwrite output files
                "-ss", time_offset,  # Seek to time offset
                "-i", source_path,  # Input file
                "-vframes", "1",  # Extract one frame
                "-s", f"{width}x{height}",  # Set size
                "-f", "image2",  # Force image2 format
                temp_output  # Output file
            ]
            
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # Get file size
            file_size = os.path.getsize(temp_output)
            
            # Get mime type
            mime_type = mimetypes.guess_type(f"file.{output_format}")[0]
            
            return temp_output, mime_type, file_size
            
        except subprocess.SubprocessError as e:
            logger.error(f"Failed to create video thumbnail: {str(e)}")
            if os.path.exists(temp_output):
                os.unlink(temp_output)
            raise
    
    def _create_video_rendition(self, source_path: str, options: Dict) -> Tuple[str, str, int]:
        """
        Create a video rendition with specified options
        
        Args:
            source_path: Path to the source video
            options: Video processing options
            
        Returns:
            Tuple[str, str, int]: (Output path, mime type, file size)
        """
        width = options.get('width', 1280)
        height = options.get('height', 720)
        bitrate = options.get('bitrate', '2000k')
        output_format = options.get('format', 'mp4')
        duration = options.get('duration')  # Optional duration limit in seconds
        audio = options.get('audio', True)
        
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
            
            # Video settings
            cmd.extend([
                "-vf", f"scale={width}:{height}",
                "-c:v", "libx264" if output_format == "mp4" else "libvpx",
                "-b:v", bitrate,
            ])
            
            # Audio settings
            if audio:
                if output_format == "mp4":
                    cmd.extend(["-c:a", "aac", "-b:a", "128k"])
                else:  # webm
                    cmd.extend(["-c:a", "libvorbis", "-b:a", "128k"])
            else:
                cmd.extend(["-an"])  # No audio
            
            # Add output file
            cmd.append(temp_output)
            
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # Get file size
            file_size = os.path.getsize(temp_output)
            
            # Get mime type
            mime_type = mimetypes.guess_type(f"file.{output_format}")[0]
            
            return temp_output, mime_type, file_size
            
        except subprocess.SubprocessError as e:
            logger.error(f"Failed to create video rendition: {str(e)}")
            if os.path.exists(temp_output):
                os.unlink(temp_output)
            raise
    
    def create_rendition(self, role: str, options: Dict = None) -> Optional[FileRendition]:
        """
        Create a video rendition for the specified role
        
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
                if role in [RenditionRole.THUMBNAIL, RenditionRole.VIDEO_THUMBNAIL]:
                    # Create thumbnail image
                    width = settings.get('width', 300)
                    height = settings.get('height', 169)
                    time_offset = settings.get('time_offset', '00:00:03')
                    output_format = settings.get('format', 'jpg')
                    
                    temp_output, mime_type, file_size = self._create_thumbnail(
                        source_path, width, height, time_offset, output_format
                    )
                    
                    # Set filename
                    name, _ = os.path.splitext(self.file.filename)
                    filename = f"{name}_{role}.{output_format}"
                    category = 'image'  # Thumbnails are images
                    
                else:
                    # Create video rendition
                    temp_output, mime_type, file_size = self._create_video_rendition(
                        source_path, settings
                    )
                    
                    # Set filename
                    name, _ = os.path.splitext(self.file.filename)
                    output_format = settings.get('format', 'mp4')
                    filename = f"{name}_{role}.{output_format}"
                    category = 'video'
                
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
            logger.error(f"Failed to create video rendition '{role}': {str(e)}")
            return None