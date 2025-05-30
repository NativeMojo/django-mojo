from mojo import decorators as jd
from django.http import JsonResponse
from mojo.helpers.settings import settings
import mojo
import django

@jd.GET('version')
def rest_version(request):
    return JsonResponse(dict(status=True, version=settings.VERSION, ip=request.ip))


@jd.GET('versions')
def rest_versions(request):
    import sys
    return JsonResponse(dict(status=True, version={
        "mojo": mojo.__version__,
        "project": settings.VERSION,
        "django": django.__version__,
        "python": sys.version.split(' ')[0]
    }))


@jd.GET('myip')
def rest_my_ip(request):
    return JsonResponse(dict(status=True, ip=request.ip))
