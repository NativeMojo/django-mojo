import logging
import socket
from mojo.helpers.settings import settings

logger = logging.getLogger(__name__)

DEDUP_WINDOW_SECONDS = settings.get_static("INCIDENT_DEDUP_WINDOW_SECONDS", 60)


def report_event(details, title=None, category="api_error", level=1, request=None, scope="global", **kwargs):
    from .models import Event

    event_data = _create_event_dict(details, title, category, level, request, scope, **kwargs)

    # Dedup: if an identical event exists within the window, increment its counter
    if DEDUP_WINDOW_SECONDS and DEDUP_WINDOW_SECONDS > 0:
        try:
            from mojo.helpers import dates
            cutoff = dates.subtract(seconds=DEDUP_WINDOW_SECONDS)
            dedup_criteria = {
                "category": event_data["category"],
                "level": event_data["level"],
                "created__gte": cutoff,
            }
            if event_data.get("source_ip"):
                dedup_criteria["source_ip"] = event_data["source_ip"]
            if event_data.get("hostname"):
                dedup_criteria["hostname"] = event_data["hostname"]

            existing = Event.objects.filter(**dedup_criteria).order_by("-created").first()
            if existing:
                count = (existing.metadata or {}).get("dedup_count", 1) + 1
                existing.metadata["dedup_count"] = count
                existing.save(update_fields=["metadata"])
                logger.debug("Dedup: incremented event %s to count %d", existing.pk, count)
                return
        except Exception:
            logger.exception("Dedup check failed, creating new event")

    event = Event(**event_data)
    event.sync_metadata()
    event.save()
    event.publish()


def _create_event_dict(details, title=None, category="api_error", level=1, request=None, scope="global", **kwargs):
    if title is None:
        title = details[:50]

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
        "source_ip": kwargs.pop("source_ip", None)
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
                event_metadata["bearer"] = request.bearer
            event_metadata["user_name"] = request.user.display_name
            event_metadata["user_email"] = request.user.email

    processed_kwargs = {}
    for k, v in kwargs.items():
        if k not in event_data:
            if is_json_serializable(v):
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
