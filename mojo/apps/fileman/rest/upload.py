from mojo import decorators as md
from mojo import JsonResponse
from mojo.apps.fileman.models import File, FileManager
from mojo.apps.fileman.utils.upload import (
    initiate_upload,
    finalize_upload,
    direct_upload,
    get_download_url
)


@md.POST('upload/initiate')
def on_upload_initiate(request):
    """
    Initiate a file upload and get upload URLs

    Request body format:
    {
        "files": [
            {
                "filename": "document.pdf",
                "content_type": "application/pdf",
                "size": 1024000
            }
        ],
        "file_manager_id": 123,  // optional
        "group_id": 456,  // optional
        "metadata": {  // optional global metadata
            "source": "web_upload",
            "category": "documents"
        }
    }
    """
    response_data = initiate_upload(request, request.DATA)
    status_code = response_data.pop('status_code', 200)
    return JsonResponse(response_data, status=status_code)


@md.POST('upload/finalize')
def on_upload_finalize(request):
    """
    Finalize a file upload

    Request body format:
    {
        "upload_token": "abc123...",
        "file_size": 1024000,  // optional
        "checksum": "md5:abcdef...",  // optional
        "metadata": {  // optional additional metadata
            "processing_complete": true
        }
    }
    """
    response_data = finalize_upload(request, request.DATA)
    status_code = response_data.pop('status_code', 200)
    return JsonResponse(response_data, status=status_code)


@md.POST('upload/<str:upload_token>')
def on_direct_upload(request, upload_token):
    """
    Handle direct file upload for backends that don't support pre-signed URLs
    """
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
