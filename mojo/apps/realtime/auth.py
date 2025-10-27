"""
Message-based Channels auth utilities that share bearer handler resolution with HTTP middleware.

This module centralizes bearer-prefix handler resolution so WebSocket consumers
can authenticate using the exact same mechanism as the HTTP AuthenticationMiddleware.

Exposed capabilities:
- Resolve and cache bearer handlers using the same settings-driven maps
- Validate tokens synchronously or asynchronously (for use in async consumers)
- Attach the authenticated identity to the Channels scope consistently

Note: WebSocket authentication is message-only; header/query parsing helpers were removed.
"""

import logging

from asgiref.sync import sync_to_async

# Reuse the same handler maps as the HTTP middleware for a single source of truth
from mojo.middleware.auth import (
    AUTH_BEARER_HANDLER_PATHS,
    AUTH_BEARER_HANDLERS_CACHE,
    AUTH_BEARER_NAME_MAP,
)
from mojo.helpers import modules

logger = logging.getLogger(__name__)

# Public API surface
__all__ = [
    "validate_bearer_token",
    "async_validate_bearer_token",
    "attach_identity_to_scope",
]


def resolve_bearer_handler(prefix):
    """
    Resolve the handler for a given bearer prefix, using the same settings maps
    and dynamic loading behavior as the HTTP middleware.

    Returns:
        (handler, key_name, error)
        - handler: callable or None
        - key_name: attribute name to use (mapped via AUTH_BEARER_NAME_MAP) or None
        - error: error message or None
    """
    if not prefix:
        return None, None, "Missing token type"

    prefix = prefix.lower()

    if prefix not in AUTH_BEARER_HANDLERS_CACHE:
        # Load from settings map if available
        if prefix not in AUTH_BEARER_HANDLER_PATHS:
            return None, None, "Invalid token type"
        try:
            fn = modules.load_function(AUTH_BEARER_HANDLER_PATHS[prefix])
        except Exception as e:
            logger.exception("Failed to load handler for prefix '%s': %s", prefix, e)
            return None, None, "failed to load handler"
        if not callable(fn):
            logger.error("Loaded handler for prefix '%s' is not callable: %r", prefix, fn)
            return None, None, "invalid handler"
        AUTH_BEARER_HANDLERS_CACHE[prefix] = fn

    handler = AUTH_BEARER_HANDLERS_CACHE[prefix]
    if not callable(handler):
        logger.error("Configured handler for prefix '%s' is not callable: %r", prefix, handler)
        return None, None, "invalid handler"

    key_name = AUTH_BEARER_NAME_MAP.get(prefix, prefix)
    return handler, key_name, None


def validate_bearer_token(
    prefix,
    token,
    request=None,
):
    """
    Validate a token for a given bearer prefix using the resolved handler.

    Returns:
        (instance, error, key_name)
        - instance: the authenticated identity (e.g., User) or None
        - error: error string if validation failed
        - key_name: attribute name (e.g., "user") to attach identity under if success
    """
    handler, key_name, err = resolve_bearer_handler(prefix)
    if err is not None:
        return None, err, None

    if not callable(handler):
        return None, "invalid handler", None

    try:
        result = handler(token, request)
    except Exception as e:
        logger.exception("Bearer handler '%s' raised error", prefix)
        return None, "handler error", None

    # Accept (instance, error) tuples. Fallback to treating non-tuple as instance-only.
    instance = None
    error = None
    if isinstance(result, tuple) and len(result) == 2:
        instance, error = result
    else:
        instance = result
        error = None if result is not None else "invalid handler result"

    if error is not None or instance is None:
        return None, error or "authentication failed", None

    return instance, None, key_name


async def async_validate_bearer_token(
    prefix,
    token,
    request=None,
):
    """
    Async wrapper for validate_bearer_token suitable for use in async consumers.
    """
    return await sync_to_async(validate_bearer_token)(prefix, token, request)


def attach_identity_to_scope(
    scope,
    instance,
    prefix,
    *,
    name_map=None,
):
    """
    Attach the authenticated identity to the Channels scope using the same
    naming convention as the HTTP middleware.

    - scope[key_name] = instance
    - scope["bearer"] = prefix

    Args:
        scope: Channels ASGI scope dict (mutable)
        instance: authenticated identity instance (e.g., User)
        prefix: bearer prefix used (e.g., "bearer")
        name_map: optional override for the bearer-to-attribute map
    """
    mapping = name_map or AUTH_BEARER_NAME_MAP
    key_name = mapping.get(prefix.lower(), prefix.lower())

    # This mirrors what the HTTP middleware does for requests
    scope[key_name] = instance
    scope["bearer"] = prefix.lower()
