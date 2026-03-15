"""
Notification preference helper.

Central check used by all delivery paths (in-app, email, push) to decide
whether a notification should be sent to a user on a given channel.

Storage lives in ``user.metadata["notification_preferences"]``.
Default is **allow** — only suppress when the user has explicitly opted out.
"""


def is_notification_allowed(user, kind, channel):
    """
    Returns True if the user has not opted out of this kind/channel combination.
    Default (no stored preference) is True — only suppress on explicit opt-out.

    Args:
        user: User instance (or None)
        kind: notification kind string (e.g. "marketing", "message")
        channel: one of "in_app", "email", "push"

    Returns:
        bool
    """
    if user is None:
        return True
    metadata = getattr(user, "metadata", None)
    if not metadata or not isinstance(metadata, dict):
        return True
    prefs = metadata.get("notification_preferences")
    if not prefs or not isinstance(prefs, dict):
        return True
    kind_prefs = prefs.get(kind)
    if not kind_prefs or not isinstance(kind_prefs, dict):
        return True
    if channel not in kind_prefs:
        return True
    return bool(kind_prefs[channel])


def get_preferences(user):
    """
    Return the full notification preferences dict for a user.
    Returns an empty dict when nothing has been set.
    """
    metadata = getattr(user, "metadata", None)
    if not metadata or not isinstance(metadata, dict):
        return {}
    prefs = metadata.get("notification_preferences")
    if not prefs or not isinstance(prefs, dict):
        return {}
    return prefs


def set_preferences(user, incoming):
    """
    Partial-update merge of *incoming* preferences into the user's stored
    preferences.  Only keys present in *incoming* are changed; others are
    left untouched.

    Args:
        user: User instance
        incoming: dict of ``{kind: {channel: bool, ...}, ...}``

    Returns:
        The full preferences dict after merging.
    """
    if not isinstance(user.metadata, dict):
        user.metadata = {}
    current = user.metadata.get("notification_preferences")
    if not current or not isinstance(current, dict):
        current = {}

    for kind, channels in incoming.items():
        if kind not in current:
            current[kind] = {}
        if isinstance(channels, dict):
            current[kind].update(channels)

    user.metadata["notification_preferences"] = current
    user.save(update_fields=["metadata", "modified"])
    return current