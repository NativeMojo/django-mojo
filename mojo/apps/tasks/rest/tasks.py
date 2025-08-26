from mojo import decorators as md
from mojo.helpers.response import JsonResponse
# from django.http import JsonResponse
from mojo.apps import tasks

@md.GET('status')
def api_status(request):
    tman = tasks.get_manager()
    return JsonResponse(dict(status=True, data=tman.get_status()))


@md.GET('runners')
def api_task_runners(request):
    tman = tasks.get_manager()
    runners = [r for r in tman.get_active_runners().values()]
    for r in runners:
        r['id'] = r['hostname']
    return JsonResponse(dict(status=True, data=runners, size=len(runners), count=len(runners)))


@md.URL('pending')
def api_pending(request):
    tman = tasks.get_manager()
    pending = tman.get_all_pending()
    size = len(pending)
    response = {
        'status': True,
        'count': size,
        'page': 0,
        'size': size,
        'data': pending
    }
    return JsonResponse(response)

@md.URL('completed')
def api_completed(request):
    tman = tasks.get_manager()
    completed = tman.get_all_completed(include_data=True)
    size = len(completed)
    response = {
        'status': True,
        'count': size,
        'page': 0,
        'size': size,
        'data': completed
    }
    return JsonResponse(response)

@md.URL('running')
def api_running(request):
    tman = tasks.get_manager()
    running = tman.get_all_running(include_data=True)
    size = len(running)
    response = {
        'status': True,
        'count': size,
        'page': 0,
        'size': size,
        'data': running
    }
    return JsonResponse(response)


@md.URL('errors')
def api_errors(request):
    tman = tasks.get_manager()
    errors = tman.get_all_errors()
    size = len(errors)
    response = {
        'status': True,
        'count': size,
        'page': 0,
        'size': size,
        'data': errors
    }
    return JsonResponse(response)
