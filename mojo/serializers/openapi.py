from mojo.helpers.settings import settings
from mojo.decorators.http import URLPATTERN_METHODS
from django.apps import apps
from mojo.helpers.response import JsonResponse


API_PREFIX = "/".join([settings.get("MOJO_PREFIX", "api/").rstrip("/"), ""])

def generate_openapi_schema(title="Mojo API", version=settings.VERSION, description="Auto-generated schema"):
    paths = {}

    for key, view_func in URLPATTERN_METHODS.items():
        try:
            method, pattern = view_func.__url__
            app_name = view_func.__app_name__
        except AttributeError:
            continue

        docs = getattr(view_func, "__docs__", {})
        clean_pattern = pattern.strip("^$").strip("/")

        # Normalize to always use base + detail route if ALL method
        if method.lower() == "all":
            path_parts = clean_pattern.split("/")
            model_slug = path_parts[-1]
            list_path = f"{API_PREFIX}/{app_name}/{model_slug}".replace("//", "/")
            detail_path = f"{list_path}/{{pk}}"

            model_cls = resolve_model_from_pattern(app_name, model_slug)
            if model_cls:
                paths[list_path] = generate_model_rest_docs(model_cls, is_detail_route=False)
                paths[detail_path] = generate_model_rest_docs(model_cls, is_detail_route=True)
            continue

        # Fallback: treat as-is
        full_path = f"{API_PREFIX}/{app_name}/{clean_pattern}".replace("//", "/")
        if not full_path.startswith("/"):
            full_path = "/" + full_path

        if full_path not in paths:
            paths[full_path] = {}

        op = method.lower()
        base_parameters = docs.get("parameters", [])
        if "{pk}" in full_path:
            base_parameters.insert(0, {
                "name": "pk",
                "in": "path",
                "required": True,
                "schema": {"type": "integer"}
            })

        paths[full_path][op] = {
            "summary": docs.get("summary", f"{method} {full_path}"),
            "description": docs.get("description", ""),
            "parameters": base_parameters,
            "responses": docs.get("responses", {
                "200": {"description": "Successful response"}
            })
        }

    return {
        "openapi": "3.0.0",
        "info": {
            "title": title,
            "version": version,
            "description": description,
        },
        "paths": paths
    }

def resolve_model_from_pattern(app_name, model_slug):
    try:
        model_cls = apps.get_model(app_name, model_slug.capitalize())
        return model_cls
    except LookupError:
        return None

def generate_model_rest_docs(model_cls, is_detail_route=False):
    from django.db import models
    base_parameters = [
        {"name": "graph", "in": "query", "schema": {"type": "string", "default": "default"}},
    ]

    list_parameters = base_parameters + [
        {"name": "size", "in": "query", "schema": {"type": "integer"}},
        {"name": "start", "in": "query", "schema": {"type": "integer"}},
        {"name": "sort", "in": "query", "schema": {"type": "string"}},
        {"name": "dr_start", "in": "query", "schema": {"type": "string", "format": "date-time"}},
        {"name": "dr_end", "in": "query", "schema": {"type": "string", "format": "date-time"}},
        {"name": "dr_field", "in": "query", "schema": {"type": "string"}},
    ]

    for field in model_cls._meta.fields:
        list_parameters.append({
            "name": field.name,
            "in": "query",
            "schema": {"type": map_field_type(field)}
        })

    post_schema = {
        "type": "object",
        "properties": {
            f.name: {"type": map_field_type(f)}
            for f in model_cls._meta.fields if not f.auto_created
        }
    }

    ops = {}

    if is_detail_route:
        detail_parameters = [{
            "name": "pk",
            "in": "path",
            "required": True,
            "schema": {"type": "integer"}
        }] + base_parameters

        ops["get"] = {
            "summary": f"Retrieve a single {model_cls.__name__}",
            "parameters": detail_parameters,
            "responses": {
                "200": {"description": "Graph-based model serialization"}
            }
        }
        ops["post"] = {
            "summary": f"Update a {model_cls.__name__} instance",
            "parameters": detail_parameters,
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": post_schema}
                }
            },
            "responses": {
                "200": {"description": "Updated model instance"}
            }
        }
        ops["delete"] = {
            "summary": f"Delete a {model_cls.__name__} instance",
            "parameters": detail_parameters,
            "responses": {
                "204": {"description": "Deleted successfully"}
            }
        }
    else:
        ops["get"] = {
            "summary": f"List {model_cls.__name__} instances",
            "parameters": list_parameters,
            "responses": {
                "200": {"description": "List of model instances using graph serialization"}
            }
        }
        ops["post"] = {
            "summary": f"Create a new {model_cls.__name__} instance",
            "parameters": base_parameters,
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": post_schema}
                }
            },
            "responses": {
                "200": {"description": "Created model instance"}
            }
        }

    return ops

def map_field_type(field):
    from django.db import models
    if isinstance(field, models.IntegerField):
        return "integer"
    elif isinstance(field, models.BooleanField):
        return "boolean"
    elif isinstance(field, models.DateTimeField):
        return "string"
    elif isinstance(field, models.DateField):
        return "string"
    return "string"


def openapi_schema_view(request):
    if settings.OPENAPI_DOCS_KEY is None:
        return JsonResponse(dict(status=False, error="disabled"), status=404)
    if settings.OPENAPI_DOCS_KEY != request.GET.get("key", ""):
        return JsonResponse(dict(status=False, error="permission denied"), status=403)
    return JsonResponse(generate_openapi_schema())
