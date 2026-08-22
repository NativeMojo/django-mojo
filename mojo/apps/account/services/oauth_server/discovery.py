"""
The two discovery documents and the WWW-Authenticate challenge builder.

Only the PATH-SUFFIXED discovery forms exist (RFC 8414 §3.1 / RFC 9728):

    /.well-known/oauth-authorization-server/api/account/oauth
    /.well-known/oauth-protected-resource/api/assistant/mcp

The root forms are deliberately left free so a different product's
authorization server can occupy them on the same host. MCP clients are
required to support the path-suffixed form (spec 2025-06-18 §2.3.2).

Every document is None — and the endpoint therefore 404s — while the server is
unconfigured: no BASE_URL, or no registered resource currently enabled.
"""
from . import resources


def authorization_server_metadata(origin, registry=None):
    """RFC 8414 authorization-server metadata, or None when not ready."""
    if not resources.is_ready(origin, registry):
        return None
    iss = resources.issuer(origin)
    return {
        "issuer": iss,
        "authorization_endpoint": f"{iss}/authorize",
        "token_endpoint": f"{iss}/token",
        "registration_endpoint": f"{iss}/register",
        "revocation_endpoint": f"{iss}/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
        "authorization_response_iss_parameter_supported": True,
        "client_id_metadata_document_supported": True,
    }


def protected_resource_metadata(origin, path, registry=None):
    """RFC 9728 metadata for one resource, or None when it is not live."""
    if not origin:
        return None
    entry = resources.resolve(path, registry)
    if entry is None or not resources.is_enabled(entry, registry):
        return None
    return {
        "resource": resources.canonical_url(origin, entry.path),
        "authorization_servers": [resources.issuer(origin)],
        "scopes_supported": list(entry.scopes),
        "bearer_methods_supported": ["header"],
    }


def _quoted(value):
    """One auth-param value, safe to place inside a response header.

    CR/LF are stripped rather than escaped — Django raises BadHeaderError on
    them, which from inside middleware would surface as an unhandled 500
    instead of a clean challenge.
    """
    text = str(value or "")
    for bad in ("\r", "\n"):
        text = text.replace(bad, "")
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def www_authenticate(path, error="invalid_token", description="", scope=""):
    """Build the `WWW-Authenticate` header VALUE for a resource path.

    Who emits it:
      - the auth middleware, for every bad-token 401 at a live resource path
        (a bad bearer never reaches a view, so the view cannot attach it);
      - the resource server itself, for the no-credential 401 and for the
        403 `insufficient_scope`.

    `resource_metadata` is omitted when the origin is unset — pointing a client
    at a URL this installation cannot serve helps nobody.
    """
    parts = [f"error={_quoted(error)}"]
    if description:
        parts.append(f"error_description={_quoted(description)}")
    if scope:
        parts.append(f"scope={_quoted(scope)}")
    origin = resources.public_origin()
    if origin:
        parts.append(f"resource_metadata={_quoted(resources.prm_url(origin, path))}")
    return "Bearer " + ", ".join(parts)
