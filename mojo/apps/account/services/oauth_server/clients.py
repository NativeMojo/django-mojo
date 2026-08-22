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
import re
import uuid
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from mojo.helpers import logit
from mojo.helpers.redis import get_connection
from mojo.helpers.safe_fetch import safe_fetch

MAX_REDIRECT_URIS = 10
MAX_REDIRECT_URI_LENGTH = 2048
MAX_CLIENT_NAME_LENGTH = 200
CIMD_CACHE_SECONDS = 300
CIMD_MAX_BYTES = 65536
CIMD_TIMEOUT = 5

# One string for "unknown" and "deactivated" alike. Telling them apart would
# let anyone probe which client identities this installation has ever seen, and
# which of them an operator has since switched off.
UNKNOWN_CLIENT = "unknown client"
CIMD_UNREADABLE = "could not read the client metadata document"

# RFC 3986 path characters that must survive re-encoding unchanged. "%" is
# deliberately absent: after unquoting, a literal percent has to come back as
# %25, which is what makes unquote-then-quote idempotent.
CIMD_PATH_SAFE = "/-._~!$&\'()*+,;=:@"
_DUP_SLASH_RE = re.compile(r"/{2,}")
_WHITESPACE_RE = re.compile(r"\s+")

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


def clean_text(value, limit=MAX_CLIENT_NAME_LENGTH):
    """Printable, single-spaced text — safe in a log line and on a page.

    A client names itself, and that name reaches `logit.info` and the consent
    screen. A newline in it would forge a log record; other control characters
    can rewrite a terminal. Keep printable characters, fold every run of
    whitespace to one space, and truncate.
    """
    if not isinstance(value, str):
        return ""
    kept = "".join(ch for ch in value if ch.isprintable() or ch in "\t\n\r")
    return _WHITESPACE_RE.sub(" ", kept).strip()[:limit]


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
    # The client chose this string and it lands in a log line and on the
    # consent screen; strip anything that could forge either.
    client_name = clean_text(client_name)

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

def _canonical_path(path):
    """One spelling of a URL path: unquoted, de-duplicated slashes, re-quoted."""
    if not path:
        return ""
    collapsed = _DUP_SLASH_RE.sub("/", unquote(path))
    return quote(collapsed, safe=CIMD_PATH_SAFE)


def canonical_cimd_url(url):
    """The ONE identity a CIMD `client_id` URL reduces to.

    `client_id` is matched by exact string, so without this a deactivated
    `https://evil.example/c.json` comes straight back to life as a brand-new
    ACTIVE row under `https://EVIL.example/c.json`, `https://evil.example:443/c.json`,
    or a percent-encoded spelling of the same path — the kill switch would be a
    formality. Every variant has to reduce to one string before the inactive-row
    check, the cache key, the self-naming check or the row lookup sees it.

    Raises ClientError for anything that is not a usable CIMD identity.
    """
    if not isinstance(url, str) or not url:
        raise ClientError("invalid_client", "client_id must be an https URL")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise ClientError("invalid_client", "client_id is not a valid URL")
    if parts.scheme.lower() != "https":
        raise ClientError("invalid_client", "client_id must be an https URL")
    if parts.username or parts.password:
        raise ClientError("invalid_client", "client_id must not carry credentials")
    if parts.query or parts.fragment:
        raise ClientError(
            "invalid_client", "client_id must not carry a query or fragment")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ClientError("invalid_client", "client_id must include a host")
    path = _canonical_path(parts.path)
    if path in ("", "/"):
        raise ClientError("invalid_client", "client_id must name a document path")
    netloc = f"[{host}]" if ":" in host else host
    if port not in (None, 443):
        netloc = f"{netloc}:{port}"
    return urlunsplit(("https", netloc, path, "", ""))


def _cimd_cache_key(url):
    return f"oauth:cimd:{hashlib.sha256(url.encode()).hexdigest()}"


def _cache_get(url):
    """The cached verdict for a canonical URL: a dict, or None on a miss."""
    try:
        raw = get_connection().get(_cimd_cache_key(url))
    except Exception:
        return None
    if not raw:
        return None
    try:
        entry = json.loads(raw)
    except Exception:
        return None
    return entry if isinstance(entry, dict) else None


def _cache_set(url, entry):
    try:
        get_connection().setex(
            _cimd_cache_key(url), CIMD_CACHE_SECONDS, json.dumps(entry))
    except Exception:
        logit.exception("oauth: could not cache a client metadata verdict")


def default_fetcher(url):
    """Fetch one client metadata document. Returns (status, content_type, body).

    Thin adapter over the shared SSRF-safe helper: https only, 5 s, 64 KiB, and
    at most one redirect — a metadata document is a static file, so a chain of
    hops is a way to spend this server's time, not a way to publish. A refusal
    (private address, unresolvable host, redirect to a private host, timeout)
    raises, and every caller maps that to `invalid_client`.
    """
    result, error = safe_fetch(
        url, timeout=CIMD_TIMEOUT, max_bytes=CIMD_MAX_BYTES,
        max_redirects=1, schemes=("https",))
    if error is not None or result is None:
        raise ValueError(error or CIMD_UNREADABLE)
    content_type = ""
    try:
        content_type = result.headers.get("content-type", "") or ""
    except Exception:
        content_type = ""
    return result.status_code, content_type, result.content


def _validate_cimd_document(canonical, status, content_type, body):
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
    # Compared canonically, so a document may spell its own URL any way it
    # likes — but it still cannot claim an identity that is not its own.
    if canonical_cimd_url(document.get("client_id")) != canonical:
        raise ClientError("invalid_client", "client metadata document does not name itself")
    document["redirect_uris"] = _clean_redirect_uris(
        document.get("redirect_uris"), error_code="invalid_client")
    return document


def _fetch_cimd_document(canonical, fetcher=None):
    """The cached-or-fetched document. Failures are cached too.

    Without the negative entry, an attacker pointing a client_id at a URL that
    stalls or 404s turns every authorize/token attempt into an outbound fetch
    from this server. One fetch per cache window, verdict either way.
    """
    cached = _cache_get(canonical)
    if cached is not None:
        if not cached.get("ok"):
            raise ClientError("invalid_client", CIMD_UNREADABLE)
        document = cached.get("document")
        if isinstance(document, dict):
            # Cached documents were validated before they were stored;
            # re-checking is cheap and keeps a poisoned entry from becoming trust.
            return _validate_cimd_document(
                canonical, 200, "application/json", json.dumps(document).encode())

    fetch = fetcher if fetcher is not None else default_fetcher
    try:
        status, content_type, body = fetch(canonical)
        document = _validate_cimd_document(canonical, status, content_type, body)
    except ClientError:
        _cache_set(canonical, {"ok": False})
        raise
    except Exception:
        _cache_set(canonical, {"ok": False})
        raise ClientError("invalid_client", CIMD_UNREADABLE)
    _cache_set(canonical, {"ok": True, "document": document})
    return document


def _resolve_cimd_client(url, fetcher=None):
    from mojo.apps.account.models.oauth_client import OAuthClient

    canonical = canonical_cimd_url(url)
    existing = OAuthClient.objects.filter(client_id=canonical).first()
    if existing is not None and not existing.is_active:
        # The Admin's deactivation is the only kill switch a CIMD client has.
        # Refuse BEFORE any fetch or write, so re-resolving — under ANY spelling
        # of the URL — can never resurrect the row.
        raise ClientError("invalid_client", UNKNOWN_CLIENT)

    document = _fetch_cimd_document(canonical, fetcher=fetcher)

    metadata = {}
    for field in HTTPS_METADATA_FIELDS + ("software_id", "software_version"):
        value = document.get(field)
        if isinstance(value, str) and value:
            metadata[field] = clean_text(value, limit=2048)

    client, _created = OAuthClient.objects.get_or_create(
        client_id=canonical, defaults=dict(kind="cimd", is_active=True))
    client_name = clean_text(document.get("client_name"))
    if not client_name:
        client_name = urlsplit(canonical).hostname
    client.kind = "cimd"
    client.client_name = client_name
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
    if client_id.lower().startswith("https://"):
        return _resolve_cimd_client(client_id, fetcher=fetcher)
    client = OAuthClient.objects.filter(client_id=client_id).first()
    # Unknown and deactivated answer identically — see UNKNOWN_CLIENT.
    if client is None or not client.is_active:
        raise ClientError("invalid_client", UNKNOWN_CLIENT)
    return client
