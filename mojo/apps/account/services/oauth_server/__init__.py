"""
The installation's OAuth 2.1 authorization server.

Sibling apps talk to this package, never to its modules:

    from mojo.apps.account.services import oauth_server

    # protect one of your endpoints with it, from AppConfig.ready()
    oauth_server.register_resource("/api/assistant/mcp", ["mcp"], is_enabled)

    # answer a bad or missing credential the way the spec expects
    response["WWW-Authenticate"] = oauth_server.www_authenticate(path)

    # the Admin surface (resource_path/limit optional; defaults are unscoped)
    oauth_server.list_grants(resource_path="/api/assistant/mcp", limit=200)
    oauth_server.count_grants(resource_path="/api/assistant/mcp")
    oauth_server.revoke_grant_by_id(grant_id, actor=request.user)

Everything else — discovery documents, the consent page, code exchange, refresh
rotation — is reached through the REST handlers in
``mojo/apps/account/rest/oauth_server.py``.
"""
from .discovery import www_authenticate
from .resources import (
    SERVER_PATH, canonical_url, public_origin, register_resource, resolve,
    unregister_resource,
)
from .tokens import (
    count_grants, list_grants, revoke_all_grants, revoke_grant_by_id,
)

__all__ = [
    "SERVER_PATH",
    "canonical_url",
    "count_grants",
    "list_grants",
    "public_origin",
    "register_resource",
    "resolve",
    "revoke_all_grants",
    "revoke_grant_by_id",
    "unregister_resource",
    "www_authenticate",
]
