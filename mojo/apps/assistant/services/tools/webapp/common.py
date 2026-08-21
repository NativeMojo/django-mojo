"""Shared authority, resolution, binding, and audit helpers for the webapp domain.

**Two tiers of authority**, because the registry can only express one of them:

* ``permission="view_admin"`` is the registry gate, and it is global-only
  (``User.has_permission`` reads ``User.permissions`` plus ``is_superuser``).
  Any WebApp-specific permission name there would lock out exactly the
  group-scoped manager ``webapp_authority`` exists to serve.
* ``authorize=authorized`` narrows the LISTING to users who hold WebApp
  authority somewhere. It runs at every listing, dispatch, proposal and
  execution, so it is one EXISTS query and never the full eligible-group scan.
* The exact per-group check runs in ``preview`` and AGAIN in the handler,
  against the freshly re-read ``User``. Three independent gates.

**Refusal style is deliberately split.**

* A ``preview`` RAISES. The approval gate turns that into an ordinary tool
  error with no record at proposal, and ``precondition_failed`` at execution.
  It must raise a builtin ``ValueError`` / ``PermissionError``: those are the
  only two types whose message reaches the model (``approvals.run_preview``);
  anything else is reported generically. ``mojo.errors`` exceptions are NOT
  builtins, so :func:`translated` converts them at the boundary.
* A READ tool returns ``{"error": ...}`` and never raises — see :func:`safe_read`.

Every message is non-oracular: a missing row and an unauthorized row get the
same sentence, so a tool call can never be used to probe which ids exist.
"""

import hashlib

from mojo import errors as me
from mojo.helpers import logit

logger = logit.get_logger("assistant", "assistant.log")

# One sentence per resource kind, used for BOTH "no such row" and "not yours".
NO_WEBAPP = "No app with that id is available to you."
NO_DEPLOYMENT = "No deployment with that id is available to you."
NO_RELEASE = "No version with that id belongs to this app."
NO_ADDRESS = "No extra address with that id belongs to this app."
NO_OPERATION = "No setup with that id is available to you."
NO_GROUP = "No workspace with that id is available to you."

MANAGE_REFUSAL = (
    "Managing apps in this workspace needs WebApp and DNS management there.")
MANAGE_DAY2_REFUSAL = (
    "Changing a live app needs the manage_webapp permission in this workspace.")

MAX_REASON = 300
MIN_REASON = 3


class Refused(ValueError):
    """A precondition or resolution refusal whose message is safe to show."""


class Denied(PermissionError):
    """An authority refusal whose message is safe to show."""


def safe_read(func):
    """Turn a read handler's refusal into ``{"error": ...}``.

    Read tools answer questions; a refusal is an answer, not a crash. Mutating
    tools do the opposite — their previews raise, which is what makes the
    approval gate fail closed with no record.
    """
    import functools

    @functools.wraps(func)
    def wrapper(params, user):
        try:
            return func(params, user)
        except (ValueError, PermissionError) as err:
            return {"error": str(err)}
        except me.MojoException as err:
            return {"error": str(err)}

    return wrapper


def translated(func, *args, **kwargs):
    """Call a service, converting its refusal into the builtin the gate reads.

    ``me.ValueException`` / ``me.PermissionDeniedException`` are plain
    ``Exception`` subclasses, so a preview that let one escape would reach the
    model as a generic "the system changed" message with the real reason only
    in the log. Convert at the boundary instead.
    """
    try:
        return func(*args, **kwargs)
    except me.PermissionDeniedException as err:
        raise Denied(str(err))
    except me.MojoException as err:
        raise Refused(str(err))


def service_error(err, code="webapp_refused"):
    """A handler's return value for a service refusal.

    ``{"error", "error_code"}`` lands the approval record ``failed`` with that
    exact ``failure_code`` and shows the service's own words on the card.
    """
    if isinstance(err, me.PermissionDeniedException):
        code = "permission_denied"
    return {"error": str(err)[:MAX_REASON], "error_code": code}


# --- identifiers ----------------------------------------------------------

def as_int(value):
    """A positive integer id, or None. Never raises on junk."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    try:
        parsed = int(str(value).strip())
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


# --- authority ------------------------------------------------------------

def authorized(user):
    """The cheap ``authorize`` predicate: does this user manage apps ANYWHERE?

    One EXISTS query plus the global check. Deliberately not
    ``eligible_webapp_groups``, which filters every active group through a
    nine-deep ``select_related`` — that answer is only worth computing when it
    IS the answer, which is ``list_webapp_groups`` and nowhere else.
    """
    from mojo.apps.account.services import webapp_authority

    try:
        if webapp_authority.has_global_webapp_authority(user):
            return True
        return user.get_groups_with_permission(
            ["manage_webapp", "security"]).exists()
    except Exception:
        logger.exception("webapp authorize() failed; treating as denied")
        return False


def can_view(user, group):
    """WebApp ``VIEW_PERMS`` on an effectively-active group, as RestMeta reads it."""
    from mojo.apps.edge.models import WebApp

    if group is None or not group.is_effectively_active():
        return False
    return bool(group.user_has_permission(
        user, WebApp.get_rest_meta_prop("VIEW_PERMS", [])))


def can_manage_onboarding(user, group):
    """The onboarding endpoints' contract: group ``security``, or webapp+dns."""
    from mojo.apps.account.services import webapp_authority

    return bool(webapp_authority.can_manage_group_webapps(user, group))


def can_manage(user, group):
    """The DAY-2 write contract.

    Every day-2 endpoint carries ``requires_perms("manage_webapp")`` on top of
    its object check, and ``implied_perms`` does not expand it — so a member
    holding only group ``security`` is refused by the Admin and must be refused
    here. Onboarding deliberately keeps the looser gate above.
    """
    if not can_manage_onboarding(user, group):
        return False
    return bool(group.user_has_permission(user, "manage_webapp"))


def require_view(user, group):
    if not can_view(user, group):
        raise Denied(NO_GROUP)
    return group


def require_manage_onboarding(user, group):
    if group is None or not group.is_effectively_active():
        raise Denied(NO_GROUP)
    if not can_manage_onboarding(user, group):
        raise Denied(MANAGE_REFUSAL)
    return group


def require_manage(user, group):
    require_manage_onboarding(user, group)
    if not group.user_has_permission(user, "manage_webapp"):
        raise Denied(MANAGE_DAY2_REFUSAL)
    return group


# --- resolution -----------------------------------------------------------

def group_for(user, value, manage=False, day2=False):
    """Resolve a client-supplied group id under the right authority tier.

    ``Group.get_active`` resolves an inactive group — or an active one under a
    deactivated ancestor — exactly like a nonexistent id.
    """
    from mojo.apps.account.models import Group

    group = Group.get_active(as_int(value))
    if group is None:
        raise Denied(NO_GROUP)
    if day2:
        return require_manage(user, group)
    if manage:
        return require_manage_onboarding(user, group)
    return require_view(user, group)


def webapp_for(user, value, write=False):
    """The app behind an id, or the same refusal for missing and unauthorized."""
    from mojo.apps.edge.models import WebApp

    pk = as_int(value)
    row = None
    if pk is not None:
        row = WebApp.objects.select_related(
            "group", "vhost__domain", "vhost__certificate", "current_release",
        ).filter(pk=pk).first()
    if row is None or not can_view(user, row.group):
        raise Refused(NO_WEBAPP)
    if write:
        require_manage(user, row.group)
    return row


def deployment_for(user, value):
    """A deployment resolved through the app that owns it.

    ``webapp_deploy.payload`` performs no authority check of its own, so the
    tenancy decision has to happen here or not at all.
    """
    from mojo.apps.edge.models import WebAppDeployment

    pk = as_int(value)
    row = None
    if pk is not None:
        row = WebAppDeployment.objects.select_related(
            "webapp__group", "release", "previous_release").filter(pk=pk).first()
    if row is None or not can_view(user, row.webapp.group):
        raise Refused(NO_DEPLOYMENT)
    return row


def release_for(web_app, value):
    """A release SCOPED to this app, so a foreign id is never distinguishable."""
    from mojo.apps.edge.models import WebAppRelease

    pk = as_int(value)
    row = None
    if pk is not None:
        row = WebAppRelease.objects.filter(pk=pk, webapp=web_app).first()
    if row is None:
        raise Refused(NO_RELEASE)
    return row


def alias_for(web_app, value):
    """An extra address SCOPED to this app's aliases, never its own address."""
    from mojo.apps.edge.models import Vhost

    pk = as_int(value)
    row = None
    if pk is not None:
        row = Vhost.objects.select_related("domain").filter(
            pk=pk, alias_of=web_app).first()
    if row is None:
        raise Refused(NO_ADDRESS)
    return row


def operation_for(user, value):
    """An onboarding operation this actor may READ, from either surface."""
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding

    row = WebAppOnboardingOperation.objects.select_related(
        "group", "actor", "web_app", "domain", "certificate", "vhost").filter(
            operation_id=str(value or "").strip()).first() if value else None
    if row is None:
        raise Refused(NO_OPERATION)
    try:
        webapp_onboarding.assert_read_authority(row, user)
    except me.MojoException:
        raise Refused(NO_OPERATION)
    return row


# --- card content ---------------------------------------------------------

def reason_text(params):
    """The required, bound operator reason on a destructive tool.

    No WebApp endpoint accepts a typed confirmation echo — the portal's is a
    browser-side box the server never sees — and a model-typed echo proves
    nothing anyway. The human act is the approval click; this is the evidence
    that outlives it, bound into the approval and written to the audit trail.
    """
    text = str(params.get("reason") or "").strip()
    if len(text) < MIN_REASON:
        raise Refused(
            "A short reason is required for this operation (3-300 characters).")
    return text[:MAX_REASON]


def revision(*pairs):
    """The bound revision string: ``key:value|key:value``, order-significant."""
    return "|".join(f"{key}:{value}" for key, value in pairs)


def bound_value(approval, key):
    """Read one bound field back out of ``approval.revision`` at execution."""
    for part in str(getattr(approval, "revision", "") or "").split("|"):
        name, _, value = part.partition(":")
        if name == key:
            return value
    return None


def choice_digest(choice):
    """A stable, secret-free fingerprint of a normalized onboarding choice."""
    import json

    return hashlib.sha256(json.dumps(
        choice or {}, sort_keys=True, separators=(",", ":"),
        default=str).encode("utf-8")).hexdigest()[:32]


def context_ref(web_app):
    """The ``add_context`` hint for an app.

    Only ``edge.WebApp`` and ``dnsman.Domain`` are ever emitted: the client
    builds ``/api/{app}/{model_lowercased}/{pk}``, and releases and deployments
    do not serve at those paths — a ref to either would render a dead link.
    Tools never build a URL themselves; the core ``add_context`` tool owns that.
    """
    return {"app_name": "edge", "model_name": "WebApp", "pk": web_app.pk,
            "label": web_app.slug}


def domain_ref(domain):
    if domain is None:
        return None
    return {"app_name": "dnsman", "model_name": "Domain", "pk": domain.pk,
            "label": domain.name}


# --- audit ----------------------------------------------------------------

def audit(user, tool_name, web_app, payload=None, message=None):
    """One ``logit.Log`` row per mutating tool run. Ids and the reason only.

    The approval gate already files ``assistant:approval:*`` and
    ``assistant:tool:<name>``; this is the edge-side trail that names the app,
    mirroring ``locked.log(..., "edge:webapp_key")``.
    """
    try:
        import ujson

        from mojo.apps.logit.models import Log
        from mojo.apps.assistant.services.tools.models import _build_request

        Log.logit(
            _build_request(user, method="POST", path="/assistant/webapp"),
            message or f"{tool_name} on webapp {getattr(web_app, 'pk', None)}",
            kind=f"assistant:webapp:{tool_name}",
            model_name="edge.WebApp",
            model_id=getattr(web_app, "pk", 0) or 0,
            payload=ujson.dumps(payload or {}),
        )
    except Exception:
        logger.exception("Failed to write webapp assistant audit entry")
