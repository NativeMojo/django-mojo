from mojo import decorators as md
# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings
from mojo.helpers import sysinfo
import mojo
import django

@md.GET('version')
@md.public_endpoint()
def rest_version(request):
    return JsonResponse(dict(status=True, version=settings.VERSION, ip=request.ip))


@md.GET('versions')
@md.public_endpoint()
def rest_versions(request):
    import sys
    return JsonResponse(dict(status=True, version={
        "mojo": mojo.__version__,
        "project": settings.VERSION,
        "django": django.__version__,
        "python": sys.version.split(' ')[0]
    }))


@md.GET('myip')
@md.public_endpoint()
def rest_my_ip(request):
    return JsonResponse(dict(status=True, ip=request.ip))


@md.GET('myip/sources')
@md.public_endpoint()
def rest_my_ip_detailed(request):
    from mojo.helpers import request as rh
    return JsonResponse(dict(status=True, **rh.get_ip_sources(request)))


@md.GET('sysinfo/detailed')
@md.custom_security("Secured by required 'key' parameter")
@md.requires_params("key")
def rest_sysinfo_detailed(request):
    return JsonResponse(dict(status=True, data=sysinfo.get_host_info()))


@md.GET('sysinfo/network/tcp/summary')
@md.custom_security("Secured by required 'key' parameter")
@md.requires_params("key")
def rest_sysinfo(request):
    return JsonResponse(dict(status=True, data=sysinfo.get_tcp_established_summary()))
