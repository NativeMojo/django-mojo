from functools import wraps

import mojo.decorators as md
from mojo import errors as me
from mojo.apps.dnsman.services import acme_hub


def _require_federation_key(request):
    """Compatibility wrapper for the shared fresh-principal policy."""
    return acme_hub.federation_principal(request)


def _federation_key_required(func):
    """Run the machine-auth floor before parameter validation or rate limits."""
    @wraps(func)
    def wrapper(request, *args, **kwargs):
        _require_federation_key(request)
        return func(request, *args, **kwargs)
    return wrapper


def _allocation_payload(allocation, participant=None):
    return acme_hub.allocation_payload(allocation, participant)


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
    payload = _allocation_payload(result.allocation, result.participant)
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
    payload = _allocation_payload(result.allocation, result.participant)
    payload.update(
        challenge_ref=request.DATA.get("challenge_ref"),
        active_value_count=result.active_value_count,
    )
    return payload


def _shared_operation(kind, required=(), optional=()):
    """Authenticate before body validation/rate work; errors contain safe tokens."""
    def decorate(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if kind == "controller":
                acme_hub.require_controller(request)
            else:
                acme_hub.authorize(request, consumer=kind == "consumer")
            data = request.DATA
            if (not isinstance(data, dict) or set(data) - set(required) - set(optional)
                    or not set(required).issubset(data)):
                raise me.ValueException("acme_invalid_request")
            return func(request, *args, **kwargs)
        return wrapper
    return decorate


@md.POST("acme/capabilities")
@_shared_operation("capabilities")
@md.strict_rate_limit("dnsman_acme_hub", ip_limit=120, ip_window=60, apikey_limit=300, apikey_window=60)
@md.custom_security("Protected underlying project key or authenticated human; capabilities confer no row authority")
def on_acme_capabilities(request):
    return acme_hub.capabilities(request)


@md.POST("acme/consumer/lookup")
@_shared_operation("consumer", ("domain",), ("allocation_ref", "cursor"))
@md.strict_rate_limit("dnsman_acme_hub", ip_limit=120, ip_window=60, apikey_limit=300, apikey_window=60)
@md.custom_security("Protected underlying project key; only own immutable participants")
def on_acme_consumer_lookup(request):
    return acme_hub.lookup_participants(request, **request.DATA)


@md.POST("acme/consumer/status")
@_shared_operation("consumer", ("client_ref",))
@md.strict_rate_limit("dnsman_acme_hub", ip_limit=120, ip_window=60, apikey_limit=300, apikey_window=60)
@md.custom_security("Protected underlying project key; resolve own participant before probing DNS")
def on_acme_consumer_status(request):
    return acme_hub.participant_status(request, **request.DATA)


@md.POST("acme/consumer/join")
@_shared_operation("consumer", ("allocation_ref", "client_ref", "domain"))
@md.strict_rate_limit("dnsman_acme_hub", ip_limit=120, ip_window=60, apikey_limit=300, apikey_window=60)
@md.custom_security("Protected underlying project key; explicit existing active grant and one-hop proof")
def on_acme_consumer_join(request):
    return acme_hub.join(request, **request.DATA)


@md.POST("acme/allocation/list")
@_shared_operation("controller", ("domain",), ("cursor",))
@md.strict_rate_limit("dnsman_acme_hub", ip_limit=120, ip_window=60, apikey_limit=300, apikey_window=60)
@md.custom_security("Allocation original project key or genuine platform superuser; row controller check")
def on_acme_allocation_list(request):
    return acme_hub.list_allocations(request, **request.DATA)


@md.POST("acme/trust/list")
@_shared_operation("controller", ("allocation_ref",), ("cursor",))
@md.strict_rate_limit("dnsman_acme_hub", ip_limit=120, ip_window=60, apikey_limit=300, apikey_window=60)
@md.custom_security("Allocation original project key or genuine platform superuser; row controller check")
def on_acme_trust_list(request):
    return acme_hub.list_participants(request, **request.DATA)


@md.POST("acme/trust/recipient")
@_shared_operation("controller", ("allocation_ref", "consumer_project_uuid"))
@md.strict_rate_limit("dnsman_acme_hub", ip_limit=120, ip_window=60, apikey_limit=300, apikey_window=60)
@md.custom_security("Allocation controller before exact active recipient lookup; no directory enumeration")
def on_acme_trust_recipient(request):
    return acme_hub.resolve_recipient(request, **request.DATA)


@md.POST("acme/trust/grant")
@_shared_operation("controller", ("connection_request",))
@md.strict_rate_limit("dnsman_acme_hub", ip_limit=120, ip_window=60, apikey_limit=300, apikey_window=60)
@md.custom_security("Allocation controller; explicit fixed issuance scope, active recipient and one-hop proof")
def on_acme_trust_grant(request):
    return acme_hub.grant(request, **request.DATA)


@md.POST("acme/trust/revoke")
@_shared_operation("controller", ("allocation_ref", "participant_ref"))
@md.strict_rate_limit("dnsman_acme_hub", ip_limit=120, ip_window=60, apikey_limit=300, apikey_window=60)
@md.custom_security("Allocation controller; permanent participant revocation with durable cleanup")
def on_acme_trust_revoke(request):
    return acme_hub.revoke(request, **request.DATA)
