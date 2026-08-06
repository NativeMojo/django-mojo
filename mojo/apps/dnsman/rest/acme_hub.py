from functools import wraps

import mojo.decorators as md
from mojo import errors as me
from mojo.apps.dnsman.services import acme_hub


def _require_federation_key(request):
    """Authorize only the protected permission on the underlying project key."""
    api_key = getattr(request, "api_key", None)
    if api_key is None:
        raise me.PermissionDeniedException(
            "An ACME federation API key is required",
            branch="dnsman.acme_hub.not_api_key")
    group = getattr(api_key, "group", None)
    if (not api_key.is_active or group is None
            or not group.is_effectively_active()
            or not api_key.has_permission("dnsman_acme_federation")):
        raise me.PermissionDeniedException(
            "ACME federation permission denied",
            branch="dnsman.acme_hub.permission")
    # Never accept a dispatcher-selected descendant or an acting user's org as
    # the project identity.  The key's own group is the immutable owner.
    request.group = group
    return api_key


def _federation_key_required(func):
    """Run the machine-auth floor before parameter validation or rate limits."""
    @wraps(func)
    def wrapper(request, *args, **kwargs):
        _require_federation_key(request)
        return func(request, *args, **kwargs)
    return wrapper


def _allocation_payload(allocation):
    return dict(
        client_ref=allocation.client_ref,
        domain=allocation.domain_name,
        source=allocation.source_name,
        target=allocation.target_name,
    )


@md.POST("acme/delegation")
@_federation_key_required
@md.requires_params("domain", "client_ref")
@md.strict_rate_limit(
    "dnsman_acme_hub", ip_limit=120, ip_window=60,
    apikey_limit=300, apikey_window=60)
@md.custom_security(
    "Underlying active project ApiKey must hold protected dnsman_acme_federation")
def on_acme_delegation(request):
    allocation = acme_hub.allocate(
        request.group,
        request.DATA.get("client_ref"),
        request.DATA.get("domain"),
        request=request)
    return _allocation_payload(allocation)


@md.POST("acme/challenge/publish")
@_federation_key_required
@md.requires_params("client_ref", "challenge_ref", "values")
@md.strict_rate_limit(
    "dnsman_acme_hub", ip_limit=120, ip_window=60,
    apikey_limit=300, apikey_window=60)
@md.custom_security(
    "Underlying active project ApiKey must hold protected dnsman_acme_federation")
def on_acme_publish(request):
    result = acme_hub.publish(
        request.group,
        request.DATA.get("client_ref"),
        request.DATA.get("challenge_ref"),
        request.DATA.get("values"),
        request=request)
    payload = _allocation_payload(result.allocation)
    payload.update(
        challenge_ref=result.lease.challenge_ref,
        active_value_count=result.active_value_count,
    )
    return payload


@md.POST("acme/challenge/withdraw")
@_federation_key_required
@md.requires_params("client_ref", "challenge_ref")
@md.strict_rate_limit(
    "dnsman_acme_hub", ip_limit=120, ip_window=60,
    apikey_limit=300, apikey_window=60)
@md.custom_security(
    "Underlying active project ApiKey must hold protected dnsman_acme_federation")
def on_acme_withdraw(request):
    result = acme_hub.withdraw(
        request.group,
        request.DATA.get("client_ref"),
        request.DATA.get("challenge_ref"),
        request=request)
    payload = _allocation_payload(result.allocation)
    payload.update(
        challenge_ref=request.DATA.get("challenge_ref"),
        active_value_count=result.active_value_count,
    )
    return payload
