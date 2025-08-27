from mojo import decorators as md
from mojo.helpers.response import JsonResponse
from mojo.models import rest
import mojo.errors
import datetime
from django.db.models import Q, Count
from django.utils import timezone
from datetime import timedelta

# Documentation for API endpoints
TASK_LOGS_LIST_DOCS = {
    "summary": "List task logs",
    "description": "Retrieves task logs with optional filtering by task_id, channel, status, event_type, and date range.",
    "parameters": [
        {
            "name": "task_id",
            "in": "query",
            "schema": {"type": "string"},
            "description": "Filter by specific task ID."
        },
        {
            "name": "task_function",
            "in": "query",
            "schema": {"type": "string"},
            "description": "Filter by task function name (supports partial match)."
        },
        {
            "name": "task_channel",
            "in": "query",
            "schema": {"type": "string"},
            "description": "Filter by task channel."
        },
        {
            "name": "event_type",
            "in": "query",
            "schema": {"type": "string", "enum": ["created", "status_change", "started", "completed", "error", "cancelled", "expired", "retry"]},
            "description": "Filter by event type."
        },
        {
            "name": "status",
            "in": "query",
            "schema": {"type": "string", "enum": ["pending", "running", "completed", "error", "cancelled", "expired"]},
            "description": "Filter by task status."
        },
        {
            "name": "runner_hostname",
            "in": "query",
            "schema": {"type": "string"},
            "description": "Filter by runner hostname."
        },
        {
            "name": "has_error",
            "in": "query",
            "schema": {"type": "boolean"},
            "description": "Filter logs that have error messages."
        },
        {
            "name": "created_after",
            "in": "query",
            "schema": {"type": "string", "format": "date-time"},
            "description": "Filter logs created after this datetime."
        },
        {
            "name": "created_before",
            "in": "query",
            "schema": {"type": "string", "format": "date-time"},
            "description": "Filter logs created before this datetime."
        },
        {
            "name": "hours",
            "in": "query",
            "schema": {"type": "integer", "default": 24},
            "description": "Filter logs from the last N hours."
        },
        {
            "name": "graph",
            "in": "query",
            "schema": {"type": "string", "enum": ["basic", "detailed", "with_data", "errors", "timeline"], "default": "basic"},
            "description": "Response graph/detail level."
        },
        {
            "name": "limit",
            "in": "query",
            "schema": {"type": "integer", "default": 100, "maximum": 1000},
            "description": "Maximum number of results to return."
        },
        {
            "name": "offset",
            "in": "query",
            "schema": {"type": "integer", "default": 0},
            "description": "Number of results to skip."
        }
    ],
    "responses": {
        "200": {
            "description": "Successful response with task logs.",
            "content": {
                "application/json": {
                    "example": {
                        "response": {
                            "data": [
                                {
                                    "id": 123,
                                    "task_id": "abc123def456",
                                    "event_type": "completed",
                                    "status": "completed",
                                    "created": "2025-01-01T12:00:00Z",
                                    "task_function": "module.my_task",
                                    "task_channel": "background"
                                }
                            ],
                            "count": 1,
                            "total": 1,
                            "status": True
                        },
                        "status_code": 200
                    }
                }
            }
        }
    }
}

TASK_TIMELINE_DOCS = {
    "summary": "Get task timeline",
    "description": "Retrieves chronological timeline of all events for a specific task.",
    "parameters": [
        {
            "name": "task_id",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
            "description": "Task ID to get timeline for."
        },
        {
            "name": "graph",
            "in": "query",
            "schema": {"type": "string", "enum": ["basic", "detailed", "timeline"], "default": "timeline"},
            "description": "Response graph/detail level."
        }
    ],
    "responses": {
        "200": {
            "description": "Successful response with task timeline.",
            "content": {
                "application/json": {
                    "example": {
                        "response": {
                            "task_id": "abc123def456",
                            "events": [
                                {
                                    "id": 123,
                                    "event_type": "created",
                                    "status": "pending",
                                    "created": "2025-01-01T12:00:00Z"
                                },
                                {
                                    "id": 124,
                                    "event_type": "status_change",
                                    "status": "running",
                                    "previous_status": "pending",
                                    "created": "2025-01-01T12:00:05Z"
                                }
                            ],
                            "status": True
                        },
                        "status_code": 200
                    }
                }
            }
        }
    }
}

ERROR_LOGS_DOCS = {
    "summary": "Get error task logs",
    "description": "Retrieves task logs for failed/error tasks with error details.",
    "parameters": [
        {
            "name": "hours",
            "in": "query",
            "schema": {"type": "integer", "default": 24},
            "description": "Get error logs from the last N hours."
        },
        {
            "name": "task_channel",
            "in": "query",
            "schema": {"type": "string"},
            "description": "Filter by task channel."
        },
        {
            "name": "runner_hostname",
            "in": "query",
            "schema": {"type": "string"},
            "description": "Filter by runner hostname."
        },
        {
            "name": "limit",
            "in": "query",
            "schema": {"type": "integer", "default": 100},
            "description": "Maximum number of results."
        }
    ],
    "responses": {
        "200": {
            "description": "Successful response with error logs.",
            "content": {
                "application/json": {
                    "example": {
                        "response": {
                            "data": [
                                {
                                    "id": 125,
                                    "task_id": "def456ghi789",
                                    "task_function": "module.failing_task",
                                    "error_message": "Division by zero",
                                    "created": "2025-01-01T12:05:00Z"
                                }
                            ],
                            "count": 1,
                            "status": True
                        },
                        "status_code": 200
                    }
                }
            }
        }
    }
}

TASK_STATS_DOCS = {
    "summary": "Get task statistics",
    "description": "Retrieves task execution statistics and metrics.",
    "parameters": [
        {
            "name": "hours",
            "in": "query",
            "schema": {"type": "integer", "default": 24},
            "description": "Get statistics from the last N hours."
        },
        {
            "name": "task_channel",
            "in": "query",
            "schema": {"type": "string"},
            "description": "Get statistics for specific channel."
        },
        {
            "name": "group_by",
            "in": "query",
            "schema": {"type": "string", "enum": ["channel", "function", "runner", "hour"]},
            "description": "Group statistics by field."
        }
    ],
    "responses": {
        "200": {
            "description": "Successful response with task statistics.",
            "content": {
                "application/json": {
                    "example": {
                        "response": {
                            "period_hours": 24,
                            "total_tasks": 150,
                            "total_events": 450,
                            "avg_duration_seconds": 3.42,
                            "status_breakdown": {
                                "completed": 140,
                                "error": 8,
                                "cancelled": 2
                            },
                            "channel_stats": {
                                "background": {
                                    "total_tasks": 100,
                                    "error_rate": 0.05
                                }
                            },
                            "status": True
                        },
                        "status_code": 200
                    }
                }
            }
        }
    }
}


@md.GET('logs', docs=TASK_LOGS_LIST_DOCS)
def on_task_logs_list(request):
    """
    List task logs with filtering and pagination.
    """
    from ..models import TaskLog

    # Build query filters
    queryset = TaskLog.objects.all()

    # Task filters
    if task_id := request.DATA.get('task_id'):
        queryset = queryset.filter(task_id=task_id)

    if task_function := request.DATA.get('task_function'):
        queryset = queryset.filter(task_function__icontains=task_function)

    if task_channel := request.DATA.get('task_channel'):
        queryset = queryset.filter(task_channel=task_channel)

    if event_type := request.DATA.get('event_type'):
        queryset = queryset.filter(event_type=event_type)

    if status := request.DATA.get('status'):
        queryset = queryset.filter(status=status)

    if runner_hostname := request.DATA.get('runner_hostname'):
        queryset = queryset.filter(runner_hostname=runner_hostname)

    if request.DATA.get('has_error'):
        queryset = queryset.filter(error_message__isnull=False)

    # Date range filters
    if created_after := request.DATA.get_typed('created_after', typed=datetime.datetime):
        queryset = queryset.filter(created__gte=created_after)

    if created_before := request.DATA.get_typed('created_before', typed=datetime.datetime):
        queryset = queryset.filter(created__lte=created_before)

    if hours := request.DATA.get_typed('hours', typed=int, default=24):
        cutoff = timezone.now() - timedelta(hours=hours)
        queryset = queryset.filter(created__gte=cutoff)

    # Pagination
    limit = min(request.DATA.get_typed('limit', typed=int, default=100), 1000)
    offset = request.DATA.get_typed('offset', typed=int, default=0)

    total = queryset.count()
    queryset = queryset[offset:offset + limit]

    # Get graph and serialize
    graph = request.DATA.get('graph', 'basic')

    return rest.rest_serialize(
        request,
        queryset,
        model=TaskLog,
        graph=graph,
        extras={'total': total}
    )


@md.GET('logs/task/{task_id}', docs=TASK_TIMELINE_DOCS)
def on_task_timeline(request, task_id):
    """
    Get chronological timeline of events for a specific task.
    """
    from ..models import TaskLog

    timeline = TaskLog.get_task_timeline(task_id)
    graph = request.DATA.get('graph', 'timeline')

    if not timeline:
        raise mojo.errors.NotFoundException(f"No logs found for task {task_id}")

    return JsonResponse({
        'task_id': task_id,
        'events': rest.serialize_queryset(timeline, model=TaskLog, graph=graph),
        'count': len(timeline),
        'status': True
    })


@md.GET('logs/errors', docs=ERROR_LOGS_DOCS)
def on_error_logs(request):
    """
    Get task error logs with error details.
    """
    from ..models import TaskLog

    hours = request.DATA.get_typed('hours', typed=int, default=24)
    limit = request.DATA.get_typed('limit', typed=int, default=100)

    queryset = TaskLog.get_failed_tasks(hours=hours)

    # Additional filters
    if task_channel := request.DATA.get('task_channel'):
        queryset = queryset.filter(task_channel=task_channel)

    if runner_hostname := request.DATA.get('runner_hostname'):
        queryset = queryset.filter(runner_hostname=runner_hostname)

    queryset = queryset[:limit]

    return rest.rest_serialize(
        request,
        queryset,
        model=TaskLog,
        graph='errors'
    )


@md.GET('logs/stats', docs=TASK_STATS_DOCS)
def on_task_stats(request):
    """
    Get task execution statistics and metrics.
    """
    from ..models import TaskLog
    from django.db.models import Count, Avg, Q

    hours = request.DATA.get_typed('hours', typed=int, default=24)
    task_channel = request.DATA.get('task_channel')
    group_by = request.DATA.get('group_by')

    cutoff = timezone.now() - timedelta(hours=hours)

    # Base queryset
    queryset = TaskLog.objects.filter(created__gte=cutoff)
    if task_channel:
        queryset = queryset.filter(task_channel=task_channel)

    # Basic stats
    basic_stats = queryset.aggregate(
        total_tasks=Count('task_id', distinct=True),
        total_events=Count('id'),
        avg_duration=Avg('duration_seconds')
    )

    # Status breakdown
    status_breakdown = queryset.filter(
        event_type='status_change'
    ).values('status').annotate(
        count=Count('id')
    ).order_by('-count')

    # Channel stats if not filtering by channel
    channel_stats = {}
    if not task_channel:
        channels = queryset.values('task_channel').annotate(
            total_tasks=Count('task_id', distinct=True),
            total_events=Count('id'),
            error_count=Count('id', filter=Q(event_type='error')),
            avg_duration=Avg('duration_seconds')
        )

        for channel in channels:
            channel_name = channel['task_channel']
            error_rate = (channel['error_count'] / channel['total_events']) if channel['total_events'] > 0 else 0
            channel_stats[channel_name] = {
                'total_tasks': channel['total_tasks'],
                'total_events': channel['total_events'],
                'error_count': channel['error_count'],
                'error_rate': round(error_rate, 3),
                'avg_duration_seconds': channel['avg_duration']
            }

    # Grouping stats
    grouped_stats = {}
    if group_by == 'function':
        grouped_stats = queryset.values('task_function').annotate(
            count=Count('task_id', distinct=True),
            avg_duration=Avg('duration_seconds')
        ).order_by('-count')[:20]
    elif group_by == 'runner':
        grouped_stats = queryset.values('runner_hostname').annotate(
            count=Count('task_id', distinct=True),
            avg_duration=Avg('duration_seconds')
        ).order_by('-count')
    elif group_by == 'hour':
        from django.db.models import Extract
        grouped_stats = queryset.annotate(
            hour=Extract('created', 'hour')
        ).values('hour').annotate(
            count=Count('task_id', distinct=True)
        ).order_by('hour')

    response_data = {
        'period_hours': hours,
        'total_tasks': basic_stats['total_tasks'] or 0,
        'total_events': basic_stats['total_events'] or 0,
        'avg_duration_seconds': round(basic_stats['avg_duration'] or 0, 3),
        'status_breakdown': {item['status']: item['count'] for item in status_breakdown},
        'status': True
    }

    if channel_stats:
        response_data['channel_stats'] = channel_stats

    if grouped_stats:
        response_data[f'{group_by}_breakdown'] = list(grouped_stats)

    return JsonResponse(response_data)


@md.GET('logs/{log_id}')
def on_task_log_detail(request, log_id):
    """
    Get detailed information for a specific task log entry.
    """
    from ..models import TaskLog

    try:
        log_entry = TaskLog.objects.get(id=log_id)
    except TaskLog.DoesNotExist:
        raise mojo.errors.NotFoundException(f"Task log {log_id} not found")

    graph = request.DATA.get('graph', 'detailed')

    return rest.rest_serialize(
        request,
        log_entry,
        model=TaskLog,
        graph=graph
    )
