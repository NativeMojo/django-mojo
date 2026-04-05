"""Files domain tools — query files, get metadata, analyze images."""
import base64

from mojo.apps.assistant import tool
from mojo.helpers import logit

logger = logit.get_logger(__name__, "assistant.log")

MAX_RESULTS = 50
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB limit for image analysis


@tool(
    name="query_files",
    domain="files",
    permission="view_fileman",
    description="Search and list uploaded files. Filter by category, content type, filename, or group. Returns up to 50 files.",
    input_schema={
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Filter by category (image, document, video, audio, etc.)"},
            "content_type": {"type": "string", "description": "Filter by MIME type (partial match, e.g. 'image/png')"},
            "filename": {"type": "string", "description": "Filter by filename (partial match)"},
            "group_id": {"type": "integer", "description": "Filter by group ID"},
            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
        },
    },
)
def _tool_query_files(params, user):
    from mojo.apps.fileman.models import File

    criteria = {"is_active": True, "upload_status": "completed"}
    if params.get("category"):
        criteria["category"] = params["category"]
    if params.get("content_type"):
        criteria["content_type__icontains"] = params["content_type"]
    if params.get("filename"):
        criteria["filename__icontains"] = params["filename"]
    if params.get("group_id"):
        criteria["group_id"] = params["group_id"]

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    files = File.objects.filter(**criteria).order_by("-created")[:limit]

    return [
        {
            "id": f.pk,
            "filename": f.filename,
            "content_type": f.content_type,
            "category": f.category,
            "file_size": f.file_size,
            "created": str(f.created),
            "group_id": f.group_id,
            "user_id": f.user_id,
            "is_public": f.is_public,
        }
        for f in files
    ]


@tool(
    name="get_file",
    domain="files",
    permission="view_fileman",
    description="Get detailed metadata for a specific file by ID, including storage info, checksums, and group/user ownership.",
    input_schema={
        "type": "object",
        "properties": {
            "file_id": {"type": "integer", "description": "The file ID"},
        },
        "required": ["file_id"],
    },
)
def _tool_get_file(params, user):
    from mojo.apps.fileman.models import File

    file_id = params["file_id"]
    try:
        f = File.objects.select_related("file_manager", "group", "user").get(pk=file_id)
    except File.DoesNotExist:
        return {"error": f"File {file_id} not found"}

    result = {
        "id": f.pk,
        "filename": f.filename,
        "content_type": f.content_type,
        "category": f.category,
        "file_size": f.file_size,
        "upload_status": f.upload_status,
        "is_active": f.is_active,
        "is_public": f.is_public,
        "checksum": f.checksum,
        "metadata": f.metadata or {},
        "created": str(f.created),
        "modified": str(f.modified),
        "group_id": f.group_id,
        "user_id": f.user_id,
    }
    if f.group:
        result["group_name"] = f.group.name
    if f.user:
        result["user_email"] = f.user.email

    return result


@tool(
    name="analyze_image",
    domain="files",
    permission="view_fileman",
    description="Analyze an uploaded image using Claude vision. Describe contents, detect text, identify objects, etc. Only works on image files under 20 MB. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "file_id": {"type": "integer", "description": "The file ID (must be an image)"},
            "prompt": {"type": "string", "description": "What to analyze (default: 'Describe this image in detail.')", "default": "Describe this image in detail."},
        },
        "required": ["file_id"],
    },
)
def _tool_analyze_image(params, user):
    from mojo.apps.fileman.models import File
    from mojo.helpers import llm

    file_id = params["file_id"]
    try:
        f = File.objects.select_related("file_manager").get(pk=file_id)
    except File.DoesNotExist:
        return {"error": f"File {file_id} not found"}

    if f.category != "image":
        return {"error": f"File {file_id} is not an image (category: {f.category})"}

    if f.upload_status != "completed":
        return {"error": f"File {file_id} upload is not completed (status: {f.upload_status})"}

    if f.file_size and f.file_size > MAX_IMAGE_BYTES:
        return {"error": f"Image too large ({f.file_size} bytes). Max is {MAX_IMAGE_BYTES} bytes."}

    # Read image bytes from storage backend
    try:
        fh = f.file_manager.backend.open(f.storage_file_path, "rb")
        image_bytes = fh.read()
        if hasattr(fh, "close"):
            fh.close()
    except Exception as e:
        logger.error("Failed to read image file %s: %s", file_id, str(e)[:200])
        return {"error": "Failed to read image from storage"}

    b64_data = base64.b64encode(image_bytes).decode("ascii")

    prompt = params.get("prompt", "Describe this image in detail.")

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": f.content_type,
                        "data": b64_data,
                    },
                },
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        }
    ]

    try:
        response = llm.call(messages, max_tokens=2048)
        parts = []
        for block in response.get("content", []):
            if block.get("type") == "text":
                parts.append(block["text"])
        analysis = "\n".join(parts)
    except Exception as e:
        logger.error("LLM image analysis failed for file %s: %s", file_id, str(e)[:200])
        return {"error": "Image analysis failed. Check LLM configuration."}

    return {
        "file_id": f.pk,
        "filename": f.filename,
        "analysis": analysis,
    }
