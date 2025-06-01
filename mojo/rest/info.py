from mojo import decorators as md
# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings
import mojo
import django

@md.GET('version')
def rest_version(request):
    return JsonResponse(dict(status=True, version=settings.VERSION, ip=request.ip))


@md.GET('versions')
def rest_versions(request):
    import sys
    return JsonResponse(dict(status=True, version={
        "mojo": mojo.__version__,
        "project": settings.VERSION,
        "django": django.__version__,
        "python": sys.version.split(' ')[0]
    }))


@md.GET('myip')
def rest_my_ip(request):
    return JsonResponse(dict(status=True, ip=request.ip))
