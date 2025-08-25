

# TODO make this async using our task queue
async def report_event(details, title=None, category="api_error", level=1, request=None, **kwargs):
    from .models import Event
    event_data = _create_event_dict(details, title, category, level, request, **kwargs)
    event = Event(**event_data)
    event.sync_metadata()
    event.save()
    event.publish()


def _create_event_dict(details, title=None, category="api_error", level=1, request=None, **kwargs):
    if title is None:
        title = details[:50]

    event_data = {
        "details": details,
        "title": title,
        "category": category,
        "level": level,
        "hostname": kwargs.pop("hostname", None),
        "model_name": kwargs.pop("model_name", None),
        "model_id": kwargs.pop("model_id", None),
        "source_ip": kwargs.pop("source_ip", None)
    }

    event_metadata = {}

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

    event_metadata.update({k: v for k, v in kwargs.items() if k not in event_data})
    event_data['metadata'] = event_metadata
    return event_data
