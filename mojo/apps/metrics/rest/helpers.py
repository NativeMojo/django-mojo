from mojo.apps import metrics
from mojo.apps.metrics import utils
from mojo.helpers.settings import settings
from objict import nobjict
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


def fetch_group_fanout(parent_id, child_kind, slugs, dt_start=None, dt_end=None,
                       granularity="hours", with_labels=False):
    """
    Sum metric series for ``slugs`` across every active descendant of
    ``parent_id`` whose ``kind`` matches ``child_kind``.

    Returns the same shape as ``metrics.fetch(slugs, with_labels=True)`` for a
    multi-slug call: ``{"labels": [...], "data": {slug: [int, ...]}}``. When
    ``with_labels=False`` the labels key is omitted and the response is
    ``{slug: [int, ...]}``.
    """
    from mojo.apps.account.models import Group

    if isinstance(slugs, str):
        slug_list = [slugs]
    else:
        slug_list = list(slugs)
    if not slug_list:
        raise mojo.errors.ValueException("fan-out requires at least one slug")

    parent = Group.objects.filter(id=parent_id).first()
    if parent is None:
        raise mojo.errors.ValueException(f"group-{parent_id} not found")

    max_children = settings.get_static("METRICS_FANOUT_MAX_CHILDREN", 200)
    child_ids = list(
        parent.get_children(is_active=True, kind=child_kind)
              .values_list("id", flat=True)
    )
    if len(child_ids) > max_children:
        raise mojo.errors.ValueException(
            f"fan-out resolved {len(child_ids)} children, exceeds "
            f"METRICS_FANOUT_MAX_CHILDREN ({max_children})"
        )

    parent_account = f"group-{parent_id}"
    label_slugs = utils.generate_slugs_for_range(
        slug_list[0], dt_start, dt_end, granularity, parent_account
    )
    labels = utils.periods_from_dr_slugs(label_slugs)
    bucket_count = len(labels)

    accumulator = {s.split(":")[-1]: [0] * bucket_count for s in slug_list}

    for cid in child_ids:
        child_account = f"group-{cid}"
        result = metrics.fetch(
            slug_list if len(slug_list) > 1 else slug_list[0],
            dt_start=dt_start, dt_end=dt_end, granularity=granularity,
            account=child_account, with_labels=False, allow_empty=True,
        )
        if len(slug_list) == 1:
            trunc = slug_list[0].split(":")[-1]
            for i, v in enumerate(result):
                if i < bucket_count:
                    accumulator[trunc][i] += int(v or 0)
        else:
            for trunc, series in result.items():
                if trunc not in accumulator:
                    continue
                for i, v in enumerate(series):
                    if i < bucket_count:
                        accumulator[trunc][i] += int(v or 0)

    if with_labels:
        return nobjict(labels=labels, data=accumulator)
    return nobjict(**accumulator)
