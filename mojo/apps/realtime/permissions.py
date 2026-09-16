"""Opt-in authorization for applications with restricted group feeds."""

import re

from mojo.helpers.settings import settings


def get_group_topic_permissions(topic):
    """None preserves legacy behavior; every other configured value opts in."""
    if not isinstance(topic, str) or not topic.startswith("group:"):
        return None
    # This security policy cannot be relaxed through database Setting rows.
    return settings.get_static("REALTIME_GROUP_TOPIC_PERMISSIONS", None)


def can_access_group_topic(identity, topic, permissions):
    """Check current primary-database grants, never a socket's stale User."""
    from mojo.apps.account.models import Group, User
    from mojo.db import use_primary

    if not isinstance(permissions, (list, tuple)) or not permissions:
        return False
    if any(not isinstance(key, str) or not key.strip() or key != key.strip()
           for key in permissions):
        return False
    if not isinstance(topic, str) or not re.fullmatch(r"group:[1-9][0-9]*", topic):
        return False
    # Other bearer identities may have the same numeric id as an account.User.
    if not isinstance(identity, User) or identity.pk is None:
        return False
    group_id = topic[6:]
    if len(group_id) > 19 or int(group_id) > 9223372036854775807:
        return False

    with use_primary():
        user = User.objects.filter(pk=identity.pk, is_active=True).first()
        if user is None:
            return False
        group = Group.get_active(int(group_id))
        return group is not None and group.user_has_permission(user, list(permissions))
