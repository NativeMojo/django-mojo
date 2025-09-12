from mojo import decorators as md
from mojo.apps import metrics
from mojo.helpers.response import JsonResponse
import mojo.errors


@md.URL('permissions')
@md.URL('permissions/<str:account>')
@md.requires_perms("manage_incidents")
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


def on_set_permissions(request, account):
    """
    Set view permissions for an account.
    """
    view_perms = request.DATA.get("view_permissions", "").split(",")
    write_perms = request.DATA.get("write_permissions", "").split(",")

    if view_perms:
        metrics.set_view_perms(account, view_perms)
    if write_perms:
        metrics.set_write_perms(account, write_perms)

    return JsonResponse({
        "id": account,
        "account": account,
        "view_permissions": view_perms,
        "write_permissions": write_perms,
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
