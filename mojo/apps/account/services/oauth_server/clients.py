"""
Client identity: redirect-URI rules, RFC 7591 registration, and CIMD resolution.

Two ways a client comes to exist, both public (no secret is ever issued):

  - **Dynamic Client Registration** (RFC 7591) — POST metadata, get a
    ``client_id``. The server is LENIENT about what it accepts and echoes what
    it actually supports (§3.2.1), because SDK defaults routinely ask for
    ``client_secret_post`` and refusing them buys nothing.
  - **Client ID Metadata Document** — the ``client_id`` IS an https URL whose
    document describes the client. The document is fetched through the shared
    SSRF-safe helper, capped and cached, and must name itself.

Redirect URIs are exact-string matched, with the one exception RFC 8252 §7.3
makes mandatory: a loopback ``http`` URI matches on any port, because CLI
clients bind an ephemeral port per run.
"""
import hashlib
import json
import uuid
from urllib.parse import urlsplit

from mojo.helpers import logit
from mojo.helpers.redis import get_connection
from mojo.helpers.safe_fetch import safe_fetch

MAX_REDIRECT_URIS = 10
MAX_REDIRECT_URI_LENGTH = 2048
MAX_CLIENT_NAME_LENGTH = 200
CIMD_CACHE_SECONDS = 300
CIMD_MAX_BYTES = 65536
CIMD_TIMEOUT = 5

LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
SUPPORTED_GRANT_TYPES = ("authorization_code", "refresh_token")
SUPPORTED_RESPONSE_TYPES = ("code",)
HTTPS_METADATA_FIELDS = ("client_uri", "logo_uri", "policy_uri", "tos_uri")


class ClientError(Exception):
    """An RFC error code plus a description safe to hand back to the client."""

    def __init__(self, code="invalid_client", description=""):
        self.code = code
        self.description = description or code
        super().__init__(self.description)


def _is_loopback_http(parts):
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def validate_redirect_uri(uri):
    """Return the canonical redirect URI, or raise ValueError.

    https anywhere, or http on a loopback host. No custom schemes (they cannot
    be attributed to anyone), no remote http, no fragment, no userinfo.

    The ASCII/isprintable test is not cosmetic: ``urlsplit`` silently strips
    CR, LF and TAB, so a URI carrying them would validate here in one shape and
    reach a ``Location`` header in another.
    """
    if not isinstance(uri, str) or not uri:
        raise ValueError("redirect_uri must be a string")
    if len(uri) > MAX_REDIRECT_URI_LENGTH:
        raise ValueError("redirect_uri is too long")
    if not uri.isascii() or not uri.isprintable():
        raise ValueError("redirect_uri contains invalid characters")
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise ValueError("redirect_uri is not a valid URL")
    if parts.fragment:
        raise ValueError("redirect_uri must not contain a fragment")
    if parts.username or parts.password:
        raise ValueError("redirect_uri must not contain credentials")
    if not parts.hostname:
        raise ValueError("redirect_uri must include a host")
    if parts.scheme == "https":
        return uri
    if _is_loopback_http(parts):
        return uri
    raise ValueError("redirect_uri must use https, or http on a loopback host")


def redirect_uri_matches(registered, presented):
    """Exact string equality — except that loopback http ignores the port.

    RFC 8252 §7.3 / OAuth 2.1 §8.4.2 make the loopback exception a MUST: a
    native client binds whatever port the OS gave it at request time, and the
    server has no way to know it in advance. Everything else stays exact, which
    is what keeps an attacker from smuggling a token to a sibling path.
    """
    if not isinstance(registered, str) or not isinstance(presented, str):
        return False
    if registered == presented:
        return True
    try:
        a = urlsplit(registered)
        b = urlsplit(presented)
        a.port  # noqa: B018 - a malformed port must not compare equal
        b.port  # noqa: B018
    except ValueError:
        return False
    if not (_is_loopback_http(a) and _is_loopback_http(b)):
        return False
    return (a.scheme, a.hostname, a.path, a.query) == (b.scheme, b.hostname, b.path, b.query)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _clean_redirect_uris(value, error_code="invalid_redirect_uri"):
    uris = _as_list(value)
    if not uris:
        raise ClientError(error_code, "redirect_uris is required")
    if len(uris) > MAX_REDIRECT_URIS:
        raise ClientError(
            error_code, f"at most {MAX_REDIRECT_URIS} redirect_uris are accepted")
    cleaned = []
    for uri in uris:
        try:
            cleaned.append(validate_redirect_uri(uri))
        except ValueError as err:
            raise ClientError(error_code, str(err))
    return cleaned


def _intersect(requested, supported):
    """The requested values this server supports, in the server's own order."""
    asked = set(str(item) for item in _as_list(requested))
    kept = [item for item in supported if item in asked]
    return kept or list(supported)


def register_client(data):
    """RFC 7591 Dynamic Client Registration. Returns the response dict.

    Substitution over refusal: an unsupported ``token_endpoint_auth_method`` is
    replaced with ``none`` and echoed, and grant/response types are intersected
    with what the server does. Only a bad ``redirect_uris`` is fatal — that one
    is a security boundary, not a preference.
    """
    from mojo.apps.account.models.oauth_client import OAuthClient

    data = data or {}
    redirect_uris = _clean_redirect_uris(data.get("redirect_uris"))

    client_name = data.get("client_name") or ""
    if not isinstance(client_name, str):
        raise ClientError("invalid_client_metadata", "client_name must be a string")
    if len(client_name) > MAX_CLIENT_NAME_LENGTH:
        raise ClientError("invalid_client_metadata", "client_name is too long")

    metadata = {}
    for field in HTTPS_METADATA_FIELDS:
        value = data.get(field)
        if value in (None, ""):
            continue
        if not isinstance(value, str) or not value.startswith("https://"):
            raise ClientError("invalid_client_metadata", f"{field} must be an https URL")
        metadata[field] = value
    for field in ("software_id", "software_version"):
        value = data.get(field)
        if isinstance(value, str) and value:
            metadata[field] = value[:200]

    grant_types = _intersect(data.get("grant_types"), SUPPORTED_GRANT_TYPES)
    response_types = _intersect(data.get("response_types"), SUPPORTED_RESPONSE_TYPES)

    client = OAuthClient(
        client_id=uuid.uuid4().hex,
        kind="dcr",
        client_name=client_name,
        redirect_uris=redirect_uris,
        metadata=metadata,
        is_active=True)
    client.save()
    logit.info(f"oauth: registered client {client.client_id} ({client_name or 'unnamed'})")

    return {
        "client_id": client.client_id,
        "client_id_issued_at": int(client.created.timestamp()),
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": grant_types,
        "response_types": response_types,
    }


# --- Client ID Metadata Documents ----------------------------------------

def _cimd_cache_key(url):
    return f"oauth:cimd:{hashlib.sha256(url.encode()).hexdigest()}"


def _cache_get(url):
    try:
        raw = get_connection().get(_cimd_cache_key(url))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _cache_set(url, document):
    try:
        get_connection().setex(
            _cimd_cache_key(url), CIMD_CACHE_SECONDS, json.dumps(document))
    except Exception:
        logit.exception("oauth: could not cache a client metadata document")


def default_fetcher(url):
    """Fetch one client metadata document. Returns (status, content_type, body).

    Thin adapter over the shared SSRF-safe helper: https only, 5 s, 64 KiB. A
    refusal (private address, unresolvable host, redirect to a private host,
    timeout) raises, and every caller maps that to ``invalid_client``.
    """
    result, error = safe_fetch(
        url, timeout=CIMD_TIMEOUT, max_bytes=CIMD_MAX_BYTES, schemes=("https",))
    if error is not None or result is None:
        raise ValueError(error or "could not fetch the client metadata document")
    content_type = ""
    try:
        content_type = result.headers.get("content-type", "") or ""
    except Exception:
        content_type = ""
    return result.status_code, content_type, result.content


def _validate_cimd_url(url):
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018
    except ValueError:
        raise ClientError("invalid_client", "client_id is not a valid URL")
    if parts.scheme != "https" or not parts.hostname:
        raise ClientError("invalid_client", "client_id must be an https URL")
    if parts.query or parts.fragment:
        raise ClientError(
            "invalid_client", "client_id must not carry a query or fragment")
    if parts.path in ("", "/"):
        raise ClientError("invalid_client", "client_id must name a document path")
    return parts


def _validate_cimd_document(url, status, content_type, body):
    if status != 200:
        raise ClientError("invalid_client", "client metadata document is unavailable")
    if "json" not in str(content_type or "").lower():
        raise ClientError("invalid_client", "client metadata document is not JSON")
    if body is None or len(body) > CIMD_MAX_BYTES:
        raise ClientError("invalid_client", "client metadata document is too large")
    try:
        document = json.loads(body)
    except Exception:
        raise ClientError("invalid_client", "client metadata document is not JSON")
    if not isinstance(document, dict):
        raise ClientError("invalid_client", "client metadata document is not an object")
    if document.get("client_id") != url:
        raise ClientError("invalid_client", "client metadata document does not name itself")
    document["redirect_uris"] = _clean_redirect_uris(
        document.get("redirect_uris"), error_code="invalid_client")
    return document


def _resolve_cimd_client(url, fetcher=None):
    from mojo.apps.account.models.oauth_client import OAuthClient

    parts = _validate_cimd_url(url)
    existing = OAuthClient.objects.filter(client_id=url).first()
    if existing is not None and not existing.is_active:
        # The Admin's deactivation is the only kill switch a CIMD client has.
        # Refuse BEFORE any fetch or write, so re-resolving can never
        # resurrect the row.
        raise ClientError("invalid_client", "client is not active")

    document = _cache_get(url)
    if document is None:
        fetch = fetcher if fetcher is not None else default_fetcher
        try:
            status, content_type, body = fetch(url)
        except ClientError:
            raise
        except Exception:
            raise ClientError("invalid_client", "could not read the client metadata document")
        document = _validate_cimd_document(url, status, content_type, body)
        _cache_set(url, document)
    else:
        # A cached document was validated before it was stored; re-checking is
        # cheap and keeps a poisoned cache entry from becoming trust.
        document = _validate_cimd_document(
            url, 200, "application/json", json.dumps(document).encode())

    metadata = {}
    for field in HTTPS_METADATA_FIELDS + ("software_id", "software_version"):
        value = document.get(field)
        if isinstance(value, str) and value:
            metadata[field] = value[:2048]

    client, _created = OAuthClient.objects.get_or_create(
        client_id=url, defaults=dict(kind="cimd", is_active=True))
    client_name = document.get("client_name")
    if not isinstance(client_name, str) or not client_name:
        client_name = parts.hostname
    client.kind = "cimd"
    client.client_name = client_name[:MAX_CLIENT_NAME_LENGTH]
    client.redirect_uris = document["redirect_uris"]
    client.metadata = metadata
    # is_active is deliberately NOT written here — only on create.
    client.save(update_fields=["kind", "client_name", "redirect_uris",
                               "metadata", "modified"])
    return client


def resolve_client(client_id, fetcher=None):
    """Return the active OAuthClient for `client_id`, or raise ClientError."""
    from mojo.apps.account.models.oauth_client import OAuthClient

    if not isinstance(client_id, str) or not client_id:
        raise ClientError("invalid_client", "client_id is required")
    if len(client_id) > 512:
        raise ClientError("invalid_client", "client_id is too long")
    if client_id.startswith("https://"):
        return _resolve_cimd_client(client_id, fetcher=fetcher)
    client = OAuthClient.objects.filter(client_id=client_id).first()
    if client is None or not client.is_active:
        raise ClientError("invalid_client", "unknown client")
    return client
