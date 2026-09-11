from mojo import decorators as md
from mojo.apps import metrics
from mojo.helpers.response import JsonResponse
import mojo.errors


@md.URL('permissions')
@md.URL('permissions/<str:account>')
@md.requires_global_perms("manage_incidents", "metrics", "manage_metrics")
def on_permissions(request, account=None):
    if request.method == 'GET':
        if account is None:
            return on_list_permissions(request)
        return on_get_permissions(request, account)
    if request.method in ['POST', 'PUT']:
        if not account:
            account = request.DATA.get("account", None)
        if account:
            return on_set_permissions(request, account)
    if request.method == 'DELETE' and account:
        return on_delete_permissions(request, account)
    return JsonResponse({
        "method": request.method,
        "error": "Invalid method",
        "status": False
    })

def on_get_permissions(request, account):
    """
    Get current view and write permissions for an account.
    """
    view_perms = metrics.get_view_perms(account)
    write_perms = metrics.get_write_perms(account)

    return JsonResponse({
        "id": account,
        "account": account,
        "view_permissions": view_perms,
        "write_permissions": write_perms,
        "status": True
    })


def _perm_list(value):
    """Normalize a submitted permission value to a list, or None to clear.

    An empty / blank value means "remove this account's perms", which is what
    set_view_perms/set_write_perms(None) does.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        perms = [str(p).strip() for p in value if str(p).strip()]
        return perms or None
    perms = [p.strip() for p in str(value).split(",") if p.strip()]
    return perms or None


def on_set_permissions(request, account):
    """
    Set view and/or write permissions for an account.

    Each list is written ONLY when its key is actually present in the request.
    The previous version did ``request.DATA.get("view_permissions", "").split(",")``,
    which yields ``[""]`` for an absent key — truthy — so a caller setting only
    write permissions silently overwrote the view list with a permission string
    that can never match any user.
    """
    if "view_permissions" in request.DATA:
        metrics.set_view_perms(account, _perm_list(request.DATA.get("view_permissions")))
    if "write_permissions" in request.DATA:
        metrics.set_write_perms(account, _perm_list(request.DATA.get("write_permissions")))

    # Report what is actually stored now, so a one-sided POST shows the
    # untouched list rather than the echo of what the caller did not send.
    return JsonResponse({
        "id": account,
        "account": account,
        "view_permissions": metrics.get_view_perms(account),
        "write_permissions": metrics.get_write_perms(account),
        "action": "set",
        "status": True
    })


def on_delete_permissions(request, account):
    """
    Remove all permissions for an account.
    """
    # Remove both view and write permissions
    metrics.set_view_perms(account, None)
    metrics.set_write_perms(account, None)

    return JsonResponse({
        "account": account,
        "action": "deleted",
        "status": True
    })


def on_list_permissions(request):
    """
    List all accounts that have permissions configured.
    """
    accounts = metrics.list_accounts()
    data = []
    for account in accounts:
        info = {"account": account, "id": account}
        info["view_permissions"] = metrics.get_view_perms(account)
        info["write_permissions"] = metrics.get_write_perms(account)
        data.append(info)

    return JsonResponse({
        "data": data,
        "size": 10,
        "start": 0,
        "count": len(accounts),
        "status": True
    })
