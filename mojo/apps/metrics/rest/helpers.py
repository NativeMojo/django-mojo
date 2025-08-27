from mojo.apps import metrics
import mojo.errors


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
        if not request.user.is_authenticated or not request.user.has_permission("view_metrics"):
            raise mojo.errors.PermissionDeniedException()
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
        if not request.user.is_authenticated or not request.user.has_permission("write_metrics"):
            raise mojo.errors.PermissionDeniedException()
    elif account != "public":
        perms = metrics.get_write_perms(account)
        if not perms:
            raise mojo.errors.PermissionDeniedException()
        if perms != "public":
            if not request.user.is_authenticated or not request.user.has_permission(perms):
                raise mojo.errors.PermissionDeniedException()
