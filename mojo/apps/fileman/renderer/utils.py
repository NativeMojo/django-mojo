import os
import mimetypes
import tempfile
import logging
import shutil
import hashlib
from typing import Dict, List, Tuple, Optional, Union, Any

logger = logging.getLogger(__name__)

# Additional MIME type mappings that might not be in the standard library
ADDITIONAL_MIME_TYPES = {
    '.webp': 'image/webp',
    '.heic': 'image/heic',
    '.heif': 'image/heif',
    '.webm': 'video/webm',
    '.mkv': 'video/x-matroska',
    '.m4a': 'audio/mp4',
    '.flac': 'audio/flac',
}

# Register additional MIME types
for ext, type_name in ADDITIONAL_MIME_TYPES.items():
    mimetypes.add_type(type_name, ext)

# Map of file categories by MIME type prefix
CATEGORY_MAP = {
    'image/': 'image',
    'video/': 'video',
    'audio/': 'audio',
    'text/': 'document',
    'application/pdf': 'pdf',
    'application/msword': 'document',
    'application/vnd.openxmlformats-officedocument.wordprocessingml': 'document',
    'application/vnd.ms-excel': 'spreadsheet',
    'application/vnd.openxmlformats-officedocument.spreadsheetml': 'spreadsheet',
    'application/vnd.ms-powerpoint': 'presentation',
    'application/vnd.openxmlformats-officedocument.presentationml': 'presentation',
    'application/zip': 'archive',
    'application/x-rar-compressed': 'archive',
    'application/x-7z-compressed': 'archive',
    'application/x-tar': 'archive',
    'application/gzip': 'archive',
}

def get_file_category(mime_type: str) -> str:
    """
    Determine the category of a file based on its MIME type.
    
    Args:
        mime_type: The MIME type of the file
        
    Returns:
        str: The category of the file (image, video, audio, document, etc.)
    """
    if not mime_type:
        return 'unknown'
    
    # Check for exact matches
    if mime_type in CATEGORY_MAP:
        return CATEGORY_MAP[mime_type]
    
    # Check for prefix matches
    for prefix, category in CATEGORY_MAP.items():
        if prefix.endswith('/'):
            if mime_type.startswith(prefix):
                return category
    
    # Default category
    return 'other'

def get_file_info(file_path: str) -> Dict[str, Any]:
    """
    Get information about a file
    
    Args:
        file_path: Path to the file
        
    Returns:
        Dict with file information (size, mime_type, category, etc.)
    """
    if not os.path.exists(file_path):
        return {
            'exists': False,
            'size': 0,
            'mime_type': None,
            'category': 'unknown',
        }
    
    file_size = os.path.getsize(file_path)
    mime_type, _ = mimetypes.guess_type(file_path)
    category = get_file_category(mime_type)
    
    return {
        'exists': True,
        'size': file_size,
        'mime_type': mime_type,
        'category': category,
        'extension': os.path.splitext(file_path)[1].lower(),
    }

def create_temp_directory() -> str:
    """
    Create a temporary directory for file processing
    
    Returns:
        str: Path to the temporary directory
    """
    return tempfile.mkdtemp()

def create_temp_file(suffix: str = '') -> str:
    """
    Create a temporary file for processing
    
    Args:
        suffix: Optional suffix for the temp file (e.g., '.jpg')
        
    Returns:
        str: Path to the temporary file
    """
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path

def cleanup_temp_files(paths: List[str]):
    """
    Clean up temporary files and directories
    
    Args:
        paths: List of paths to clean up
    """
    for path in paths:
        if not path:
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.unlink(path)
        except Exception as e:
            logger.warning(f"Failed to clean up temporary path {path}: {str(e)}")

def calculate_file_hash(file_path: str, algorithm: str = 'md5') -> str:
    """
    Calculate hash for a file
    
    Args:
        file_path: Path to the file
        algorithm: Hash algorithm to use (md5, sha1, sha256)
        
    Returns:
        str: Hex digest of the hash
    """
    if algorithm == 'md5':
        hash_obj = hashlib.md5()
    elif algorithm == 'sha1':
        hash_obj = hashlib.sha1()
    elif algorithm == 'sha256':
        hash_obj = hashlib.sha256()
    else:
        raise ValueError(f"Unsupported hash algorithm: {algorithm}")
    
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            hash_obj.update(chunk)
    
    return hash_obj.hexdigest()

def calculate_dimensions(original_width: int, original_height: int, 
                         target_width: int, target_height: int, 
                         mode: str = 'contain') -> Tuple[int, int]:
    """
    Calculate dimensions for resizing an image or video
    
    Args:
        original_width: Original width
        original_height: Original height
        target_width: Target width
        target_height: Target height
        mode: Resize mode ('contain', 'cover', 'stretch')
        
    Returns:
        Tuple[int, int]: (new_width, new_height)
    """
    if mode == 'stretch':
        return target_width, target_height
    
    # Calculate aspect ratios
    original_ratio = original_width / original_height
    target_ratio = target_width / target_height
    
    if mode == 'contain':
        # Fit within target dimensions while maintaining aspect ratio
        if original_ratio > target_ratio:
            # Width is the limiting factor
            new_width = target_width
            new_height = int(new_width / original_ratio)
        else:
            # Height is the limiting factor
            new_height = target_height
            new_width = int(new_height * original_ratio)
    else:  # cover
        # Fill target dimensions while maintaining aspect ratio
        if original_ratio > target_ratio:
            # Height is the limiting factor
            new_height = target_height
            new_width = int(new_height * original_ratio)
        else:
            # Width is the limiting factor
            new_width = target_width
            new_height = int(new_width / original_ratio)
    
    return new_width, new_height

def get_video_duration(video_path: str) -> Optional[float]:
    """
    Get the duration of a video file in seconds
    
    Args:
        video_path: Path to the video file
        
    Returns:
        float: Duration in seconds, or None if duration couldn't be determined
    """
    try:
        import subprocess
        
        # Use ffprobe to get video duration
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        
        result = subprocess.run(cmd, 
                               stdout=subprocess.PIPE, 
                               stderr=subprocess.PIPE, 
                               text=True, 
                               check=True)
        
        duration = float(result.stdout.strip())
        return duration
    except Exception as e:
        logger.error(f"Failed to get video duration: {str(e)}")
        return None

def get_audio_duration(audio_path: str) -> Optional[float]:
    """
    Get the duration of an audio file in seconds
    
    Args:
        audio_path: Path to the audio file
        
    Returns:
        float: Duration in seconds, or None if duration couldn't be determined
    """
    return get_video_duration(audio_path)  # Use the same ffprobe method

def get_image_dimensions(image_path: str) -> Optional[Tuple[int, int]]:
    """
    Get dimensions of an image
    
    Args:
        image_path: Path to the image file
        
    Returns:
        Tuple[int, int]: (width, height), or None if dimensions couldn't be determined
    """
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            return img.size
    except Exception as e:
        logger.error(f"Failed to get image dimensions: {str(e)}")
        return None

def format_filesize(size_in_bytes: int) -> str:
    """
    Format a file size in bytes to a human-readable string
    
    Args:
        size_in_bytes: Size in bytes
        
    Returns:
        str: Human-readable file size (e.g., "1.2 MB")
    """
    if size_in_bytes < 1024:
        return f"{size_in_bytes} B"
    
    for unit in ['KB', 'MB', 'GB', 'TB', 'PB']:
        size_in_bytes /= 1024.0
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes:.1f} {unit}"
    
    return f"{size_in_bytes:.1f} PB"