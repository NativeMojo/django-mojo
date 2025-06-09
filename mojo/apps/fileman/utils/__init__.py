# File upload utility functions

# from .upload import (
#     get_file_manager,
#     validate_file_request,
#     initiate_upload,
#     finalize_upload,
#     direct_upload,
#     get_download_url
# )

def get_file_category(content_type: str) -> str:
    if not content_type:
        return "unknown"

    content_type = content_type.lower()

    if content_type.startswith("image/"):
        return "image"
    elif content_type.startswith("video/"):
        return "video"
    elif content_type.startswith("audio/"):
        return "audio"
    elif content_type == "application/pdf":
        return "pdf"
    elif content_type in ["text/csv", "application/csv"]:
        return "csv"
    elif content_type in [
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ]:
        return "spreadsheet"
    elif content_type in [
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ]:
        return "document"
    elif content_type in ["application/zip", "application/x-zip-compressed"]:
        return "archive"
    elif content_type.startswith("text/"):
        return "text"
    else:
        return "other"
