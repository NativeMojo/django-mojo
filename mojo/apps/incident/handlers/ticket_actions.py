"""
Ticket Action Handler Registry — structured action dispatch for TicketNotes.

Actions live on notes (not tickets). A note's `metadata.action` block describes
what action is being proposed; a response note's `metadata.action_response`
triggers the handler to execute or reject it.

Handler naming: "app.handler_name" (e.g., "incident.rule_approval").
"""
from mojo.helpers import logit

logger = logit.get_logger(__name__, "incident.log")

ACTION_HANDLERS = {}


def register_handler(name, func):
    ACTION_HANDLERS[name] = func


def _find_matching_action_note(ticket, handler_name):
    """Find an unresolved action note on this ticket matching the given handler."""
    from mojo.apps.incident.models import TicketNote
    for tn in TicketNote.objects.filter(parent=ticket).order_by("-created"):
        meta = tn.metadata or {}
        action = meta.get("action")
        if not action or not isinstance(action, dict):
            continue
        if action.get("handler") == handler_name and not action.get("resolved"):
            return tn
    return None


def dispatch_action(ticket, note, response_meta):
    """Dispatch an action response to the appropriate handler.

    Args:
        ticket: The parent Ticket instance
        note: The TicketNote that carries the action_response
        response_meta: The action_response dict from note.metadata
            Expected keys: handler, action (approve/deny/choice), context
    """
    handler_name = response_meta.get("handler")
    if not handler_name:
        logger.warning("Action response on ticket %s missing handler", ticket.pk)
        return False

    handler = ACTION_HANDLERS.get(handler_name)
    if not handler:
        logger.warning("Unknown action handler: %s (ticket %s)", handler_name, ticket.pk)
        return False

    # Verify a matching unresolved action note exists on this ticket
    action_note = _find_matching_action_note(ticket, handler_name)
    if action_note is None:
        logger.warning(
            "No matching action note for handler %s on ticket %s — rejecting",
            handler_name, ticket.pk,
        )
        return False

    # Prevent double-dispatch: if ticket is already in a terminal state, skip
    if ticket.status in ("closed", "resolved"):
        logger.info("Ticket %s already %s — skipping action dispatch", ticket.pk, ticket.status)
        return False

    action = response_meta.get("action")
    context = response_meta.get("context") or {}

    try:
        handler(ticket, note, action, context)
        # Mark the original action note as resolved
        action_note.metadata["action"]["resolved"] = True
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
        ticket.status = "closed"
        ticket.save(update_fields=["status"])
        return

    if not (ruleset.metadata or {}).get("llm_proposed"):
        _add_system_note(ticket, "Target ruleset is not an LLM proposal — refusing to modify.")
        return

    if action == "approve":
        if ruleset.is_active:
            _add_system_note(ticket, f"RuleSet #{ruleset.pk} \"{ruleset.name}\" is already active.")
        else:
            ruleset.is_active = True
            ruleset.save(update_fields=["is_active"])
            _add_system_note(
                ticket,
                f"Rule approved and activated. RuleSet #{ruleset.pk} \"{ruleset.name}\" is now live.",
            )
        ticket.status = "resolved"
        ticket.save(update_fields=["status"])

    elif action == "deny":
        name = ruleset.name
        ruleset.delete()
        _add_system_note(ticket, f"Rule denied and deleted. RuleSet \"{name}\" has been removed.")
        ticket.status = "closed"
        ticket.save(update_fields=["status"])

    else:
        logger.warning("Unknown action '%s' for rule_approval on ticket %s", action, ticket.pk)


def _handler_rule_update(ticket, note, action, context):
    """Handle rule update approval/denial.

    Approve: replace the target RuleSet's rules with the proposed rules from context.
    Deny: close ticket, no changes.
    """
    from mojo.apps.incident.models import RuleSet

    ruleset = _resolve_model_ref(context)
    if ruleset is None or not isinstance(ruleset, RuleSet):
        _add_system_note(ticket, "Cannot resolve linked ruleset — it may have been deleted.")
        ticket.status = "closed"
        ticket.save(update_fields=["status"])
        return

    if action == "approve":
        proposed_rules = context.get("proposed_rules") or []
        if proposed_rules:
            from mojo.apps.incident.models import Rule
            ruleset.rules.all().delete()
            for i, rule_data in enumerate(proposed_rules):
                Rule.objects.create(
                    parent=ruleset,
                    name=rule_data.get("name", ""),
                    index=i,
                    field_name=rule_data.get("field_name", ""),
                    comparator=rule_data.get("comparator", "=="),
                    value=rule_data.get("value", ""),
                    value_type=rule_data.get("value_type", "str"),
                )
            _add_system_note(
                ticket,
                f"Rule update approved. RuleSet #{ruleset.pk} \"{ruleset.name}\" "
                f"updated with {len(proposed_rules)} new rule(s).",
            )
        else:
            _add_system_note(ticket, "Rule update approved but no proposed rules found in context.")
        ticket.status = "resolved"
        ticket.save(update_fields=["status"])

    elif action == "deny":
        _add_system_note(ticket, f"Rule update denied. RuleSet #{ruleset.pk} \"{ruleset.name}\" unchanged.")
        ticket.status = "closed"
        ticket.save(update_fields=["status"])

    else:
        logger.warning("Unknown action '%s' for rule_update on ticket %s", action, ticket.pk)


def _handler_block_confirm(ticket, note, action, context):
    """Handle IP block confirmation.

    Approve: execute the block via IPSet.
    Deny: close ticket.
    """
    if action == "approve":
        import ipaddress
        ip = context.get("ip")
        reason = context.get("reason", "Approved via ticket action")
        if ip:
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                _add_system_note(ticket, f"Invalid IP address format: {ip}")
                return
            try:
                from mojo.apps.incident.models import IPSet
                IPSet.block_ip(ip, reason=reason)
                _add_system_note(ticket, f"IP {ip} blocked successfully.")
            except Exception:
                logger.exception("Failed to block IP %s via ticket action", ip)
                _add_system_note(ticket, f"Failed to block IP {ip} — see logs for details.")
        else:
            _add_system_note(ticket, "No IP specified in block context.")
        ticket.status = "resolved"
        ticket.save(update_fields=["status"])

    elif action == "deny":
        _add_system_note(ticket, "Block request denied.")
        ticket.status = "closed"
        ticket.save(update_fields=["status"])


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
