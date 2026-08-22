"""Day-2 WebApp operations: addresses, certificates, serving, routes, rollback,
take offline, delete, and turning a deploy key off.

Every tool here declares the gates its Admin twin declares — a fresh-auth
window of 600 seconds (300 on the key revoke, as ``webapp/revoke_key`` does) —
and every ``preview`` runs the day-2 authority gate before it reads the state
it binds. ``delete_webapp`` deliberately carries a step-up the REST delete does
not: chat is a model-initiated surface, and the escalation is one-directional.

Minting or rotating a deploy key is NOT here and is not deferred: ``link_once``
returns the token once and clears the encrypted copy before commit, so routing
it through an approval would either put the credential in the record the model
reads next turn, or mint a key nobody can ever see.
"""

from mojo import errors as me
from mojo.apps.assistant import tool

from . import common


DOMAIN = "webapp"
PERMISSION = "view_admin"
FRESH = 600
FRESH_KEY = 300


def _writable(user, params):
    return common.webapp_for(user, params.get("webapp"), write=True)


def _primary(web_app):
    from mojo.apps.edge.services import webapp_serving

    return common.translated(webapp_serving._require_primary, web_app)


def _serving_summary(web_app):
    """The small projection a card and a model actually need after a change."""
    from mojo.apps.edge.services import webapp_serving

    serving = webapp_serving.serving_for(web_app, include_editables=False)
    return {
        "webapp": web_app.pk,
        "address": (serving.get("address") or {}).get("hostname"),
        "certificate": {
            "id": (serving.get("certificate") or {}).get("id"),
            "status": (serving.get("certificate") or {}).get("status"),
        },
        "serving": {
            "pool": (serving.get("serving") or {}).get("pool"),
            "spa": (serving.get("serving") or {}).get("spa"),
        },
        "routes": [{"path_prefix": row.get("path_prefix"),
                    "upstream": (row.get("upstream") or {}).get("name")}
                   for row in (serving.get("routes") or [])],
    }


def _flag(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# addresses
# ---------------------------------------------------------------------------

def _summarize_attach(params, user):
    return (f"Point {params.get('hostname')} at this app. It will serve the "
            f"same release as the app's own address.")


def _preview_attach(params, user):
    from mojo.apps.edge.services import webapp_alias

    web_app = _writable(user, params)
    verdict = common.translated(
        webapp_alias.preview, web_app, params.get("hostname"), user)
    if verdict.get("status") == "unusable":
        raise common.Refused(str(verdict.get("reason") or "That address cannot be used here."))
    if verdict.get("status") == "needs_domain":
        raise common.Refused(str(verdict.get("reason") or "That domain is not connected here yet."))
    return {
        "summary": (f"Adds {verdict.get('hostname')} as an address for "
                    f"'{web_app.slug}' ({verdict.get('dns')} DNS)."),
        "details": {"hostname": verdict.get("hostname"),
                    "dns": verdict.get("dns"),
                    "domain": (verdict.get("domain") or {}).get("name")},
        "revision": common.revision(
            ("app", web_app.pk), ("host", verdict.get("hostname")),
            ("preview", verdict.get("status"))),
    }


@tool(
    name="attach_webapp_address",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    fresh_auth_seconds=FRESH,
    summarize=_summarize_attach,
    preview=_preview_attach,
    description=(
        "Add one more address to an app. Check it first with "
        "preview_webapp_alias. The result may be records_needed (the operator "
        "publishes the returned DNS records themselves) or certificate_pending "
        "— neither means the address is live yet. Safe to run again; it makes "
        "at most one provider write. Requires operator approval: calling this "
        "tool creates an approval card and does not execute."),
    input_schema={
        "type": "object",
        "properties": {
            "webapp": {"type": "integer", "description": "WebApp id"},
            "hostname": {"type": "string", "description": "The address to add, e.g. www.customer.com"},
            "retry_certificate": {"type": "boolean",
                                  "description": "Re-request a certificate that previously failed"},
        },
        "required": ["webapp", "hostname"],
    },
)
def _tool_attach_webapp_address(params, user, approval=None):
    from mojo.apps.edge.services import webapp_alias

    web_app = _writable(user, params)
    try:
        result = webapp_alias.attach(
            web_app, params.get("hostname"), user,
            retry_certificate=_flag(params.get("retry_certificate")))
    except me.MojoException as err:
        return common.service_error(err, code="address_refused")
    common.audit(user, "attach_webapp_address", web_app,
                 payload={"bound": (approval.revision if approval else "")})
    return dict(result, webapp=web_app.pk,
                context_ref=common.context_ref(web_app))


def _summarize_detach(params, user):
    return "Remove one extra address from this app. The app itself stays up."


def _preview_detach(params, user):
    web_app = _writable(user, params)
    alias = common.alias_for(web_app, params.get("vhost"))
    reason = common.reason_text(params)
    return {
        "summary": (f"Stops serving {alias.server_name} for '{web_app.slug}'. "
                    f"The app's own address is untouched."),
        "details": {"hostname": alias.server_name, "reason": reason},
        "revision": common.revision(
            ("app", web_app.pk), ("vhost", alias.pk),
            ("host", alias.server_name)),
    }


@tool(
    name="detach_webapp_address",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    fresh_auth_seconds=FRESH,
    summarize=_summarize_detach,
    preview=_preview_detach,
    description=(
        "Remove one EXTRA address from an app. Taking the app's own address "
        "down is take_webapp_offline, deliberately a different and louder "
        "action. Get the address id from get_webapp_serving. Requires operator "
        "approval: calling this tool creates an approval card and does not "
        "execute."),
    input_schema={
        "type": "object",
        "properties": {
            "webapp": {"type": "integer", "description": "WebApp id"},
            "vhost": {"type": "integer", "description": "The address id from get_webapp_serving"},
            "reason": {"type": "string", "description": "Why it is being removed (3-300 characters)"},
        },
        "required": ["webapp", "vhost", "reason"],
    },
)
def _tool_detach_webapp_address(params, user, approval=None):
    from mojo.apps.edge.services import webapp_alias

    web_app = _writable(user, params)
    alias = common.alias_for(web_app, params.get("vhost"))
    reason = common.reason_text(params)
    try:
        result = common.translated(webapp_alias.detach, web_app, alias)
    except me.MojoException as err:
        return common.service_error(err, code="address_refused")
    common.audit(user, "detach_webapp_address", web_app,
                 payload={"vhost": alias.pk, "reason": reason,
                          "bound": (approval.revision if approval else "")})
    return dict(result, webapp=web_app.pk)


def _offline_effect(primary):
    """What ``take_offline`` really does to THIS primary address.

    ``webapp_lifecycle.take_offline`` deletes the primary vhost for both serving
    kinds (``site`` and ``site_api``), so the address stops answering either
    way; it refuses outright — touching nothing — for any other kind, and when
    an alias is also another app's primary address. The kind is still bound
    into the approval so the card describes exactly the address that was
    there, and the wording only degrades for a kind the service would refuse.
    """
    if primary is None:
        return "", True
    return primary.kind, primary.kind in webapp_lifecycle.SERVING_KINDS


def _summarize_offline(params, user):
    stops = True
    try:
        web_app = _writable(user, params)
        _kind, stops = _offline_effect(
            web_app.vhost if web_app.vhost_id else None)
    except Exception:
        # Unresolvable here means the preview is about to refuse and no card
        # will exist; fall back to the plain sentence.
        pass
    if stops:
        return ("Visitors will stop reaching this app. The app and its versions "
                "are kept.")
    return ("This app is unlinked from its address, and its extra addresses "
            "stop serving. Its own address KEEPS serving its upstream routes "
            "until they are removed. The app and its versions are kept.")


def _preview_offline(params, user):
    from mojo.apps.edge.models import Vhost

    web_app = _writable(user, params)
    reason = common.reason_text(params)
    aliases = list(Vhost.objects.filter(alias_of=web_app).order_by("pk"))
    primary = web_app.vhost if web_app.vhost_id else None
    if primary is None and not aliases:
        raise common.Refused(
            "This app has no address, so there is nothing to take offline.")
    hostname = primary.server_name if primary is not None else ""
    kind, stops = _offline_effect(primary)
    if stops:
        effect = (f"its address {hostname or '(none)'} and {len(aliases)} extra "
                  f"address(es) stop serving")
    else:
        effect = (f"{len(aliases)} extra address(es) stop serving and the app is "
                  f"unlinked from {hostname}, which KEEPS serving its upstream "
                  f"routes until they are removed")
    return {
        "summary": (f"Takes '{web_app.slug}' offline: {effect}. The app and its "
                    f"versions are kept."),
        "details": {"hostname": hostname, "address_kind": kind,
                    "address_stops_serving": stops,
                    "extra_addresses": len(aliases), "reason": reason},
        "revision": common.revision(
            ("app", web_app.pk), ("vhost", web_app.vhost_id or 0),
            ("host", hostname), ("kind", kind), ("aliases", len(aliases))),
    }


@tool(
    name="take_webapp_offline",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    fresh_auth_seconds=FRESH,
    summarize=_summarize_offline,
    preview=_preview_offline,
    description=(
        "Take an app offline: every extra address stops serving, and the app "
        "is unlinked from its own address. For an app served straight from its "
        "build that address stops serving too; for an API-backed app the "
        "address keeps answering from its upstream routes until they are "
        "removed, and the approval card says which case this is. The app, its "
        "versions and its deploy key are kept. Requires operator approval: "
        "calling this tool creates an approval card and does not execute."),
    input_schema={
        "type": "object",
        "properties": {
            "webapp": {"type": "integer", "description": "WebApp id"},
            "reason": {"type": "string", "description": "Why it is going offline (3-300 characters)"},
        },
        "required": ["webapp", "reason"],
    },
)
def _tool_take_webapp_offline(params, user, approval=None):
    from mojo.apps.edge.services import webapp_lifecycle

    web_app = _writable(user, params)
    reason = common.reason_text(params)
    # Read the effect BEFORE the teardown: afterwards the primary vhost may be
    # gone, and the card's claim is about the address that WAS there.
    _kind, stops = _offline_effect(web_app.vhost if web_app.vhost_id else None)
    try:
        result = common.translated(webapp_lifecycle.take_offline, web_app)
    except me.MojoException as err:
        return common.service_error(err, code="offline_refused")
    common.audit(user, "take_webapp_offline", web_app,
                 payload={"reason": reason,
                          "bound": (approval.revision if approval else "")})
    return dict(result, status="offline",
                address_stopped_serving=stops,
                kept="The app and its versions are kept.",
                note=(None if stops else
                      "The app's own address is API-backed and keeps serving "
                      "its upstream routes; remove them to stop it."))


# ---------------------------------------------------------------------------
# serving, certificates, routes
# ---------------------------------------------------------------------------

def _summarize_serving(params, user):
    return "Change how this app is served, on every address it answers on."


def _preview_serving(params, user):
    from mojo.apps.edge import validators

    web_app = _writable(user, params)
    primary = _primary(web_app)
    pool = params.get("pool")
    pool = str(pool).strip() if pool not in (None, "") else None
    spa = None if params.get("spa") is None else _flag(params.get("spa"))
    if pool is None and spa is None:
        raise common.Refused("Give a pool or an spa setting to change.")
    if pool is not None:
        common.translated(validators.validate_pool, pool)
    new_pool = pool if pool is not None else primary.pool
    new_spa = spa if spa is not None else bool(primary.spa)
    return {
        "summary": (f"Serves '{web_app.slug}' from pool {new_pool} with "
                    f"single-page fallback {'on' if new_spa else 'off'}, on "
                    f"every address it answers on."),
        "details": {"pool": {"from": primary.pool, "to": new_pool},
                    "spa": {"from": bool(primary.spa), "to": new_spa}},
        "revision": common.revision(
            ("app", web_app.pk), ("pool", f"{primary.pool}->{new_pool}"),
            ("spa", f"{bool(primary.spa)}->{new_spa}")),
    }


@tool(
    name="set_webapp_serving",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    fresh_auth_seconds=FRESH,
    summarize=_summarize_serving,
    preview=_preview_serving,
    description=(
        "Change an app's node pool or its single-page fallback. Applies to the "
        "app's own address and every extra address. Offer only pools that "
        "get_webapp_serving returned. Requires operator approval: calling this "
        "tool creates an approval card and does not execute."),
    input_schema={
        "type": "object",
        "properties": {
            "webapp": {"type": "integer", "description": "WebApp id"},
            "pool": {"type": "string", "description": "A declared node pool from get_webapp_serving"},
            "spa": {"type": "boolean", "description": "Serve index.html for unknown paths"},
        },
        "required": ["webapp"],
    },
)
def _tool_set_webapp_serving(params, user, approval=None):
    from mojo.apps.edge.services import webapp_serving

    web_app = _writable(user, params)
    data = {}
    if params.get("pool") not in (None, ""):
        data["pool"] = str(params["pool"]).strip()
    if params.get("spa") is not None:
        data["spa"] = _flag(params.get("spa"))
    try:
        common.translated(webapp_serving.apply, web_app, data)
    except me.MojoException as err:
        return common.service_error(err, code="serving_refused")
    common.audit(user, "set_webapp_serving", web_app,
                 payload={"bound": (approval.revision if approval else "")})
    return _serving_summary(web_app)


def _summarize_switch_certificate(params, user):
    return "Switch this app onto a different certificate for its address."


def _preview_switch_certificate(params, user):
    from mojo.apps.edge.services import webapp_serving

    web_app = _writable(user, params)
    primary = _primary(web_app)
    certificate = common.translated(
        webapp_serving._resolve_certificate, primary, params.get("certificate"))
    return {
        "summary": (f"Serves '{web_app.slug}' on {primary.server_name} with "
                    f"certificate {certificate.common_name}."),
        "details": {"certificate": certificate.common_name,
                    "status": certificate.status,
                    "covers": primary.server_name},
        "revision": common.revision(
            ("app", web_app.pk), ("cert", certificate.pk),
            ("status", certificate.status), ("covers", primary.server_name)),
    }


@tool(
    name="switch_webapp_certificate",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    fresh_auth_seconds=FRESH,
    summarize=_summarize_switch_certificate,
    preview=_preview_switch_certificate,
    description=(
        "Switch an app onto a certificate that is already active and really "
        "covers its address — get the id from get_webapp_serving. A pending "
        "certificate is refused: an app pointed at one would serve nothing. "
        "Requires operator approval: calling this tool creates an approval "
        "card and does not execute."),
    input_schema={
        "type": "object",
        "properties": {
            "webapp": {"type": "integer", "description": "WebApp id"},
            "certificate": {"type": "integer", "description": "Certificate id from get_webapp_serving"},
        },
        "required": ["webapp", "certificate"],
    },
)
def _tool_switch_webapp_certificate(params, user, approval=None):
    from mojo.apps.edge.services import webapp_serving

    web_app = _writable(user, params)
    try:
        common.translated(webapp_serving.apply, web_app,
                          {"certificate": params.get("certificate")})
    except me.MojoException as err:
        return common.service_error(err, code="certificate_refused")
    common.audit(user, "switch_webapp_certificate", web_app,
                 payload={"bound": (approval.revision if approval else "")})
    return _serving_summary(web_app)


def _summarize_request_certificate(params, user):
    return "Ask for a certificate covering this app's address alone."


def _preview_request_certificate(params, user):
    from mojo.apps.edge.services import webapp_serving

    web_app = _writable(user, params)
    primary = _primary(web_app)
    domain = primary.domain
    supported, reason = webapp_serving.dedicated_support(domain)
    if not supported:
        raise common.Refused(str(reason))
    return {
        "summary": (f"Requests a certificate for {primary.server_name} alone. "
                    f"Issuance takes a few minutes and the app is NOT switched "
                    f"onto it automatically."),
        "details": {"hostname": primary.server_name, "domain": domain.name},
        "revision": common.revision(
            ("app", web_app.pk), ("host", primary.server_name),
            ("domain", domain.pk)),
    }


@tool(
    name="request_webapp_certificate",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    fresh_auth_seconds=FRESH,
    summarize=_summarize_request_certificate,
    preview=_preview_request_certificate,
    description=(
        "Request a certificate covering this app's address alone. This only "
        "REQUESTS it — switching the app onto it is a separate "
        "switch_webapp_certificate once it is active. Not available where the "
        "whole domain is covered by one certificate. Requires operator "
        "approval: calling this tool creates an approval card and does not "
        "execute."),
    input_schema={
        "type": "object",
        "properties": {"webapp": {"type": "integer", "description": "WebApp id"}},
        "required": ["webapp"],
    },
)
def _tool_request_webapp_certificate(params, user, approval=None):
    from mojo.apps.edge.services import webapp_serving

    web_app = _writable(user, params)
    try:
        certificate = common.translated(
            webapp_serving.request_dedicated_certificate, web_app, user)
    except me.MojoException as err:
        return common.service_error(err, code="certificate_refused")
    common.audit(user, "request_webapp_certificate", web_app,
                 payload={"bound": (approval.revision if approval else "")})
    return {
        "webapp": web_app.pk,
        "certificate": certificate.pk,
        "status": certificate.status,
        "note": ("Requested only. It is not covering the app until it is "
                 "active and the app is switched onto it."),
    }


def _summarize_add_route(params, user):
    return (f"Send {params.get('path_prefix')} on this app to another "
            f"destination, on every address it answers on.")


def _route_context(user, params):
    from mojo.apps.edge.services import webapp_serving

    web_app = _writable(user, params)
    primary = common.translated(
        webapp_serving._require_routes, _primary(web_app))
    prefix = common.translated(
        webapp_serving._clean_prefix, params.get("path_prefix"))
    return web_app, primary, prefix


def _preview_add_route(params, user):
    from mojo.apps.edge.services import webapp_serving

    web_app, primary, prefix = _route_context(user, params)
    upstream = common.translated(
        webapp_serving._resolve_upstream, web_app, primary,
        params.get("upstream"))
    return {
        "summary": (f"Sends {prefix} on '{web_app.slug}' to {upstream.name}, "
                    f"on every address it answers on."),
        "details": {"path_prefix": prefix, "upstream": upstream.name},
        "revision": common.revision(
            ("app", web_app.pk), ("prefix", prefix), ("upstream", upstream.pk)),
    }


@tool(
    name="add_webapp_route",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    fresh_auth_seconds=FRESH,
    summarize=_summarize_add_route,
    preview=_preview_add_route,
    description=(
        "Send one path on an app to a declared destination, on every address "
        "it answers on. Offer only destinations get_webapp_serving returned. "
        "Sign-in and account paths are handled by the platform and cannot be "
        "changed. Requires operator approval: calling this tool creates an "
        "approval card and does not execute."),
    input_schema={
        "type": "object",
        "properties": {
            "webapp": {"type": "integer", "description": "WebApp id"},
            "path_prefix": {"type": "string", "description": "Path prefix, e.g. /api"},
            "upstream": {"type": "string", "description": "Destination id or name from get_webapp_serving"},
        },
        "required": ["webapp", "path_prefix", "upstream"],
    },
)
def _tool_add_webapp_route(params, user, approval=None):
    from mojo.apps.edge.services import webapp_serving

    web_app = _writable(user, params)
    try:
        common.translated(webapp_serving.add_route, web_app,
                          params.get("path_prefix"), params.get("upstream"))
    except me.MojoException as err:
        return common.service_error(err, code="route_refused")
    common.audit(user, "add_webapp_route", web_app,
                 payload={"bound": (approval.revision if approval else "")})
    return _serving_summary(web_app)


def _summarize_remove_route(params, user):
    return (f"Stop sending {params.get('path_prefix')} on this app elsewhere; "
            f"it will be served from the build again.")


def _preview_remove_route(params, user):
    from mojo.apps.edge.models import VhostRoute

    web_app, primary, prefix = _route_context(user, params)
    route = VhostRoute.objects.select_related("upstream").filter(
        vhost=primary, path_prefix=prefix).first()
    if route is None:
        raise common.Refused(f"{prefix} isn't set up on this app.")
    return {
        "summary": (f"Stops sending {prefix} on '{web_app.slug}' to "
                    f"{route.upstream.name if route.upstream_id else 'its destination'}."),
        "details": {"path_prefix": prefix,
                    "upstream": route.upstream.name if route.upstream_id else None},
        "revision": common.revision(
            ("app", web_app.pk), ("prefix", prefix),
            ("upstream", route.upstream_id or 0)),
    }


@tool(
    name="remove_webapp_route",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    fresh_auth_seconds=FRESH,
    summarize=_summarize_remove_route,
    preview=_preview_remove_route,
    description=(
        "Stop sending one path on an app elsewhere, on every address it "
        "answers on. The platform's own sign-in and account paths cannot be "
        "removed. Requires operator approval: calling this tool creates an "
        "approval card and does not execute."),
    input_schema={
        "type": "object",
        "properties": {
            "webapp": {"type": "integer", "description": "WebApp id"},
            "path_prefix": {"type": "string", "description": "Path prefix, e.g. /api"},
        },
        "required": ["webapp", "path_prefix"],
    },
)
def _tool_remove_webapp_route(params, user, approval=None):
    from mojo.apps.edge.services import webapp_serving

    web_app = _writable(user, params)
    try:
        common.translated(webapp_serving.remove_route, web_app,
                          params.get("path_prefix"))
    except me.MojoException as err:
        return common.service_error(err, code="route_refused")
    common.audit(user, "remove_webapp_route", web_app,
                 payload={"bound": (approval.revision if approval else "")})
    return _serving_summary(web_app)


# ---------------------------------------------------------------------------
# rollback, key, delete
# ---------------------------------------------------------------------------

def _summarize_rollback(params, user):
    return "Put an earlier, already-verified version of this app back in front of visitors."


def _preview_rollback(params, user):
    web_app = _writable(user, params)
    release = common.release_for(web_app, params.get("release"))
    reason = common.reason_text(params)
    if not release.is_promotable:
        raise common.Refused(
            f"Version {release.version} is {release.status} and was never "
            f"verified, so it cannot go live.")
    current = web_app.current_release
    return {
        "summary": (f"Puts version {release.version} back in front of "
                    f"visitors for '{web_app.slug}'"
                    + (f", replacing {current.version}." if current else ".")),
        "details": {"to": release.version,
                    "from": current.version if current else None,
                    "reason": reason},
        "revision": common.revision(
            ("app", web_app.pk), ("from", web_app.current_release_id or 0),
            ("to", release.pk), ("version", release.version)),
    }


@tool(
    name="rollback_webapp",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    fresh_auth_seconds=FRESH,
    summarize=_summarize_rollback,
    preview=_preview_rollback,
    description=(
        "Roll an app back to an earlier version it already verified. Get the "
        "version id from get_webapp_deploy_history; an unverified (pending) "
        "version is refused. The deployment is queued, not instant — read "
        "get_webapp_deployment for what actually landed. Requires operator "
        "approval: calling this tool creates an approval card and does not "
        "execute."),
    input_schema={
        "type": "object",
        "properties": {
            "webapp": {"type": "integer", "description": "WebApp id"},
            "release": {"type": "integer", "description": "Version id from get_webapp_deploy_history"},
            "reason": {"type": "string", "description": "Why it is being rolled back (3-300 characters)"},
        },
        "required": ["webapp", "release", "reason"],
    },
)
def _tool_rollback_webapp(params, user, approval=None):
    from mojo.apps.edge.services import releases

    web_app = _writable(user, params)
    release = common.release_for(web_app, params.get("release"))
    reason = common.reason_text(params)
    already = web_app.current_release_id == release.pk
    try:
        deployment = common.translated(
            releases.promote, web_app, release, user)
    except me.MojoException as err:
        return common.service_error(err, code="rollback_refused")
    common.audit(user, "rollback_webapp", web_app,
                 payload={"release": release.pk, "reason": reason,
                          "bound": (approval.revision if approval else "")})
    return {
        "webapp": web_app.pk,
        "deployment": deployment.pk,
        "version": release.version,
        "status": deployment.status,
        "already_serving": already,
        "note": ("This app was already serving that version; no new "
                 "deployment was started."
                 if already else
                 "Queued. Read get_webapp_deployment for the fleet outcome — "
                 "queued is not live."),
        "context_ref": common.context_ref(web_app),
    }


def _summarize_revoke_key(params, user):
    return ("Turn this app's deploy key off. Automated deploys will stop until "
            "a new key is created in the Admin portal.")


def _preview_revoke_key(params, user):
    web_app = _writable(user, params)
    reason = common.reason_text(params)
    if web_app.api_key_id is None:
        raise common.Refused("This app has no deploy key to turn off.")
    return {
        "summary": (f"Turns off the deploy key for '{web_app.slug}'. Pushes "
                    f"from CI will stop working immediately."),
        "details": {"reason": reason,
                    "note": "A new key can only be created in the Admin portal."},
        "revision": common.revision(
            ("app", web_app.pk), ("key", web_app.api_key_id)),
    }


@tool(
    name="revoke_webapp_deploy_key",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    fresh_auth_seconds=FRESH_KEY,
    summarize=_summarize_revoke_key,
    preview=_preview_revoke_key,
    description=(
        "Turn off an app's MOJO_DEPLOY_KEY. Automated deploys stop until a new "
        "key is created, which is an Admin-portal action because the key is "
        "shown exactly once. This tool never reads or returns a key. Requires "
        "operator approval: calling this tool creates an approval card and "
        "does not execute."),
    input_schema={
        "type": "object",
        "properties": {
            "webapp": {"type": "integer", "description": "WebApp id"},
            "reason": {"type": "string", "description": "Why it is being turned off (3-300 characters)"},
        },
        "required": ["webapp", "reason"],
    },
)
def _tool_revoke_webapp_deploy_key(params, user, approval=None):
    from mojo.apps.edge.services import webapp_keys

    web_app = _writable(user, params)
    reason = common.reason_text(params)
    if approval is None:
        return {"error": "This operation requires an approval receipt.",
                "error_code": "approval_required"}
    try:
        result = common.translated(
            webapp_keys.revoke_once, web_app, user, str(approval.uuid))
    except me.MojoException as err:
        return common.service_error(err, code="key_refused")
    common.audit(user, "revoke_webapp_deploy_key", web_app,
                 payload={"reason": reason, "bound": approval.revision})
    status = result.get("status") or {}
    return {
        "webapp": web_app.pk,
        "secret_name": "MOJO_DEPLOY_KEY",
        "replayed": bool(result.get("replayed")),
        "key": {"linked": bool(status.get("linked")),
                "active": bool(status.get("active"))},
        "next": ("Create a new key from Admin -> Deployments -> the app -> "
                 "the Key tab; it is shown exactly once."),
    }


def _summarize_delete(params, user):
    return ("Delete this app, its versions, its addresses and its deploy key. "
            "This cannot be undone.")


def _preview_delete(params, user):
    from mojo.apps.edge.models import Vhost, WebAppRelease

    web_app = _writable(user, params)
    reason = common.reason_text(params)
    releases = WebAppRelease.objects.filter(webapp=web_app).count()
    addresses = Vhost.objects.filter(alias_of=web_app).count() + (
        1 if web_app.vhost_id else 0)
    return {
        "summary": (f"Permanently deletes '{web_app.slug}' with {releases} "
                    f"version(s) and {addresses} address(es). This cannot be "
                    f"undone."),
        "details": {"slug": web_app.slug, "releases": releases,
                    "addresses": addresses, "reason": reason},
        "revision": common.revision(
            ("app", web_app.pk), ("slug", web_app.slug),
            ("releases", releases), ("addresses", addresses)),
    }


@tool(
    name="delete_webapp",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    fresh_auth_seconds=FRESH,
    summarize=_summarize_delete,
    preview=_preview_delete,
    description=(
        "Permanently delete an app: its versions, its addresses and its deploy "
        "key go with it, and nothing is recoverable. To stop serving without "
        "losing anything, use take_webapp_offline instead. Requires operator "
        "approval: calling this tool creates an approval card and does not "
        "execute."),
    input_schema={
        "type": "object",
        "properties": {
            "webapp": {"type": "integer", "description": "WebApp id"},
            "reason": {"type": "string", "description": "Why it is being deleted (3-300 characters)"},
        },
        "required": ["webapp", "reason"],
    },
)
def _tool_delete_webapp(params, user, approval=None):
    from mojo.apps.edge.services import webapp_lifecycle

    web_app = _writable(user, params)
    reason = common.reason_text(params)
    slug = web_app.slug
    try:
        common.translated(webapp_lifecycle.teardown, web_app)
    except me.MojoException as err:
        return common.service_error(err, code="delete_refused")
    except Exception:
        return {"error": "The app could not be deleted; nothing was changed.",
                "error_code": "delete_failed"}
    common.audit(user, "delete_webapp", None,
                 payload={"slug": slug, "reason": reason,
                          "bound": (approval.revision if approval else "")},
                 message=f"delete_webapp removed '{slug}'")
    return {"deleted": True, "slug": slug,
            "note": "The app, its versions, its addresses and its key are gone."}
