"""Guided WebApp setup: create, answer a step, cancel.

The assistant runs setup under its OWN surface identity
(``webapp_onboarding.ASSISTANT_ORIGIN``). A setup started in the Admin portal
can be REPORTED on here (``get_webapp_setup_status``) but never continued, and
vice versa — each surface owns what it started, and neither impersonates the
other.

Two paths are excluded at the service, not merely omitted here: buying a domain
(the one-use ``confirm_token`` must never enter model context) and creating a
workspace as a side effect of setup (the group would not exist at proposal
time, so "the exact group" could not be bound).
"""

from mojo import errors as me
from mojo.apps.assistant import tool

from . import common


DOMAIN = "webapp"
PERMISSION = "view_admin"

PORTAL_SETUP = (
    "This setup was started in the Admin portal, so it has to be continued "
    "there. Report on it here any time with get_webapp_setup_status.")
SETUP_ELSEWHERE = (
    "A setup for this app is already in progress on another surface or under "
    "another administrator. Continue or cancel it there first.")


def _profile_payload(params):
    return {
        "slug": str(params.get("slug") or "").strip().lower(),
        "display_name": str(params.get("display_name") or "").strip(),
        "environment": str(params.get("environment") or "production").strip(),
        "bucket": str(params.get("bucket") or "").strip(),
    }


def _validated_profile(params):
    from mojo.apps.edge.services import webapp_onboarding

    return common.translated(
        webapp_onboarding._profile, _profile_payload(params))


def _assistant_operation(user, params, mutating=True):
    """The operation behind an id, continuable by THIS actor from chat."""
    from mojo.apps.edge.models.web_app_onboarding_operation import TERMINAL_STATUSES
    from mojo.apps.edge.services import webapp_onboarding

    operation = common.operation_for(user, params.get("operation_id"))
    if operation.origin != webapp_onboarding.ASSISTANT_ORIGIN:
        raise common.Denied(PORTAL_SETUP)
    common.require_manage_onboarding(user, operation.group)
    if mutating and operation.status in TERMINAL_STATUSES:
        raise common.Refused(
            f"This setup is already {operation.status}; nothing left to do.")
    return operation


# ---------------------------------------------------------------------------
# start_webapp_setup
# ---------------------------------------------------------------------------

def _summarize_start(params, user):
    profile = _profile_payload(params)
    return (f"Start setup for the app '{profile['slug']}' "
            f"({profile['environment']}) in workspace {params.get('group')}. "
            f"Nothing is served until the address step is answered.")


def _preview_start(params, user):
    from mojo.apps.edge.services import webapp_destination

    group = common.group_for(user, params.get("group"), manage=True)
    profile = _validated_profile(params)
    # The installation-level backstop the create endpoint runs: refuse before
    # any WebApp exists when nothing could ever serve it.
    common.translated(webapp_destination.resolve)
    return {
        "summary": (f"Creates the app '{profile['slug']}' in {group.name} and "
                    f"opens its address step."),
        "details": {"group": group.name, "slug": profile["slug"],
                    "environment": profile["environment"],
                    "bucket": profile["bucket"]},
        "revision": common.revision(
            ("group", group.pk), ("slug", profile["slug"]),
            ("env", profile["environment"]), ("bucket", profile["bucket"])),
    }


@tool(
    name="start_webapp_setup",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    summarize=_summarize_start,
    preview=_preview_start,
    description=(
        "Create a new web app and open its guided setup in one workspace. The "
        "workspace must already exist — creating one from chat is not "
        "supported. Read get_webapp_setup_options first and offer only the "
        "buckets and environments it returns. Requires operator approval: "
        "calling this tool creates an approval card and does not execute."),
    input_schema={
        "type": "object",
        "properties": {
            "group": {"type": "integer", "description": "Workspace id from list_webapp_groups"},
            "slug": {"type": "string", "description": "Short app name: lowercase letters, digits, - or _"},
            "display_name": {"type": "string", "description": "Human-facing name (defaults to the slug)"},
            "environment": {"type": "string",
                            "enum": ["production", "staging", "preview", "development"],
                            "description": "Defaults to production"},
            "bucket": {"type": "string", "description": "A release bucket from get_webapp_setup_options"},
        },
        "required": ["group", "slug", "bucket"],
    },
)
def _tool_start_webapp_setup(params, user, approval=None):
    from mojo.apps.edge.services import webapp_destination, webapp_onboarding

    try:
        group = common.group_for(user, params.get("group"), manage=True)
        webapp_destination.resolve()
        payload = _profile_payload(params)
        if approval is not None:
            # Exactly-once: a replayed approval reconciles the same receipt
            # instead of starting a second setup for the same app.
            payload["operation_id"] = str(approval.uuid)
        operation, created = webapp_onboarding.create(
            group, user, webapp_onboarding.ASSISTANT_ORIGIN, payload)
    except me.PermissionDeniedException as err:
        if "owned by another" in str(err):
            return {"error": SETUP_ELSEWHERE, "error_code": "setup_elsewhere"}
        return common.service_error(err)
    except me.MojoException as err:
        return common.service_error(err, code="setup_refused")

    common.audit(user, "start_webapp_setup", operation.web_app,
                 payload={"group_id": group.pk,
                          "operation": str(operation.operation_id),
                          "bound": (approval.revision if approval else "")})
    return {
        "created": created,
        "operation_id": str(operation.operation_id),
        "status": operation.status,
        "cursor": operation.cursor,
        "revision": operation.revision,
        "webapp": operation.web_app_id,
        "next": ("Read get_webapp_setup_status, then answer the current step "
                 "with answer_webapp_setup_step."),
    }


# ---------------------------------------------------------------------------
# answer_webapp_setup_step
# ---------------------------------------------------------------------------

def _summarize_answer(params, user):
    return (f"Answer the '{params.get('step')}' step of setup "
            f"{params.get('operation_id')} and let it continue.")


def _preview_answer(params, user):
    from django.utils import timezone

    from mojo.apps.edge.services import webapp_onboarding

    operation = _assistant_operation(user, params)
    choice = params.get("choice") or {}
    if not isinstance(choice, dict):
        raise common.Refused("The choice must be an object.")
    if webapp_onboarding._carries_purchase(choice):
        raise common.Denied(
            "Buying a domain is not available in chat. Choose a domain the "
            "workspace already has, or connect one on the Domains page.")
    step = str(params.get("step") or "").strip()
    if step != operation.cursor:
        raise common.Refused(
            f"The current setup step is '{operation.cursor}', not '{step}'.")
    if (operation.lease_expires_at and
            operation.lease_expires_at > timezone.now()):
        raise common.Refused(
            "This setup is reconciling right now. Read it again in a moment.")
    return {
        "summary": f"Answers the '{step}' step of setup {operation.operation_id}.",
        "details": {"step": step, "status": operation.status,
                    "webapp": operation.web_app_id},
        # The revision is BOUND, not re-read at execution: passing a live read
        # back to choose() would turn its optimistic-concurrency guard into a
        # tautology. A moved revision is precondition_failed, and the model
        # re-reads with get_webapp_setup_status and proposes the current step.
        "revision": common.revision(
            ("op", operation.operation_id), ("rev", operation.revision),
            ("cursor", operation.cursor), ("choice", common.choice_digest(choice))),
    }


@tool(
    name="answer_webapp_setup_step",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    fresh_auth_seconds=600,
    summarize=_summarize_answer,
    preview=_preview_answer,
    description=(
        "Answer the setup's CURRENT step with a choice the server offered. "
        "Pass only values that came back from get_webapp_setup_options, "
        "precheck_new_webapp_address or get_webapp_setup_status — never a "
        "domain, bucket or pool invented in chat. Buying a domain is refused "
        "here. Requires operator approval: calling this tool creates an "
        "approval card and does not execute."),
    input_schema={
        "type": "object",
        "properties": {
            "operation_id": {"type": "string", "description": "The setup's operation_id"},
            "step": {"type": "string",
                     "enum": ["app", "address", "github", "verify"],
                     "description": "Must equal the setup's current step"},
            "choice": {"type": "object",
                       "description": "The step's answer, e.g. {\"label\": \"app\", \"domain\": 12} for address"},
        },
        "required": ["operation_id", "step", "choice"],
    },
)
def _tool_answer_webapp_setup_step(params, user, approval=None):
    from mojo.apps.edge.services import webapp_onboarding

    operation = _assistant_operation(user, params)
    bound = common.bound_value(approval, "rev")
    revision = common.as_int(bound)
    if revision is None and str(bound or "") == "0":
        revision = 0
    if revision is None:
        return {"error": "This approval carried no setup revision.",
                "error_code": "revision_unbound"}
    try:
        updated = webapp_onboarding.choose_for_actor(
            operation, user, webapp_onboarding.ASSISTANT_ORIGIN,
            {"revision": revision, "step": params.get("step"),
             "choice": params.get("choice") or {}})
    except me.MojoException as err:
        return common.service_error(err, code="setup_step_refused")

    common.audit(user, "answer_webapp_setup_step", operation.web_app,
                 payload={"operation": str(operation.operation_id),
                          "step": str(params.get("step")),
                          "bound": (approval.revision if approval else "")})
    updated.refresh_from_db()
    return {
        "operation_id": str(updated.operation_id),
        "status": updated.status,
        "cursor": updated.cursor,
        "revision": updated.revision,
        "last_error": updated.last_error,
        "next": ("Read get_webapp_setup_status for progress. Queued work is "
                 "not finished work — report what the evidence says."),
    }


# ---------------------------------------------------------------------------
# cancel_webapp_setup
# ---------------------------------------------------------------------------

def _summarize_cancel(params, user):
    return (f"Cancel setup {params.get('operation_id')}. Anything already "
            f"created (the app, its domain, its certificate) is kept.")


def _preview_cancel(params, user):
    operation = _assistant_operation(user, params)
    reason = common.reason_text(params)
    return {
        "summary": (f"Cancels setup {operation.operation_id} at the "
                    f"'{operation.cursor}' step. Completed resources are kept."),
        "details": {"status": operation.status, "cursor": operation.cursor,
                    "webapp": operation.web_app_id, "reason": reason},
        "revision": common.revision(
            ("op", operation.operation_id), ("status", operation.status)),
    }


@tool(
    name="cancel_webapp_setup",
    domain=DOMAIN,
    permission=PERMISSION,
    authorize=common.authorized,
    mutates=True,
    fresh_auth_seconds=600,
    summarize=_summarize_cancel,
    preview=_preview_cancel,
    description=(
        "Stop a setup that is in progress. Whatever it already created — the "
        "app, its domain, its certificate — is kept, so this is not a delete. "
        "Requires operator approval: calling this tool creates an approval "
        "card and does not execute."),
    input_schema={
        "type": "object",
        "properties": {
            "operation_id": {"type": "string", "description": "The setup's operation_id"},
            "reason": {"type": "string", "description": "Why it is being stopped (3-300 characters)"},
        },
        "required": ["operation_id", "reason"],
    },
)
def _tool_cancel_webapp_setup(params, user, approval=None):
    from mojo.apps.edge.services import webapp_onboarding

    operation = _assistant_operation(user, params)
    reason = common.reason_text(params)
    try:
        cancelled = webapp_onboarding.cancel_for_actor(
            operation, user, webapp_onboarding.ASSISTANT_ORIGIN)
    except me.MojoException as err:
        return common.service_error(err, code="setup_cancel_refused")

    common.audit(user, "cancel_webapp_setup", operation.web_app,
                 payload={"operation": str(operation.operation_id),
                          "reason": reason,
                          "bound": (approval.revision if approval else "")})
    return {
        "operation_id": str(cancelled.operation_id),
        "status": cancelled.status,
        "cursor": cancelled.cursor,
        "kept": "The app and any domain or certificate already created are kept.",
    }
