"""Private topic boundary using the existing Fleet actor and freshness rules."""
from functools import wraps

from django.apps import apps

import mojo.decorators as md
from mojo import errors as me
from mojo.helpers.response import JsonResponse
from .admin_fleet import _actor


def _private(func):
    """Bounded secret-response pattern, including denials and unavailable state."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        try:
            response = JsonResponse({"status": True, "data": func(*args, **kwargs)})
        except me.MojoException as error:
            response = JsonResponse({"status": False, "error": error.reason,
                                     "error_code": getattr(error, "error_code", error.reason),
                                     "code": error.code}, status=error.status)
        except Exception:
            response = JsonResponse({"status": False, "error": "publisher_unavailable",
                                     "error_code": "publisher_unavailable"}, status=503)
        response["Cache-Control"] = "no-store"
        response.log_context = {"sensitive_body": "admin_secret"}
        return response
    return wrapped


def _installed():
    return apps.is_installed("mojo.apps.dnsman")


@md.GET("account/admin/fleet/shared-validation")
@_private
@md.denies_key_backed_session(deny_user_api_keys=True)
@md.requires_global_perms("admin")
def on_shared_validation(request):
    actor = _actor(request)
    if request.DATA:
        raise me.ValueException("Shared-validation state accepts no input fields")
    if not _installed():
        return {"schema_version": 1, "available": False,
                "unavailable_reasons": ["dnsman_unavailable"], "catalog_revision": None,
                "profiles": [], "selected_profile_id": None, "expected_identity": None,
                "loaded_identity": None, "credential": {"configured": False, "source": "unknown", "overridden": False},
                "revision": None, "loaded_revision": None, "pending_restart": False,
                "fleet": {"status": "unavailable", "nodes": [], "healthy_everywhere": False},
                "setup_destination": {"route": "setup", "section": "aws", "capacity_route": "capacity",
                                      "actor": "deployment_operator", "adoption_item": 8903}}
    from mojo.apps.dnsman.services import connection_config
    return connection_config.state(actor)


@md.POST("account/admin/fleet/shared-validation")
@_private
@md.denies_key_backed_session(deny_user_api_keys=True)
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("admin")
def on_shared_validation_mutate(request):
    actor = _actor(request, write=True)
    if not _installed():
        raise me.ValueException("dnsman_unavailable", code=503, status=503)
    from mojo.apps.dnsman.services import connection_config
    return connection_config.mutate(actor, dict(request.DATA))
