"""Human-only WebApp onboarding and frozen summary endpoints."""

import mojo.decorators as md

from mojo import errors as me
from mojo.apps.account.models import Group
from mojo.apps.account.services import webapp_authority
from mojo.apps.edge.models import WebApp, WebAppOnboardingOperation
from mojo.apps.edge.services import webapp_onboarding as _webapp_onboarding


def _human(request):
    if (not webapp_authority.is_interactive_request(request) or
            not getattr(request.user, "is_authenticated", False) or
            getattr(request, "group_token", None) is not None):
        raise me.PermissionDeniedException(
            "An interactive user session is required for WebApp onboarding")


def _positive_group(value):
    if isinstance(value, (bool, float, list, tuple, dict)):
        raise me.ValueException("group must be a positive numeric id")
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            raise me.ValueException("group must be a positive numeric id")
    try:
        group_id = int(value)
    except (TypeError, ValueError):
        raise me.ValueException("group must be a positive numeric id")
    if group_id <= 0:
        raise me.ValueException("group must be a positive numeric id")
    group = Group.get_active(group_id)
    if group is None:
        raise me.PermissionDeniedException("The selected group is unavailable")
    return group


def _group_intent(request):
    _human(request)
    has_group = "group" in request.DATA
    has_intent = "group_intent" in request.DATA
    if has_group == has_intent:
        raise me.ValueException(
            "Provide exactly one of group or group_intent=new")
    if has_intent:
        intent = request.DATA.get("group_intent")
        if not isinstance(intent, str) or intent.strip().lower() != "new":
            raise me.ValueException("group_intent must be new")
        if not webapp_authority.can_create_webapp_group(request.user):
            raise me.PermissionDeniedException(
                "Creating a WebApp group is not granted globally")
        return "new", None
    group = _positive_group(request.DATA.get("group"))
    if not webapp_authority.can_manage_group_webapps(request.user, group):
        raise me.PermissionDeniedException(
            "WebApp and DNS management are not granted in this group")
    return "existing", group


def _group(request):
    """Compatibility helper for concrete-group callers and focused tests."""
    intent, group = _group_intent(request)
    if intent != "existing":
        raise me.ValueException("A concrete group is required")
    return group


def _operation(request, mutate=False):
    _human(request)
    operation = WebAppOnboardingOperation.objects.select_related(
        "group", "actor", "web_app", "domain", "certificate", "vhost").filter(
            operation_id=request.DATA.get("operation")).first()
    if operation is None:
        raise me.RestErrorException(
            "WebApp onboarding operation not found", code=404, status=404)
    _webapp_onboarding._assert_current(operation, request, mutate=mutate)
    return operation


@md.GET("webapp/onboarding/options")
@md.denies_key_backed_session()
@md.custom_security("interactive user plus centralized WebApp group authority")
def on_webapp_onboarding_options(request):
    intent, group = _group_intent(request)
    return _webapp_onboarding.options(group, group_intent=intent)


@md.GET("webapp/onboarding/precheck")
@md.denies_key_backed_session()
@md.custom_security("interactive user plus centralized WebApp group authority")
def on_webapp_onboarding_precheck(request):
    """URL-first pre-flight: normalize the desired address and report the
    verdict before any operation is created. Read-only, so no fresh-auth gate."""
    intent, group = _group_intent(request)
    return _webapp_onboarding.precheck(
        group, request.DATA.get("url"), group_intent=intent)


@md.POST("webapp/onboarding/create")
@md.denies_key_backed_session()
@md.custom_security("interactive user plus centralized WebApp group authority")
def on_webapp_onboarding_create(request):
    from mojo.apps.edge.services import webapp_destination

    intent, group = _group_intent(request)
    # Refuse before any WebApp exists when the installation has no serving
    # destination. This is the boundary backstop for the connected-domain and
    # purchase paths, which reach create without a precheck verdict — and the
    # only thing standing before a purchase moves money on an installation that
    # could never serve the app. A DestinationUnavailable is a plain 400 whose
    # message steers the operator to System Setup.
    webapp_destination.resolve()
    operation, created = _webapp_onboarding.create(
        group, request.user, _webapp_onboarding.request_origin(request),
        request.DATA, group_intent=intent)
    return {"created": created, "operation": _webapp_onboarding.serialize(operation)}


@md.GET("webapp/onboarding/detail")
@md.denies_key_backed_session()
@md.requires_params("operation")
@md.custom_security("operation actor and RestMeta group scope in body")
def on_webapp_onboarding_detail(request):
    return _webapp_onboarding.serialize(_operation(request))


@md.POST("webapp/onboarding/choose")
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("operation", "revision", "step", "choice")
@md.custom_security("operation actor, origin, revision, and RestMeta group scope in body")
def on_webapp_onboarding_choose(request):
    operation = _webapp_onboarding.choose(
        _operation(request, mutate=True), request, request.DATA)
    operation.refresh_from_db()
    return _webapp_onboarding.serialize(operation)


@md.POST("webapp/onboarding/cancel")
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("operation")
@md.custom_security("operation actor, origin, and RestMeta group scope in body")
def on_webapp_onboarding_cancel(request):
    operation = _webapp_onboarding.cancel(
        _operation(request, mutate=True), request)
    return _webapp_onboarding.serialize(operation)


@md.POST("webapp/onboarding/workflow")
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("webapp")
@md.custom_security("exact WebApp group manage permission in body")
def on_webapp_onboarding_workflow(request):
    """Return safe workflow text and optionally a newly minted key once."""
    from mojo.apps.edge.services import webapp_keys

    _human(request)
    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    if not webapp_authority.can_manage_group_webapps(
            request.user, web_app.group):
        raise me.PermissionDeniedException(
            "WebApp and DNS management are not granted in this group")
    result = _webapp_onboarding.workflow(
        web_app, _webapp_onboarding.request_origin(request))
    action = str(request.DATA.get("action") or "").strip().lower()
    if action:
        operation_id = request.DATA.get("operation_id")
        if not operation_id:
            raise me.ValueException("operation_id is required to create a key")
        receipt = webapp_keys.link_once(
            web_app, action, request.user, operation_id)
        result["deployment_key"] = receipt
        if receipt.get("replayed") and not receipt.get("token"):
            result["deployment_key"]["delivery"] = "secret_unavailable"
    return result


@md.GET("webapp/summary")
@md.denies_key_backed_session()
@md.requires_params("webapp")
@md.custom_security("WebApp RestMeta group scope in body")
def on_webapp_summary(request):
    _human(request)
    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["VIEW_PERMS", "SAVE_PERMS"], web_app)
    return _webapp_onboarding.summary_for(web_app)


SUMMARIES_LIMIT = 50


def _is_wildcard(certificate, domain_name):
    """Whether this certificate covers `*.<domain>`.

    Decided here rather than in the browser: the client would have to know
    that a wildcard can be carried by either the common name or a SAN, and
    two surfaces guessing that rule is one guess too many.
    """
    if certificate is None or not domain_name:
        return False
    wildcard = f"*.{domain_name}".lower()
    names = [certificate.common_name or ""]
    names.extend(certificate.sans or [])
    return any(str(name).strip().lower() == wildcard for name in names)


def _fleet(rows):
    """What the LISTED apps, together, are serving on.

    Scoped to exactly the rows in this response — never a fleet-wide read.
    A subhead that counted apps the caller cannot see would be a leak, and a
    subhead that disagreed with the rows under it would be worse than none.

    Every value comes from the objects `select_related` already loaded, so
    this block adds no query.

    `certificate` is all-or-nothing on purpose: it is populated only when ONE
    certificate backs EVERY listed address. Naming one of several would be a
    claim about apps it does not cover.
    """
    primaries = [row.vhost for row in rows if row.vhost_id]
    live = len([row for row in rows if row.vhost_id and row.current_release_id])
    domains = sorted({vhost.domain.name for vhost in primaries if vhost.domain_id})
    certificate_ids = {vhost.certificate_id for vhost in primaries
                       if vhost.certificate_id}
    certificate = None
    # One certificate, and no listed address left uncovered by it.
    if len(certificate_ids) == 1 and all(v.certificate_id for v in primaries):
        covered = next(v for v in primaries if v.certificate_id)
        found = covered.certificate
        certificate = {
            "wildcard": _is_wildcard(found, covered.domain.name
                                     if covered.domain_id else ""),
            "common_name": found.common_name,
            "not_after": found.not_after.isoformat() if found.not_after else None,
            "renew_after": (found.renew_after.isoformat()
                            if found.renew_after else None),
        }
    return {
        "live": live,
        "domains": domains,
        "certificate_count": len(certificate_ids),
        "certificate": certificate,
    }


@md.GET("webapp/summaries")
@md.denies_key_backed_session()
@md.custom_security("interactive user; WebApp VIEW_PERMS list-path parity, "
                    "including the unconditional request.group intersection")
def on_webapp_summaries(request):
    """Bounded slim row projection for every app this caller may LIST.

    Serves the merged Deployments lane one item per app — the summary-v1
    subset a list row needs (address + certificate, current release, latest
    deployment) — at flat query cost, where per-app ``summary_for()`` would be
    ~4 queries each. The full summary stays the drill-in's contract.

    Visibility copies BOTH halves of the real list path:

    1. The permission branches: a global ``VIEW_PERMS`` holder sees all rows;
       otherwise groups where the member holds a VIEW perm; otherwise denied.
    2. The **unconditional** ``request.group`` intersection — the twin of
       ``on_rest_list``'s group filter. The dispatcher binds ``request.group``
       from caller input on every request, which flips
       ``rest_check_permission`` into its member branch; without this
       intersection a member-level grant would read every tenant's rows.

    Invariant: returns rows for exactly the ids ``GET /api/edge/webapp`` would
    list for the same request.

    Additive (item 2230), ``schema_version`` still 1: each row carries
    ``current_release.source`` and ``latest_deployment.release``, and the
    envelope carries a ``fleet`` block describing what the LISTED apps share —
    see ``_fleet``.
    """
    from mojo.apps.edge.models import WebAppDeployment

    _human(request)
    view_perms = WebApp.get_rest_meta_prop("VIEW_PERMS", [])
    if WebApp.rest_check_permission(request, "VIEW_PERMS"):
        queryset = WebApp.objects.all()
    else:
        groups = request.user.get_groups_with_permission(view_perms)
        if not groups:
            raise me.PermissionDeniedException(
                "WebApp visibility is not granted globally or in any group")
        queryset = WebApp.objects.filter(group__in=groups)
    if request.group is not None:
        queryset = queryset.filter(group=request.group)

    rows = list(queryset.select_related(
        "vhost__domain", "vhost__certificate", "current_release",
        "group").order_by("slug")[:SUMMARIES_LIMIT + 1])
    truncated = len(rows) > SUMMARIES_LIMIT
    rows = rows[:SUMMARIES_LIMIT]
    # One batched latest-deployment read (Postgres DISTINCT ON), matching
    # summary_for's `.first()` "latest" semantics (-created default ordering).
    latest = {
        deployment.webapp_id: deployment
        for deployment in WebAppDeployment.objects.filter(
            webapp__in=rows).select_related("release").order_by(
                "webapp_id", "-created").distinct("webapp_id")
    }

    items = []
    for row in rows:
        vhost = row.vhost if row.vhost_id else None
        certificate = vhost.certificate if vhost and vhost.certificate_id else None
        release = row.current_release
        deployment = latest.get(row.pk)
        items.append({
            "webapp": {
                "id": row.pk, "slug": row.slug,
                "display_name": row.display_name,
                "environment": row.environment,
                "deployment_ref": row.deployment_ref,
            },
            "address": ({
                "hostname": vhost.server_name,
                "certificate": ({
                    "status": certificate.status,
                    "not_after": (certificate.not_after.isoformat()
                                  if certificate.not_after else None),
                } if certificate else None),
            } if vhost else None),
            "current_release": ({
                "id": release.pk, "version": release.version,
                "status": release.status,
                "created": release.created.isoformat(),
                "source": release.source,
            } if release else None),
            "latest_deployment": ({
                "id": deployment.pk, "status": deployment.status,
                "created": deployment.created.isoformat(),
                "finished": (deployment.finished.isoformat()
                             if deployment.finished else None),
                # The release this DEPLOYMENT carried, which after a rollback
                # is deliberately not the one `current_release` names. A
                # failure banner reads this; what is still serving reads that.
                "release": ({
                    "id": deployment.release_id,
                    "version": deployment.release.version,
                    "source": deployment.release.source,
                } if deployment.release_id else None),
            } if deployment else None),
        })
    return {"schema_version": 1, "items": items, "count": len(items),
            "limit": SUMMARIES_LIMIT, "truncated": truncated,
            "fleet": _fleet(rows)}
