"""Custom People operations that are not ordinary RestMeta CRUD."""

from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.account.models import ApiKey
from mojo.apps.account.services import admin_passwords, admin_people
from mojo.helpers.response import JsonResponse


def _secret_response(data):
    response = JsonResponse({"status": True, "data": data})
    response.log_context = {"sensitive_body": "admin_secret"}
    return response


@md.POST("account/admin/user/password/reset")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("users", "manage_users")
@md.requires_params("user")
def on_admin_user_password_reset(request):
    return admin_passwords.send_reset_link(request, request.DATA.get("user"))


@md.POST("account/admin/user/password/temporary")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("users", "manage_users")
@md.requires_params("user")
def on_admin_user_password_temporary(request):
    return _secret_response(admin_passwords.issue_temporary_password(
        request, request.DATA.get("user")))


@md.GET("account/admin/people/permission-bundles")
@md.denies_key_backed_session()
@md.requires_global_perms("view_users", "manage_users", "users")
@md.requires_params("user")
def on_admin_permission_bundles(request):
    return admin_people.describe(request.DATA.get("user"))


@md.POST("account/admin/people/permission-bundles")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_users", "users")
@md.requires_params("user", "version", "selected")
def on_admin_permission_bundles_save(request):
    return admin_people.apply(
        request, request.DATA.get("user"), request.DATA.get("version"),
        request.DATA.get("selected"))


@md.POST("account/admin/apikey/action")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_auth()
@md.requires_params("api_key", "action")
def on_admin_apikey_action(request):
    try:
        key_id = int(request.DATA.get("api_key"))
    except (TypeError, ValueError):
        raise merrors.ValueException("api_key must be an integer")
    api_key = ApiKey.objects.select_related("group").filter(pk=key_id).first()
    if api_key is None:
        raise merrors.PermissionDeniedException()
    ApiKey.rest_check_permission_or_raise(request, "SAVE_PERMS", api_key)
    action = request.DATA.get("action")
    data = {"id": api_key.pk, "action": action}
    if action == "rotate":
        data["token"] = api_key.rotate_token()
    elif action in ("deactivate", "reactivate"):
        active = action == "reactivate"
        if api_key.is_active != active:
            api_key.is_active = active
            api_key.save(update_fields=["is_active", "modified"])
            api_key.log(
                f"API Key '{api_key.name}' {action}d by administrator {request.user.pk}",
                f"api_key:{action}d")
        data["is_active"] = api_key.is_active
    elif action == "revoke":
        api_key.log(
            f"API Key '{api_key.name}' revoked by administrator {request.user.pk}",
            "api_key:revoked")
        api_key.delete()
        data["revoked"] = True
    else:
        raise merrors.ValueException(
            "action must be rotate, revoke, deactivate, or reactivate")
    return _secret_response(data) if action == "rotate" else data
