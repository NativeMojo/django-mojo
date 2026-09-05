"""
Ticket Action Handler Registry — structured action dispatch for TicketNotes.

Actions live on notes (not tickets). A note's `metadata.action` block describes
what action is being proposed; a response note's `metadata.action_response`
triggers the handler to execute or reject it.

Handler naming: "app.handler_name" (e.g., "incident.rule_approval").
"""
from copy import deepcopy
import hashlib
import json
import math

from django.db import transaction

from mojo.helpers import logit

logger = logit.get_logger(__name__, "incident.log")

ACTION_HANDLERS = {}
ACTION_SCHEMA = "incident.ticket_approval"
ACTION_SCHEMA_VERSION = 1
MAX_ACTION_REVIEW_BYTES = 65536
MAX_ACTION_REVIEW_DEPTH = 8
MAX_ACTION_REVIEW_ITEMS = 64
MAX_ACTION_REVIEW_STRING = 4096


def register_handler(name, func):
    ACTION_HANDLERS[name] = func


def _bounded_review_value(value, depth=0):
    if depth > MAX_ACTION_REVIEW_DEPTH:
        raise ValueError("action review exceeds the nesting limit")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("action review numbers must be finite")
        return value
    if isinstance(value, str):
        if len(value) > MAX_ACTION_REVIEW_STRING:
            raise ValueError("action review string exceeds the length limit")
        return value
    if isinstance(value, list):
        if len(value) > MAX_ACTION_REVIEW_ITEMS:
            raise ValueError("action review list exceeds the item limit")
        return [_bounded_review_value(item, depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > MAX_ACTION_REVIEW_ITEMS:
            raise ValueError("action review object exceeds the item limit")
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 80:
                raise ValueError("action review keys must be bounded strings")
            result[key] = _bounded_review_value(item, depth + 1)
        return result
    raise ValueError("action review must contain JSON-safe values")


def _review_payload(context):
    """Build the exact structured fields rendered for human review."""
    proposal = _bounded_review_value(context)
    target = proposal.get("target")
    if target is None and context.get("ip"):
        target = {"ip": proposal["ip"]}
    confirmations = {
        key: value for key, value in (
            ("approve", proposal.get("confirm")),
            ("approve_catch_all", proposal.get("confirm_catch_all")),
            ("deny", proposal.get("deny_confirm")),
        ) if value is not None
    }
    review = {
        "proposal": proposal,
        "target": deepcopy(target),
        "revision": proposal.get("expected_modified"),
        "confirmation": confirmations,
    }
    encoded = json.dumps(
        review, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("utf-8")
    if len(encoded) > MAX_ACTION_REVIEW_BYTES:
        raise ValueError("action review exceeds the serialized size limit")
    return review


def _review_digest(review):
    encoded = json.dumps(
        review, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def bind_action_note(note, metadata):
    """Stamp a server-created proposal with its immutable note identity."""
    stored = deepcopy(metadata or {})
    action = stored.get("action")
    if not isinstance(action, dict) or not note.pk:
        raise ValueError("an action note must be saved before it is bound")
    context = action.get("context")
    if not isinstance(context, dict):
        raise ValueError("an action note requires structured context")
    if (action.get("handler") in (
            "incident.rule_approval", "incident.rule_update") and
            not isinstance(context.get("ruleset"), dict)):
        raise ValueError("a rule approval requires the complete ruleset proposal")
    review = _review_payload(context)
    action.update({
        "schema": ACTION_SCHEMA,
        "schema_version": ACTION_SCHEMA_VERSION,
        "proposal_note_id": note.pk,
        "proposal_digest": _review_digest(review),
        "review": review,
        "state": "pending",
        "resolved": False,
    })
    note.metadata = stored
    note.save(update_fields=["metadata"])
    return note


def _has_global_authority(note, handler_name):
    request = getattr(note, "active_request", None)
    actor = getattr(request, "user", None) if request is not None else None
    try:
        from mojo.apps.account.services import fresh_auth
        from mojo.helpers.request import is_key_backed_session
        if (request is None or actor is None or
                not getattr(actor, "is_authenticated", False) or
                is_key_backed_session(request) or
                not actor.has_permission(["manage_security", "security"])):
            logger.warning(
                "Ticket action %s lacked global authority",
                handler_name)
            return None
        # The deployment owns the interactive freshness policy. Positively
        # validated UserAPIKeys bypass it in fresh_auth because a machine
        # credential has no interactive login ceremony to repeat.
        fresh_auth.require_fresh(request)
    except Exception:
        logger.warning(
            "Ticket action %s failed fresh-auth authority", handler_name)
        return None
    return actor


def _dispatch_key(proposal_note_id, proposal_digest, requested_action):
    material = (
        f"{ACTION_SCHEMA}:{ACTION_SCHEMA_VERSION}:{proposal_note_id}:"
        f"{proposal_digest}:{requested_action}")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _stored_contract(action_note, handler_name, proposal_digest):
    metadata = deepcopy(action_note.metadata or {})
    stored_action = metadata.get("action")
    if not isinstance(stored_action, dict):
        raise ValueError("proposal action is missing")
    if (stored_action.get("schema") != ACTION_SCHEMA or
            stored_action.get("schema_version") != ACTION_SCHEMA_VERSION or
            stored_action.get("proposal_note_id") != action_note.pk or
            stored_action.get("handler") != handler_name):
        raise ValueError("proposal identity is stale or malformed")
    context = stored_action.get("context")
    review = _review_payload(context) if isinstance(context, dict) else None
    if (review is None or stored_action.get("review") != review or
            stored_action.get("proposal_digest") != _review_digest(review) or
            proposal_digest != stored_action.get("proposal_digest")):
        raise ValueError("proposal review contract is malformed")
    return metadata, stored_action, context


def _claim_dispatch(ticket_id, proposal_note_id, proposal_digest,
                    handler_name, requested_action, response_note_id, actor_id):
    """Durably claim exactly one proposal before any handler side effect."""
    from mojo.apps.incident.models import Ticket, TicketNote

    dispatch_key = _dispatch_key(
        proposal_note_id, proposal_digest, requested_action)
    # durable=True refuses accidental nesting. The claim must be committed,
    # not merely released from a savepoint, before dispatch begins.
    with transaction.atomic(durable=True):
        ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
        action_note = TicketNote.objects.select_for_update().filter(
            pk=proposal_note_id, parent_id=ticket.pk).first()
        if action_note is None:
            return {"status": "rejected"}
        try:
            metadata, stored_action, context = _stored_contract(
                action_note, handler_name, proposal_digest)
        except ValueError:
            logger.warning("Proposal note %s is stale or malformed", proposal_note_id)
            return {"status": "rejected"}

        resolution = stored_action.get("resolution")
        if stored_action.get("resolved"):
            if (isinstance(resolution, dict) and
                    resolution.get("dispatch_key") == dispatch_key):
                return {"status": "resolved", "dispatch_key": dispatch_key}
            return {"status": "rejected"}

        state = stored_action.get("state")
        claim = stored_action.get("claim")
        if state in ("claimed", "unknown"):
            if (isinstance(claim, dict) and
                    claim.get("dispatch_key") == dispatch_key):
                return {"status": state, "dispatch_key": dispatch_key}
            return {"status": "rejected"}
        if state != "pending" or ticket.status in ("closed", "resolved"):
            return {"status": "rejected"}

        stored_action["state"] = "claimed"
        stored_action["claim"] = {
            "dispatch_key": dispatch_key,
            "proposal_note_id": proposal_note_id,
            "proposal_digest": proposal_digest,
            "handler": handler_name,
            "action": requested_action,
            "response_note_id": response_note_id,
            "actor_id": actor_id,
        }
        action_note.metadata = metadata
        action_note.save(update_fields=["metadata"])
        return {
            "status": "execute",
            "dispatch_key": dispatch_key,
            "context": deepcopy(context),
        }


def _finalize_dispatch(proposal_note_id, dispatch_key, succeeded,
                       failure_code=None):
    """Record a known result separately; uncertainty never reopens a claim."""
    from mojo.apps.incident.models import TicketNote

    with transaction.atomic(durable=True):
        action_note = TicketNote.objects.select_for_update().get(
            pk=proposal_note_id)
        metadata = deepcopy(action_note.metadata or {})
        stored_action = metadata.get("action")
        claim = stored_action.get("claim") if isinstance(stored_action, dict) else None
        if (not isinstance(claim, dict) or
                claim.get("dispatch_key") != dispatch_key):
            return False
        if succeeded:
            stored_action["state"] = "resolved"
            stored_action["resolved"] = True
            stored_action["resolution"] = {
                "dispatch_key": dispatch_key,
                "handler": claim["handler"],
                "action": claim["action"],
                "response_note_id": claim["response_note_id"],
                "actor_id": claim["actor_id"],
            }
            stored_action.pop("execution", None)
        else:
            # The handler may have crossed an external side-effect boundary
            # before it failed. Preserve a durable unknown claim for operator
            # reconciliation; never make it runnable again automatically.
            stored_action["state"] = "unknown"
            stored_action["execution"] = {
                "status": "unknown",
                "dispatch_key": dispatch_key,
                "failure_code": failure_code or "ambiguous_execution",
            }
        action_note.metadata = metadata
        action_note.save(update_fields=["metadata"])
        return True


def dispatch_action(ticket, note, response_meta):
    """Dispatch an action response to the appropriate handler.

    Args:
        ticket: The parent Ticket instance
        note: The TicketNote that carries the action_response
        response_meta: The action_response dict from note.metadata
            Expected keys: proposal_note_id, proposal_digest, handler,
                action (approve/deny)
    """
    if (not isinstance(response_meta, dict) or
            set(response_meta) != {
                "proposal_note_id", "proposal_digest", "handler", "action"}):
        logger.warning("Action response on ticket %s has an invalid shape", ticket.pk)
        return False
    handler_name = response_meta.get("handler")
    requested_action = response_meta.get("action")
    proposal_note_id = response_meta.get("proposal_note_id")
    proposal_digest = response_meta.get("proposal_digest")
    if (not handler_name or requested_action not in ("approve", "deny") or
            isinstance(proposal_note_id, bool) or
            not isinstance(proposal_note_id, int) or
            not isinstance(proposal_digest, str) or
            len(proposal_digest) != 64):
        logger.warning("Action response on ticket %s missing handler", ticket.pk)
        return False

    handler = ACTION_HANDLERS.get(handler_name)
    if not handler:
        logger.warning("Unknown action handler: %s (ticket %s)", handler_name, ticket.pk)
        return False

    if (note.pk is None or note.parent_id != ticket.pk or
            (note.metadata or {}).get("action_response") != response_meta):
        logger.warning("Action response note is not bound to ticket %s", ticket.pk)
        return False

    actor = _has_global_authority(note, handler_name)
    if actor is None:
        return False

    try:
        claim = _claim_dispatch(
            ticket.pk, proposal_note_id, proposal_digest, handler_name,
            requested_action, note.pk, actor.pk)
    except Exception:
        logger.exception("Ticket action %s could not be durably claimed", handler_name)
        return False
    if claim["status"] == "resolved":
        return True
    if claim["status"] == "claimed":
        # Another same-choice request owns execution. Never invoke the handler
        # again or report an outcome that is not known yet.
        return False
    if claim["status"] == "unknown":
        return False
    if claim["status"] != "execute":
        return False

    dispatch_key = claim["dispatch_key"]
    try:
        # Deliberately outside the durable claim/finalize transactions. Any
        # crash after an external effect leaves the proposal claimed, never
        # pending and eligible for an unsafe automatic retry.
        from mojo.apps.incident.models import Ticket
        execution_ticket = Ticket.objects.get(pk=ticket.pk)
        completed = handler(
            execution_ticket, note, requested_action, claim["context"])
    except Exception:
        logger.exception("Action handler %s failed for ticket %s", handler_name, ticket.pk)
        try:
            _finalize_dispatch(
                proposal_note_id, dispatch_key, False,
                failure_code="handler_exception")
        except Exception:
            logger.exception("Ticket action %s remains durably claimed", handler_name)
        return False

    if completed is False:
        try:
            _finalize_dispatch(
                proposal_note_id, dispatch_key, False,
                failure_code="handler_refused")
        except Exception:
            logger.exception("Ticket action %s remains durably claimed", handler_name)
        return False
    try:
        return _finalize_dispatch(
            proposal_note_id, dispatch_key, True)
    except Exception:
        logger.exception("Ticket action %s succeeded but remains claimed", handler_name)
        return False


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------

def _add_system_note(ticket, text):
    """Add an [LLM Agent] note to the ticket."""
    from mojo.apps.incident.models import TicketNote
    TicketNote.objects.create(
        parent=ticket,
        user=None,
        note=f"[LLM Agent] {text}",
        group=ticket.group,
    )


ALLOWED_MODEL_REFS = {"incident.RuleSet"}


def _resolve_model_ref(context):
    """Resolve a model reference from action context.

    Context format: {"target": {"model": "app.Model", "pk": 123}}
    Only models in ALLOWED_MODEL_REFS can be resolved.
    Returns the model instance or None.
    """
    target = context.get("target")
    if not target:
        return None

    model_path = target.get("model")
    pk = target.get("pk")
    if not model_path or not pk:
        return None

    if model_path not in ALLOWED_MODEL_REFS:
        logger.warning("Model ref %s not in allowed list — rejecting", model_path)
        return None

    try:
        from django.apps import apps
        model_class = apps.get_model(model_path)
        return model_class.objects.get(pk=pk)
    except Exception:
        return None


def _handler_rule_approval(ticket, note, action, context):
    """Handle rule approval/denial.

    Approve: set is_active=True on the linked RuleSet, close ticket.
    Deny: delete the RuleSet, close ticket.
    """
    from mojo.apps.incident.models import RuleSet

    ruleset = _resolve_model_ref(context)
    if ruleset is None or not isinstance(ruleset, RuleSet):
        _add_system_note(ticket, "Cannot resolve linked ruleset — it may have been deleted.")
        return False

    if not (ruleset.metadata or {}).get("llm_proposed"):
        _add_system_note(ticket, "Target ruleset is not an LLM proposal — refusing to modify.")
        return False

    actor = note.active_request.user
    from mojo.helpers.request import safe_actor_context
    actor_context = safe_actor_context(note.active_request)
    if action == "approve":
        from mojo.apps.incident.services import admin_security
        admin_security.apply_action({
            "action": "ruleset.activate", "ruleset_id": ruleset.pk,
            "expected_modified": context.get("expected_modified"),
            "confirm": context.get("confirm"),
            "confirm_catch_all": context.get("confirm_catch_all"),
        }, actor, actor_context=actor_context)
        _add_system_note(
            ticket,
            f"Rule approved and activated. RuleSet #{ruleset.pk} \"{ruleset.name}\" is now live.",
        )
        ticket.status = "resolved"
        ticket.save(update_fields=["status"])

    elif action == "deny":
        name = ruleset.name
        from mojo.apps.incident.services import admin_security
        admin_security.apply_action({
            "action": "ruleset.delete", "ruleset_id": ruleset.pk,
            "expected_modified": context.get("expected_modified"),
            "confirm": context.get("deny_confirm"),
        }, actor, actor_context=actor_context)
        _add_system_note(ticket, f"Rule denied and deleted. RuleSet \"{name}\" has been removed.")
        ticket.status = "closed"
        ticket.save(update_fields=["status"])

    else:
        logger.warning("Unknown action '%s' for rule_approval on ticket %s", action, ticket.pk)
        return False
    return True


def _handler_rule_update(ticket, note, action, context):
    """Handle rule update approval/denial.

    Approve: replace the target RuleSet's rules with the proposed rules from context.
    Deny: close ticket, no changes.
    """
    from mojo.apps.incident.models import RuleSet

    ruleset = _resolve_model_ref(context)
    if ruleset is None or not isinstance(ruleset, RuleSet):
        _add_system_note(ticket, "Cannot resolve linked ruleset — it may have been deleted.")
        return False

    if action == "approve":
        proposed = context.get("ruleset")
        if proposed:
            from mojo.apps.incident.services import admin_security
            from mojo.helpers.request import safe_actor_context
            admin_security.apply_action({
                "action": "ruleset.replace", "ruleset_id": ruleset.pk,
                "expected_modified": context.get("expected_modified"),
                "confirm": context.get("confirm"), "ruleset": proposed,
            }, note.active_request.user,
                actor_context=safe_actor_context(note.active_request))
            _add_system_note(
                ticket,
                f"Rule update approved. RuleSet #{ruleset.pk} \"{ruleset.name}\" "
                f"updated with {len(proposed.get('rules') or [])} new rule(s).",
            )
        else:
            _add_system_note(ticket, "Rule update approved but no proposed rules found in context.")
            return False
        # Full replacements are deliberately inactive and need a distinct
        # activation confirmation after review.
        ticket.status = "closed"
        ticket.save(update_fields=["status"])

    elif action == "deny":
        _add_system_note(ticket, f"Rule update denied. RuleSet #{ruleset.pk} \"{ruleset.name}\" unchanged.")
        ticket.status = "closed"
        ticket.save(update_fields=["status"])

    else:
        logger.warning("Unknown action '%s' for rule_update on ticket %s", action, ticket.pk)
        return False
    return True


def _handler_block_confirm(ticket, note, action, context):
    """Handle IP block confirmation.

    Approve: validated block through the MojoSec action service, with the
    approver's identity and permission checked. The ticket resolves only
    when enforcement actually holds (applied, or verifiably pre-existing) —
    never on a swallowed failure.
    Deny: close ticket.
    """
    if action == "approve":
        from mojo.apps.incident.services import mojosec_actions

        actor = note.active_request.user
        if actor is None or not actor.has_permission(
                ["manage_security", "security"]):
            _add_system_note(
                ticket, "Block approval refused: approver lacks security "
                        "permissions.")
            return False
        ip = context.get("ip")
        reason = context.get("reason", "Approved via ticket action")
        if not ip:
            _add_system_note(ticket, "No IP specified in block context.")
            return False
        try:
            result = mojosec_actions.execute_manual_block(ip, reason, actor)
        except Exception:
            logger.exception("Failed to block IP %s via ticket action", ip)
            _add_system_note(
                ticket, f"Failed to block IP {ip} — see logs for details. "
                        "Ticket left open.")
            return False
        outcome = result.get("outcome")
        if outcome == "applied":
            _add_system_note(
                ticket, f"IP {result['ip']} blocked for {result['ttl']}s "
                        f"(approved by {actor.username}).")
        elif outcome == "pre_existing":
            _add_system_note(
                ticket, f"IP {result['ip']} was already blocked "
                        f"(reason: {result.get('prior_reason') or 'unknown'}); "
                        "the requested TTL/reason were not applied.")
        else:
            error = result.get("error")
            error_code = (error.get("code") if isinstance(error, dict)
                          else None)
            detail = str(error_code or result.get("reason") or outcome)[:64]
            _add_system_note(
                ticket, f"Block of {result.get('ip', ip)} refused: "
                        f"{detail}. Firewall state was not verified; ticket "
                        "left open.")
            return False
        ticket.status = "resolved"
        ticket.save(update_fields=["status"])

    elif action == "deny":
        _add_system_note(ticket, "Block request denied.")
        ticket.status = "closed"
        ticket.save(update_fields=["status"])
    else:
        return False
    return True


def _handler_escalate(ticket, note, action, context):
    """Handle escalation confirmation.

    Approve: send notification to on-call.
    Deny: close ticket.
    """
    if action == "approve":
        targets = context.get("targets", [])
        message = context.get("message", "")
        channel = context.get("channel", "email")

        if targets and message:
            try:
                from mojo.apps.incident.handlers.event_handlers import (
                    _resolve_users, INCIDENT_EMAIL_FROM,
                )
                from mojo.apps.aws.services import email as email_service

                if channel == "email" and INCIDENT_EMAIL_FROM:
                    users = _resolve_users(targets, require_email=True)
                    if users:
                        emails = [u.email for u in users]
                        email_service.send(
                            from_email=INCIDENT_EMAIL_FROM,
                            to=emails,
                            subject="[Security Escalation] Action Required",
                            body=message,
                        )
                _add_system_note(ticket, f"Escalation sent to {', '.join(targets)} via {channel}.")
            except Exception:
                logger.exception("Failed to send escalation for ticket %s", ticket.pk)
                _add_system_note(ticket, "Failed to send escalation — see logs for details.")
        else:
            _add_system_note(ticket, "Escalation approved but missing targets or message.")
        ticket.status = "resolved"
        ticket.save(update_fields=["status"])

    elif action == "deny":
        _add_system_note(ticket, "Escalation denied.")
        ticket.status = "closed"
        ticket.save(update_fields=["status"])


# ---------------------------------------------------------------------------
# Register all handlers
# ---------------------------------------------------------------------------

register_handler("incident.rule_approval", _handler_rule_approval)
register_handler("incident.rule_update", _handler_rule_update)
register_handler("incident.block_confirm", _handler_block_confirm)
register_handler("incident.escalate", _handler_escalate)
