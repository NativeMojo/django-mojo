from mojo import decorators as md
from mojo.apps import metrics
from mojo.helpers.response import JsonResponse
import mojo.errors

import datetime
@md.POST('record', docs={
    "summary": "Record metrics data",
    "description": "Records metrics data for specified slug for the specified account, increments by 1 each call.",
    "parameters": [
        {
            "name": "slug",
            "in": "body",
            "required": True,
            "schema": {"type": "string"},
            "description": "Unique identifier for the metric to be recorded."
        },
        {
            "name": "account",
            "in": "body",
            "required": False,
            "schema": {"type": "string", "default": "public"},
            "description": "Account identifier (e.g., 'public', 'global', or 'group_<id>')."
        },
        {
            "name": "count",
            "in": "body",
            "required": False,
            "schema": {"type": "integer", "default": 1},
            "description": "The count by which the metric data should be incremented."
        },
        {
            "name": "min_granularity",
            "in": "body",
            "required": False,
            "schema": {"type": "string", "default": "hours"},
            "description": "Minimum granularity of the metric (e.g., 'hours')."
        },
        {
            "name": "max_granularity",
            "in": "body",
            "required": False,
            "schema": {"type": "string", "default": "years"},
            "description": "Maximum granularity of the metric (e.g., 'years')."
        }
    ]
})
@md.requires_params("slug")
@md.custom_security("protected by metrics permissions")
def on_metrics_record(request):
    account = request.DATA.get("account", "public")
    count = request.DATA.get_typed("count", default=1, typed=int)
    min_granularity = request.DATA.get("min_granularity", "hours")
    max_granularity = request.DATA.get("max_granularity", "years")
    if account != "public":
        perms = metrics.get_write_perms(account)
        if not perms:
            raise mojo.errors.PermissionDeniedException()
        if perms != "public":
            if not request.user.is_authenticated or not request.user.has_permission(perms):
                raise mojo.errors.PermissionDeniedException()
    metrics.record(request.DATA.slug, count=count, min_granularity=min_granularity,
        max_granularity=max_granularity)
    return JsonResponse(dict(status=True))


@md.GET('fetch', docs={
    "summary": "Fetch metrics data",
    "description": "Retrieves metrics data for specified slugs within a given date range and granularity.",
    "parameters": [
        {
            "name": "slugs",
            "in": "query",
            "required": True,
            "schema": {"type": "array", "items": {"type": "string"}},
            "description": "List of slugs to fetch metrics for."
        },
        {
            "name": "dt_start",
            "in": "query",
            "schema": {"type": "string", "format": "date-time"},
            "description": "Start date and time for the data range."
        },
        {
            "name": "dt_end",
            "in": "query",
            "schema": {"type": "string", "format": "date-time"},
            "description": "End date and time for the data range."
        },
        {
            "name": "account",
            "in": "query",
            "schema": {"type": "string"},
            "description": "Account identifier (e.g., 'public', 'global', or 'group_<id>')."
        },
        {
            "name": "granularity",
            "in": "query",
            "schema": {"type": "string", "default": "hours"},
            "description": "Granularity of the data (e.g., 'hours')."
        }
    ],
    "responses": {
        "200": {
            "description": "Successful response with metrics data.",
            "content": {
                "application/json": {
                    "example": {
                        "response": {
                            "data": {
                                "data": {
                                    "slug": "c3",
                                    "values": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
                                },
                                "periods": [
                                    "03:00", "04:00", "05:00", "06:00", "07:00",
                                    "08:00", "09:00", "10:00", "11:00", "12:00",
                                    "13:00", "14:00"
                                ]
                            },
                            "status": True
                        },
                        "status_code": 200
                    }
                }
            }
        }
    }
})
@md.custom_security("protected by metrics permissions")
def on_metrics_data(request):
    """
    TODO add support for group based permissions where account == "group_<id>"
    """
    dt_start = request.DATA.get_typed("dt_start", typed=datetime.datetime)
    dt_end = request.DATA.get_typed("dt_end", typed=datetime.datetime)
    account = request.DATA.get("account", "public")
    granularity = request.DATA.get("granularity", "hours")
    if account == "global":
        if not request.user.is_authenticated or not request.user.has_permission("view_metrics"):
            raise mojo.errors.PermissionDeniedException()
    elif account.startswith("group-"):
        if not request.user.is_authenticated:
            raise mojo.errors.PermissionDeniedException()
        if not request.user.has_permission("view_metrics"):
            from mojo.apps.account.models import Group
            group_id = account.split("-")[1]
            group = Group.objects.get(id=group_id)
            if not group.user_has_permission(request.user, "view_metrics", False):
                raise mojo.errors.PermissionDeniedException()
    elif account != "public":
        perms = metrics.get_view_perms(account)
        if not perms:
            raise mojo.errors.PermissionDeniedException(f"Permission denied for account {account}")
        if perms and not request.user.has_permission(perms):
            raise mojo.errors.PermissionDeniedException(f"Permission denied for account {account}")

    category = request.DATA.get("category", None)
    if "slugs" in request.DATA:
        slugs = request.DATA.get_typed("slugs", typed=list)
    elif category:
        slugs = list(metrics.get_category_slugs(category, account=account))
    else:
        raise mojo.errors.ValueException("missing required parameter")
    if len(slugs) == 1:
        slugs = slugs[0]
    records = metrics.fetch(slugs, dt_start=dt_start, dt_end=dt_end,
        granularity=granularity, account=account, with_labels=True)
    return JsonResponse(dict(status=True, data=records))
