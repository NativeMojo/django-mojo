"""
The resource registry and the origin/URL algebra the whole server derives from.

An app that wants an endpoint protected by this authorization server registers
its absolute request path here from its ``AppConfig.ready()``:

    from mojo.apps.account.services import oauth_server
    oauth_server.register_resource("/api/assistant/mcp", ["mcp"],
                                   lambda: settings.get("ASSISTANT_MCP_ENABLED",
                                                        False, kind="bool"))

`enabled` is a zero-arg callable re-evaluated on EVERY read, never cached, so
flipping the switch takes effect immediately in both directions and an
exception while reading it counts as disabled (fail closed).

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


class ResourceRegistry:
    """Absolute request path -> (scopes, enabled callable)."""

    def __init__(self):
        self._entries = {}

    def register(self, path, scopes, enabled):
        """Register (or replace) one protected resource.

        `path` is the absolute request path exactly as routed — a leading
        slash, no trailing slash (`/api/assistant/mcp`). Re-registering a path
        replaces the entry, so a repeated ``ready()`` is idempotent.
        """
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("resource path must be an absolute request path")
        if not callable(enabled):
            raise ValueError("resource `enabled` must be a callable")
        entry = objict(path=path, scopes=list(scopes or []), enabled=enabled)
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


def register_resource(path, scopes, enabled, registry=None):
    return (registry or REGISTRY).register(path, scopes, enabled)


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
