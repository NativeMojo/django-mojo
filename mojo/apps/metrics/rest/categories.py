from mojo import decorators as md
from mojo.apps import metrics
from mojo.helpers.response import JsonResponse
import mojo.errors
import datetime
from .helpers import check_view_permissions, check_write_permissions

# Documentation for API endpoints
CATEGORIES_LIST_DOCS = {
    "summary": "List all categories",
    "description": "Retrieves all categories for a specific account.",
    "parameters": [
        {
            "name": "account",
            "in": "query",
            "schema": {"type": "string", "default": "public"},
            "description": "Account identifier (e.g., 'public', 'global', or 'group_<id>')."
        }
    ],
    "responses": {
        "200": {
            "description": "Successful response with list of categories.",
            "content": {
                "application/json": {
                    "example": {
                        "response": {
                            "categories": ["activity", "engagement", "performance"],
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

CATEGORY_SLUGS_DOCS = {
    "summary": "Get slugs in a category",
    "description": "Retrieves all slugs within a specific category.",
    "parameters": [
        {
            "name": "category",
            "in": "query",
            "required": True,
            "schema": {"type": "string"},
            "description": "The category name to get slugs for."
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
            "description": "Successful response with list of slugs in the category.",
            "content": {
                "application/json": {
                    "example": {
                        "response": {
                            "slugs": ["user_login", "user_signup", "user_logout"],
                            "category": "activity",
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

CATEGORY_FETCH_DOCS = {
    "summary": "Fetch metrics for category",
    "description": "Retrieves metrics data for all slugs within a category, date range, and granularity.",
    "parameters": [
        {
            "name": "category",
            "in": "query",
            "required": True,
            "schema": {"type": "string"},
            "description": "The category name to fetch metrics for."
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
            "schema": {"type": "string", "default": "public"},
            "description": "Account identifier (e.g., 'public', 'global', or 'group_<id>')."
        },
        {
            "name": "granularity",
            "in": "query",
            "schema": {"type": "string", "default": "hours"},
            "description": "Granularity of the data (e.g., 'hours', 'days', 'months', 'years')."
        },
        {
            "name": "with_labels",
            "in": "query",
            "schema": {"type": "boolean", "default": False},
            "description": "Include timestamp labels in response data."
        }
    ],
    "responses": {
        "200": {
            "description": "Successful response with category metrics data.",
            "content": {
                "application/json": {
                    "example": {
                        "response": {
                            "data": {
                                "user_login": [1, 2, 3, 4],
                                "user_signup": [0, 1, 0, 2]
                            },
                            "category": "activity",
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

CATEGORY_DELETE_DOCS = {
    "summary": "Delete category",
    "description": "Deletes an entire category and all its associated slugs and data.",
    "parameters": [
        {
            "name": "category",
            "in": "body",
            "required": True,
            "schema": {"type": "string"},
            "description": "The category name to delete."
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
            "description": "Successful deletion of category.",
            "content": {
                "application/json": {
                    "example": {
                        "response": {
                            "deleted_category": "activity",
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


@md.GET('categories', docs=CATEGORIES_LIST_DOCS)
@md.custom_security("protected by metrics permissions")
@md.requires_params("account")
def on_categories_list(request):
    """
    List all categories for an account.
    """
    account = request.DATA.get("account", "DOES_NOT_EXIST")
    check_view_permissions(request, account)

    categories = list(metrics.get_categories(account=account))

    return JsonResponse({
        "categories": categories,
        "account": account,
        "status": True
    })


@md.GET('category_slugs', docs=CATEGORY_SLUGS_DOCS)
@md.custom_security("protected by metrics permissions")
@md.requires_params("category")
def on_category_slugs(request):
    """
    Get all slugs within a specific category.
    """
    category = request.DATA.get("category")
    account = request.DATA.get("account", "public")
    check_view_permissions(request, account)

    slugs = list(metrics.get_category_slugs(category, account=account))

    return JsonResponse({
        "slugs": slugs,
        "category": category,
        "account": account,
        "status": True
    })


@md.GET('category_fetch', docs=CATEGORY_FETCH_DOCS)
@md.custom_security("protected by metrics permissions")
@md.requires_params("category")
def on_category_fetch(request):
    """
    Fetch metrics for all slugs within a category.
    """
    category = request.DATA.get("category")
    dt_start = request.DATA.get_typed("dt_start", typed=datetime.datetime)
    dt_end = request.DATA.get_typed("dt_end", typed=datetime.datetime)
    account = request.DATA.get("account", "public")
    granularity = request.DATA.get("granularity", "hours")
    with_labels = request.DATA.get_typed("with_labels", default=False, typed=bool)

    check_view_permissions(request, account)

    data = metrics.fetch_by_category(
        category,
        dt_start=dt_start,
        dt_end=dt_end,
        granularity=granularity,
        account=account,
        with_labels=with_labels
    )

    return JsonResponse({
        "data": data,
        "category": category,
        "account": account,
        "status": True
    })


@md.DELETE('category_delete', docs=CATEGORY_DELETE_DOCS)
@md.custom_security("protected by metrics permissions")
@md.requires_params("category")
def on_category_delete(request):
    """
    Delete an entire category and all its associated slugs and data.
    """
    category = request.DATA.get("category")
    account = request.DATA.get("account", "public")

    check_write_permissions(request, account)

    metrics.delete_category(category, account=account)

    return JsonResponse({
        "deleted_category": category,
        "account": account,
        "status": True
    })
