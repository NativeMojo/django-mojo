"""Shared helpers for the cloud domain tools.

Four jobs, all of them about the boundary between a service that answers an
Admin page and a model that must never see more than it needs:

* :func:`actor_request` — the shim that carries an ACTOR into the four
  ``admin_platform`` read overviews, which are the only services in scope that
  still take a request. Reads only; no mutation runs behind a fabricated
  request (``apply_framework_update`` was refactored instead).
* :func:`bounded` — one bounding function with one budget, applied to every
  projection. A per-source key allowlist across fourteen dashboard collectors
  would be a second schema to maintain; one deny rule plus one budget is
  auditable in a single test.
* :func:`provider_reason` — the four wire-safe reason codes an AWS failure
  collapses to, mirroring ``mojo/apps/aws/rest/cloudwatch.py``.
* :func:`audit` — the Admin's own audit vocabulary plus one ``logit.Log`` row
  tying the operation to the conversation.

Plus the two refusal helpers: :func:`refuse` builds the
``{"error", "error_code"}`` return #2569 lands as ``state="failed"`` with that
exact ``failure_code``, and :func:`interactive_refusal` is the session check the
two System Setup reads need, because their REST twins deny key-backed sessions
and demand an interactive bearer.
"""

import objict

from mojo.helpers import logit


logger = logit.get_logger("assistant", "assistant.log")


# --- Bounding budget ------------------------------------------------------
#
# The depth is per tool because the envelopes are not the same shape: an
# `_aws_inventory` row sits at depth 4-5 under sections -> envelope -> data ->
# resources -> ec2[], so a flat depth-2 cap would leave a caller holding only
# status strings. The NODE and BYTE budgets are what actually bound the result;
# depth only stops one deep branch from eating the whole budget.
DEFAULT_DEPTH = 4
MAX_STRING = 200
MAX_ITEMS = 40
MAX_NODES = 400
MAX_BYTES = 24 * 1024

# Dropped by NAME, whatever their depth. `stderr_tail` exists because the
# redactor has gaps a credential survives (platform_deploy.strip_stderr_tail);
# the other four are unbounded evidence journals whose bounded summary
# (`node_summary`) is what an operator actually reads.
DROP_KEYS = frozenset({
    "stderr_tail", "node_evidence", "transitions", "diagnosis", "frozen_roster",
})

# Mirrors the assistant's own list in services/tools/models.py. A key matching
# either this or logit.SENSITIVE_KEYS keeps its NAME and loses its value, so a
# reader can see that something was withheld.
SENSITIVE_SUBSTRINGS = ("password", "secret", "token", "auth_key", "onetime_code")
REDACTED = "*****"

TRUNCATED = "…"
# The depth marker is a SCALAR on purpose: a dict marker would itself add a
# level, so "bounded to depth N" would return N+1 levels.
TRUNCATED_DEPTH = "[truncated: depth]"


def _sensitive(key):
    lowered = str(key).lower()
    if lowered in logit.SENSITIVE_KEYS:
        return True
    return any(part in lowered for part in SENSITIVE_SUBSTRINGS)


def _scalar(value, budget):
    if isinstance(value, str):
        text = logit.mask_sensitive_data(value)
        if len(text) > MAX_STRING:
            # MAX_STRING is the CAP, marker included — a "bounded to 200" that
            # returns 201 characters is not bounded.
            text = text[:MAX_STRING - len(TRUNCATED)] + TRUNCATED
        budget["bytes"] += len(text)
        return text
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        budget["bytes"] += 8
        return value
    return _scalar(str(value), budget)


def _spent(budget):
    return budget["nodes"] >= MAX_NODES or budget["bytes"] >= MAX_BYTES


def _walk(value, depth, budget):
    budget["nodes"] += 1
    if isinstance(value, dict):
        if depth <= 0:
            return TRUNCATED_DEPTH
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_ITEMS or _spent(budget):
                out["truncated"] = True
                break
            name = str(key)[:80]
            if name in DROP_KEYS:
                continue
            if _sensitive(name):
                out[name] = REDACTED
                continue
            out[name] = _walk(item, depth - 1, budget)
        return out
    if isinstance(value, (list, tuple, set)):
        if depth <= 0:
            return TRUNCATED_DEPTH
        out = []
        for index, item in enumerate(value):
            if index >= MAX_ITEMS or _spent(budget):
                out.append({"truncated": True})
                break
            out.append(_walk(item, depth - 1, budget))
        return out
    return _scalar(value, budget)


def bounded(value, depth=DEFAULT_DEPTH):
    """A model-safe projection of one service envelope.

    Scalar leaves only, strings capped at 200 characters and run through the
    string masker, at most 40 keys or items per container, a global budget of
    400 nodes / 24 KB, every ``DROP_KEYS`` name removed and every sensitive key
    name kept with its value replaced. Never raises: a projection that cannot
    be walked is worth less than a turn that crashes.
    """
    try:
        return _walk(value, depth, {"nodes": 0, "bytes": 0})
    except Exception:
        logger.exception("cloud tools: a result could not be bounded")
        return {"truncated": True, "note": "This result could not be projected safely."}


# --- The actor shim -------------------------------------------------------

def actor_request(user, **data):
    """A read-only request shim carrying the actor into a service overview.

    ``platform_overview`` / ``dashboard_overview`` / ``advanced_overview`` /
    ``framework_overview`` read exactly ``request.user`` (through ``_permitted``)
    and, in one place, ``request.DATA.get("sections")``. That filter honours only
    a STRING (``admin_platform.py``: ``isinstance(wanted, str)``), so a caller
    passing a JSON array would silently read as "no filter" and trigger the full
    ten-section fan-out — pass ``sections`` already comma-joined.
    """
    request = objict.objict()
    request.user = user
    request.DATA = objict.objict(data)
    request.QUERY_PARAMS = objict.objict(data)
    request.META = {}
    request.ip = "assistant"
    request.path = "/assistant/cloud"
    request.method = "GET"
    request.bearer = None
    request.group = None
    request.api_key = getattr(user, "api_key", None)
    return request


# --- Refusals -------------------------------------------------------------

def refuse(message, code):
    """The documented-refusal return.

    #2569 lands a handler returning ``{"error", "error_code"}`` as
    ``state="failed"`` carrying that exact ``failure_code``, so a wrapped
    service's contract survives instead of being flattened into "something went
    wrong". Raising is reserved for genuine bugs — ``_execute_tool`` turns a
    raise into "encountered an internal error" plus a level-6 event, which is
    the wrong report for "you are not a superuser".
    """
    return {"error": str(message), "error_code": str(code)}


def interactive_refusal(request_meta):
    """Refuse anything but an interactive bearer session, or return None.

    The System Setup reads' REST twins carry ``@md.denies_key_backed_session()``
    and ``require_request_admin``, which demands ``request.bearer == "bearer"``
    and a non-key-backed session. The chat path can only see that through
    ``request_meta``, so a turn that carries none — a programmatic call, an
    older transport — is refused rather than assumed interactive.
    """
    bearer = None
    key_backed = True
    if request_meta is not None:
        bearer = request_meta.get("bearer")
        key_backed = bool(request_meta.get("key_backed", True))
    if bearer != "bearer" or key_backed:
        return refuse(
            "System Setup can only be read from an interactive Admin session, "
            "not from an API key or a machine credential.",
            "interactive_session_required")
    return None


def superuser_refusal(user):
    """Refuse a caller who is not an active literal superuser, or return None.

    Belt to ``authorize=``'s braces: the hook keeps the tool out of a
    non-superuser's listing, and this re-checks at call time against the row.
    """
    if not is_system_admin(user):
        return refuse(
            "System Setup requires an active superuser account.",
            "permission_denied")
    return None


def can_system_admin(user):
    """The LISTING-time superuser predicate. A plain attribute read.

    Deliberately no per-request query, exactly as
    ``system_settings.can_system_admin`` documents for the same pair: this runs
    for every registry entry on every listing build, and the authoritative
    re-read stays where the authority is — ``requires_superuser`` at resolution
    and :func:`is_system_admin` inside the read handlers. Keep the two in
    lockstep; a capability that outruns the writer advertises a tool that 403s.
    """
    return bool(getattr(user, "is_superuser", False))


def is_system_admin(user):
    """True only for an ACTIVE literal superuser, re-read from the database.

    Wraps ``system_settings.require_system_admin`` — the same predicate the
    setup endpoints enforce, so a read handler refuses somebody the service
    would refuse even if their row changed since the listing was built.
    """
    from mojo.apps.account.services import system_settings

    try:
        return bool(system_settings.require_system_admin(user))
    except Exception:
        return False


def maintenance_tier(user):
    """The maintenance apply AND-half: superuser OR manage_platform OR admin.

    Mirrors ``_require_manage_tier`` in ``mojo/apps/aws/rest/maintenance.py``.
    ``manage_aws`` alone reads CloudWatch charts; it is not the grant that
    reboots the production database.
    """
    if getattr(user, "is_superuser", False):
        return True
    try:
        return bool(user.has_permission(["manage_platform", "admin"]))
    except Exception:
        return False


# --- Provider failures ----------------------------------------------------

PROVIDER_REASONS = (
    "credentials_unavailable", "denied", "network_unavailable", "service_error")


def provider_reason(exc, operation):
    """One of four wire-safe reason codes, or None when this is not provider-shaped.

    ``map_error(...).detail()`` is the only provider-exception shape safe to
    record — raw botocore text can carry credentials, signed URLs and
    parameters. Anything that is not a provider failure returns None and the
    caller re-raises, keeping the assistant's ordinary tool-error path.
    """
    from botocore.exceptions import BotoCoreError, ClientError
    from mojo.helpers.aws.provider_call import ProviderCallError, map_error

    if not isinstance(exc, (ProviderCallError, ClientError, BotoCoreError)):
        return None
    error = map_error(exc, operation)
    logger.error("cloud tool provider call degraded %s", error.detail())
    if error.provider_code in ("credentials_unavailable", "network_unavailable"):
        return error.provider_code
    if error.denied:
        return "denied"
    return "service_error"


# --- Audit ----------------------------------------------------------------

def audit(user, action, target, conversation=None, model_name=None):
    """The Admin's own audit action string, plus one conversation-linked Log row.

    ``audit_after_commit`` is called with the EXACT action string the mirrored
    REST handler uses, so the two trails cannot drift. #2569 already files
    ``assistant:approval:*`` and ``assistant:tool:<name>``; nothing here
    duplicates them — this row is the one that ties the operation record to the
    conversation it came from.
    """
    from mojo.apps.account.services import admin_platform

    try:
        admin_platform.audit_after_commit(user, action, target)
    except Exception:
        logger.exception("cloud tool audit event failed for %s", action)
    try:
        import ujson
        from mojo.apps.assistant.services.tools.models import _build_request
        from mojo.apps.logit.models import Log

        payload = {"action": action, "target": str(target)[:120]}
        conversation_id = getattr(conversation, "pk", None)
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        Log.logit(
            _build_request(user, method="POST", path="/assistant/cloud"),
            f"cloud:{action} target={str(target)[:120]}",
            kind=f"assistant:cloud:{action}",
            model_name=model_name or "assistant.CloudTool",
            model_id=0,
            payload=ujson.dumps(payload),
        )
    except Exception:
        logger.exception("cloud tool audit log failed for %s", action)
