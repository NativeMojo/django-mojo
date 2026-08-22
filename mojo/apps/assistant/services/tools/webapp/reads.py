"""Read tools for the webapp domain — every one a thin wrapper over the service
the matching Admin endpoint already calls.

Nothing here infers. DNS ownership, certificate support, serving pools and
completion all come from the service that owns them; a hostname is never parsed
to guess who controls it, and ``queued``/``deploying``/``pending`` is never
reported as done.
"""

from mojo.apps.assistant import tool

from . import common


DOMAIN = "webapp"
PERMISSION = "view_admin"

GROUPS_LIMIT = 50
RELEASES_LIMIT = 25
DEPLOYMENTS_LIMIT = 15
MAX_NODE_ERRORS = 5
MAX_NODE_ERROR_CHARS = 200

UPLOAD_HANDOFF = (
    "Uploading a build from a laptop is an interactive Admin workflow "
    "(register, upload, complete) and is not available in chat. Push to the "
    "connected repository, or use Admin -> Deployments -> the app -> Deploys.")
KEY_HANDOFF = (
    "A deploy key is shown exactly once, at the moment it is created, so it "
    "cannot be created or rotated in chat. Use Admin -> Deployments -> the "
    "app -> the Key tab. Turning an existing key off IS available here "
    "(revoke_webapp_deploy_key).")


def _deployment_projection(deployment, payload, include_nodes):
    """Counts for every viewer; per-node detail only for a writer; runner ids
    for nobody.

    ``webapp_deploy.payload`` carries up to 2000 characters of raw job error
    per node and the runner id that produced it. The error is job stderr by
    another name and the runner id is fleet inventory — the model needs to know
    how many nodes failed, never which host they were.
    """
    rows = list(payload.get("targets") or [])
    completed = len([row for row in rows if row.get("status") == "completed"])
    failed = len([row for row in rows
                  if row.get("status") in ("failed", "canceled", "expired", "missing")])
    result = {
        "deployment": payload.get("deployment"),
        "webapp": payload.get("webapp"),
        "release": payload.get("release"),
        "version": payload.get("version"),
        "status": payload.get("status"),
        "terminal": payload.get("terminal"),
        "success": payload.get("success"),
        "detail": payload.get("detail"),
        "created": payload.get("created"),
        "started": payload.get("started"),
        "finished": payload.get("finished"),
        "nodes": {
            "expected": len(rows),
            "completed": completed,
            "failed": failed,
            "pending": max(len(rows) - completed - failed, 0),
        },
    }
    if not include_nodes:
        return result
    detail = []
    errors = []
    for row in rows:
        outcome = row.get("result") or {}
        detail.append({
            "status": row.get("status"),
            "changed": outcome.get("changed"),
            "generation": outcome.get("generation"),
        })
        text = str(row.get("error") or "").strip()
        if text and len(errors) < MAX_NODE_ERRORS:
            errors.append(text[:MAX_NODE_ERROR_CHARS])
    result["nodes"]["detail"] = detail
    result["nodes"]["errors"] = errors
    return result


@tool(
    name="list_webapp_groups",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    description=(
        "List the workspaces where this operator can create and manage web "
        "apps. Start here when the operator has not named a workspace: the "
        "ids returned are the only ones the other webapp tools accept."),
    input_schema={"type": "object", "properties": {}},
)
@common.safe_read
def _tool_list_webapp_groups(params, user):
    from mojo.apps.account.services import webapp_authority

    groups = webapp_authority.eligible_webapp_groups(user)
    rows = [{"id": group.pk, "name": group.name, "can_manage": True}
            for group in groups[:GROUPS_LIMIT]]
    return {"groups": rows, "count": len(rows),
            "truncated": len(groups) > GROUPS_LIMIT}


@tool(
    name="get_webapp_setup_options",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    description=(
        "The server-owned choices for creating a new web app in one workspace: "
        "release buckets, environments, the serving destination, the "
        "workspace's apps domain, and whether GitHub is connected. Offer only "
        "what this returns — never a bucket or environment invented in chat. "
        "A destination_error or apps_domain_error is the plain-language reason "
        "a path is unavailable; relay it as-is."),
    input_schema={
        "type": "object",
        "properties": {
            "group": {"type": "integer", "description": "Workspace id from list_webapp_groups"},
        },
        "required": ["group"],
    },
)
@common.safe_read
def _tool_get_webapp_setup_options(params, user):
    from mojo.apps.edge.services import webapp_onboarding

    group = common.group_for(user, params.get("group"))
    return common.translated(webapp_onboarding.options, group)


@tool(
    name="precheck_new_webapp_address",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    description=(
        "Check a web address BEFORE creating an app: normalize it, decide "
        "which workspace domain covers it, and report the verdict (ready, "
        "records_needed, taken, apex, deep_label, domain_unknown, conflict, "
        "configuration_required, invalid). When records are returned the "
        "operator must publish them at their own DNS host. Buying a domain is "
        "not available in chat and is never offered here."),
    input_schema={
        "type": "object",
        "properties": {
            "group": {"type": "integer", "description": "Workspace id"},
            "url": {"type": "string", "description": "The address the operator typed, e.g. app.example.com"},
        },
        "required": ["group", "url"],
    },
)
@common.safe_read
def _tool_precheck_new_webapp_address(params, user):
    from mojo.apps.edge.services import webapp_onboarding

    group = common.group_for(user, params.get("group"))
    result = common.translated(
        webapp_onboarding.precheck, group, params.get("url"))
    options = result.get("options")
    if isinstance(options, dict):
        # Purchase is excluded by design; offering it here would be offering a
        # path that the service itself refuses for this surface.
        result["options"] = {key: value for key, value in options.items()
                             if key not in ("purchase_available", "godaddy_available")}
    return result


@tool(
    name="list_webapps",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    description=(
        "List the web apps this operator can see, with each one's address, "
        "certificate, current version and latest deployment, plus a fleet "
        "summary of what the listed apps share. Optionally narrowed to one "
        "workspace or environment."),
    input_schema={
        "type": "object",
        "properties": {
            "group": {"type": "integer", "description": "Only apps in this workspace"},
            "environment": {"type": "string",
                            "enum": ["production", "staging", "preview", "development"],
                            "description": "Only apps in this environment"},
        },
    },
)
@common.safe_read
def _tool_list_webapps(params, user):
    from mojo.apps.edge.models import WebApp, WebAppDeployment
    from mojo.apps.edge.rest.webapp_onboarding import SUMMARIES_LIMIT, _fleet

    view_perms = WebApp.get_rest_meta_prop("VIEW_PERMS", [])
    if user.has_permission(view_perms):
        queryset = WebApp.objects.all()
    else:
        groups = user.get_groups_with_permission(view_perms)
        if not groups:
            return {"error": "You cannot list web apps in any workspace."}
        queryset = WebApp.objects.filter(group__in=groups)
    if params.get("group") is not None:
        group = common.group_for(user, params.get("group"))
        queryset = queryset.filter(group=group)
    if params.get("environment"):
        queryset = queryset.filter(environment=str(params["environment"]))

    rows = list(queryset.select_related(
        "vhost__domain", "vhost__certificate", "current_release",
        "group").order_by("slug")[:SUMMARIES_LIMIT + 1])
    truncated = len(rows) > SUMMARIES_LIMIT
    rows = rows[:SUMMARIES_LIMIT]
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
                "id": row.pk, "slug": row.slug, "group": row.group_id,
                "display_name": row.display_name,
                "environment": row.environment,
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
                "status": release.status, "source": release.source,
            } if release else None),
            "latest_deployment": ({
                "id": deployment.pk, "status": deployment.status,
                "created": deployment.created.isoformat(),
                "finished": (deployment.finished.isoformat()
                             if deployment.finished else None),
            } if deployment else None),
            "context_ref": common.context_ref(row),
        })
    return {"items": items, "count": len(items), "limit": SUMMARIES_LIMIT,
            "truncated": truncated, "fleet": _fleet(rows)}


@tool(
    name="get_webapp",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    description=(
        "Everything about one web app: address, certificate, extra addresses, "
        "current version, latest deployment, onboarding state, and whether a "
        "deploy key is linked. Pass context_ref straight to add_context so the "
        "operator can open the app in the Admin portal."),
    input_schema={
        "type": "object",
        "properties": {"webapp": {"type": "integer", "description": "WebApp id"}},
        "required": ["webapp"],
    },
)
@common.safe_read
def _tool_get_webapp(params, user):
    from mojo.apps.edge.services import webapp_onboarding

    web_app = common.webapp_for(user, params.get("webapp"))
    result = common.translated(webapp_onboarding.summary_for, web_app)
    result["context_ref"] = common.context_ref(web_app)
    return result


@tool(
    name="get_webapp_serving",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    description=(
        "How one app is served: its address and DNS mode, its certificate "
        "(including whether a dedicated one is even supported here), its shape "
        "and pool, its paths, and every address it answers on. Fleet pools and "
        "upstream inventory are null for a viewer who could not save them."),
    input_schema={
        "type": "object",
        "properties": {"webapp": {"type": "integer", "description": "WebApp id"}},
        "required": ["webapp"],
    },
)
@common.safe_read
def _tool_get_webapp_serving(params, user):
    from mojo.apps.edge.services import webapp_alias, webapp_serving

    web_app = common.webapp_for(user, params.get("webapp"))
    writer = common.can_manage(user, web_app.group)
    result = common.translated(
        webapp_serving.serving_for, web_app, include_editables=writer)
    result["addresses"] = common.translated(webapp_alias.status_rows, web_app)
    result["can_manage"] = writer
    result["context_ref"] = common.context_ref(web_app)
    return result


@tool(
    name="preview_webapp_alias",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    description=(
        "What adding a custom address to an app WOULD do, without doing any of "
        "it: ready (with who publishes the DNS), needs_domain (connect it on "
        "the Domains page first), or unusable with the reason. Reports nothing "
        "about whether the address is already occupied."),
    input_schema={
        "type": "object",
        "properties": {
            "webapp": {"type": "integer", "description": "WebApp id"},
            "hostname": {"type": "string", "description": "The address to check, e.g. www.customer.com"},
        },
        "required": ["webapp", "hostname"],
    },
)
@common.safe_read
def _tool_preview_webapp_alias(params, user):
    from mojo.apps.edge.services import webapp_alias

    # This answers a question ABOUT A WRITE, so it carries the write's
    # authority exactly as `GET webapp/attach_preview` does.
    web_app = common.webapp_for(user, params.get("webapp"), write=True)
    result = common.translated(
        webapp_alias.preview, web_app, params.get("hostname"), user)
    return dict(result, webapp=web_app.pk)


@tool(
    name="check_webapp_health",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    description=(
        "Probe an app's live address over HTTPS right now. not_configured "
        "means no public address is set up yet, which is not a failure. The "
        "detail is a safe status line, never a raw error."),
    input_schema={
        "type": "object",
        "properties": {"webapp": {"type": "integer", "description": "WebApp id"}},
        "required": ["webapp"],
    },
)
@common.safe_read
def _tool_check_webapp_health(params, user):
    from django.utils import timezone

    from mojo.apps.edge.services.public_probe import (
        UnsafePublicProbe, probe_https_root)

    web_app = common.webapp_for(user, params.get("webapp"))
    if web_app.vhost_id is None:
        return {"webapp": web_app.pk, "status": "not_configured",
                "checked": None,
                "detail": "No public address is configured yet"}
    checked = timezone.now().isoformat()
    try:
        result = probe_https_root(f"https://{web_app.vhost.server_name}")
    except UnsafePublicProbe as err:
        return {"webapp": web_app.pk, "status": "unhealthy",
                "checked": checked, "detail": str(err)}
    except Exception:
        return {"webapp": web_app.pk, "status": "unhealthy",
                "checked": checked, "detail": "unreachable"}
    if result.get("ok"):
        return {"webapp": web_app.pk, "status": "healthy", "checked": checked,
                "detail": f"HTTP {result.get('status')}"}
    detail = (f"HTTP {result.get('status')}" if result.get("status")
              else result.get("reason") or "unreachable")
    return {"webapp": web_app.pk, "status": "unhealthy", "checked": checked,
            "detail": detail}


@tool(
    name="get_webapp_deploy_history",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    description=(
        "An app's recent versions and recent deployments. A version's status "
        "says whether it is live, superseded or still pending (an abandoned "
        "upload); only an uploaded, live or superseded version can be rolled "
        "back to. A deployment that is queued or deploying has not landed yet."),
    input_schema={
        "type": "object",
        "properties": {"webapp": {"type": "integer", "description": "WebApp id"}},
        "required": ["webapp"],
    },
)
@common.safe_read
def _tool_get_webapp_deploy_history(params, user):
    from mojo.apps.edge.models import WebAppDeployment, WebAppRelease

    web_app = common.webapp_for(user, params.get("webapp"))
    releases = [
        row.to_dict(graph="basic")
        for row in WebAppRelease.objects.filter(webapp=web_app).order_by(
            "-created")[:RELEASES_LIMIT]
    ]
    deployments = [
        row.to_dict(graph="list")
        for row in WebAppDeployment.objects.filter(webapp=web_app).select_related(
            "release", "previous_release").order_by("-created")[:DEPLOYMENTS_LIMIT]
    ]
    return {
        "webapp": web_app.pk,
        "current_release": web_app.current_release_id,
        "releases": releases,
        "deployments": deployments,
        "context_ref": common.context_ref(web_app),
    }


@tool(
    name="get_webapp_deployment",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    description=(
        "One deployment's status and fleet evidence: node counts always, and "
        "per-node outcomes plus a few truncated errors for an operator who "
        "could change it. A failed deployment is restored to the previous "
        "version by the platform itself — never call that the rollback an "
        "operator asked for."),
    input_schema={
        "type": "object",
        "properties": {"deployment": {"type": "integer", "description": "Deployment id"}},
        "required": ["deployment"],
    },
)
@common.safe_read
def _tool_get_webapp_deployment(params, user):
    from mojo.apps.edge.services import webapp_deploy

    deployment = common.deployment_for(user, params.get("deployment"))
    payload = common.translated(webapp_deploy.payload, deployment)
    writer = common.can_manage(user, deployment.webapp.group)
    result = _deployment_projection(deployment, payload, writer)
    result["context_ref"] = common.context_ref(deployment.webapp)
    return result


@tool(
    name="get_webapp_deploy_setup",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    description=(
        "How automated deploys are set up for an app: the safe status of its "
        "deploy key (never the key itself), and the GitHub Actions workflow "
        "file to drop into the repository. The api-url placeholder in the yaml "
        "must be replaced with the platform's own origin. Creating or rotating "
        "a key is an Admin-only, shown-once action."),
    input_schema={
        "type": "object",
        "properties": {"webapp": {"type": "integer", "description": "WebApp id"}},
        "required": ["webapp"],
    },
)
@common.safe_read
def _tool_get_webapp_deploy_setup(params, user):
    from mojo.apps.edge.services import webapp_keys, webapp_onboarding

    web_app = common.webapp_for(user, params.get("webapp"))
    status = common.translated(webapp_keys.status, web_app)
    # No `action` is ever passed, so `link_once` is unreachable from here and
    # no key can be minted. `api_origin=None` renders the placeholder origin.
    workflow = common.translated(webapp_onboarding.workflow, web_app, None)
    return {
        "webapp": web_app.pk,
        "secret_name": "MOJO_DEPLOY_KEY",
        "key": {
            "linked": bool(status.get("linked")),
            "active": bool(status.get("active")),
            "created": str(status.get("created")) if status.get("created") else None,
            "last_used": str(status.get("last_used")) if status.get("last_used") else None,
            "last_action": status.get("last_action"),
        },
        "workflow": {
            "filename": workflow.get("filename"),
            "yaml": workflow.get("yaml"),
            "repository": workflow.get("repository"),
        },
        "key_handoff": KEY_HANDOFF,
        "upload_handoff": UPLOAD_HANDOFF,
        "context_ref": common.context_ref(web_app),
    }


@tool(
    name="get_webapp_setup_status",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    description=(
        "The current state of one app setup: which step it is on, its "
        "revision, its evidence, and its activity trail. WAIT_FOR_USER (status "
        "waiting with records in the address evidence) means the operator must "
        "publish those DNS records themselves; a pending certificate is not an "
        "issued one. Readable for a setup started on either surface; only "
        "continuing it is tied to where it began."),
    input_schema={
        "type": "object",
        "properties": {
            "operation_id": {"type": "string", "description": "The setup's operation_id (a UUID)"},
        },
        "required": ["operation_id"],
    },
)
@common.safe_read
def _tool_get_webapp_setup_status(params, user):
    from mojo.apps.edge.services import webapp_onboarding

    operation = common.operation_for(user, params.get("operation_id"))
    result = common.translated(webapp_onboarding.serialize, operation)
    result["origin_surface"] = (
        "assistant" if operation.origin == webapp_onboarding.ASSISTANT_ORIGIN
        else "admin_portal")
    if operation.web_app_id:
        result["context_ref"] = {
            "app_name": "edge", "model_name": "WebApp",
            "pk": operation.web_app_id,
            "label": (operation.state or {}).get("profile", {}).get("slug") or "",
        }
    return result
