"""The approval boundary for mutating assistant tools.

``mutates=True`` means "cannot run until this exact operator approves this exact
argument set, over an authenticated transport, within a bounded window". This
module owns that protocol end to end: normalization, the durable
:class:`~mojo.apps.assistant.models.pending_action.PendingAction` record, the
operator-facing card, and the single resolution path that consumes a record and
dispatches the handler.

What makes it a boundary rather than a formality:

* The model proposes; it never approves. A proposal returns a tool_result that
  says nothing happened. Only :func:`resolve` calls a handler.
* Resolution reads the STORED arguments. Nothing the client or the model sends
  at approve time is consulted beyond the opaque id and the decision, so an
  altered argument cannot ride an approval that was granted for something else.
* The registry snapshot on the row is for audit and rendering only. Every gate
  is re-read from the live registry and re-checked against a freshly reloaded
  ``User`` immediately before dispatch — a permission removed, a user
  deactivated, a tool unregistered or de-mutated between proposal and approval
  all refuse.
* Consumption is one conditional UPDATE. A double-click returns the first
  outcome; it never runs the handler twice. ``select_for_update`` would hold a
  database lock across a handler that may call a cloud provider, so the
  conditional update is the right primitive here even though row locking is the
  repo's usual pattern.
* Every unresolvable case returns ONE non-oracular failure, so a stolen id
  learns nothing. Distinguishable outcomes exist only for the bound owner of a
  live record.

Transport neutrality is deliberate: :func:`resolve` raises
:class:`ApprovalRefused` with one of five codes and never builds an HTTP
response, because the WebSocket dispatcher shares this code and
``infrastructure.refuse()`` returns a ``JsonResponse``.
"""
import hashlib
import json
import re
import uuid as uuid_module

from mojo import errors as merrors
from mojo.helpers import dates, infrastructure, logit
from mojo.helpers.settings import settings

logger = logit.get_logger("assistant", "assistant.log")


# --- Tunables -------------------------------------------------------------

DEFAULT_TTL_SECONDS = 600
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 3600

# One conversation may hold this many live proposals at once; a 21st supersedes
# the oldest. Bounds growth inside a single 25-turn agent loop; lazy expiry
# handles everything slower than that.
MAX_LIVE_PER_CONVERSATION = 20

MAX_ARGS_BYTES = 16 * 1024
MAX_RESULT_BYTES = 8 * 1024
MAX_PREVIEW_BYTES = 8 * 1024
MAX_SUMMARY_CHARS = 500
MAX_ERROR_CHARS = 300

# --- The one non-oracular failure ----------------------------------------

CODE_UNAVAILABLE = "action_unavailable"
CODE_REAUTH = "reauth_required"
CODE_PERMISSION = "permission_denied"
CODE_INFRASTRUCTURE = "infrastructure_external"
CODE_PRECONDITION = "precondition_failed"

REFUSAL_CODES = (
    CODE_UNAVAILABLE, CODE_REAUTH, CODE_PERMISSION,
    CODE_INFRASTRUCTURE, CODE_PRECONDITION,
)

GENERIC_UNAVAILABLE = "This action is no longer available."
GENERIC_PERMISSION = "You are no longer allowed to run this action."
GENERIC_PRECONDITION = (
    "The system changed since this action was proposed. Reload and try again.")
HANDLER_ERROR_MESSAGE = "The operation failed. Check the incident log for details."
UNKNOWN_OUTCOME_MESSAGE = (
    "This operation started but its outcome is unknown. Reconcile the target "
    "system before retrying — it was NOT retried automatically.")

REFUSAL_MESSAGES = {
    CODE_UNAVAILABLE: GENERIC_UNAVAILABLE,
    CODE_REAUTH: "reauth_required",
    CODE_PERMISSION: GENERIC_PERMISSION,
    CODE_INFRASTRUCTURE: "",  # filled from infrastructure.refusal_message()
    CODE_PRECONDITION: GENERIC_PRECONDITION,
}

# Redaction: logit owns the key list; these substrings mirror the assistant's
# own list in services/tools/models.py so a card can never carry a credential.
EXTRA_SENSITIVE_SUBSTRINGS = ("password", "secret", "token", "auth_key", "onetime_code")
REDACTED = "*****"

_FAILURE_CODE_RE = re.compile(r"^[a-z_]{1,32}$")

_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


class ApprovalRefused(Exception):
    """A resolution that cannot proceed. ``code`` is one of REFUSAL_CODES.

    Transport-neutral by construction: each transport maps the code to its own
    status/event exactly once.
    """

    def __init__(self, code, message=None):
        self.code = code if code in REFUSAL_CODES else CODE_UNAVAILABLE
        self.message = message or REFUSAL_MESSAGES.get(self.code) or GENERIC_UNAVAILABLE
        super().__init__(self.message)


class ArgumentError(Exception):
    """Normalization rejected the model's arguments. Never reaches the operator."""


class PreviewFailed(Exception):
    """A tool's ``preview`` refused. Carries a message safe to show the model."""


# --- Settings -------------------------------------------------------------

def ttl_seconds():
    """The approval window, clamped. ``LLM_ADMIN_APPROVAL_TTL``, default 600s."""
    try:
        value = int(settings.get("LLM_ADMIN_APPROVAL_TTL", DEFAULT_TTL_SECONDS, kind="int"))
    except (TypeError, ValueError):
        value = DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, value))


# --- Normalization, fingerprinting, redaction -----------------------------

def canonical_json(value):
    """Stable JSON for hashing and size limits. Key order is fixed."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _type_matches(declared, value):
    if declared is None:
        return True
    names = declared if isinstance(declared, list) else [declared]
    for name in names:
        if name == "null":
            if value is None:
                return True
            continue
        expected = _JSON_TYPES.get(name)
        if expected is None:
            return True  # a type we do not model — nothing to enforce
        if name in ("integer", "number") and isinstance(value, bool):
            continue  # a bool is not a number, whatever Python thinks
        if isinstance(value, expected):
            return True
    return False


def normalize_args(input_schema, raw):
    """Return the validated argument set that will be STORED and later executed.

    Hand-rolled on purpose: the repo carries no JSON-Schema dependency and this
    does not justify adding one. Unknown keys are DROPPED rather than rejected,
    because a handler reading ``params.get("...")`` would otherwise honour a
    field the operator never saw on the card. Types are checked, never coerced.

    Raises :class:`ArgumentError` with a message the model can act on. No record
    is created for a rejected argument set.
    """
    if not isinstance(input_schema, dict):
        input_schema = {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ArgumentError("Tool arguments must be a JSON object.")

    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    required = input_schema.get("required") or []

    normalized = {}
    for key, value in raw.items():
        if key not in properties:
            continue
        spec = properties[key] if isinstance(properties[key], dict) else {}
        declared = spec.get("type")
        if not _type_matches(declared, value):
            raise ArgumentError(
                f"Argument '{key}' must be of type {declared!r}.")
        enum = spec.get("enum")
        if isinstance(enum, list) and enum and value not in enum:
            raise ArgumentError(f"Argument '{key}' must be one of {enum}.")
        normalized[key] = value

    for key in required:
        if key not in normalized:
            raise ArgumentError(f"Argument '{key}' is required.")

    if len(canonical_json(normalized).encode("utf-8")) > MAX_ARGS_BYTES:
        raise ArgumentError(
            "Tool arguments are too large to approve. Split the request into "
            "smaller operations.")
    return normalized


def fingerprint(tool_name, args):
    """A stable, secret-free identifier for "the same operation".

    Evidence, not a key: resolution finds the row by id plus the bound user and
    conversation, then recomputes this from the STORED arguments to catch a
    tampered row and to give audit one identifier it can correlate on.
    """
    return hashlib.sha256(
        canonical_json({"tool": tool_name, "args": args or {}}).encode("utf-8")
    ).hexdigest()


def _mask(value):
    if isinstance(value, dict):
        masked = {}
        for key, item in value.items():
            if isinstance(key, str) and any(
                    s in key.lower() for s in EXTRA_SENSITIVE_SUBSTRINGS):
                masked[key] = REDACTED
            else:
                masked[key] = _mask(item)
        return masked
    if isinstance(value, list):
        return [_mask(item) for item in value]
    if isinstance(value, str):
        return logit.mask_sensitive_data(value)
    return value


def redact(value):
    """Strip secrets from anything that will reach a card, a log, or the model."""
    return _mask(logit.sanitize_dict(value))


def _bounded(value, max_bytes):
    """A JSON-safe copy of ``value``, capped. Never raises."""
    try:
        encoded = canonical_json(value)
    except Exception:
        return {"note": "Result could not be serialized."}
    if len(encoded.encode("utf-8")) <= max_bytes:
        try:
            return json.loads(encoded)
        except Exception:
            return {"note": "Result could not be serialized."}
    return {"truncated": True, "preview": encoded[:max_bytes]}


def _bounded_text(value):
    if not isinstance(value, str):
        value = str(value)
    return logit.mask_sensitive_data(value)[:MAX_ERROR_CHARS]


def _failure_code(raw):
    """A handler's documented refusal code, or ``handler_error``.

    Bounded to 32 lowercase/underscore characters so a wrapped service's
    contract (``capacity_revision_stale``, …) survives into the record without
    letting a handler write arbitrary text into an indexed column.
    """
    if isinstance(raw, str) and _FAILURE_CODE_RE.match(raw):
        return raw
    return "handler_error"


# --- Card rendering -------------------------------------------------------

def safe_summary(entry, params, user):
    """The one operator-facing sentence on the card."""
    name = entry["definition"]["name"]
    summarize = entry.get("summarize")
    text = ""
    if summarize is not None:
        try:
            text = summarize(params, user)
        except Exception:
            logger.exception("summarize() failed for assistant tool %s", name)
            text = ""
    if not isinstance(text, str) or not text.strip():
        text = f"{name} will run with the arguments below."
    return logit.mask_sensitive_data(text.strip())[:MAX_SUMMARY_CHARS]


def run_preview(entry, params, user):
    """Call a tool's read-only ``preview``. ``None`` when it declares none.

    A raising preview is a REFUSAL, not a crash — it is the supported place for
    a per-object or per-group authority check to fail closed. The message a
    ``ValueError``/``PermissionError`` carries reaches the model, so tool
    authors keep it non-oracular; anything else is reported generically.
    """
    preview = entry.get("preview")
    if preview is None:
        return None
    try:
        result = preview(params, user)
    except (ValueError, PermissionError) as exc:
        raise PreviewFailed(_bounded_text(str(exc) or GENERIC_PRECONDITION))
    except Exception:
        logger.exception("preview() raised for assistant tool %s",
                         entry["definition"]["name"])
        raise PreviewFailed(GENERIC_PRECONDITION)

    if not isinstance(result, dict):
        return None
    summary = result.get("summary")
    details = result.get("details")
    revision = result.get("revision")
    return {
        "summary": (logit.mask_sensitive_data(str(summary))[:MAX_SUMMARY_CHARS]
                    if summary else ""),
        "details": (_bounded(redact(details), MAX_PREVIEW_BYTES)
                    if details is not None else None),
        "revision": str(revision)[:128] if revision is not None else "",
    }


def render_block(row, now=None):
    """The server-authored ``approval`` block.

    ``approval`` is deliberately absent from ``agent.VALID_BLOCK_TYPES``, so a
    model-emitted ``assistant_block`` claiming this type is dropped by
    ``_validate_block``. This function is the only way one exists.
    """
    from mojo.apps.assistant.models import PendingAction

    state = row.effective_state(now=now)
    terminal = state in PendingAction.TERMINAL_STATES
    failure_code = row.failure_code or ""
    result = row.result if terminal else None

    if state == PendingAction.STATE_FAILED and row.state == PendingAction.STATE_EXECUTING:
        # Lazily aged out of `executing` — the sweep has not written it yet.
        failure_code = "unknown_outcome"
        result = {"error": UNKNOWN_OUTCOME_MESSAGE}

    return {
        "type": "approval",
        "action_id": str(row.uuid),
        "conversation_id": row.conversation_id,
        "tool": row.tool_name,
        "title": row.tool_name.replace("_", " ").title(),
        "description": row.summary or "",
        "args": redact(row.args or {}),
        "preview": row.preview,
        "requires_fresh_auth": bool(row.fresh_auth_seconds),
        "requires_superuser": bool(row.requires_superuser),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "state": state,
        "result": result,
        "failure_code": failure_code if terminal else "",
    }


def states_for_conversation(conversation, limit=50):
    """Current state of a conversation's most recent approvals, oldest first.

    One query. Lets a re-loaded history render resolved and expired cards inert
    instead of offering buttons that cannot work.
    """
    from mojo.apps.assistant.models import PendingAction

    if conversation is None:
        return []
    rows = list(PendingAction.objects.filter(
        conversation=conversation).order_by("-created")[:limit])
    now = dates.utcnow()
    return [render_block(row, now=now) for row in reversed(rows)]


def states_for_user(user, limit=50):
    """The caller's own approval cards, newest first. Owner-scoped by the query."""
    from mojo.apps.assistant.models import PendingAction

    if user is None:
        return []
    rows = list(PendingAction.objects.filter(user=user).order_by("-created")[:limit])
    now = dates.utcnow()
    return [render_block(row, now=now) for row in rows]


def proposal_result(block):
    """What the MODEL sees when it calls a mutating tool. Nothing has happened."""
    return {
        "status": "approval_required",
        "action_id": block["action_id"],
        "tool": block["tool"],
        "summary": block["description"],
        "expires_at": block["expires_at"],
        "message": (
            "NOT EXECUTED. This operation requires operator approval. An approval "
            "card has been shown to the operator; only they can approve it. Do not "
            "call this tool again for the same request and do not say the work is "
            "done — tell the operator the approval is waiting, then stop."
        ),
    }


# --- Proposal -------------------------------------------------------------

def propose(user, conversation, tool_name, entry, raw_args,
            request_meta=None, on_event=None, _reporter=None):
    """Create (or return) the pending approval for one mutating tool call.

    Returns ``(tool_result, block)``. ``block`` is ``None`` when the proposal
    itself was refused — a rejected argument set, a refusing ``preview``, or an
    installation that does not own this infrastructure — in which case the model
    gets an ordinary tool error and no record exists.
    """
    from mojo.apps.assistant.models import PendingAction

    if entry.get("requires_managed_infrastructure") and infrastructure.is_external():
        return {"error": infrastructure.refusal_message(f"'{tool_name}'")}, None

    try:
        args = normalize_args(entry["definition"].get("input_schema"), raw_args)
    except ArgumentError as exc:
        return {"error": str(exc)}, None

    try:
        preview = run_preview(entry, args, user)
    except PreviewFailed as exc:
        return {"error": str(exc)}, None

    args_fingerprint = fingerprint(tool_name, args)
    now = dates.utcnow()

    # The model calling the same tool twice with the same arguments in one turn
    # is one operation, not two. Different arguments coexist: "block these five
    # IPs" is five cards.
    existing = PendingAction.objects.filter(
        conversation=conversation, tool_name=tool_name,
        args_fingerprint=args_fingerprint,
        state=PendingAction.STATE_PENDING, expires_at__gt=now,
    ).order_by("-pk").first()
    if existing is not None:
        block = render_block(existing, now=now)
        return proposal_result(block), block

    row = PendingAction.objects.create(
        user=user,
        conversation=conversation,
        group=getattr(conversation, "group", None),
        tool_name=tool_name,
        permission=entry.get("permission") or "",
        args=args,
        args_fingerprint=args_fingerprint,
        summary=safe_summary(entry, args, user),
        preview=preview,
        fresh_auth_seconds=entry.get("fresh_auth_seconds"),
        requires_superuser=bool(entry.get("requires_superuser")),
        requires_managed_infrastructure=bool(entry.get("requires_managed_infrastructure")),
        revision=(preview or {}).get("revision") or "",
        expires_at=dates.add(now, seconds=ttl_seconds()),
    )

    # Insert first, then supersede monotonically on pk. Two concurrent
    # proposals from the tool thread pool cannot annihilate each other this way
    # — whichever inserted last is the survivor, and the other is superseded.
    PendingAction.objects.filter(
        conversation=conversation, tool_name=tool_name,
        args_fingerprint=args_fingerprint,
        state=PendingAction.STATE_PENDING, pk__lt=row.pk,
    ).update(state=PendingAction.STATE_SUPERSEDED, resolved_at=now, modified=now)

    _enforce_conversation_cap(conversation, now)

    block = render_block(row, now=now)
    _audit(row, user, "proposed", 4, decision="propose", request_meta=request_meta,
           _reporter=_reporter)
    if on_event:
        try:
            on_event("approval_required", {
                "action_id": block["action_id"],
                "block": block,
            })
        except Exception:
            logger.exception("Failed to publish approval_required for %s", row.uuid)
    return proposal_result(block), block


def _enforce_conversation_cap(conversation, now):
    from mojo.apps.assistant.models import PendingAction

    live = list(PendingAction.objects.filter(
        conversation=conversation, state=PendingAction.STATE_PENDING,
        expires_at__gt=now,
    ).order_by("-pk").values_list("pk", flat=True))
    if len(live) <= MAX_LIVE_PER_CONVERSATION:
        return
    PendingAction.objects.filter(
        pk__in=live[MAX_LIVE_PER_CONVERSATION:],
        state=PendingAction.STATE_PENDING,
    ).update(state=PendingAction.STATE_SUPERSEDED, resolved_at=now, modified=now)


# --- Resolution -----------------------------------------------------------

def resolve(user, action_id, decision, request=None, conversation_id=None,
            request_meta=None, _reporter=None):
    """Approve or cancel one pending action. The ONLY path to a mutating handler.

    ``user`` is the authenticated caller; the record must be bound to them. The
    live ``User`` row is re-read before dispatch, so the object a socket
    authenticated with hours ago never authorizes anything.

    Returns ``{"block": <resolved block>, "message_id": <int|None>}``.
    Raises :class:`ApprovalRefused`.
    """
    from mojo.apps.assistant import get_registry, user_can_use_tool
    from mojo.apps.assistant.models import PendingAction

    if decision not in ("approve", "cancel"):
        _deny(user, None, CODE_UNAVAILABLE, _reporter=_reporter)
        raise ApprovalRefused(CODE_UNAVAILABLE)

    row = _load(user, action_id, conversation_id, _reporter=_reporter)
    now = dates.utcnow()

    if row.effective_state(now=now) != PendingAction.STATE_PENDING:
        _deny(user, row, CODE_UNAVAILABLE, _reporter=_reporter)
        raise ApprovalRefused(CODE_UNAVAILABLE)

    # A tampered row cannot be executed: the fingerprint is recomputed from the
    # stored arguments, not trusted from the column.
    if fingerprint(row.tool_name, row.args or {}) != row.args_fingerprint:
        logger.error("PendingAction %s failed its fingerprint check", row.uuid)
        _deny(user, row, CODE_UNAVAILABLE, _reporter=_reporter)
        raise ApprovalRefused(CODE_UNAVAILABLE)

    # The snapshot never outranks the registry. An unregistered tool, or one
    # whose mutates flag was removed, resolves to the generic failure.
    entry = get_registry().get(row.tool_name)
    if entry is None or not entry.get("mutates"):
        _deny(user, row, CODE_UNAVAILABLE, _reporter=_reporter)
        raise ApprovalRefused(CODE_UNAVAILABLE)

    if decision == "cancel":
        return _cancel(row, user, now, request_meta=request_meta, _reporter=_reporter)

    # Infrastructure mode answers before the caller's grants — the refusal is
    # about the installation, not about who is asking.
    if ((entry.get("requires_managed_infrastructure") or row.requires_managed_infrastructure)
            and infrastructure.is_external()):
        _deny(user, row, CODE_INFRASTRUCTURE, _reporter=_reporter)
        raise ApprovalRefused(
            CODE_INFRASTRUCTURE, infrastructure.refusal_message(f"'{row.tool_name}'"))

    live_user = _reload_user(row.user_id)
    if live_user is None:
        _fail_permission(row, user, "user_inactive", _reporter=_reporter)
    if not user_can_use_tool(live_user, entry):
        _fail_permission(row, live_user, "permission_lost", _reporter=_reporter)
    if entry.get("requires_superuser") and not getattr(live_user, "is_superuser", False):
        _fail_permission(row, live_user, "superuser_required", _reporter=_reporter)
    if row.group is not None and not row.group.is_effectively_active():
        _fail_permission(row, live_user, "group_inactive", _reporter=_reporter)

    _require_fresh_auth(entry, row, live_user, request, _reporter=_reporter)
    _require_bound_revision(entry, row, live_user, _reporter=_reporter)

    # Single-use consumption. The expiry predicate rides along so the atomic
    # claim and the lazy effective_state() can never disagree, and so the sweep
    # can never race this.
    claimed = PendingAction.objects.filter(
        pk=row.pk, state=PendingAction.STATE_PENDING, expires_at__gt=now,
    ).update(state=PendingAction.STATE_EXECUTING, modified=now)
    if not claimed:
        # Someone else already claimed it — a second tab, a double-click, a
        # retried request. Return THEIR outcome; never run the handler twice.
        row.refresh_from_db()
        return {"block": render_block(row), "message_id": None}

    row.refresh_from_db()
    _audit(row, live_user, "approved", 5, decision="approve",
           request=request, request_meta=request_meta, _reporter=_reporter)

    state, failure_code, result = _dispatch(entry, row, live_user, request_meta)

    resolved_at = dates.utcnow()
    PendingAction.objects.filter(pk=row.pk).update(
        state=state, failure_code=failure_code, result=result,
        resolved_at=resolved_at, modified=resolved_at)
    row.refresh_from_db()

    block = render_block(row, now=resolved_at)
    message = _write_outcome_message(row, block)

    if state == PendingAction.STATE_COMPLETED:
        # Category unchanged from the pre-approval world, so existing RuleSets
        # keep firing on `assistant:tool:<name>`.
        _report(f"assistant:tool:{row.tool_name}", 5,
                f"Assistant tool: {row.tool_name}",
                f"User {live_user.email} (id={live_user.pk}) executed approved "
                f"mutating tool '{row.tool_name}'. conv={row.conversation_id} "
                f"action={row.uuid}",
                user=live_user, model_name="account.User", model_id=live_user.pk,
                _reporter=_reporter)
    else:
        _audit(row, live_user, "failed", 6, decision="approve",
               request=request, request_meta=request_meta, _reporter=_reporter)

    return {"block": block, "message_id": getattr(message, "pk", None)}


def _load(user, action_id, conversation_id, _reporter=None):
    from mojo.apps.assistant.models import PendingAction

    try:
        parsed = uuid_module.UUID(str(action_id))
    except (ValueError, AttributeError, TypeError):
        _deny(user, None, CODE_UNAVAILABLE, _reporter=_reporter)
        raise ApprovalRefused(CODE_UNAVAILABLE)

    row = PendingAction.objects.select_related(
        "conversation", "group").filter(uuid=parsed, user=user).first()
    if row is None:
        _deny(user, None, CODE_UNAVAILABLE, _reporter=_reporter)
        raise ApprovalRefused(CODE_UNAVAILABLE)
    if conversation_id is not None and str(row.conversation_id) != str(conversation_id):
        _deny(user, row, CODE_UNAVAILABLE, _reporter=_reporter)
        raise ApprovalRefused(CODE_UNAVAILABLE)
    return row


def _cancel(row, user, now, request_meta=None, _reporter=None):
    from mojo.apps.assistant.models import PendingAction

    claimed = PendingAction.objects.filter(
        pk=row.pk, state=PendingAction.STATE_PENDING, expires_at__gt=now,
    ).update(state=PendingAction.STATE_CANCELED, resolved_at=now, modified=now)
    row.refresh_from_db()
    block = render_block(row, now=now)
    if not claimed:
        return {"block": block, "message_id": None}
    _audit(row, user, "canceled", 4, decision="cancel",
           request_meta=request_meta, _reporter=_reporter)
    message = _write_outcome_message(row, block)
    return {"block": block, "message_id": getattr(message, "pk", None)}


def _reload_user(user_id):
    """The live, active User row — never the object the transport carried in."""
    from mojo.apps.account.models import User

    return User.objects.filter(pk=user_id, is_active=True).first()


def _require_fresh_auth(entry, row, live_user, request, _reporter=None):
    """Prove recent authentication when the Admin twin demands it.

    Never delegated to ``fresh_auth.is_fresh`` in the missing-evidence case:
    ``is_fresh(None, …)`` and ``is_fresh(<non-bearer request>, …)`` both return
    True by design (``fresh_auth.py:81-94``), because machine credentials have
    no interactive login to be recent. Here that would be a bypass, so an
    approval with no bearer request is refused BEFORE ``is_fresh`` is consulted.
    The WebSocket authenticates once at connect and holds no per-message token,
    which is why it lands here and the client re-submits over REST.
    """
    from mojo.apps.account.services import fresh_auth

    window = entry.get("fresh_auth_seconds")
    if not window:
        return
    if request is None or getattr(request, "bearer", None) != "bearer":
        _deny(live_user, row, CODE_REAUTH, _reporter=_reporter)
        raise ApprovalRefused(CODE_REAUTH)
    try:
        fresh_auth.require_fresh(request, seconds=window)
    except merrors.ReauthRequiredException:
        _deny(live_user, row, CODE_REAUTH, _reporter=_reporter)
        raise ApprovalRefused(CODE_REAUTH)


def _require_bound_revision(entry, row, live_user, _reporter=None):
    """Re-run a plan/apply tool's preview and refuse when the revision moved."""
    if entry.get("preview") is None:
        return
    try:
        fresh = run_preview(entry, row.args or {}, live_user)
    except PreviewFailed as exc:
        _deny(live_user, row, CODE_PRECONDITION, _reporter=_reporter)
        raise ApprovalRefused(CODE_PRECONDITION, str(exc))
    if row.revision and (fresh or {}).get("revision", "") != row.revision:
        _deny(live_user, row, CODE_PRECONDITION, _reporter=_reporter)
        raise ApprovalRefused(CODE_PRECONDITION)


def _fail_permission(row, user, failure_code, _reporter=None):
    """Mark the record failed and refuse. Always raises."""
    from mojo.apps.assistant.models import PendingAction

    now = dates.utcnow()
    PendingAction.objects.filter(
        pk=row.pk, state=PendingAction.STATE_PENDING,
    ).update(state=PendingAction.STATE_FAILED, failure_code=failure_code,
             resolved_at=now, modified=now)
    _deny(user, row, CODE_PERMISSION, detail=failure_code, _reporter=_reporter)
    raise ApprovalRefused(CODE_PERMISSION)


def _dispatch(entry, row, live_user, request_meta):
    """Call the handler with the STORED arguments. Returns (state, code, result)."""
    from mojo.apps.assistant.models import PendingAction
    # Function-level: agent.py imports this module at module scope, so the
    # reverse import must not happen at import time.
    from mojo.apps.assistant.services.agent import _call_handler

    try:
        raw = _call_handler(entry["handler"], row.args or {}, live_user,
                            request_meta, row.conversation, approval=row)
    except Exception:
        logger.exception("Approved tool %s failed (action=%s)", row.tool_name, row.uuid)
        return PendingAction.STATE_FAILED, "handler_error", {"error": HANDLER_ERROR_MESSAGE}

    if isinstance(raw, dict) and "error" in raw:
        # A wrapped service's documented refusal survives into the record
        # instead of being flattened into "something went wrong".
        return (PendingAction.STATE_FAILED,
                _failure_code(raw.get("error_code")),
                {"error": _bounded_text(raw.get("error"))})

    return PendingAction.STATE_COMPLETED, "", _bounded(redact(raw), MAX_RESULT_BYTES)


def _write_outcome_message(row, block):
    """Persist the SERVER-authored outcome into the conversation.

    No LLM call happens on the approval path. The model reads this from history
    on its next turn, which keeps the conversation coherent without letting a
    model paraphrase an execution report it has an incentive to soften — and
    keeps the security path free of API-key, rate-limit and token-cost failures.
    """
    from mojo.apps.assistant.models import Message, PendingAction

    state = block.get("state")
    if state == PendingAction.STATE_COMPLETED:
        text = f"Approved and completed: {row.summary}"
    elif state == PendingAction.STATE_CANCELED:
        text = f"Canceled: {row.summary} Nothing was changed."
    else:
        detail = (block.get("result") or {}).get("error") or ""
        text = f"Approved but FAILED: {row.summary} {detail}".strip()

    try:
        return Message.objects.create(
            conversation=row.conversation, role="assistant",
            content=text, blocks=[block])
    except Exception:
        logger.exception("Failed to write approval outcome message for %s", row.uuid)
        return None


# --- Audit ----------------------------------------------------------------

def _report(category, level, title, details, user=None, _reporter=None, **kwargs):
    try:
        if _reporter is None:
            from mojo.apps.incident import report_event as _reporter
        extra = {}
        if user is not None:
            extra["uid"] = getattr(user, "pk", None)
        extra.update(kwargs)
        _reporter(details, title=title, category=category, level=level, **extra)
    except Exception:
        logger.exception("Failed to report approval event %s", category)


def _synthetic_request(user, request_meta=None):
    """A request object carrying the actor, for Log.logit.

    ``Log.logit(None, …)`` writes ``uid=0`` — the actor would be lost on exactly
    the rows where it matters most, so the proposal path reuses the same
    synthetic request the model tools build.
    """
    from mojo.apps.assistant.services.tools.models import _build_request

    return _build_request(user, method="POST", path="/assistant/approval",
                          request_meta=request_meta)


def _audit(row, user, state, level, decision="", request=None, request_meta=None,
           _reporter=None):
    """One incident event plus one logit.Log row per lifecycle point.

    Argument NAMES only — never a value, a token, or a credential.
    """
    import ujson

    arg_names = sorted((row.args or {}).keys())
    email = getattr(user, "email", None) or "unknown"
    user_pk = getattr(user, "pk", None)
    details = (
        f"Assistant approval {state}: tool='{row.tool_name}' "
        f"action={row.uuid} conv={row.conversation_id} user={email} (id={user_pk}) "
        f"args=[{','.join(arg_names)}] fingerprint={row.args_fingerprint[:16]}"
    )
    _report(f"assistant:approval:{state}", level,
            f"Assistant approval {state}: {row.tool_name}", details,
            user=user, model_name="assistant.PendingAction", model_id=row.pk,
            _reporter=_reporter)

    try:
        from mojo.apps.logit.models import Log

        req = request if request is not None else _synthetic_request(user, request_meta)
        Log.logit(
            req,
            f"approval:{state} {row.tool_name} action={row.uuid} "
            f"args=[{','.join(arg_names)}]",
            kind=f"assistant:approval:{state}",
            model_name="assistant.PendingAction",
            model_id=row.pk,
            payload=ujson.dumps({
                "conversation_id": row.conversation_id,
                "tool": row.tool_name,
                "args_fingerprint": row.args_fingerprint,
                "decision": decision or state,
            }),
        )
    except Exception:
        logger.exception("Failed to write approval audit log for %s", row.uuid)


def _deny(user, row, code, detail=None, _reporter=None):
    """File a refused resolution — suppressed, because an id-guessing loop is free.

    Keyed on ``<user_id>:<failure_code>`` with a budget: the unknown-id case has
    no bound tool to key on, and a caller minting distinct ids must not turn
    per-key suppression into an unbounded event flood.
    """
    code = detail or code
    user_pk = getattr(user, "pk", None) or 0
    tool = getattr(row, "tool_name", None) or "unknown"
    action = getattr(row, "uuid", None) or "unknown"
    try:
        if _reporter is not None:
            _reporter(
                f"Assistant approval denied ({code}) for user id={user_pk} "
                f"tool='{tool}' action={action}",
                title=f"Assistant approval denied: {code}",
                category="assistant:approval:denied", level=6, uid=user_pk)
            return
        from mojo.apps.incident.reporter import report_event_suppressed

        report_event_suppressed(
            f"Assistant approval denied ({code}) for user id={user_pk} "
            f"tool='{tool}' action={action}",
            key=f"{user_pk}:{code}",
            title=f"Assistant approval denied: {code}",
            category="assistant:approval:denied",
            level=6, window=3600, budget=50, uid=user_pk, group=None,
        )
    except Exception:
        logger.exception("Failed to report approval denial")


# --- Sweep ----------------------------------------------------------------

def sweep(retention_days=30):
    """Persist lapsed states and delete old terminal rows. Idempotent.

    The lazy ``effective_state()`` already makes the correct answer available
    without this; the sweep only stops the table growing and makes the states
    queryable.
    """
    from mojo.apps.assistant.models import PendingAction

    now = dates.utcnow()
    stats = {"expired": 0, "unknown_outcome": 0, "deleted": 0}

    stats["expired"] = PendingAction.objects.filter(
        state=PendingAction.STATE_PENDING, expires_at__lte=now,
    ).update(state=PendingAction.STATE_EXPIRED, resolved_at=now, modified=now)

    stale = dates.subtract(now, seconds=PendingAction.EXECUTING_TIMEOUT_SECONDS)
    stats["unknown_outcome"] = PendingAction.objects.filter(
        state=PendingAction.STATE_EXECUTING, modified__lt=stale,
    ).update(state=PendingAction.STATE_FAILED, failure_code="unknown_outcome",
             result={"error": UNKNOWN_OUTCOME_MESSAGE},
             resolved_at=now, modified=now)

    cutoff = dates.subtract(now, days=retention_days)
    deleted, _detail = PendingAction.objects.filter(
        state__in=PendingAction.TERMINAL_STATES, modified__lt=cutoff,
    ).delete()
    stats["deleted"] = deleted
    return stats
