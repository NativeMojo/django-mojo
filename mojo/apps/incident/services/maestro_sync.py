"""Workspace-scoped Maestro reporting for Tickets and Incidents.

The deployment has one Maestro integration configured in ``django.conf``.
Only per-source remote item links live in the database.  All wire shapes stay
in this module so the client and Maestro server can version the contract in one
place.
"""

import ipaddress
from urllib.parse import urlparse

import requests
from django.utils import timezone

from mojo.errors import ValueException
from mojo.helpers import logit
from mojo.helpers.settings import settings

logger = logit.get_logger("incident", "incident.log")

PROTOCOL_VERSION = 1
DEFAULT_API_URL = "https://maestromojo.com"
TITLE_MAX = 255
DESCRIPTION_MAX = 20000
NOTE_TEXT_MAX = 10000
INTEGRATION_ID_MAX = 100
PROJECT_MAX = 255
REMOTE_URL_MAX = 500


class MaestroRequestError(Exception):
    """A failed Maestro reporting call."""

    def __init__(self, message, status=None, retriable=False):
        super().__init__(message)
        self.status = status
        self.retriable = retriable


def _is_blocked_host(host):
    if not host or host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def get_config(require_key=True):
    """Return ``(api_url, api_key)`` from static deployment settings."""
    api_url = str(settings.get_static("MAESTRO_API_URL", DEFAULT_API_URL) or "").strip().rstrip("/")
    key = str(settings.get_static("MAESTRO_API_KEY", "") or "").strip()
    allow_http = bool(settings.get_static("MAESTRO_ALLOW_HTTP", False))
    parsed = urlparse(api_url)
    schemes = ("http", "https") if allow_http else ("https",)
    if (
        parsed.scheme not in schemes
        or not parsed.netloc
        or parsed.path not in ("", "/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueException("MAESTRO_API_URL must be an HTTPS origin", 400)
    if not allow_http and _is_blocked_host(parsed.hostname):
        raise ValueException("MAESTRO_API_URL must use a public host", 400)
    if require_key and not key:
        raise ValueException("MAESTRO_API_KEY is not configured", 400)
    return api_url, key


def get_callback_url():
    base = settings.get_static("MAESTRO_CALLBACK_BASE", None) or settings.get_static("BASE_URL", None)
    base = str(base or "").strip().rstrip("/")
    if not base:
        raise ValueException(
            "BASE_URL (or MAESTRO_CALLBACK_BASE) must be configured for Maestro callbacks", 400)
    parsed = urlparse(base)
    allow_http = bool(settings.get_static("MAESTRO_ALLOW_HTTP", False))
    schemes = ("http", "https") if allow_http else ("https",)
    if (
        parsed.scheme not in schemes
        or not parsed.netloc
        or parsed.path not in ("", "/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueException("Maestro callback base must be an HTTPS origin", 400)
    if not allow_http and _is_blocked_host(parsed.hostname):
        raise ValueException("Maestro callback base must use a public host", 400)
    return f"{base}/api/incident/maestro/webhook"


def _post(path, payload):
    """POST a versioned payload with the deployment reporting ApiKey."""
    api_url, key = get_config()
    url = f"{api_url}/api/boards/{path.lstrip('/')}"
    body = {"v": PROTOCOL_VERSION}
    body.update(payload)
    timeout = settings.get_static("MAESTRO_LINK_TIMEOUT", 10)
    try:
        response = requests.post(
            url,
            json=body,
            headers={"Authorization": f"apikey {key}"},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.Timeout:
        logger.warning("[maestro] %s timed out after %ss", url, timeout)
        raise MaestroRequestError(f"maestro timed out after {timeout}s", retriable=True)
    except Exception as err:
        logger.warning("[maestro] %s failed: %s", url, err)
        raise MaestroRequestError("maestro is unreachable", retriable=True)

    if response.status_code >= 400:
        logger.warning("[maestro] HTTP %s from %s", response.status_code, url)
        raise MaestroRequestError(
            f"maestro rejected the request (HTTP {response.status_code})",
            status=response.status_code,
            retriable=response.status_code >= 500,
        )
    try:
        data = response.json()
    except Exception:
        raise MaestroRequestError("maestro returned an invalid response", retriable=False)
    if not isinstance(data, dict):
        raise MaestroRequestError("maestro returned an invalid response", retriable=False)
    return data


def _integration_id(data):
    value = data.get("integration_id")
    if value is None and isinstance(data.get("integration"), dict):
        value = data["integration"].get("id")
    value = str(value or "").strip()
    if not value or len(value) > INTEGRATION_ID_MAX:
        raise MaestroRequestError("maestro returned no valid integration id", retriable=False)
    return value


def _board_id(data):
    value = data.get("board")
    if isinstance(value, dict):
        value = value.get("id")
    if isinstance(value, bool):
        value = None
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = None
    if value is None or value <= 0:
        raise MaestroRequestError("maestro returned no valid board id", retriable=False)
    return value


def register():
    """Validate configuration and register the fixed callback endpoint."""
    from mojo.apps.incident.models import MaestroItemLink

    try:
        data = _post("link/register", {"callback_url": get_callback_url()})
        integration_id = _integration_id(data)
    except MaestroRequestError as err:
        raise ValueException(f"maestro registration failed: {err}", 400)

    linked_ids = set(MaestroItemLink.objects.values_list("remote_integration_id", flat=True).distinct())
    if linked_ids and linked_ids != {integration_id}:
        raise ValueException(
            "MAESTRO_API_KEY belongs to a different integration than existing item links", 409)

    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    default_board = data.get("default_board")
    if default_board is None:
        default_board = data.get("board")
    if not isinstance(default_board, dict):
        default_board = {"id": default_board}
    if not workspace.get("id"):
        raise ValueException("maestro registration returned incomplete workspace routing", 400)
    try:
        default_board_id = _board_id({"board": default_board})
    except MaestroRequestError as err:
        raise ValueException(f"maestro registration returned incomplete workspace routing: {err}", 400)
    return {
        "integration_id": integration_id,
        "workspace": {"id": workspace.get("id"), "name": workspace.get("name") or ""},
        "default_board": {
            "id": default_board_id,
            "name": default_board.get("name") or "",
        },
    }


def parse_board_selector(value, allow_default=True):
    """Return a strict remote board id, or None for Maestro's default."""
    if value is None or value is True:
        if allow_default:
            return None
        raise ValueException("a remote Maestro board id is required", 400)
    if isinstance(value, bool):
        raise ValueException("Maestro board must be a positive integer", 400)
    try:
        board_id = int(value)
    except (TypeError, ValueError):
        raise ValueException("Maestro board must be a positive integer", 400)
    if board_id <= 0 or str(value).strip() != str(board_id):
        raise ValueException("Maestro board must be a positive integer", 400)
    return board_id


def lifecycle_for_status(status):
    status = str(status or "").lower()
    if status in ("resolved", "closed"):
        return "done"
    if status in ("paused", "ignored", "parked"):
        return "parked"
    return "active"


def build_item_payload(source):
    base = str(settings.get_static("BASE_URL", "") or "").rstrip("/")
    project = settings.get_static("PROJECT_NAME", "") or urlparse(base).netloc
    if source._meta.model_name == "ticket":
        kind = "ticket"
        title = source.title
        description = source.description
        url = f"{base}/api/incident/ticket/{source.pk}" if base else ""
    else:
        kind = "incident"
        title = source.title or source.category or f"Incident {source.pk}"
        description = source.details
        url = f"{base}/api/incident/incident/{source.pk}" if base else ""
    return {
        "title": str(title or "")[:TITLE_MAX],
        "description": str(description or "")[:DESCRIPTION_MAX],
        "priority": source.priority,
        "lifecycle": lifecycle_for_status(source.status),
        "source": {
            "project": str(project)[:PROJECT_MAX],
            "kind": kind,
            "id": source.pk,
            "url": url[:REMOTE_URL_MAX],
        },
    }


def push_source(source, board_id=None):
    """Create or update the remote item for a Ticket or Incident."""
    from django.db import IntegrityError
    from mojo.apps.incident.models import MaestroItemLink

    board_id = parse_board_selector(board_id) if board_id is not None else None
    link = source.maestro_links.first()
    payload = build_item_payload(source)
    if link is None:
        if board_id is not None:
            payload["board"] = board_id
        data = _post("link/item", payload)
        remote_id = data.get("id")
        if isinstance(remote_id, bool):
            remote_id = None
        try:
            remote_id = int(remote_id)
        except (TypeError, ValueError):
            remote_id = None
        if remote_id is None or remote_id <= 0:
            raise MaestroRequestError("maestro item create returned no id", retriable=False)
        integration_id = _integration_id(data)
        resolved_board = _board_id(data)
        remote_url = str(data.get("url") or "")[:REMOTE_URL_MAX]
        api_url, _key = get_config()
        if remote_url.startswith("/"):
            remote_url = f"{api_url}{remote_url}"
        source_field = source._meta.model_name
        try:
            link = MaestroItemLink.objects.create(
                **{source_field: source},
                remote_integration_id=integration_id,
                remote_item_id=remote_id,
                remote_board_id=resolved_board,
                remote_url=remote_url,
            )
        except IntegrityError:
            link = source.maestro_links.get()
        else:
            _record_link_created(source, link)
    else:
        _post(f"link/item/{link.remote_item_id}", payload)
    link.last_synced = timezone.now()
    link.save(update_fields=["last_synced", "modified"])
    return link


def push_ticket(ticket, board_id=None):
    return push_source(ticket, board_id)


def push_incident(incident, board_id=None):
    return push_source(incident, board_id)


def _record_link_created(source, link):
    meta = {
        "origin": "maestro",
        "type": "item_link",
        "remote_integration_id": link.remote_integration_id,
        "remote_item_id": link.remote_item_id,
    }
    text = f"Pushed to Maestro: {link.remote_url}" if link.remote_url else "Pushed to Maestro"
    if source._meta.model_name == "ticket":
        source.add_note(text, None, metadata=meta)
    else:
        source.add_history("maestro:linked", note=text, metadata=meta)


def sync_change(link, changed):
    source = link.source
    payload = {}
    if source._meta.model_name == "ticket":
        if "title" in changed:
            payload["title"] = (source.title or "")[:TITLE_MAX]
        if "description" in changed:
            payload["description"] = source.description or ""
    else:
        if "title" in changed or "category" in changed:
            payload["title"] = (source.title or source.category or f"Incident {source.pk}")[:TITLE_MAX]
        if "details" in changed:
            payload["description"] = source.details or ""
    if "priority" in changed:
        payload["priority"] = source.priority
    if "status" in changed:
        payload["lifecycle"] = lifecycle_for_status(source.status)
    if not payload:
        return
    _post(f"link/item/{link.remote_item_id}", payload)
    link.last_synced = timezone.now()
    link.save(update_fields=["last_synced", "modified"])


def push_note(link, note):
    _post("link/note", {
        "item": link.remote_item_id,
        "text": (note.note or "")[:NOTE_TEXT_MAX],
    })
    link.last_synced = timezone.now()
    link.save(update_fields=["last_synced", "modified"])


def handle_webhook(payload):
    """Apply a signature-verified callback to its linked local source."""
    from mojo.apps.incident.models import IncidentHistory, MaestroItemLink, TicketNote

    event = payload.get("event") or ""
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    integration_id = payload.get("integration_id")
    if integration_id is None and isinstance(payload.get("integration"), dict):
        integration_id = payload["integration"].get("id")
    integration_id = str(integration_id or "")
    remote_item_id = item.get("id")
    if isinstance(remote_item_id, bool):
        remote_item_id = None
    try:
        remote_item_id = int(remote_item_id)
    except (TypeError, ValueError):
        remote_item_id = None
    if (
        not integration_id
        or len(integration_id) > INTEGRATION_ID_MAX
        or remote_item_id is None
        or remote_item_id <= 0
    ):
        logger.info("[maestro] callback has no valid integration/item identity")
        return {"status": True, "ignored": True}
    link = MaestroItemLink.objects.filter(
        remote_integration_id=integration_id,
        remote_item_id=remote_item_id,
    ).select_related("ticket", "incident").first()
    if link is None:
        logger.info("[maestro] callback for unlinked integration/item %s/%s", integration_id, item.get("id"))
        return {"status": True, "ignored": True}

    _refresh_remote_location(link, item, payload)
    source = link.source
    meta = {
        "origin": "maestro",
        "event": event,
        "remote_integration_id": integration_id,
        "remote_item_id": link.remote_item_id,
    }
    if event == "note.created":
        note = payload.get("note") if isinstance(payload.get("note"), dict) else {}
        remote_note_id = note.get("id")
        if remote_note_id is not None:
            if link.ticket_id and TicketNote.objects.filter(
                parent=source, metadata__origin="maestro",
                metadata__remote_note_id=remote_note_id,
            ).exists():
                return {"status": True, "ignored": True}
            if link.incident_id and IncidentHistory.objects.filter(
                parent=source, metadata__origin="maestro",
                metadata__remote_note_id=remote_note_id,
            ).exists():
                return {"status": True, "ignored": True}
        author = note.get("author") or "maestro"
        meta.update({"remote_note_id": remote_note_id, "author": author})
        text = f"[maestro] {author}: {note.get('text') or ''}"
        _record_remote_activity(source, event, text, meta)
    elif event in ("item.updated", "item.moved"):
        changes = payload.get("changes") if isinstance(payload.get("changes"), list) else []
        summary = "; ".join(
            f"{c.get('column')}: {c.get('old')} -> {c.get('new')}"
            for c in changes if isinstance(c, dict)
        ) or ("item moved" if event == "item.moved" else "item updated")
        meta["changes"] = changes
        _record_remote_activity(source, event, f"[maestro] {summary}", meta)
        _apply_lifecycle(source, item.get("lifecycle") or payload.get("lifecycle"))
    elif event in ("item.archived", "item.restored"):
        _record_remote_activity(source, event, f"[maestro] Item {event.split('.', 1)[1]}", meta)
    else:
        logger.info("[maestro] unknown callback event %r — ignored", event)
        return {"status": True, "ignored": True}
    return {"status": True}


def _refresh_remote_location(link, item, payload):
    board = item.get("board", payload.get("board"))
    if isinstance(board, dict):
        board = board.get("id")
    try:
        board = int(board) if not isinstance(board, bool) else None
    except (TypeError, ValueError):
        board = None
    url = item.get("url") or payload.get("url")
    fields = []
    if board and board != link.remote_board_id:
        link.remote_board_id = board
        fields.append("remote_board_id")
    if url and str(url) != link.remote_url:
        link.remote_url = str(url)[:REMOTE_URL_MAX]
        fields.append("remote_url")
    if fields:
        link.last_synced = timezone.now()
        fields.extend(["last_synced", "modified"])
        link.save(update_fields=fields)


def _record_remote_activity(source, event, text, metadata):
    if source._meta.model_name == "ticket":
        source.add_note(text, None, metadata=metadata)
    else:
        source.add_history(f"maestro:{event}", note=text, metadata=metadata)


def _apply_lifecycle(source, lifecycle):
    mapping = {"active": "open", "done": "resolved", "parked": "paused"}
    new_status = mapping.get(lifecycle)
    if new_status and source.status != new_status:
        source.status = new_status
        source.save(update_fields=["status", "modified"] if hasattr(source, "modified") else ["status"])


def _enqueue(func, payload):
    try:
        from mojo.apps import jobs
        jobs.publish(
            func, payload, channel="incident_handlers",
            max_retries=3, backoff_base=2.0,
        )
    except Exception:
        logger.exception("[maestro] failed to enqueue %s %s", func, payload)


def enqueue_push(source_kind, source_id, board_id=None):
    payload = {"source_kind": source_kind, "source_id": source_id}
    if board_id is not None:
        payload["board_id"] = board_id
    _enqueue("mojo.apps.incident.asyncjobs.maestro_push_source", payload)


def enqueue_sync(link_id, changed):
    _enqueue("mojo.apps.incident.asyncjobs.maestro_sync_change", {
        "link_id": link_id,
        "changed": list(changed),
    })


def enqueue_note(link_id, note_kind, note_id):
    _enqueue("mojo.apps.incident.asyncjobs.maestro_push_note", {
        "link_id": link_id,
        "note_kind": note_kind,
        "note_id": note_id,
    })
