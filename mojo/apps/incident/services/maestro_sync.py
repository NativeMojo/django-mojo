"""
Maestro board link client (DM-040) — the wire layer for pushing incident
tickets into a remote maestro board and applying signed board webhooks back
onto tickets.

Every request/response shape of maestro's board link API lives in this one
file so contract drift is a one-file fix. Contract source: maestro repo
planning/confirmed/maestro-connect.md (superseded by maestro's
docs/web_developer/boards/linking.md once that ships).

Outbound sync is fail-open: calls run in jobs (small retry, then drop with a
local log) and never raise into a ticket save. Registration is the exception —
it runs synchronously inside MaestroBoard's REST save and is fail-closed so an
admin pasting a bad link sees the failure immediately.
"""
from urllib.parse import urlparse

import requests

from mojo.errors import ValueException
from mojo.helpers import dates, logit
from mojo.helpers.settings import settings

logger = logit.get_logger("incident", "incident.log")

PROTOCOL_VERSION = 1
SYNCED_TICKET_FIELDS = ("title", "description", "status")
TITLE_MAX = 255
NOTE_TEXT_MAX = 10000


class MaestroRequestError(Exception):
    """A failed call to the maestro link API.

    retriable=True (timeouts, connection errors, 5xx) means the jobs engine
    should retry; False (4xx — revoked key, wrong board, validation) is
    terminal and must be dropped with a log, never retried.
    """

    def __init__(self, message, status=None, retriable=False):
        super().__init__(message)
        self.status = status
        self.retriable = retriable


def parse_paste_url(url):
    """Parse a pasted maestro link URL into (api_base, raw_key).

    Paste format: https://<host>/api/boards/link/<key> — nothing is served at
    that path; it exists only to carry the endpoint and key in one string.
    """
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueException("invalid maestro board link", 400)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 4 or parts[:3] != ["api", "boards", "link"]:
        raise ValueException("invalid maestro board link", 400)
    return f"{parsed.scheme}://{parsed.netloc}", parts[3]


def get_callback_url(board):
    """This project's webhook receiver URL, submitted at registration."""
    base = settings.get("MAESTRO_CALLBACK_BASE", None) or settings.get("BASE_URL", None)
    base = str(base or "").strip().rstrip("/")
    if not base:
        raise ValueException(
            "BASE_URL (or MAESTRO_CALLBACK_BASE) must be configured to register a maestro board", 400)
    return f"{base}/api/incident/maestro/webhook/{board.callback_token}"


def _post(board, path, payload):
    """POST a versioned payload to maestro with the board's link key.

    Returns the parsed JSON response on 2xx; raises MaestroRequestError
    otherwise. Remote error bodies are logged for operators but never echoed
    into user-visible messages.
    """
    key = board.get_secret("link_key")
    if not key or not board.api_url:
        raise MaestroRequestError("board has no link key or endpoint", retriable=False)
    url = f"{board.api_url.rstrip('/')}/api/boards/{path}"
    body = {"v": PROTOCOL_VERSION}
    body.update(payload)
    timeout = settings.get_static("MAESTRO_LINK_TIMEOUT", 10)
    try:
        resp = requests.post(
            url, json=body,
            headers={"Authorization": f"linkkey {key}"},
            timeout=timeout, allow_redirects=False)
    except requests.Timeout:
        logger.warning("[maestro] %s timed out after %ss", url, timeout)
        raise MaestroRequestError(f"maestro timed out after {timeout}s", retriable=True)
    except Exception as err:
        logger.warning("[maestro] %s failed: %s", url, err)
        raise MaestroRequestError("maestro is unreachable", retriable=True)

    if resp.status_code >= 400:
        try:
            raw = resp.text[:500]
        except Exception:
            raw = ""
        logger.warning("[maestro] HTTP %s from %s: %s", resp.status_code, url, raw)
        raise MaestroRequestError(
            f"maestro rejected the request (HTTP {resp.status_code})",
            status=resp.status_code, retriable=resp.status_code >= 500)
    try:
        return resp.json()
    except Exception:
        raise MaestroRequestError("maestro returned an invalid response", retriable=False)


def register(board):
    """Validate the board's link against maestro and cache the board schema.

    Called synchronously from MaestroBoard.on_rest_pre_save — raises
    ValueException (400) on any failure so a bad paste never persists.
    Mutates name/remote_board_id/schema on the instance; the caller saves.
    """
    if not board.api_url or not board.get_secret("link_key"):
        raise ValueException("a maestro board link (paste_url) is required", 400)
    callback_url = get_callback_url(board)
    try:
        data = _post(board, "link/register", {"callback_url": callback_url})
    except MaestroRequestError as err:
        raise ValueException(f"maestro board registration failed: {err}", 400)
    binfo = data.get("board") if isinstance(data, dict) else None
    if not isinstance(binfo, dict) or not binfo.get("id"):
        raise ValueException("maestro board registration failed: unexpected response", 400)
    board.remote_board_id = binfo.get("id")
    board.name = binfo.get("name") or board.name
    board.schema = {
        "label": data.get("label") or "",
        "columns": binfo.get("columns") or [],
    }


def _status_values(board, status):
    """Map a ticket status onto the board's category column via status_map.

    Returns a values dict ({column_slug: option_value}) or None when no map is
    configured or the status has no option (logged, never fatal).
    """
    smap = board.status_map or {}
    column = smap.get("column")
    mapping = smap.get("map") or {}
    if not column:
        return None
    value = mapping.get(status)
    if value is None:
        logger.info("[maestro] board %s status_map has no option for ticket status %r", board.pk, status)
        return None
    return {column: value}


def build_item_payload(board, ticket):
    base = str(settings.get("BASE_URL", "") or "").rstrip("/")
    payload = {
        "title": (ticket.title or "")[:TITLE_MAX],
        "description": ticket.description or "",
        "source": {
            "project": settings.get("PROJECT_NAME", "") or urlparse(base).netloc,
            "ticket_id": ticket.pk,
            "url": f"{base}/api/incident/ticket/{ticket.pk}" if base else "",
        },
    }
    values = _status_values(board, ticket.status)
    if values:
        payload["values"] = values
    return payload


def push_ticket(board, ticket):
    """Create or update the board item for a ticket. Idempotent — an existing
    link (or one that wins a concurrent-create race) updates instead of
    creating a duplicate item."""
    from django.db import IntegrityError
    from mojo.apps.incident.models import MaestroBoardLink

    link = MaestroBoardLink.objects.filter(ticket=ticket, maestro_board=board).first()
    payload = build_item_payload(board, ticket)
    if link is None:
        data = _post(board, "link/item", payload)
        remote_id = (data or {}).get("id") if isinstance(data, dict) else None
        if not remote_id:
            raise MaestroRequestError("maestro item create returned no id", retriable=False)
        remote_url = str((data or {}).get("url") or "")
        if remote_url.startswith("/"):
            remote_url = f"{board.api_url.rstrip('/')}{remote_url}"
        try:
            link = MaestroBoardLink.objects.create(
                ticket=ticket, maestro_board=board,
                remote_item_id=remote_id, remote_url=remote_url)
        except IntegrityError:
            # Concurrent push won the race — the ticket is linked; fall through
            # to stamping last_synced on the winner's row.
            link = MaestroBoardLink.objects.get(ticket=ticket, maestro_board=board)
        else:
            ticket.add_note(
                f"Pushed to maestro board '{board.name}': {link.remote_url}",
                None,
                metadata={"origin": "maestro", "type": "board_link", "board": board.pk})
    else:
        _post(board, f"link/item/{link.remote_item_id}", payload)
    link.last_synced = dates.utcnow()
    link.save(update_fields=["last_synced", "modified"])
    return link


def sync_ticket_change(link, changed):
    """Push changed ticket fields (title/description/status) to the board item."""
    board = link.maestro_board
    ticket = link.ticket
    payload = {}
    if "title" in changed:
        payload["title"] = (ticket.title or "")[:TITLE_MAX]
    if "description" in changed:
        payload["description"] = ticket.description or ""
    if "status" in changed:
        values = _status_values(board, ticket.status)
        if values:
            payload["values"] = values
    if not payload:
        return
    _post(board, f"link/item/{link.remote_item_id}", payload)
    link.last_synced = dates.utcnow()
    link.save(update_fields=["last_synced", "modified"])


def push_note(link, note):
    """Mirror a ticket note as a board item comment."""
    _post(link.maestro_board, "link/note", {
        "item": link.remote_item_id,
        "text": (note.note or "")[:NOTE_TEXT_MAX],
    })
    link.last_synced = dates.utcnow()
    link.save(update_fields=["last_synced", "modified"])


def handle_board_webhook(board, payload):
    """Apply a signature-verified maestro webhook to the linked ticket.

    Every write here is a direct ORM save (add_note / save(update_fields)) —
    never the REST pipeline — so nothing in this path can re-enter the
    outbound sync hooks (echo suppression). Unknown items and events return
    200/ignored so maestro's queue stays quiet.
    """
    from mojo.apps.incident.models import MaestroBoardLink

    event = payload.get("event") or ""
    item = payload.get("item") or {}
    link = MaestroBoardLink.objects.filter(
        maestro_board=board, remote_item_id=item.get("id")).select_related("ticket").first()
    if link is None:
        logger.info("[maestro] webhook for unlinked item %s on board %s — ignored", item.get("id"), board.pk)
        return {"status": True, "ignored": True}

    ticket = link.ticket
    meta = {
        "origin": "maestro",
        "event": event,
        "board": board.pk,
        "remote_item_id": link.remote_item_id,
    }
    if event == "note.created":
        note = payload.get("note") or {}
        author = note.get("author") or "maestro"
        meta["remote_note_id"] = note.get("id")
        meta["author"] = author
        ticket.add_note(f"[maestro] {author}: {note.get('text') or ''}", None, metadata=meta)
    elif event == "item.updated":
        changes = payload.get("changes") or []
        summary = "; ".join(
            f"{c.get('column')}: {c.get('old')} -> {c.get('new')}" for c in changes
        ) or "item updated"
        meta["changes"] = changes
        ticket.add_note(f"[maestro] Board item updated — {summary}", None, metadata=meta)
        _apply_status_change(board, ticket, changes)
    elif event in ("item.archived", "item.restored"):
        ticket.add_note(f"[maestro] Board item {event.split('.', 1)[1]}", None, metadata=meta)
    else:
        logger.info("[maestro] unknown webhook event %r on board %s — ignored", event, board.pk)
        return {"status": True, "ignored": True}
    return {"status": True}


def _apply_status_change(board, ticket, changes):
    """Reverse-map a board category-column change onto ticket.status.

    Only runs when status_map is configured; a reverse-map miss leaves the
    status untouched (the change note above already records it). The save is
    compare-before-write and direct ORM — no REST hooks, no outbound echo.
    """
    smap = board.status_map or {}
    column = smap.get("column")
    mapping = smap.get("map") or {}
    if not column:
        return
    reverse = {v: k for k, v in mapping.items()}
    for change in changes:
        if change.get("column") != column:
            continue
        new_status = reverse.get(change.get("new"))
        if new_status and ticket.status != new_status:
            ticket.status = new_status
            ticket.save(update_fields=["status", "modified"])
        return


def _enqueue(func, payload):
    # Fail-open: outbound sync must never break a ticket save — publish
    # errors are logged and dropped.
    try:
        from mojo.apps import jobs
        jobs.publish(
            func, payload,
            channel="incident_handlers",
            max_retries=3, backoff_base=2.0)
    except Exception:
        logger.exception("[maestro] failed to enqueue %s %s", func, payload)


def enqueue_push(ticket_id, board_id):
    _enqueue("mojo.apps.incident.asyncjobs.maestro_push_ticket",
             {"ticket_id": ticket_id, "board_id": board_id})


def enqueue_sync(link_id, changed):
    _enqueue("mojo.apps.incident.asyncjobs.maestro_sync_change",
             {"link_id": link_id, "changed": list(changed)})


def enqueue_note(link_id, note_id):
    _enqueue("mojo.apps.incident.asyncjobs.maestro_push_note",
             {"link_id": link_id, "note_id": note_id})
