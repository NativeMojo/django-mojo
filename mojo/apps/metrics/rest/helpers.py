from mojo.apps import metrics
from mojo.apps.metrics import utils
from mojo.helpers.settings import settings
from mojo.helpers.request import identity_allows_group, is_override_user_session
from objict import nobjict
import mojo.errors


def _global_perm(request, permission):
    """`request.user.has_permission`, skipped for an assumed-member session.

    TWO DIFFERENT RULES live in this module and must not be conflated:

      * the tenant bound (identity_allows_group) applies to EVERY restricted
        identity, unconditionally;
      * this global short-circuit is skipped ONLY when the session ASSUMES a
        member (an override ApiKey, or any group token) — exactly the case
        where request.user is a real User whose untenanted platform-wide dict
        would be consulted. For a reference-mode or unlinked ApiKey
        request.user IS the ApiKey, so this read is the KEY's own
        group-bounded dict; skipping it there would break working deployments.
    """
    if is_override_user_session(request):
        return False
    return request.user.has_permission(permission)


def _check_group_account_permission(request, account, permission):
    if not account.startswith("group-"):
        return False
    if not request.user.is_authenticated:
        raise mojo.errors.PermissionDeniedException()
    from mojo.apps.account.models import Group
    try:
        group_id = int(account.split("-", 1)[1])
    except (ValueError, TypeError):
        raise mojo.errors.PermissionDeniedException()
    group = Group.objects.filter(id=group_id).first()
    # TENANT BOUND, before either grant path. This endpoint authorizes against
    # an arbitrary caller-named group with no model-security instance re-bind,
    # so a confined credential has to be pinned to its own group here or it
    # reads any tenant its acting member can reach.
    if not identity_allows_group(request, group):
        raise mojo.errors.PermissionDeniedException()
    if _global_perm(request, permission):
        return True
    if group is None or not group.user_has_permission(request.user, permission, False):
        raise mojo.errors.PermissionDeniedException()
    return True


def _check_user_account_permission(request, account, permission):
    if not account.startswith("user-"):
        return False
    if not request.user.is_authenticated:
        raise mojo.errors.PermissionDeniedException()
    # system-level permission can access user accounts
    if _global_perm(request, permission):
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
        if not request.user.is_authenticated or not _global_perm(request, ["view_metrics", "metrics"]):
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
            if not request.user.is_authenticated or not _global_perm(request, perms):
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
        if not request.user.is_authenticated or not _global_perm(request, ["write_metrics", "metrics"]):
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
            if not request.user.is_authenticated or not _global_perm(request, perms):
                raise mojo.errors.PermissionDeniedException()
    else:
        # "public" reads are open, but writes are not: every distinct slug is a
        # permanent registry member, so an anonymous writer could grow Redis
        # without bound. Anonymous writes need the explicit per-account opt-in
        # (set_write_perms("public", "public")); otherwise a configured perm or
        # write_metrics/metrics is required.
        perms = metrics.get_write_perms("public")
        if perms == "public":
            return
        required = perms if perms else ["write_metrics", "metrics"]
        if not request.user.is_authenticated or not _global_perm(request, required):
            raise mojo.errors.PermissionDeniedException()


def fetch_group_fanout(parent_id, child_kind, slugs, dt_start=None, dt_end=None,
                       granularity="hours", with_labels=False, breakdown=False):
    """
    Aggregate metric series for ``slugs`` across every active descendant of
    ``parent_id`` whose ``kind`` matches ``child_kind``.

    Default (``breakdown=False``) sums per-bucket across children and returns
    the same shape as ``metrics.fetch(slugs, with_labels=True)`` for a
    multi-slug call: ``{"labels": [...], "data": {slug: [int, ...]}}``.

    When ``breakdown=True`` returns one series per child, keyed by the child
    group's ``name`` (with a ``#<id>`` suffix when names collide), plus a
    ``groups`` map for ``key -> id`` lookup. Single-slug only — caller must
    pass exactly one slug or ``ValueException`` is raised.
    """
    from mojo.apps.account.models import Group

    if isinstance(slugs, str):
        slug_list = [slugs]
    else:
        slug_list = list(slugs)
    if not slug_list:
        raise mojo.errors.ValueException("fan-out requires at least one slug")
    if breakdown and len(slug_list) > 1:
        raise mojo.errors.ValueException(
            f"breakdown=true requires a single slug, got {len(slug_list)}"
        )

    parent = Group.objects.filter(id=parent_id).first()
    if parent is None:
        raise mojo.errors.ValueException(f"group-{parent_id} not found")

    max_children = settings.get_static("METRICS_FANOUT_MAX_CHILDREN", 200)
    children = list(
        parent.get_children(is_active=True, kind=child_kind)
              .values("id", "name")
    )
    if len(children) > max_children:
        raise mojo.errors.ValueException(
            f"fan-out resolved {len(children)} children, exceeds "
            f"METRICS_FANOUT_MAX_CHILDREN ({max_children})"
        )

    parent_account = f"group-{parent_id}"
    label_slugs = utils.generate_slugs_for_range(
        slug_list[0], dt_start, dt_end, granularity, parent_account
    )
    labels = utils.periods_from_dr_slugs(label_slugs)
    bucket_count = len(labels)

    if breakdown:
        return _build_breakdown(
            children, slug_list[0], dt_start, dt_end, granularity,
            bucket_count, labels, with_labels,
        )
    return _build_sum(
        children, slug_list, dt_start, dt_end, granularity,
        bucket_count, labels, with_labels,
    )


def _build_sum(children, slug_list, dt_start, dt_end, granularity,
               bucket_count, labels, with_labels):
    accumulator = {s.split(":")[-1]: [0] * bucket_count for s in slug_list}

    for child in children:
        child_account = f"group-{child['id']}"
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


def _build_breakdown(children, slug, dt_start, dt_end, granularity,
                     bucket_count, labels, with_labels):
    name_counts = {}
    for child in children:
        name_counts[child["name"]] = name_counts.get(child["name"], 0) + 1

    data = {}
    groups = {}
    for child in children:
        cid, name = child["id"], child["name"]
        key = f"{name}#{cid}" if name_counts[name] > 1 else name
        series = metrics.fetch(
            slug, dt_start=dt_start, dt_end=dt_end, granularity=granularity,
            account=f"group-{cid}", with_labels=False, allow_empty=True,
        )
        bucket = [0] * bucket_count
        for i, v in enumerate(series):
            if i < bucket_count:
                bucket[i] = int(v or 0)
        data[key] = bucket
        groups[key] = cid

    if with_labels:
        return nobjict(labels=labels, data=data, groups=groups)
    return nobjict(data=data, groups=groups)
