from functools import wraps
import mojo.errors
from mojo.helpers import logit
from mojo.helpers.request import (
    is_request_user, is_key_backed_session, is_override_user_session)
from mojo.helpers.settings import settings

logger = logit.get_logger("error", "error.log")

# Global security registry - stores security metadata for all decorated functions
SECURITY_REGISTRY = {}
REQUIRES_PERMS_IS_GROUP = settings.get_static('REQUIRES_PERMS_IS_GROUP', True)


def _register_security(func, **info):
    """Merge security metadata into the function's SECURITY_REGISTRY entry.

    Decorators apply bottom-up, so several registering decorators can stack on
    one view (e.g. @public_endpoint above @requires_geofence). Each must MERGE
    into the shared entry — a full-dict overwrite drops what the others wrote
    (DM-044: the `geofence` sub-entry vanished from enforced_endpoints).
    Overlapping scalar keys keep last-writer-wins semantics via update().
    All wrapping decorators use functools.wraps, so the key is stable
    regardless of stack position.
    """
    key = f"{func.__module__}.{func.__name__}"
    entry = SECURITY_REGISTRY.get(key, {})
    entry.update(info)
    SECURITY_REGISTRY[key] = entry


def _deny_machine_identity_without_active_group(request, perms):
    """The ONE machine-identity gate for requires_perms/requires_group_perms.

    A confined identity (a group-scoped ApiKey, a GroupScopedToken, or any
    custom bearer identity) is trusted only within an ACTIVE group context —
    validate_token strips request.group when the key's group is deactivated,
    and a group token cannot validate at all without one, so this fails closed
    the moment a tenant is suspended (ITEM-037). Without it, the has_permission
    short-circuit in the decorators would trust the identity's self-claimed
    perms with no group consideration at all. Platform-global machine access
    uses requires_global_perms (which ignores request.group) instead.

    The `not group.is_effectively_active()` half is unreachable today — every
    pre-decorator request.group assignment is effective-active-only
    (validate_token, the dispatcher's Group.get_active resolution) — but is
    kept deliberately as defense-in-depth on a security boundary (DM-045): a
    future group-context source that forgets the active filter still fails
    closed here. Effective = the group AND every ancestor (DM-048).
    """
    # is_key_backed_session, not is_request_user: an ApiKey with
    # override_user=True puts a real User in request.user, so the marker test
    # would let a key skip this gate entirely and a suspended tenant's key
    # would keep working. The question here is "is this a machine identity",
    # and request.api_key is what still answers it truthfully.
    if is_request_user(request) and not is_key_backed_session(request):
        return
    group = getattr(request, "group", None)
    if group is None or not group.is_effectively_active():
        logger.error(
            f"{getattr(request.user, 'username', request.user)} has no active group context for {perms}")
        raise mojo.errors.PermissionDeniedException()


def denies_key_backed_session(methods=None):
    """Refuse any request authenticated with a CONFINED bearer credential — an
    ApiKey (linked or not) or a GroupScopedToken.

    `methods` restricts the refusal to the listed HTTP methods — use it on
    combined CRUD endpoints where only the mutating verbs change credentials
    and the reads are ordinary. Omit it (the default) to refuse every method,
    which is right for the dedicated credential endpoints.

    For endpoints that change how a user AUTHENTICATES — registering a passkey,
    minting a long-lived token, enrolling or disabling MFA, moving the email or
    phone used for recovery. An ApiKey may act as a member
    (account.ApiKey.user) and do that member's work; it must never be able to
    hand itself a credential that OUTLIVES the key. Otherwise revoking the key
    stops revoking the access, which is the one guarantee that makes the
    acting-as feature safe to offer at all.

    Applies in both key modes. A reference-mode or unlinked key has no business
    on these endpoints either — it simply used to fail later, messily, on a
    type error instead of cleanly here. A GroupScopedToken is refused for the
    same reason and one more: it is delivered to a browser on a tenant-
    controlled page, so any credential it could mint would outlive both the
    token and the tenant relationship.

    NOT interchangeable with requires_fresh_auth(): fresh_auth deliberately
    returns True for every non-bearer caller
    (mojo/apps/account/services/fresh_auth.py), so it is a no-op for key
    sessions and provides none of this protection.
    """
    blocked = None if methods is None else {m.upper() for m in methods}

    def decorator(func):
        func._mojo_denies_key_backed_session = True
        func._mojo_denies_key_backed_methods = blocked
        _register_security(
            func,
            denies_key_backed_session=True,
            denies_key_backed_methods=sorted(blocked) if blocked else "all",
            function=func,
        )

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if is_key_backed_session(request):
                if blocked is None or request.method.upper() in blocked:
                    logger.error(
                        "api key denied on credential endpoint "
                        f"{func.__module__}.{func.__name__} ({request.method})")
                    raise mojo.errors.PermissionDeniedException()
            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def requires_perms(*required_perms):
    def decorator(func):
        # Add metadata for security detection
        func._mojo_requires_perms = True
        func._mojo_required_permissions = list(required_perms)
        func._mojo_security_type = "permissions"

        # Register in global security registry
        _register_security(
            func,
            type='permissions',
            permissions=list(required_perms),
            function=func,
            requires_auth=True,
        )

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                raise mojo.errors.PermissionDeniedException()
            perms = set(required_perms)

            # Machine identities need an ACTIVE group context (ITEM-037) — see
            # _deny_machine_identity_without_active_group.
            _deny_machine_identity_without_active_group(request, perms)

            # First check user-based permissions.
            #
            # Skipped ONLY for an override_user session. For an unlinked or
            # reference-mode key request.user IS the ApiKey, so this reads the
            # KEY's own permissions dict — correct, and already bounded to the
            # key's group. Under override_user it would instead read the
            # member's GLOBAL platform-wide dict, which has no tenant bound. Falling through to the group check below resolves the same
            # member's permissions AS A MEMBER OF THE KEY'S GROUP, which is
            # both the intent of override_user and the boundary it must
            # respect. _deny_machine_identity_without_active_group above has
            # already guaranteed an active group context here.
            if not is_override_user_session(request) and request.user.has_permission(perms):
                return func(request, *args, **kwargs)

            # If user doesn't have permissions, fallback to group-based checking
            if REQUIRES_PERMS_IS_GROUP:
                if "group" in request.DATA and not request.group:
                    from mojo.apps.account.models.group import Group
                    try:
                        # Active groups only — a member grant in a deactivated
                        # group must not authorize (mirrors the dispatcher).
                        request.group = Group.get_active(int(request.DATA.group))
                    except (TypeError, ValueError):
                        # Unusable group param -> no group context (fail closed)
                        request.group = None
                # check_user is the SAME untenanted global lookup skipped
                # above — Group.user_has_permission starts with
                # `if check_user and user.has_permission(perms)`. Leaving
                # it True for a key session re-enables it one line later
                # and makes the skip inert.
                _check_user = not is_override_user_session(request)
                if not request.group or not request.group.user_has_permission(request.user, perms, _check_user):
                    logger.error(f"{request.user.username} is missing {perms}")
                    raise mojo.errors.PermissionDeniedException()
            else:
                # No group checking allowed, user already failed permission check
                logger.error(f"{request.user.username} is missing {perms}")
                raise mojo.errors.PermissionDeniedException()

            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def requires_group_perms(*required_perms):
    def decorator(func):
        # Add metadata for security detection
        func._mojo_requires_perms = True
        func._mojo_required_permissions = list(required_perms)
        func._mojo_security_type = "permissions"

        # Register in global security registry
        _register_security(
            func,
            type='permissions',
            permissions=list(required_perms),
            function=func,
            requires_auth=True,
        )

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                raise mojo.errors.PermissionDeniedException()
            perms = set(required_perms)

            # Same machine-identity active-group gate as requires_perms (ITEM-037).
            _deny_machine_identity_without_active_group(request, perms)

            # First check user-based permissions. Skipped for key-backed
            # sessions for the same reason as requires_perms: the global
            # permission dict has no tenant bound, so an override_user key
            # must be resolved as a MEMBER of the key's group instead.
            if not is_override_user_session(request) and request.user.has_permission(perms):
                return func(request, *args, **kwargs)

            # If user doesn't have permissions, fallback to group-based checking
            if "group" in request.DATA and not request.group:
                from mojo.apps.account.models.group import Group
                try:
                    # Active groups only — a member grant in a deactivated
                    # group must not authorize (mirrors the dispatcher).
                    request.group = Group.get_active(int(request.DATA.group))
                except (TypeError, ValueError):
                    # Unusable group param -> no group context (fail closed)
                    request.group = None
            # check_user is the SAME untenanted global lookup skipped above —
            # Group.user_has_permission starts with
            # `if check_user and user.has_permission(perms)`. Leaving it True
            # for a key session re-enables it one line later and makes the
            # skip inert.
            _check_user = not is_override_user_session(request)
            if not request.group or not request.group.user_has_permission(request.user, perms, _check_user):
                logger.error(f"{request.user.username} is missing {perms}")
                raise mojo.errors.PermissionDeniedException()
            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def requires_global_perms(*required_perms, allow_api_keys=False):
    """Like ``requires_perms`` but WITHOUT the group-permission fallback.

    ``requires_perms`` falls back to
    ``request.group.user_has_permission(request.user, perms)`` using a
    client-supplied ``group`` param. That is correct for endpoints whose effect
    is confined to that group, but a cross-tenant privilege escalation for
    endpoints whose effect is PLATFORM-WIDE (global settings, fleet operations,
    cross-tenant data): a GroupMember-scoped grant — which any group admin can
    assign, with arbitrary key names — would otherwise authorize a global
    action just by naming the caller's own group. Use this decorator for those
    endpoints; it authorizes on the caller's GLOBAL ``User.permissions`` (or
    superuser) only, never the member/group fallback.

    A non-User identity (a group-scoped ``ApiKey`` authenticating via
    ``Authorization: apikey <token>``) is rejected by default — a group
    credential must not satisfy a platform-global gate. ``is_request_user`` is
    the framework's canonical "real request User?" marker (``User`` defines it,
    ``ApiKey`` does not). Pass ``allow_api_keys=True`` ONLY for endpoints that
    are themselves a federation / machine-ingest surface (e.g. the geoip sync
    receiver), where an ApiKey holding the perm is the intended caller; the
    member/group fallback is still never consulted.

    ``allow_api_keys`` means exactly "ApiKey". A ``GroupScopedToken``
    (``Authorization: grouptoken <token>``) is refused unconditionally — see
    the guard in the wrapper.
    """
    perm_set = set(required_perms)

    def decorator(func):
        func._mojo_requires_perms = True
        func._mojo_required_permissions = list(required_perms)
        func._mojo_security_type = "permissions"

        _register_security(
            func,
            type='permissions',
            permissions=list(required_perms),
            function=func,
            requires_auth=True,
            global_only=True,
        )

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            user = getattr(request, "user", None)
            if user is None or not getattr(user, "is_authenticated", False):
                raise mojo.errors.PermissionDeniedException()
            if getattr(request, "group_token", None) is not None:
                # A GroupScopedToken NEVER satisfies a platform-global gate,
                # not even with allow_api_keys=True. That flag means "a
                # provisioned ApiKey is the intended caller" (the federation
                # ingest surfaces); a visitor-grade group token is not that,
                # and the has_permission check below reads the REAL user's
                # untenanted global dict — exactly what a group token must
                # never reach.
                raise mojo.errors.PermissionDeniedException()
            if not allow_api_keys and is_key_backed_session(request):
                # A group-scoped ApiKey must not satisfy a platform-global
                # gate — including one that assumes a member
                # (ApiKey.override_user), where request.user IS a real User and
                # the old is_request_user test would have passed. The key's
                # tenant scope is exactly what a global gate has no bound on.
                raise mojo.errors.PermissionDeniedException()
            if not allow_api_keys and not is_request_user(request):
                # Any other non-User identity (custom AUTH_BEARER_HANDLERS)
                # is likewise refused. Fail-closed on an unknown identity.
                raise mojo.errors.PermissionDeniedException()
            if not user.has_permission(perm_set):
                logger.error(f"{getattr(user, 'username', user)} is missing global {perm_set}")
                raise mojo.errors.PermissionDeniedException()
            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def public_endpoint(reason=""):
    """
    Decorator to explicitly mark an endpoint as intentionally public.
    This helps security auditing distinguish between endpoints that are
    intentionally public vs those missing security.

    Usage: @public_endpoint("GeoIP lookup for security monitoring")
    """
    def decorator(func):
        func._mojo_public_endpoint = True
        func._mojo_public_reason = reason
        func._mojo_security_type = "public"

        # Register in global security registry
        _register_security(
            func,
            type='public',
            reason=reason,
            function=func,
            requires_auth=False,
        )

        return func
    return decorator


def custom_security(description=""):
    """
    Decorator to mark endpoints with custom security logic that doesn't
    fit standard patterns (like dynamic permission checking, token validation, etc.)

    Usage: @custom_security("Dynamic account-level permission checking")
    """
    def decorator(func):
        func._mojo_custom_security = True
        func._mojo_security_description = description
        func._mojo_security_type = "custom"

        # Register in global security registry
        _register_security(
            func,
            type='custom',
            description=description,
            function=func,
            requires_auth=True,  # Custom security usually requires auth
        )

        return func
    return decorator


def uses_model_security(model_class=None):
    """
    Decorator to explicitly indicate that an endpoint relies on model-level
    security (RestMeta permissions) for its protection.

    Usage: @uses_model_security(User)
    """
    def decorator(func):
        func._mojo_uses_model_security = True
        func._mojo_secured_model = model_class
        func._mojo_secured_model_name = model_class.__name__ if model_class else None
        func._mojo_security_type = "model"

        # Register in global security registry
        _register_security(
            func,
            type='model',
            model_class=model_class,
            model_name=model_class.__name__ if model_class else None,
            function=func,
            requires_auth=True,
        )

        return func
    return decorator


def token_secured(token_types=None, description=""):
    """
    Decorator to mark endpoints secured by token-based authentication
    (like upload tokens, download tokens, etc.)

    Usage: @token_secured(['upload_token'], "Secured by upload token validation")
    """
    def decorator(func):
        func._mojo_token_secured = True
        func._mojo_token_types = token_types or []
        func._mojo_security_description = description
        func._mojo_security_type = "token"

        # Register in global security registry
        _register_security(
            func,
            type='token',
            token_types=token_types or [],
            description=description,
            function=func,
            requires_auth=False,  # Token auth doesn't require user session
        )

        return func
    return decorator


def requires_auth():
    def decorator(func):
        # Add metadata for security detection
        func._mojo_requires_auth = True
        func._mojo_security_type = "authentication"

        # Register in global security registry
        _register_security(
            func,
            type='authentication',
            function=func,
            requires_auth=True,
        )

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                raise mojo.errors.PermissionDeniedException()
            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def requires_fresh_auth(seconds=None):
    """Require recent ("step-up") authentication for a sensitive endpoint.

    Authenticated callers whose JWT auth_time is older than the window get a
    440 reauth_required (see mojo.apps.account.services.fresh_auth). Inert when
    FRESH_AUTH_WINDOW <= 0 (the default), so this is safe to apply broadly.
    Pass `seconds` to override the global window for one endpoint.
    """
    def decorator(func):
        func._mojo_requires_fresh_auth = True
        func._mojo_fresh_auth_seconds = seconds
        func._mojo_security_type = "fresh_auth"

        _register_security(
            func,
            type='fresh_auth',
            seconds=seconds,
            function=func,
            requires_auth=True,
        )

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                raise mojo.errors.PermissionDeniedException()
            from mojo.apps.account.services import fresh_auth
            fresh_auth.require_fresh(request, seconds)
            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def requires_bearer(bearer):
    def decorator(func):
        # Add metadata for security detection
        func._mojo_requires_bearer = True
        func._mojo_bearer_token = bearer
        func._mojo_security_type = "bearer_token"

        # Register in global security registry
        _register_security(
            func,
            type='bearer_token',
            bearer_token=bearer,
            function=func,
            requires_auth=False,  # Bearer token is alternative to user auth
        )

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if request.bearer is None or request.bearer.lower() != bearer.lower():
                raise mojo.errors.PermissionDeniedException(f"invalid bearer token '{request.bearer}'")
            return func(request, *args, **kwargs)
        return wrapper
    return decorator
