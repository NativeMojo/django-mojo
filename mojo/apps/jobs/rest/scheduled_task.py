from mojo import decorators as md
from mojo.apps.jobs.models import ScheduledTask, TaskResult


@md.URL('scheduled_task')
@md.URL('scheduled_task/<str:pk>')
@md.uses_model_security(ScheduledTask)
def on_scheduled_task(request, pk=None):
    return ScheduledTask.on_rest_request(request, pk)


@md.URL('task_result')
@md.URL('task_result/<str:pk>')
@md.uses_model_security(TaskResult)
def on_task_result(request, pk=None):
    return TaskResult.on_rest_request(request, pk)
