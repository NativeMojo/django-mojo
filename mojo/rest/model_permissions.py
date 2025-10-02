"""
REST API endpoint for model permissions.

Provides programmatic access to model RestMeta permission configurations.
"""

import mojo.decorators as md
from mojo.helpers.response import JsonResponse
from django.apps import apps
from mojo.models import MojoModel


def get_model_permissions_data(app_filter=None, verbose=False):
    """
    Extract permission information from all models.

    Args:
        app_filter: Optional app name to filter by
        verbose: Include additional details like CREATE_PERMS, DELETE_PERMS, etc.

    Returns:
        List of dictionaries containing model permission information
    """
    models_data = []

    for app_config in apps.get_app_configs():
        # Skip if filtering by app
        if app_filter and app_config.label != app_filter:
            continue

        for model in app_config.get_models():
            # Check if model has MojoModel
            if not issubclass(model, MojoModel):
                continue

            # Skip models without RestMeta
            if not hasattr(model, 'RestMeta'):
                continue

            # Get model info
            model_info = extract_model_info(model, app_config.label, verbose=verbose)
            models_data.append(model_info)

    # Sort by app and model name
    models_data.sort(key=lambda x: (x['app'], x['model']))

    return models_data


def extract_model_info(model, app_label, verbose=False):
    """Extract permission information from a model."""
    rest_meta = model.RestMeta

    info = {
        'app': app_label,
        'model': model.__name__,
        'full_name': f"{app_label}.{model.__name__}",
        'view_perms': getattr(rest_meta, 'VIEW_PERMS', []),
        'save_perms': getattr(rest_meta, 'SAVE_PERMS', []),
    }

    if verbose:
        info['create_perms'] = getattr(rest_meta, 'CREATE_PERMS', [])
        info['delete_perms'] = getattr(rest_meta, 'DELETE_PERMS', [])
        info['can_delete'] = getattr(rest_meta, 'CAN_DELETE', False)
        info['log_changes'] = getattr(rest_meta, 'LOG_CHANGES', False)
        info['owner_field'] = getattr(rest_meta, 'OWNER_FIELD', 'user')
        info['group_field'] = getattr(rest_meta, 'GROUP_FIELD', 'group')
        info['search_fields'] = getattr(rest_meta, 'SEARCH_FIELDS', None)
        info['no_save_fields'] = getattr(rest_meta, 'NO_SAVE_FIELDS', [])
        info['no_show_fields'] = getattr(rest_meta, 'NO_SHOW_FIELDS', [])

        # Check if model has user or group fields
        field_names = [f.name for f in model._meta.get_fields()]
        info['has_user_field'] = 'user' in field_names
        info['has_group_field'] = 'group' in field_names

        # Get available graphs
        graphs = getattr(rest_meta, 'GRAPHS', {})
        info['graphs'] = list(graphs.keys())

    return info


@md.GET('models/permissions')
@md.requires_perms(['view_admin', 'manage_users', 'admin'])
def rest_model_permissions(request):
    """
    GET /api/models/permissions

    Get a list of all models with their RestMeta permission configurations.

    Query Parameters:
        - app: Filter by app name (e.g., account, incident)
        - verbose: Include additional details (true/false)
        - model: Filter by specific model name

    Returns:
        JSON response with model permission data

    Example:
        GET /api/models/permissions?app=account&verbose=true
    """
    app_filter = request.DATA.get('app')
    verbose = request.DATA.get('verbose', 'false').lower() == 'true'
    model_filter = request.DATA.get('model')

    models_data = get_model_permissions_data(app_filter=app_filter, verbose=verbose)

    # Apply model name filter if provided
    if model_filter:
        models_data = [m for m in models_data if model_filter.lower() in m['model'].lower()]

    return JsonResponse({
        'status': True,
        'count': len(models_data),
        'data': models_data
    })


@md.GET('models/permissions/<str:app_label>/<str:model_name>')
@md.requires_perms(['view_admin', 'manage_users', 'admin'])
def rest_model_permission_detail(request, app_label, model_name):
    """
    GET /api/models/permissions/{app_label}/{model_name}

    Get detailed permission configuration for a specific model.

    Returns:
        JSON response with detailed model permission data

    Example:
        GET /api/models/permissions/account/User
    """
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError:
        return JsonResponse({
            'status': False,
            'error': f"Model {app_label}.{model_name} not found"
        }, status=404)

    if not issubclass(model, MojoModel):
        return JsonResponse({
            'status': False,
            'error': f"Model {app_label}.{model_name} is not a MojoModel"
        }, status=400)

    if not hasattr(model, 'RestMeta'):
        return JsonResponse({
            'status': False,
            'error': f"Model {app_label}.{model_name} has no RestMeta"
        }, status=404)

    model_info = extract_model_info(model, app_label, verbose=True)

    return JsonResponse({
        'status': True,
        'data': model_info
    })
