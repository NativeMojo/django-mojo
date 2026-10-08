"""
Notification kinds registry.

A process-local catalogue of the notification kinds a project sends, so a
client can render a preferences screen (label + description per kind)
without hard-coding the list.

Register kinds once at startup (e.g. in your app's ``AppConfig.ready()``)
so every process — web, jobs, realtime — sees the same catalogue::

    from mojo.apps.account.services.notification_kinds import register_notification_kinds

    register_notification_kinds([
        {"kind": "billing", "label": "Billing", "description": "Invoices and receipts",
         "channels": ["email", "in_app"]},
        {"kind": "marketing", "label": "News & offers"},
    ])

The registry is descriptive only: it does not gate delivery. Preferences
for unregistered kinds are still stored and enforced. ``"*"`` is reserved
for the per-channel master switch and can never be registered.
"""
import re

KIND_PATTERN = re.compile(r"^[a-z0-9_.-]+$")
MAX_KIND_LENGTH = 64
RESERVED_KINDS = {"*"}

# kind -> entry dict; dict insertion order is the registration order.
_REGISTRY = {}


def _clean_entry(entry):
    if not isinstance(entry, dict):
        raise ValueError("notification kind entry must be a dict")
    kind = entry.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("notification kind must be a non-empty string")
    if kind in RESERVED_KINDS:
        raise ValueError(f"notification kind '{kind}' is reserved")
    if len(kind) > MAX_KIND_LENGTH or not KIND_PATTERN.match(kind):
        raise ValueError(
            f"invalid notification kind '{kind}': use lowercase [a-z0-9_.-], max {MAX_KIND_LENGTH} chars")
    label = entry.get("label")
    if not isinstance(label, str) or not label:
        raise ValueError(f"notification kind '{kind}' needs a non-empty label")
    description = entry.get("description") or ""
    if not isinstance(description, str):
        raise ValueError(f"notification kind '{kind}' description must be a string")
    channels = entry.get("channels")
    if channels is not None:
        if not isinstance(channels, (list, tuple)):
            raise ValueError(f"notification kind '{kind}' channels must be a list or None")
        for channel in channels:
            if not isinstance(channel, str) or not channel:
                raise ValueError(f"notification kind '{kind}' has an invalid channel: {channel!r}")
        channels = list(channels)
    return {"kind": kind, "label": label, "description": description, "channels": channels}


def register_notification_kinds(kinds):
    """
    Register (or replace) notification kinds.

    Args:
        kinds: list of dicts ``{"kind", "label", "description"="", "channels"=None}``.
            ``kind`` must match ``[a-z0-9_.-]+`` (max 64 chars) and must not be
            ``"*"``. ``channels`` is None (all channels) or a list of channel
            names the kind is delivered on.

    Re-registering an existing kind replaces its entry but keeps its original
    position. The whole list is validated before anything is stored; on a bad
    entry ``ValueError`` is raised and the registry is unchanged.
    """
    if not isinstance(kinds, (list, tuple)):
        raise ValueError("kinds must be a list of dicts")
    cleaned = [_clean_entry(entry) for entry in kinds]
    for entry in cleaned:
        _REGISTRY[entry["kind"]] = entry


def list_notification_kinds():
    """Return the registered kinds (copies), in registration order."""
    return [
        dict(entry, channels=list(entry["channels"]) if entry["channels"] is not None else None)
        for entry in _REGISTRY.values()
    ]


register_notification_kinds([
    {"kind": "general", "label": "General", "description": "Messages from this service"},
])
