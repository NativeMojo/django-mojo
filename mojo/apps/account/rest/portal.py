"""
Public auth portal-config endpoint.

Lets a custom front-end fetch the resolved portal config (theme + which
registration and login methods are offered) for a group, so it can render its
own login/registration UI. The framework-hosted bouncer pages render
server-side and do not need this.
"""
from mojo import decorators as md
from mojo.helpers.response import JsonResponse
from mojo.apps.account.services import portal_config


@md.GET("auth/portal")
@md.public_endpoint("Resolved auth portal config (theme + offered methods)")
def on_auth_portal(request):
    """Return the resolved, public-subset portal config for an optional
    `group_uuid` — the same config the hosted auth pages render from."""
    group = portal_config.resolve_group_from_request(request)
    cfg = portal_config.resolve_portal_config(group=group, request=request)
    return JsonResponse({
        "status": True,
        "data": portal_config.public_portal_config(cfg),
    })
