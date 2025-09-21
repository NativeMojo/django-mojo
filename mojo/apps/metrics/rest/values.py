from mojo import decorators as md
from mojo.apps import metrics
from mojo.helpers.response import JsonResponse
import mojo.errors
import datetime
from .helpers import check_view_permissions, check_write_permissions

# Documentation for time-series metrics endpoints
SERIES_GET_DOCS = {
    "summary": "Get time-series values at a point in time",
    "description": "Retrieves time-series metric values for multiple slugs at a specific datetime and granularity.",
    "parameters": [
        {
            "name": "slugs",
            "in": "query",
            "required": True,
            "schema": {"type": "string"},
            "description": "Comma-separated list of slugs to fetch values for (e.g., 'user_activity_day,page_views')."
        },
        {
            "name": "when",
            "in": "query",
            "required": True,
            "schema": {"type": "string", "format": "date-time"},
            "description": "The specific date/datetime to fetch values for."
        },
        {
            "name": "granularity",
            "in": "query",
            "schema": {"type": "string", "default": "hours"},
            "description": "Granularity of the data (e.g., 'hours', 'days', 'months', 'years')."
        },
        {
            "name": "account",
            "in": "query",
            "schema": {"type": "string", "default": "public"},
            "description": "Account identifier (e.g., 'public', 'global', or 'group_<id>')."
        }
    ],
    "responses": {
        "200": {
            "description": "Successful response with metric values.",
            "content": {
                "application/json": {
                    "example": {
                        "response": {
                            "data": {
                                "user_activity_day": 22,
                                "page_views": 156
                            },
                            "slugs": ["user_activity_day", "page_views"],
                            "when": "2025-08-01T00:00:00",
                            "granularity": "days",
                            "account": "public",
                            "status": True
                        },
                        "status_code": 200
                    }
                }
            }
        }
    }
}

SERIES_POST_DOCS = {
    "summary": "Get time-series values at a point in time (POST)",
    "description": "Retrieves time-series metric values for multiple slugs at a specific datetime and granularity using POST method.",
    "parameters": [
        {
            "name": "slugs",
            "in": "body",
            "required": True,
            "schema": {"type": "string"},
            "description": "Comma-separated list of slugs to fetch values for (e.g., 'user_activity_day,page_views')."
        },
        {
            "name": "when",
            "in": "body",
            "required": True,
            "schema": {"type": "string", "format": "date-time"},
            "description": "The specific date/datetime to fetch values for."
        },
        {
            "name": "granularity",
            "in": "body",
            "schema": {"type": "string", "default": "hours"},
            "description": "Granularity of the data (e.g., 'hours', 'days', 'months', 'years')."
        },
        {
            "name": "account",
            "in": "body",
            "schema": {"type": "string", "default": "public"},
            "description": "Account identifier (e.g., 'public', 'global', or 'group_<id>')."
        }
    ]
}

# Documentation for simple value storage endpoints
SET_VALUE_DOCS = {
    "summary": "Set simple global values",
    "description": "Sets simple key-value pairs for global storage (not time-series).",
    "parameters": [
        {
            "name": "slug",
            "in": "body",
            "required": True,
            "schema": {"type": "string"},
            "description": "The key identifier for the value to set."
        },
        {
            "name": "value",
            "in": "body",
            "required": True,
            "schema": {"type": "string"},
            "description": "The value to store."
        },
        {
            "name": "account",
            "in": "body",
            "schema": {"type": "string", "default": "public"},
            "description": "Account identifier (e.g., 'public', 'global', or 'group_<id>')."
        }
    ],
    "responses": {
        "200": {
            "description": "Successful value storage.",
            "content": {
                "application/json": {
                    "example": {
                        "response": {
                            "slug": "max_users",
                            "value": "1000",
                            "account": "public",
                            "status": True
                        },
                        "status_code": 200
                    }
                }
            }
        }
    }
}

VALUE_GET_DOCS = {
    "summary": "Get simple global values",
    "description": "Retrieves simple key-value pairs from global storage (not time-series). Supports multiple slugs.",
    "parameters": [
        {
            "name": "slugs",
            "in": "query",
            "required": True,
            "schema": {"type": "string"},
            "description": "Comma-separated list of slugs to fetch values for (e.g., 'max_users,maintenance_mode')."
        },
        {
            "name": "account",
            "in": "query",
            "schema": {"type": "string", "default": "public"},
            "description": "Account identifier (e.g., 'public', 'global', or 'group_<id>')."
        },
        {
            "name": "default",
            "in": "query",
            "schema": {"type": "string"},
            "description": "Default value to return for missing keys."
        }
    ],
    "responses": {
        "200": {
            "description": "Successful response with values.",
            "content": {
                "application/json": {
                    "example": {
                        "response": {
                            "data": {
                                "max_users": "1000",
                                "maintenance_mode": "false"
                            },
                            "slugs": ["max_users", "maintenance_mode"],
                            "account": "public",
                            "status": True
                        },
                        "status_code": 200
                    }
                }
            }
        }
    }
}


# Time-series metrics endpoints
@md.GET('series', docs=SERIES_GET_DOCS)
@md.custom_security("protected by metrics permissions")
@md.requires_params("slugs")
def on_metrics_series(request):
    """
    Get time-series values for multiple slugs at a single point in time.
    """
    when = request.DATA.get_typed("when")
    account = request.DATA.get("account", "public")
    granularity = request.DATA.get("granularity", "hours")
    slugs = request.DATA.get("slugs")

    check_view_permissions(request, account)

    # Fetch the values using our new method
    result = metrics.fetch_values(slugs, when, granularity, account=account)
    result['status'] = True

    return JsonResponse(result)


@md.POST('series', docs=SERIES_POST_DOCS)
@md.custom_security("protected by metrics permissions")
@md.requires_params("slugs")
def on_metrics_series_post(request):
    """
    POST version of the series endpoint - same functionality as GET.
    """
    return on_metrics_series(request)


# Simple value storage endpoints
@md.POST('value/set', docs=SET_VALUE_DOCS)
@md.custom_security("protected by metrics permissions")
@md.requires_params("slug", "value")
def on_set_value(request):
    """
    Set a simple global value (not time-series).
    """
    slug = request.DATA.get("slug")
    value = request.DATA.get("value")
    account = request.DATA.get("account", "public")

    check_write_permissions(request, account)

    metrics.set_value(slug, value, account=account)

    return JsonResponse({
        "slug": slug,
        "value": value,
        "account": account,
        "status": True
    })


@md.GET('value/get', docs=VALUE_GET_DOCS)
@md.custom_security("protected by metrics permissions")
@md.requires_params("slugs")
def on_get_value(request):
    """
    Get simple global values for multiple slugs (not time-series).
    """
    slugs = request.DATA.get("slugs")
    account = request.DATA.get("account", "public")
    default = request.DATA.get("default")

    check_view_permissions(request, account)

    # Handle comma-separated string input
    if isinstance(slugs, str):
        if ',' in slugs:
            slugs_list = [s.strip() for s in slugs.split(',')]
        else:
            slugs_list = [slugs]
    else:
        slugs_list = slugs

    # Fetch values for all slugs
    data = {}
    for slug in slugs_list:
        value = metrics.get_value(slug, account=account, default=default)
        data[slug] = value

    return JsonResponse({
        "data": data,
        "slugs": slugs_list,
        "account": account,
        "status": True
    })
