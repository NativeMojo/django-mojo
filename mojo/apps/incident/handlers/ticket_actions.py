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


def _has_interactive_authority(note, handler_name):
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
                "Ticket action %s lacked interactive global authority",
                handler_name)
            return None
        fresh_auth.require_fresh(request, seconds=600)
    except Exception:
        logger.warning(
            "Ticket action %s failed fresh-auth authority", handler_name)
        return None
    return actor


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

    actor = _has_interactive_authority(note, handler_name)
    if actor is None:
        return False

    try:
        from mojo.apps.incident.models import Ticket, TicketNote
        with transaction.atomic():
            locked_ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
            action_note = TicketNote.objects.select_for_update().filter(
                pk=proposal_note_id, parent_id=locked_ticket.pk).first()
            if action_note is None:
                logger.warning(
                    "Proposal note %s does not belong to ticket %s",
                    proposal_note_id, ticket.pk)
                return False
            metadata = deepcopy(action_note.metadata or {})
            stored_action = metadata.get("action")
            if not isinstance(stored_action, dict):
                return False
            if (stored_action.get("schema") != ACTION_SCHEMA or
                    stored_action.get("schema_version") != ACTION_SCHEMA_VERSION or
                    stored_action.get("proposal_note_id") != action_note.pk or
                    stored_action.get("handler") != handler_name):
                logger.warning("Proposal note %s is stale or malformed", action_note.pk)
                return False
            context = stored_action.get("context")
            review = _review_payload(context) if isinstance(context, dict) else None
            if (review is None or stored_action.get("review") != review or
                    stored_action.get("proposal_digest") != _review_digest(review) or
                    proposal_digest != stored_action.get("proposal_digest")):
                logger.warning("Proposal note %s review contract is malformed", action_note.pk)
                return False

            resolution = stored_action.get("resolution")
            if stored_action.get("resolved"):
                # A same-choice concurrent response or retry observes success
                # without dispatching the mutation twice. A conflicting choice
                # never rewrites the committed decision.
                return bool(
                    isinstance(resolution, dict) and
                    resolution.get("action") == requested_action and
                    resolution.get("handler") == handler_name)
            if locked_ticket.status in ("closed", "resolved"):
                logger.info(
                    "Ticket %s already %s — skipping unresolved action",
                    locked_ticket.pk, locked_ticket.status)
                return False
            if stored_action.get("state") != "pending":
                logger.warning("Proposal note %s is not pending", action_note.pk)
                return False

            stored_action["state"] = "claimed"
            stored_action["claim"] = {
                "response_note_id": note.pk,
                "actor_id": actor.pk,
            }
            action_note.metadata = metadata
            action_note.save(update_fields=["metadata"])

            completed = handler(
                locked_ticket, note, requested_action, deepcopy(context))
            if completed is False:
                stored_action["state"] = "pending"
                stored_action.pop("claim", None)
                action_note.metadata = metadata
                action_note.save(update_fields=["metadata"])
                return False

            stored_action["state"] = "resolved"
            stored_action["resolved"] = True
            stored_action["resolution"] = {
                "handler": handler_name,
                "action": requested_action,
                "response_note_id": note.pk,
                "actor_id": actor.pk,
            }
            stored_action.pop("claim", None)
            action_note.metadata = metadata
            action_note.save(update_fields=["metadata"])
            return True
    except Exception:
        logger.exception("Action handler %s failed for ticket %s", handler_name, ticket.pk)
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
    if action == "approve":
        from mojo.apps.incident.services import admin_security
        admin_security.apply_action({
            "action": "ruleset.activate", "ruleset_id": ruleset.pk,
            "expected_modified": context.get("expected_modified"),
            "confirm": context.get("confirm"),
            "confirm_catch_all": context.get("confirm_catch_all"),
        }, actor)
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
        }, actor)
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
            admin_security.apply_action({
                "action": "ruleset.replace", "ruleset_id": ruleset.pk,
                "expected_modified": context.get("expected_modified"),
                "confirm": context.get("confirm"), "ruleset": proposed,
            }, note.active_request.user)
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
            _add_system_note(
                ticket, f"Block of {result.get('ip', ip)} refused: "
                        f"{result.get('reason', outcome)}. Ticket left open.")
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
