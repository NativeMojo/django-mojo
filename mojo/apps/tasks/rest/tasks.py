from mojo import decorators as jd
from django.http import JsonResponse
import mojo.tasks

@jd.URL('status')
def api_status(request):
    tman = mojo.tasks.get_manager()
    return JsonResponse(tman.get_status())

@jd.URL('pending')
def api_pending(request):
    tman = mojo.tasks.get_manager()
    pending = tman.get_all_pending()
    size = len(pending)
    response = {
        'count': size,
        'page': 0,
        'size': size,
        'data': pending
    }
    return JsonResponse(response)

@jd.URL('completed')
def api_completed(request):
    tman = mojo.tasks.get_manager()
    completed = tman.get_all_completed()
    size = len(completed)
    response = {
        'count': size,
        'page': 0,
        'size': size,
        'data': completed
    }
    return JsonResponse(response)

@jd.URL('running')
def api_running(request):
    tman = mojo.tasks.get_manager()
    running = tman.get_all_running()
    size = len(running)
    response = {
        'count': size,
        'page': 0,
        'size': size,
        'data': running
    }
    return JsonResponse(response)


@jd.URL('errors')
def api_errors(request):
    tman = mojo.tasks.get_manager()
    errors = tman.get_all_errors()
    size = len(errors)
    response = {
        'count': size,
        'page': 0,
        'size': size,
        'data': errors
    }
    return JsonResponse(response)
