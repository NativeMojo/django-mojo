import os
import io
import subprocess
import tempfile
import mimetypes
import shutil
from typing import Dict, Optional, Tuple, Union, BinaryIO, List

from mojo.apps.fileman.models import File, FileRendition
from mojo.apps.fileman.renderer.base import BaseRenderer, RenditionRole
from mojo.apps.fileman.renderer.process import RendererProcessError, run as run_process
from mojo.helpers import logit

logger = logit.get_logger(__name__, "fileman.log")


def _fit_filter(width, height):
    """Scale into a WxH bounding box: keep aspect ratio, never upscale (the
    same semantics as the image renderer's "contain"), and round to even
    dimensions, which libx264/libx265 require."""
    return (
        "scale='min(iw,%d)':'min(ih,%d)':force_original_aspect_ratio=decrease,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2" % (width, height)
    )


def build_thumbnail_args(source_path, output_path, options):
    """ffmpeg argv for one still frame. Pure: no I/O, so tests can assert it."""
    width = options.get('width', 300)
    height = options.get('height', 169)
    time_offset = options.get('time_offset', '00:00:03')
    return [
        "ffmpeg",
        "-y",  # Overwrite output files
        "-ss", time_offset,  # Seek to time offset
        "-i", source_path,  # Input file
        "-vframes", "1",  # Extract one frame
        "-vf", _fit_filter(width, height),
        "-f", "image2",  # Force image2 format
        output_path,
    ]


def build_transcode_args(source_path, output_path, options):
    """ffmpeg argv for a transcode. Pure: no I/O, so tests can assert it.

    `format` wins: webm is always VP8 at the given bitrate; mp4 honors
    `codec` — h264 (libx264, bitrate-driven, today's behavior) or h265
    (libx265, CRF-driven; `bitrate` is ignored because mixing -b:v and -crf
    under x265 degrades to ABR).
    """
    width = options.get('width', 1280)
    height = options.get('height', 720)
    bitrate = options.get('bitrate', '2000k')
    output_format = options.get('format', 'mp4')
    codec = options.get('codec', 'h264')
    duration = options.get('duration')  # Optional duration limit in seconds
    audio = options.get('audio', True)

    cmd = [
        "ffmpeg",
        "-y",  # Overwrite output files
        "-i", source_path,  # Input file
    ]
    if duration:
        cmd.extend(["-t", str(duration)])
    cmd.extend(["-vf", _fit_filter(width, height)])

    if output_format == "webm":
        cmd.extend(["-c:v", "libvpx", "-b:v", bitrate])
    elif codec == "h265":
        cmd.extend([
            "-c:v", "libx265",
            "-tag:v", "hvc1",  # the tag Apple players require for HEVC in mp4
            "-pix_fmt", "yuv420p",
            "-crf", str(options.get('crf', 28)),
            "-preset", options.get('preset', 'medium'),
        ])
    else:
        cmd.extend(["-c:v", "libx264", "-b:v", bitrate])

    if audio:
        if output_format == "mp4":
            cmd.extend(["-c:a", "aac", "-b:a", "128k"])
        else:  # webm
            cmd.extend(["-c:a", "libvorbis", "-b:a", "128k"])
    else:
        cmd.extend(["-an"])  # No audio

    cmd.append(output_path)
    return cmd


class VideoRenderer(BaseRenderer):
    """
    Renderer for video files
    
    Creates various renditions like thumbnails, previews, and different formats using ffmpeg
    """
    
    # Video file categories
    supported_categories = ['video']
    config_category = 'video'

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
            'codec': 'h264',
            'audio': True,
        },
        # The main playable rendition is H.265/HEVC in mp4: about half the
        # bytes of H.264 at the same quality, tagged hvc1 for Safari and
        # played by Chrome/Edge with hardware decode. Firefox is not a
        # supported player. A deployment that needs H.264 sets
        # {"video_mp4": {"codec": "h264", "bitrate": "2000k"}}.
        RenditionRole.VIDEO_MP4: {
            'width': 1280,
            'height': 720,
            'format': 'mp4',
            'codec': 'h265',
            'crf': 28,
            'preset': 'medium',
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

    # The full transcode runs on upload again: the jobs engine caps rendition
    # jobs to one at a time per engine (JOBS_CHANNEL_LIMITS), which was the
    # reason it was ever opt-in. WebM stays declared but opt-in.
    automatic_rendition_roles = (
        RenditionRole.VIDEO_THUMBNAIL,
        RenditionRole.THUMBNAIL,
        RenditionRole.VIDEO_PREVIEW,
        RenditionRole.VIDEO_MP4,
    )
    
    def __init__(self, file: File):
        super().__init__(file)
        # Check if ffmpeg is available
        self._check_ffmpeg()
    
    def _check_ffmpeg(self):
        """Check if ffmpeg is available in the system"""
        try:
            run_process(["ffmpeg", "-version"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=True)
        except RendererProcessError:
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
            
            # Download file from storage (backend.download writes to a path)
            backend.download(self.file.storage_file_path, temp_path)

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
            cmd = build_thumbnail_args(source_path, temp_output, {
                'width': width, 'height': height, 'time_offset': time_offset,
            })

            run_process(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
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
        output_format = options.get('format', 'mp4')

        temp_output = self.get_temp_path(f".{output_format}")

        try:
            cmd = build_transcode_args(source_path, temp_output, options)

            run_process(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
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
            # Get rendition settings (class defaults + admin override)
            try:
                settings = self.get_rendition_options(role)
            except ValueError:
                logger.warning(f"Unsupported rendition role for videos: {role}")
                return None
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
                    
        except RendererProcessError as e:
            self.record_failure(role, e)
            logger.error("Video rendition '%s' failed: %s", role, str(e))
            return None
        except Exception as e:
            logger.error(f"Failed to create video rendition '{role}': {str(e)}")
            return None
