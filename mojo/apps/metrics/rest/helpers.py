from mojo.apps import metrics
import mojo.errors


def _check_group_account_permission(request, account, permission):
    if not account.startswith("group-"):
        return False
    if not request.user.is_authenticated:
        raise mojo.errors.PermissionDeniedException()
    if request.user.has_permission(permission):
        return True
    try:
        from mojo.apps.account.models import Group
        group_id = int(account.split("-", 1)[1])
        group = Group.objects.filter(id=group_id).first()
        if group is None or not group.user_has_permission(request.user, permission, False):
            raise mojo.errors.PermissionDeniedException()
    except (ValueError, TypeError):
        raise mojo.errors.PermissionDeniedException()
    return True


def _check_user_account_permission(request, account, permission):
    if not account.startswith("user-"):
        return False
    if not request.user.is_authenticated:
        raise mojo.errors.PermissionDeniedException()
    # system-level permission can access user accounts
    if request.user.has_permission(permission):
        return True
    account_user_id = account.split("-", 1)[1]
    if str(request.user.pk) != account_user_id:
        raise mojo.errors.PermissionDeniedException()
    return True


def check_view_permissions(request, account="public"):
    """
    Helper function to check view permissions for metrics operations.

    Args:
        request: The Django request object
        account: The account to check permissions for

    Raises:
        PermissionDeniedException: If user doesn't have proper permissions
    """
    if account == "global":
        if not request.user.is_authenticated or not request.user.has_permission(["view_metrics", "metrics"]):
            raise mojo.errors.PermissionDeniedException()
    elif _check_group_account_permission(request, account, ["view_metrics", "metrics"]):
        return
    elif _check_user_account_permission(request, account, ["view_metrics", "metrics"]):
        return
    elif account != "public":
        perms = metrics.get_view_perms(account)
        if not perms:
            raise mojo.errors.PermissionDeniedException()
        if perms != "public":
            if not request.user.is_authenticated or not request.user.has_permission(perms):
                raise mojo.errors.PermissionDeniedException()


def check_write_permissions(request, account="public"):
    """
    Helper function to check write permissions for metrics operations.

    Args:
        request: The Django request object
        account: The account to check permissions for

    Raises:
        PermissionDeniedException: If user doesn't have proper permissions
    """
    if account == "global":
        if not request.user.is_authenticated or not request.user.has_permission(["write_metrics", "metrics"]):
            raise mojo.errors.PermissionDeniedException()
    elif _check_group_account_permission(request, account, ["write_metrics", "metrics"]):
        return
    elif _check_user_account_permission(request, account, ["write_metrics", "metrics"]):
        return
    elif account != "public":
        perms = metrics.get_write_perms(account)
        if not perms:
            raise mojo.errors.PermissionDeniedException()
        if perms != "public":
            if not request.user.is_authenticated or not request.user.has_permission(perms):
                raise mojo.errors.PermissionDeniedException()
