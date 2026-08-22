"""
The resource registry and the origin/URL algebra the whole server derives from.

An app that wants an endpoint protected by this authorization server registers
its absolute request path here from its ``AppConfig.ready()``:

    from mojo.apps.account.services import oauth_server
    oauth_server.register_resource("/api/assistant/mcp", ["mcp"], is_enabled)
    oauth_server.register_resource("/api", ["mcp", "api"], is_enabled,
                                   prefix=True)

`enabled` is a zero-arg callable re-evaluated on EVERY read, never cached, so
flipping the switch takes effect immediately in both directions and an
exception while reading it counts as disabled (fail closed).

A resource is EXACT by default: it is one endpoint, and a token minted for it
authenticates at that path and nowhere else. A ``prefix=True`` resource is a
whole subtree — the REST API root — and the invariant that keeps the two kinds
apart is enforced here, at registration: a prefix resource MUST offer the
``api`` scope and an exact one may NOT. ``resolve`` stays exact in both cases
(it is keyed by the token audience's own path); ``covers`` is what answers "may
this entry's token be presented at this request path".

Everything that consults the registry takes a ``registry=`` argument. The
default instance is module-level state shared by every test module in the
threaded runner, so tests build their own instance rather than mutating it.
"""
from urllib.parse import urlsplit

from objict import objict

from mojo.helpers import logit
from mojo.helpers.settings import settings

# Read once at import, like the bouncer's _ABS_LOGIN_PATH: the same constant
# derives the issuer, the routes and the sensitive-body labels, and they must
# not be able to disagree within one process.
SERVER_PATH = "/" + settings.get_static(
    "OAUTH_SERVER_PATH", "api/account/oauth").strip("/")

# The two scopes this server offers. `mcp` is the Assistant's tool door;
# `api` is full REST reach as the consenting person, and is reserved for a
# prefix resource — see `register` for the invariant that ties them together.
DEFAULT_SCOPE = "mcp"
API_SCOPE = "api"


class ResourceRegistry:
    """Absolute request path -> (scopes, prefix flag, enabled callable)."""

    def __init__(self):
        self._entries = {}

    def register(self, path, scopes, enabled, prefix=False):
        """Register (or replace) one protected resource.

        `path` is the absolute request path exactly as routed — a leading
        slash, no trailing slash (`/api/assistant/mcp`). Re-registering a path
        replaces the entry, so a repeated ``ready()`` is idempotent.

        `prefix=True` declares a SUBTREE rather than an endpoint: a token
        bound to it may be presented at the path itself or anywhere strictly
        beneath it. That is the whole API, so it is only ever consented to
        under the `api` scope — and the two must not be able to drift apart:
        a prefix resource that did not offer `api` would be a full-reach
        resource nothing had to ask for, and an exact resource offering `api`
        would advertise a scope its confinement cannot honour.
        """
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("resource path must be an absolute request path")
        if not callable(enabled):
            raise ValueError("resource `enabled` must be a callable")
        scopes = list(scopes or [])
        if prefix and API_SCOPE not in scopes:
            raise ValueError("a prefix resource must offer the api scope")
        if not prefix and API_SCOPE in scopes:
            raise ValueError("the api scope is reserved for prefix resources")
        entry = objict(path=path, scopes=scopes, enabled=enabled,
                       prefix=bool(prefix))
        self._entries[path] = entry
        return entry

    def unregister(self, path):
        return self._entries.pop(path, None)

    def resolve(self, path):
        return self._entries.get(path)

    def is_enabled(self, entry):
        """True only when the entry's switch says so. Any error is False."""
        if entry is None:
            return False
        try:
            return bool(entry.enabled())
        except Exception:
            logit.exception(f"oauth resource enable check failed for {entry.path}")
            return False

    def enabled(self):
        """Every currently enabled entry, in registration order."""
        return [entry for entry in self._entries.values() if self.is_enabled(entry)]

    def paths(self):
        return list(self._entries.keys())


# The shared default. Apps register into this one; tests use their own.
REGISTRY = ResourceRegistry()


def register_resource(path, scopes, enabled, registry=None, prefix=False):
    return (registry or REGISTRY).register(path, scopes, enabled, prefix=prefix)


def covers(entry, request_path):
    """May a token bound to `entry` be presented at `request_path`?

    This is the reach half of confinement, and it is deliberately separate from
    ``resolve``: the registry lookup is EXACT and keyed by the token audience's
    own path, so the entry is settled before this question is asked. An exact
    entry covers its path and nothing else — byte-for-byte the rule the server
    has always applied. A prefix entry additionally covers anything STRICTLY
    beneath it, compared with the separator attached (``/api`` covers
    ``/api/x`` but never ``/apix``), so a sibling path that merely starts with
    the same characters is not reachable.
    """
    if entry is None or not isinstance(request_path, str) or not request_path:
        return False
    if request_path == entry.path:
        return True
    if not entry.prefix:
        return False
    return request_path.startswith(entry.path.rstrip("/") + "/")


def offered_scopes(registry=None):
    """Every scope the currently ENABLED resources offer, in order, once each.

    Discovery advertises this, so an installation that registers only an exact
    MCP resource never tells a client that `api` exists.
    """
    found = []
    for entry in (registry or REGISTRY).enabled():
        for scope in entry.scopes or []:
            if scope not in found:
                found.append(scope)
    return found


def unregister_resource(path, registry=None):
    return (registry or REGISTRY).unregister(path)


def resolve(path, registry=None):
    return (registry or REGISTRY).resolve(path)


def is_enabled(entry, registry=None):
    return (registry or REGISTRY).is_enabled(entry)


def enabled_resources(registry=None):
    return (registry or REGISTRY).enabled()


def public_origin():
    """The installation's canonical public origin, or "" when unconfigured.

    Read through ``settings.get`` because BASE_URL legitimately lives in either
    plane — a System Setup database row or the deployment file. An unset or
    invalid value returns "", which every caller treats as "this authorization
    server is not configured" and refuses. There is deliberately no fallback to
    the request's Host header: an attacker-chosen host must never become an
    advertised issuer or a minted audience.
    """
    from mojo.apps.account.services import system_settings

    raw = settings.get("BASE_URL", "")
    if not raw:
        return ""
    try:
        return system_settings.validate_base_url(raw)
    except Exception:
        return ""


def issuer(origin):
    return f"{origin}{SERVER_PATH}"


def canonical_url(origin, path):
    return f"{origin}{path}"


def prm_url(origin, path):
    return f"{origin}/.well-known/oauth-protected-resource{path}"


def metadata_url(origin):
    return f"{origin}/.well-known/oauth-authorization-server{SERVER_PATH}"


def resource_path(resource_url):
    """The path half of a canonical resource URL, or "" when unreadable."""
    if not isinstance(resource_url, str) or not resource_url:
        return ""
    try:
        return urlsplit(resource_url).path
    except ValueError:
        return ""


def is_ready(origin, registry=None):
    """The ONE gate discovery, consent, registration and issuance consult."""
    if not origin:
        return False
    return bool((registry or REGISTRY).enabled())


# --- credential lifetimes -------------------------------------------------
# All read with get_static (deployment file only). A `manage_settings` holder
# must not be able to lengthen a credential lifetime through the database
# plane — same rule as AUTH_CSP_* and MOJO_TEST_MODE.

def access_ttl():
    return settings.get_static("OAUTH_ACCESS_TTL", 3600, kind="int")


def refresh_ttl_days():
    return settings.get_static("OAUTH_REFRESH_TTL_DAYS", 30, kind="int")


def refresh_grace_seconds():
    return settings.get_static("OAUTH_REFRESH_GRACE_SECONDS", 30, kind="int")


def code_ttl():
    return settings.get_static("OAUTH_CODE_TTL", 300, kind="int")
