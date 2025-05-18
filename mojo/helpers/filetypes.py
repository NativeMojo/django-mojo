EXTENSION_TO_MIME = {
    # Documents
    '.pdf': 'application/pdf',
    '.doc': 'application/msword',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xls': 'application/vnd.ms-excel',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.ppt': 'application/vnd.ms-powerpoint',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.csv': 'text/csv',
    '.tsv': 'text/tab-separated-values',
    '.txt': 'text/plain',
    '.rtf': 'application/rtf',
    '.odt': 'application/vnd.oasis.opendocument.text',
    '.ods': 'application/vnd.oasis.opendocument.spreadsheet',
    '.md': 'text/markdown',
    '.epub': 'application/epub+zip',
    '.ics': 'text/calendar',

    # Images
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.bmp': 'image/bmp',
    '.webp': 'image/webp',
    '.tiff': 'image/tiff',
    '.svg': 'image/svg+xml',
    '.heic': 'image/heic',
    '.ico': 'image/vnd.microsoft.icon',

    # Audio
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.ogg': 'audio/ogg',
    '.m4a': 'audio/mp4',
    '.flac': 'audio/flac',

    # Video
    '.mp4': 'video/mp4',
    '.mov': 'video/quicktime',
    '.avi': 'video/x-msvideo',
    '.wmv': 'video/x-ms-wmv',
    '.webm': 'video/webm',
    '.mkv': 'video/x-matroska',

    # Archives
    '.zip': 'application/zip',
    '.tar': 'application/x-tar',
    '.gz': 'application/gzip',
    '.rar': 'application/vnd.rar',
    '.7z': 'application/x-7z-compressed',
    '.tar.gz': 'application/gzip',  # Special case
    '.tgz': 'application/gzip',
    '.bz2': 'application/x-bzip2',
    '.tar.bz2': 'application/x-bzip2',

    # Code / Data
    '.json': 'application/json',
    '.xml': 'application/xml',
    '.html': 'text/html',
    '.htm': 'text/html',
    '.css': 'text/css',
    '.js': 'application/javascript',
    '.py': 'text/x-python',
    '.java': 'text/x-java-source',
    '.c': 'text/x-c',
    '.cpp': 'text/x-c++',
    '.ts': 'application/typescript',
    '.yml': 'application/x-yaml',
    '.yaml': 'application/x-yaml',
    '.sql': 'application/sql',

    # Fonts
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.ttf': 'font/ttf',
    '.otf': 'font/otf',

    # Misc
    '.apk': 'application/vnd.android.package-archive',
    '.exe': 'application/vnd.microsoft.portable-executable',
    '.dmg': 'application/x-apple-diskimage',
    '.bat': 'application/x-bat',
    '.sh': 'application/x-sh',
    '.pdfa': 'application/pdf',
    '.webmanifest': 'application/manifest+json',
}

def parse_extension(filename):
    """
    Extract the extension from a filename, handling both single and double extensions,
    and return the extension along with the filename without the extension.

    Parameters:
    filename (str): The name of the file for which to extract the extension.

    Returns:
    tuple: A tuple containing the extracted extension (or an empty string if no valid
           extension is found) and the filename without the extension.
    """
    filename = filename.lower()
    parts = filename.rsplit('.', 2)  # Max 2 splits: base.name.ext1.ext2
    if len(parts) == 3:
        double_ext = f".{parts[-2]}.{parts[-1]}"
        if double_ext in EXTENSION_TO_MIME:
            return double_ext, '.'.join(parts[:-1])
    ext = f".{parts[-1]}" if '.' in filename else ''
    base_filename = parts[0] if len(parts) == 1 else '.'.join(parts[:-1])
    return ext, base_filename

def guess_type(filename):
    """
    Guess the MIME type of a file based on its extension.

    This function attempts to determine the MIME type of a file by examining
    its extension. It can handle both single and double extensions, such as
    ".tar.gz" or ".tar.bz2".

    Parameters:
    filename (str): The name of the file whose MIME type needs to be guessed.

    Returns:
    str: The guessed MIME type if found in EXTENSION_TO_MIME mapping, or
         'application/octet-stream' as a default fallback.
    """
    ext, _ = parse_extension(filename)
    return EXTENSION_TO_MIME.get(ext, 'application/octet-stream')
