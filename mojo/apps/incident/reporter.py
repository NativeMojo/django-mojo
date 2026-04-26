import socket


def report_event(details, title=None, category="api_error", level=1, request=None, scope="global", **kwargs):
    from .models import Event
    event_data = _create_event_dict(details, title, category, level, request, scope, **kwargs)
    event = Event(**event_data)
    event.sync_metadata()
    event.save()
    event.publish()


def _resolve_event_group(kwargs, request):
    """
    Resolve the originating group for an event:

      1. Caller-supplied ``group=`` kwarg wins (including explicit
         ``None``, which suppresses the auto-stamp). Higher layers
         (MojoModel.report_incident / class_report_incident_for_user)
         pre-resolve instance.group / request.group and pass it as
         ``group=`` so the reporter respects their decision.
      2. If the caller did not pass ``group``, fall back to
         ``request.group`` so direct callers still get group context.

    Returns a Group instance or None. Pops the ``group`` kwarg from
    ``kwargs`` so it does not leak into ``processed_kwargs``. The
    ``isinstance`` guard rejects non-Group truthy values (e.g. a
    model that uses ``.group`` as a string).
    """
    from mojo.apps.account.models import Group

    if "group" in kwargs:
        candidate = kwargs.pop("group")
        return candidate if isinstance(candidate, Group) else None

    if request is not None:
        candidate = getattr(request, "group", None)
        if isinstance(candidate, Group):
            return candidate

    return None


def _create_event_dict(details, title=None, category="api_error", level=1, request=None, scope="global", **kwargs):
    if title is None:
        title = details[:50]

    group = _resolve_event_group(kwargs, request)

    event_data = {
        "details": details,
        "title": title,
        "scope": scope,
        "category": category,
        "level": level,
        "uid": kwargs.pop("uid", None),
        "hostname": kwargs.pop("hostname", None),
        "model_name": kwargs.pop("model_name", None),
        "model_id": kwargs.pop("model_id", None),
        "source_ip": kwargs.pop("source_ip", None),
        "group": group,
    }

    event_metadata = {
        "server": socket.gethostname()
    }

    if request:
        event_data["source_ip"] = request.ip if event_data["source_ip"] is None else event_data["source_ip"]
        event_metadata.update({
            "request_ip": request.ip,
            "http_path": request.path,
            "http_protocol": request.META.get("SERVER_PROTOCOL", ""),
            "http_method": request.method,
            "http_query_string": request.META.get("QUERY_STRING", ""),
            "http_user_agent": request.META.get("HTTP_USER_AGENT", ""),
            "http_host": request.META.get("HTTP_HOST", "")
        })
        if request.user.is_authenticated:
            event_data["uid"] = request.user.id
            if request.bearer:
                from mojo.helpers.logit import mask_token
                event_metadata["bearer"] = mask_token(request.bearer)
            event_metadata["user_name"] = request.user.display_name
            event_metadata["user_email"] = request.user.email

    if group is not None:
        event_metadata["group_id"] = getattr(group, "id", None)
        event_metadata["group_name"] = getattr(group, "name", None)

    from mojo.helpers.logit import sanitize_dict

    processed_kwargs = {}
    for k, v in kwargs.items():
        if k not in event_data:
            if isinstance(v, dict):
                processed_kwargs[k] = sanitize_dict(v)
            elif is_json_serializable(v):
                processed_kwargs[k] = v
            elif hasattr(v, 'id'):
                processed_kwargs[k] = v.id
            else:
                processed_kwargs[k] = str(v)

    event_metadata.update(processed_kwargs)
    event_data['metadata'] = event_metadata
    return event_data

def is_json_serializable(value):
    return isinstance(value, (str, int, float, bool, type(None), list, dict))
