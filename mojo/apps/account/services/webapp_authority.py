"""One authorization contract for WebApp ownership and onboarding."""

from django.db.models.functions import Lower

from mojo.apps.account.models import Group
from mojo.helpers import request as request_helpers


_PARENT_CHAIN = "__".join(["parent"] * 9)


class _AuthorityRequest:
    pass


def _is_user(user):
    request = _AuthorityRequest()
    request.user = user
    return request_helpers.is_request_user(request)


def _global_permission(user, permission):
    if not _is_user(user):
        return False
    return bool(getattr(user, "is_superuser", False) or
                user.has_permission(permission))


def is_interactive_request(request):
    """Reject every key-backed session even when it overrides to a User."""
    return (request_helpers.is_request_user(request) and
            not request_helpers.is_key_backed_session(request) and
            getattr(request, "group_token", None) is None)


def has_global_webapp_authority(user):
    """Return whether global grants cover both WebApp and DNS authority."""
    if _global_permission(user, "security"):
        return True
    return (_global_permission(user, "manage_webapp") and
            _global_permission(user, "manage_dns"))


def can_manage_group_webapps(user, group):
    """Apply the complete WebApp + DNS contract to one active group."""
    if (group is None or not _is_user(user) or
            not group.is_effectively_active()):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if group.user_has_permission(user, "security"):
        return True
    return (group.user_has_permission(user, "manage_webapp") and
            group.user_has_permission(user, "manage_dns"))


def can_create_webapp_group(user):
    """Require global group creation in addition to global WebApp authority."""
    if not _is_user(user):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return (has_global_webapp_authority(user) and
            (_global_permission(user, "manage_groups") or
             _global_permission(user, "groups")))


def eligible_webapp_groups(user):
    """Return effectively-active concrete groups this user can administer."""
    if not _is_user(user):
        return []
    groups = Group.objects.filter(is_active=True)
    if not has_global_webapp_authority(user):
        group_ids = user.get_group_ids(is_active=True, include_children=True)
        groups = groups.filter(pk__in=group_ids)
    groups = groups.select_related(_PARENT_CHAIN).order_by(Lower("name"), "id")
    return [group for group in groups if can_manage_group_webapps(user, group)]
