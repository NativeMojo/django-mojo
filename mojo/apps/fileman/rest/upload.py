from mojo import decorators as md
from mojo import JsonResponse
import mojo.errors
import io
from mojo.apps.fileman.models import File, FileManager
from mojo.apps.fileman.utils.upload import get_download_url


class RawUploadFile:
    """
    Wraps a raw request body (bytes) to expose the same interface that
    direct_upload and the storage backends expect from a Django UploadedFile:
      .size         — byte length
      .content_type — MIME type from the Content-Type request header
      .read(n)      — standard file read
      .seek(pos)    — standard file seek
      .chunks()     — generator yielding chunks (matches UploadedFile.chunks)
    """
    CHUNK_SIZE = 65536

    def __init__(self, body, content_type='application/octet-stream'):
        self._buf = io.BytesIO(body)
        self.size = len(body)
        self.content_type = content_type

    def read(self, size=-1):
        if size == -1:
            return self._buf.read()
        return self._buf.read(size)

    def seek(self, pos):
        return self._buf.seek(pos)

    def chunks(self, chunk_size=None):
        chunk_size = chunk_size or self.CHUNK_SIZE
        self._buf.seek(0)
        while True:
            chunk = self._buf.read(chunk_size)
            if not chunk:
                break
            yield chunk


@md.POST('upload/initiate')
@md.requires_auth()
def on_upload_initiate(request):
    """
    Initiate a file upload and get upload URLs

    Request body format:
    {
        "files": [
            {
                "filename": "document.pdf",
                "content_type": "application/pdf",
                "file_size": 1024000
            }
        ],
        "file_manager": 123,  // optional
        "group": 456,  // optional
        "user": 789,  // optional
        "metadata": {  // optional global metadata
            "source": "web_upload",
            "category": "documents"
        }
    }
    """
    # first we need to get the correct file manager
    file_manager = FileManager.get_from_request(request)
    if file_manager is None:
        raise mojo.errors.ValueException("No file manager found")
    # new lets create a new file
    file = File(
        filename=request.DATA['filename'],
        content_type=request.DATA['content_type'],
        file_size=request.DATA['file_size'],
        file_manager=file_manager,
        group=file_manager.group,
        user=request.user)
    file.on_rest_pre_save({}, True)
    file.mark_as_uploading()
    file.save()
    file.request_upload_url()
    return file.on_rest_get(request, "upload")


@md.POST('upload/<str:upload_token>')
@md.PUT('upload/<str:upload_token>')
@md.custom_security("requires upload token")
def on_direct_upload(request, upload_token):
    """
    Handle direct file upload.

    Supports two upload styles:
      PUT  — raw body upload, matching the presigned S3 PUT pattern.
             The file bytes are the entire request body; Content-Type header
             carries the MIME type.  No multipart encoding.
      POST — multipart/form-data upload (browser forms, SDK fallback).
             The file must be in the 'file' field of request.FILES.
    """
    from mojo.apps.fileman.utils.upload import direct_upload

    if request.method == 'PUT':
        body = request.body
        if not body:
            return JsonResponse({
                'success': False,
                'error': 'No file body provided'
            }, status=400)
        content_type = request.META.get('CONTENT_TYPE', 'application/octet-stream')
        # Strip any parameters (e.g. "application/pdf; charset=utf-8")
        content_type = content_type.split(';')[0].strip()
        file_data = RawUploadFile(body, content_type)
    else:
        if not request.FILES or 'file' not in request.FILES:
            return JsonResponse({
                'success': False,
                'error': 'No file provided'
            }, status=400)
        file_data = request.FILES['file']

    response_data = direct_upload(request, upload_token, file_data)
    status_code = response_data.pop('status_code', 200)
    return JsonResponse(response_data, status=status_code)


@md.GET('download/<str:download_token>')
@md.custom_security("requires download token")
def on_download(request, download_token):
    """
    Get a download URL for a file
    """
    response_data = get_download_url(request, download_token)

    # If direct URL is available, redirect to it
    if response_data.get('success') and 'download_url' in response_data:
        return JsonResponse({
            'success': True,
            'download_url': response_data['download_url'],
            'file': response_data.get('file', {})
        })

    status_code = response_data.pop('status_code', 200)
    return JsonResponse(response_data, status=status_code)
