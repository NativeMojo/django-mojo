"""Shared policy for dnsman's interactive platform administration boundary."""

from mojo.helpers.request import is_key_backed_session


def is_platform_admin(request, user=None):
    if is_key_backed_session(request):
        return False
    return bool(getattr(user if user is not None else request.user, "is_superuser", False))
