"""
LLM Security Agent — autonomous incident triage via Claude API.

The agent receives an incident, investigates it using tools (query events,
IP history, related incidents, metrics), and takes action: ignore noise,
resolve real threats (block IPs, send alerts), or escalate to humans via tickets.

Entry points:
    - execute_llm_handler(job)  — called by the job engine when llm:// handler fires
    - execute_llm_ticket_reply(job) — called when a human replies to an llm_linked ticket

Prompt hierarchy:
    1. SYSTEM_PROMPT (always loaded) — generic security context + tool instructions
    2. RuleSet.metadata.agent_prompt — custom per-rule instructions
    3. RuleSet.metadata.agent_memory — LLM's own learnings for this rule type
    4. Event/incident context — structured data in the user message
"""
import ujson
from mojo.helpers.settings import settings
from mojo.helpers import logit, llm

logger = logit.get_logger(__name__, "incident.log")


def _get_llm_api_key():
    return llm.get_api_key()

def _get_llm_model():
    return llm.get_model("general")

SYSTEM_PROMPT = """You are a security operations agent responsible for triaging incidents in a web application fleet.

Your job is to investigate each incident, determine if it's real or noise, and take appropriate action.
You have access to tools that let you query the system and take actions.

## Status Workflow
- Incidents arrive with status="new" (unhandled by human or LLM)
- Set status="investigating" when you start working on an incident
- Set status="ignored" for noise/false positives (with reasoning in the note)
- Set status="resolved" for real threats you've handled (blocked, notified, etc.)
- Create a ticket if you need human input — leave incident as "investigating"

## Guidelines
- OSSEC alerts are ~99% noise. File changes in system directories and SSH logins are exceptions — take those seriously.
- When unsure, create a ticket for human review rather than ignoring.
- Always record your reasoning in incident history notes.
- Check related incidents and IP history before deciding — context matters.
- Use query_event_counts to spot trends — a single event might be noise, but a spike in similar events is real.
- If you see a recurring pattern, create a new rule (disabled, for human approval).
- When blocking an IP, always provide a clear reason.
- For critical issues (active intrusion, data exfiltration indicators), send SMS alerts.
- For concerning issues that need human attention, create a ticket and send email/notify alerts.
- Before creating a ticket, check the "Open Tickets for This Category" section in the incident context. If a ticket already covers this pattern, use add_ticket_note to append your findings to it instead of creating a duplicate ticket.
- Read the agent_memory for this rule type — it contains your past learnings.
- Update agent_memory when you learn something new about a pattern.
- Before creating a new rule, check the existing active rules shown in the prompt context. If an existing rule covers a similar pattern but missed this event, use suggest_rule_update to propose modifications to that rule instead of creating a new one.
- Use request_approval when you want human confirmation before taking a destructive action (blocking IPs, escalations). This creates a structured approval request on the ticket.
- Use add_ticket_note with references when discussing specific objects (rulesets, incidents, IPs). This creates clickable context cards in the UI so admins can quickly navigate to the referenced items without searching.

## Incident Deletion Lifecycle
- RuleSets can have `metadata.delete_on_resolution = true` — incidents matching that rule are auto-deleted when resolved or closed. Use `delete_on_resolution` when proposing rules for noise patterns (bot scanning, brute-force, health blips) where the incident has no long-term value.
- For serious incidents (real threats, active intrusions, data exfiltration), set `do_not_delete = true` via update_incident to protect them from auto-deletion and periodic pruning. This overrides any RuleSet delete_on_resolution setting.
- When in doubt about severity, do NOT set do_not_delete — only use it for confirmed serious threats.

## Event Deduplication & Bundling
When creating rules, ALWAYS configure bundling to prevent duplicate incidents:
- bundle_by: groups events into a single incident (4=source_ip is most common)
- bundle_minutes: time window for grouping (30-60 min is typical)
- min_count + window_minutes: threshold before handlers fire (e.g., min_count=5, window_minutes=10 means "5 events in 10 min")
- Events are also deduplicated at ingestion: identical events within 60s increment a counter instead of creating new rows.
Without proper bundling, rapid-fire events (like OSSEC bursts) create hundreds of separate incidents.
"""

ANALYSIS_PROMPT = """You are a security operations agent performing a deep analysis of an incident.

Your goal is to identify the pattern behind this incident, find and merge related incidents, and propose
a RuleSet that will auto-handle this pattern in the future — so no new open incidents pile up.

## Your Workflow
1. Set the target incident to "investigating".
2. Review the pre-loaded events and related incidents below.
3. Use query_open_incidents to find all open incidents in this category.
4. For incidents that clearly represent the same pattern, merge them into the target incident using merge_incidents.
5. Identify the pattern: what category, fields, levels, and bundling would match these events?
6. Check existing rulesets (query_events with the category) — don't duplicate an existing rule.
7. Create a new rule (disabled, for human approval) via create_rule with proper bundling config.
8. Resolve the merged incident with a note explaining the new rule.
9. Summarize: how many incidents merged, what rule was proposed, what pattern it covers.

## Rules for Merging
- Only merge incidents with the SAME category.
- Only merge if you're confident they represent the same underlying pattern.
- Don't merge incidents that are already resolved or ignored.

## Rules for Rule Creation
- Always set bundle_by and bundle_minutes to prevent duplicate incidents.
- Choose a handler chain that matches the threat level (block for attacks, notify for health issues).
- The rule is created DISABLED — a human will review and approve it via a ticket.
- For noise patterns (bot scanning, brute-force, health blips), set delete_on_resolution=true so incidents are cleaned up automatically on resolution.

## Event Deduplication & Bundling Reference
- bundle_by: 0=none, 1=hostname, 2=model_name, 3=model_name+id, 4=source_ip, 5=hostname+model_name,
  7=source_ip+model_name, 8=source_ip+model_name+id, 9=source_ip+hostname
- bundle_minutes: time window for grouping (30-60 min typical)
- min_count + window_minutes: threshold before handlers fire
"""

# Claude API tool definitions
TOOLS = [
    {
        "name": "query_events",
        "description": "Query recent events by category, source IP, hostname, or time range. Returns up to 50 events.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter by event category"},
                "source_ip": {"type": "string", "description": "Filter by source IP"},
                "hostname": {"type": "string", "description": "Filter by hostname"},
                "minutes": {"type": "integer", "description": "Look back N minutes (default 60)", "default": 60},
                "limit": {"type": "integer", "description": "Max events to return (default 50)", "default": 50},
            },
        },
    },
    {
        "name": "query_event_counts",
        "description": "Get aggregate event counts grouped by category over a time window. Useful for detecting spikes and trends.",
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "description": "Look back N minutes (default 60)", "default": 60},
                "source_ip": {"type": "string", "description": "Filter by source IP (optional)"},
                "hostname": {"type": "string", "description": "Filter by hostname (optional)"},
            },
        },
    },
    {
        "name": "query_ip_history",
        "description": "Get GeoLocatedIP record for an IP: threat level, block history, country, geo data, past incidents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ip": {"type": "string", "description": "The IP address to look up"},
            },
            "required": ["ip"],
        },
    },
    {
        "name": "query_related_incidents",
        "description": "Find other incidents from the same source IP or category, including past LLM assessments.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_ip": {"type": "string", "description": "Filter by source IP"},
                "category": {"type": "string", "description": "Filter by category"},
                "limit": {"type": "integer", "description": "Max incidents (default 20)", "default": 20},
            },
        },
    },
    {
        "name": "query_incident_events",
        "description": "Get all events bundled into a specific incident.",
        "input_schema": {
            "type": "object",
            "properties": {
                "incident_id": {"type": "integer", "description": "The incident ID"},
            },
            "required": ["incident_id"],
        },
    },
    {
        "name": "update_incident",
        "description": "Change an incident's status and add a history note with your reasoning.",
        "input_schema": {
            "type": "object",
            "properties": {
                "incident_id": {"type": "integer", "description": "The incident ID"},
                "status": {
                    "type": "string",
                    "description": "New status",
                    "enum": ["investigating", "resolved", "ignored"],
                },
                "note": {"type": "string", "description": "Your reasoning for this status change"},
                "do_not_delete": {
                    "type": "boolean",
                    "description": "Set true for serious incidents (real threats, active intrusions) that should be preserved permanently — overrides delete_on_resolution and pruning.",
                },
            },
            "required": ["incident_id", "status", "note"],
        },
    },
    {
        "name": "block_ip",
        "description": "Block an IP address fleet-wide via GeoLocatedIP.block(). Records in IncidentHistory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ip": {"type": "string", "description": "IP address to block"},
                "reason": {"type": "string", "description": "Reason for blocking"},
                "ttl": {"type": "integer", "description": "Block duration in seconds (0=permanent, default 3600)", "default": 3600},
                "incident_id": {"type": "integer", "description": "Associated incident ID"},
            },
            "required": ["ip", "reason"],
        },
    },
    {
        "name": "create_ticket",
        "description": "Create a ticket for human review. IMPORTANT: Check the 'Open Tickets' section first — if an existing ticket covers this pattern, use add_ticket_note instead. The tool auto-deduplicates within the same incident category, but preferring add_ticket_note avoids unnecessary API calls. The ticket is automatically linked to the LLM agent — when the human responds, you'll be re-invoked.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Ticket title"},
                "note": {"type": "string", "description": "Your question or analysis for the human"},
                "priority": {"type": "integer", "description": "1-10 priority (default 5)", "default": 5},
                "incident_id": {"type": "integer", "description": "Associated incident ID"},
            },
            "required": ["title", "note"],
        },
    },
    {
        "name": "add_note",
        "description": "Add a history note to an incident with your reasoning or findings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "incident_id": {"type": "integer", "description": "The incident ID"},
                "note": {"type": "string", "description": "The note text"},
            },
            "required": ["incident_id", "note"],
        },
    },
    {
        "name": "send_alert",
        "description": "Send an alert via email, SMS, or in-app notification to users. Use SMS only for critical issues.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "Alert channel",
                    "enum": ["email", "sms", "notify"],
                },
                "targets": {"type": "string", "description": "Comma-separated targets (perm@name, protected@key, or username)"},
                "message": {"type": "string", "description": "Alert message"},
                "subject": {"type": "string", "description": "Email subject (for email channel)"},
            },
            "required": ["channel", "targets", "message"],
        },
    },
    {
        "name": "create_rule",
        "description": "Create a new RuleSet with rules. The rule is created DISABLED and a ticket is created for human approval. Use when you detect a recurring pattern that should be automated. IMPORTANT: always set bundle_by and bundle_minutes to prevent duplicate incidents from the same source.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Rule name describing the pattern"},
                "category": {"type": "string", "description": "Event category to match"},
                "handler": {"type": "string", "description": "Handler chain (e.g. 'block://?ttl=3600,notify://perm@manage_security')"},
                "rules": {
                    "type": "array",
                    "description": "List of field match rules",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Short description of this condition"},
                            "field": {"type": "string", "description": "Event field or metadata key to match"},
                            "comparator": {"type": "string", "enum": ["==", ">", ">=", "<", "<=", "contains", "regex"], "description": "Comparison operator"},
                            "value": {"type": "string", "description": "Value to compare against"},
                            "value_type": {"type": "string", "enum": ["int", "float", "str", "bool"], "description": "Type to convert value to. Default: str", "default": "str"},
                        },
                        "required": ["field", "comparator", "value"],
                    },
                },
                "min_count": {"type": "integer", "description": "Minimum events before triggering (threshold)"},
                "window_minutes": {"type": "integer", "description": "Time window for threshold counting"},
                "bundle_by": {
                    "type": "integer",
                    "description": "How to group events into incidents to prevent duplicates. 0=none, 2=model_name, 3=model_name+id, 4=source_ip, 7=source_ip+model_name, 8=source_ip+model_name+id, 9=source_ip+hostname. Default 4 (source_ip) is usually correct.",
                    "default": 4,
                },
                "bundle_minutes": {
                    "type": "integer",
                    "description": "Time window in minutes for bundling events into one incident (0=disabled). Use this to prevent duplicate incidents from rapid-fire events. Recommended: 30-60 for most patterns.",
                    "default": 30,
                },
                "reasoning": {"type": "string", "description": "Why you're proposing this rule"},
                "delete_on_resolution": {
                    "type": "boolean",
                    "description": "Set true for noise patterns (bot scanning, health blips) where the incident should be auto-deleted on resolution or close.",
                },
            },
            "required": ["name", "category", "handler", "reasoning"],
        },
    },
    {
        "name": "update_rule_memory",
        "description": "Write learnings to the RuleSet's agent_memory. This persists across invocations so you remember past decisions for this rule type.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ruleset_id": {"type": "integer", "description": "The RuleSet ID"},
                "memory": {"type": "string", "description": "What you learned (appended to existing memory)"},
            },
            "required": ["ruleset_id", "memory"],
        },
    },
    {
        "name": "add_ticket_note",
        "description": "Add a note to a ticket, optionally with context references that render as clickable model links in the UI. Use references when you mention specific objects (rulesets, incidents, IPs) so admins can click through to them directly. When appending to an existing ticket for a related incident, pass incident_id to auto-add an incident reference.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "description": "The ticket to add the note to"},
                "note": {"type": "string", "description": "The note text (your message to the admin)"},
                "incident_id": {"type": "integer", "description": "Optional incident ID — auto-adds a clickable reference to this incident on the note"},
                "references": {
                    "type": "array",
                    "description": "Optional model references for clickable context cards. Each reference renders as a linked card in the UI.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "model": {"type": "string", "description": "Model path (e.g., 'incident.RuleSet', 'incident.Incident', 'account.GeoLocatedIP')"},
                            "pk": {"type": "integer", "description": "Primary key of the model instance"},
                            "label": {"type": "string", "description": "Display label (e.g., 'SSH brute force blocker', 'IP 10.0.0.1')"},
                        },
                        "required": ["model", "pk"],
                    },
                },
            },
            "required": ["ticket_id", "note"],
        },
    },
    {
        "name": "request_approval",
        "description": "Request human approval for an action. Creates a ticket note with structured action metadata that the UI renders as Approve/Deny buttons. Use this instead of executing destructive actions directly when you're uncertain or the action requires human confirmation (blocking IPs, escalations, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "description": "The ticket to add the approval request to"},
                "handler": {"type": "string", "description": "Action handler name (e.g., 'incident.block_confirm', 'incident.escalate')"},
                "label": {"type": "string", "description": "Human-readable description of what is being requested (e.g., 'Block IP 10.0.0.1?')"},
                "context": {
                    "type": "object",
                    "description": "Handler-specific context. For model references use {\"target\": {\"model\": \"app.Model\", \"pk\": 123}}. For block_confirm use {\"ip\": \"...\", \"reason\": \"...\"}.",
                },
                "reasoning": {"type": "string", "description": "Your reasoning for requesting this action"},
            },
            "required": ["ticket_id", "handler", "label", "context", "reasoning"],
        },
    },
    {
        "name": "suggest_rule_update",
        "description": "Suggest modifications to an existing active rule. Use when an existing rule covers a similar pattern but missed this event — instead of creating a new rule, propose changes to widen the existing rule. Creates a ticket with an approval action for the update.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ruleset_id": {"type": "integer", "description": "The existing RuleSet to update"},
                "proposed_rules": {
                    "type": "array",
                    "description": "The new set of rules that should replace the current ones",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "field_name": {"type": "string", "description": "Event field or metadata key to match"},
                            "comparator": {"type": "string", "enum": ["==", ">", ">=", "<", "<=", "contains", "regex"]},
                            "value": {"type": "string"},
                            "value_type": {"type": "string", "enum": ["int", "float", "str", "bool"], "default": "str"},
                        },
                        "required": ["field_name", "comparator", "value"],
                    },
                },
                "reasoning": {"type": "string", "description": "Why the existing rule needs updating and what the changes accomplish"},
                "incident_id": {"type": "integer", "description": "The incident that triggered this suggestion"},
            },
            "required": ["ruleset_id", "proposed_rules", "reasoning"],
        },
    },
]

# Additional tools available only during analysis mode
ANALYSIS_TOOLS = [
    {
        "name": "merge_incidents",
        "description": "Merge related incidents into a target incident. Moves all events from the source incidents into the target and deletes the source incidents. Only merge incidents with the same category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_incident_id": {"type": "integer", "description": "The incident to merge INTO (keeps this one)"},
                "incident_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of incident IDs to merge FROM (these get deleted)",
                },
            },
            "required": ["target_incident_id", "incident_ids"],
        },
    },
    {
        "name": "query_open_incidents",
        "description": "Query open/new/investigating incidents, optionally filtered by category. Returns incidents with event counts. Use this to find incidents that could be merged or covered by a new rule.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter by category (optional)"},
                "limit": {"type": "integer", "description": "Max incidents to return (default 50)", "default": 50},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_query_events(params):
    from mojo.apps.incident.models import Event
    from mojo.helpers import dates

    criteria = {}
    if params.get("category"):
        criteria["category"] = params["category"]
    if params.get("source_ip"):
        criteria["source_ip"] = params["source_ip"]
    if params.get("hostname"):
        criteria["hostname"] = params["hostname"]

    minutes = params.get("minutes", 60)
    criteria["created__gte"] = dates.subtract(minutes=minutes)

    limit = min(params.get("limit", 50), 100)
    events = Event.objects.filter(**criteria).order_by("-created")[:limit]

    return [
        {
            "id": e.pk,
            "created": str(e.created),
            "category": e.category,
            "level": e.level,
            "source_ip": e.source_ip,
            "hostname": e.hostname,
            "title": e.title,
            "details": (e.details or "")[:500],
            "incident_id": e.incident_id,
            "metadata": e.metadata or {},
        }
        for e in events
    ]


def _tool_query_event_counts(params):
    from mojo.apps.incident.models import Event
    from mojo.helpers import dates
    from django.db.models import Count

    minutes = params.get("minutes", 60)
    criteria = {"created__gte": dates.subtract(minutes=minutes)}
    if params.get("source_ip"):
        criteria["source_ip"] = params["source_ip"]
    if params.get("hostname"):
        criteria["hostname"] = params["hostname"]

    counts = (
        Event.objects.filter(**criteria)
        .values("category")
        .annotate(count=Count("id"))
        .order_by("-count")[:50]
    )
    return list(counts)


def _tool_query_ip_history(params):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import Incident

    ip = params["ip"]
    try:
        geo = GeoLocatedIP.objects.get(ip_address=ip)
        ip_data = {
            "ip": geo.ip_address,
            "country_code": geo.country_code,
            "city": geo.city,
            "region": geo.region,
            "threat_level": geo.threat_level,
            "is_blocked": geo.is_blocked,
            "blocked_reason": geo.blocked_reason,
            "blocked_at": str(geo.blocked_at) if geo.blocked_at else None,
            "blocked_until": str(geo.blocked_until) if geo.blocked_until else None,
            "block_count": geo.block_count,
            "is_whitelisted": geo.is_whitelisted,
        }
    except GeoLocatedIP.DoesNotExist:
        ip_data = {"ip": ip, "found": False}

    # Past incidents from this IP
    incidents = Incident.objects.filter(source_ip=ip).order_by("-created")[:10]
    ip_data["past_incidents"] = [
        {
            "id": i.pk,
            "status": i.status,
            "priority": i.priority,
            "category": i.category,
            "created": str(i.created),
            "llm_assessment": (i.metadata or {}).get("llm_assessment"),
        }
        for i in incidents
    ]
    return ip_data


def _tool_query_related_incidents(params):
    from mojo.apps.incident.models import Incident
    from django.db.models import Q

    q = Q()
    if params.get("source_ip"):
        q |= Q(source_ip=params["source_ip"])
    if params.get("category"):
        q |= Q(category=params["category"])

    if not q:
        return []

    limit = min(params.get("limit", 20), 50)
    incidents = Incident.objects.filter(q).order_by("-created")[:limit]

    return [
        {
            "id": i.pk,
            "status": i.status,
            "priority": i.priority,
            "category": i.category,
            "source_ip": i.source_ip,
            "created": str(i.created),
            "title": i.title,
            "llm_assessment": (i.metadata or {}).get("llm_assessment"),
        }
        for i in incidents
    ]


def _tool_query_incident_events(params):
    from mojo.apps.incident.models import Event

    incident_id = params["incident_id"]
    events = Event.objects.filter(incident_id=incident_id).order_by("-created")[:100]

    return [
        {
            "id": e.pk,
            "created": str(e.created),
            "category": e.category,
            "level": e.level,
            "source_ip": e.source_ip,
            "hostname": e.hostname,
            "title": e.title,
            "details": (e.details or "")[:500],
            "metadata": e.metadata or {},
        }
        for e in events
    ]


def _tool_update_incident(params):
    from mojo.apps.incident.models import Incident

    incident = Incident.objects.get(pk=params["incident_id"])
    old_status = incident.status
    incident.status = params["status"]
    incident.save(update_fields=["status"])
    incident.add_history("status_changed",
        note=f"[LLM Agent] {params['note']} (status: {old_status} → {params['status']})")

    # Store assessment in metadata
    if not incident.metadata:
        incident.metadata = {}
    incident.metadata["llm_assessment"] = {
        "status": params["status"],
        "note": params["note"],
    }
    if params.get("do_not_delete"):
        incident.metadata["do_not_delete"] = True
    incident.save(update_fields=["metadata"])

    # Check if this incident should be auto-deleted on resolution
    if incident.status in ("resolved", "closed"):
        if incident.check_delete_on_resolution():
            return {"ok": True, "incident_id": params["incident_id"], "status": params["status"], "deleted": True}

    return {"ok": True, "incident_id": incident.pk, "status": params["status"]}


def _tool_block_ip(params):
    from mojo.apps.account.models import GeoLocatedIP

    ip = params["ip"]
    reason = f"[LLM Agent] {params['reason']}"
    ttl = params.get("ttl", 3600)

    geo, _ = GeoLocatedIP.objects.get_or_create(ip_address=ip)
    geo.block(reason=reason, ttl=ttl)

    # Record in incident history if linked
    if params.get("incident_id"):
        try:
            from mojo.apps.incident.models import Incident
            incident = Incident.objects.get(pk=params["incident_id"])
            incident.add_history("handler:llm",
                note=f"[LLM Agent] Blocked IP {ip}: {params['reason']} (ttl={ttl}s)")
        except Exception:
            pass

    return {"ok": True, "ip": ip, "blocked": True, "ttl": ttl}


TICKET_CLOSED_STATUSES = ("closed", "resolved")


def _get_llm_system_user():
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return User.objects.filter(is_superuser=True, is_active=True).first()


def _append_ticket_note(ticket, note_text, system_user=None):
    from mojo.apps.incident.models import TicketNote
    if system_user is None:
        system_user = _get_llm_system_user()
    if not system_user:
        return None
    return TicketNote.objects.create(
        parent=ticket,
        user=system_user,
        note=f"[LLM Agent] {note_text}",
        group=ticket.group,
    )


def _find_open_category_ticket(incident_category):
    """Find an open LLM-linked ticket for the same incident category."""
    from mojo.apps.incident.models import Ticket
    if not incident_category:
        return None
    return (
        Ticket.objects.filter(
            category="llm_review",
            metadata__llm_linked=True,
            incident__category=incident_category,
            incident__isnull=False,
            incident__status__in=["new", "open", "investigating"],
        )
        .exclude(status__in=TICKET_CLOSED_STATUSES)
        .exclude(metadata__has_key="update_suggestion")
        .exclude(metadata__has_key="requires_approval")
        .order_by("-modified")
        .first()
    )


def _dedup_to_existing_ticket(existing, incident, params):
    """Append a note to an existing ticket and record history on the incident."""
    note_text = (
        f"Related incident #{incident.pk}: {params['title']}\n\n"
        f"{params['note']}"
    )
    note = _append_ticket_note(existing, note_text)
    if note:
        note.metadata = {
            "action": {
                "type": "context",
                "references": [{
                    "model": "incident.Incident",
                    "pk": incident.pk,
                    "label": f"Incident #{incident.pk}",
                }],
            }
        }
        note.save(update_fields=["metadata"])
    incident.add_history(
        "handler:llm",
        note=f"[LLM Agent] Appended to existing ticket #{existing.pk}: {params['title']}",
    )
    return {"ok": True, "ticket_id": existing.pk, "deduplicated": True}


def _tool_create_ticket(params):
    from mojo.apps.incident.models import Ticket

    incident = None
    if params.get("incident_id"):
        try:
            from mojo.apps.incident.models import Incident
            incident = Incident.objects.get(pk=params["incident_id"])
        except Exception:
            pass

    # Dedup layer 1: same incident already has an open LLM-linked ticket
    if incident is not None:
        existing = (
            incident.tickets
            .filter(metadata__llm_linked=True)
            .exclude(status__in=TICKET_CLOSED_STATUSES)
            .order_by("-modified")
            .first()
        )
        if existing is not None:
            return _dedup_to_existing_ticket(existing, incident, params)

    # Dedup layer 2: another incident in the same category has an open ticket
    if incident is not None:
        existing = _find_open_category_ticket(incident.category)
        if existing is not None:
            return _dedup_to_existing_ticket(existing, incident, params)

    metadata = {"llm_linked": True, "llm_enabled": True}
    if params.get("ruleset_id"):
        metadata["ruleset_id"] = params["ruleset_id"]

    ticket = Ticket.objects.create(
        title=params["title"],
        description=params["note"],
        priority=params.get("priority", 5),
        category="llm_review",
        incident=incident,
        metadata=metadata,
    )

    _append_ticket_note(ticket, params["note"])

    if incident:
        incident.add_history("handler:llm",
            note=f"[LLM Agent] Created ticket #{ticket.pk}: {params['title']}")

    return {"ok": True, "ticket_id": ticket.pk}


def _tool_add_note(params):
    from mojo.apps.incident.models import Incident

    incident = Incident.objects.get(pk=params["incident_id"])
    incident.add_history("handler:llm", note=f"[LLM Agent] {params['note']}")
    return {"ok": True}


def _tool_send_alert(params):
    from mojo.apps.incident.handlers.event_handlers import (
        _resolve_users, INCIDENT_EMAIL_FROM, ADMIN_PORTAL_URL,
    )

    channel = params["channel"]
    targets = [t.strip() for t in params["targets"].split(",") if t.strip()]
    message = params["message"]

    if channel == "email":
        users = _resolve_users(targets, require_email=True)
        if users and INCIDENT_EMAIL_FROM:
            from mojo.apps.aws.services import email as email_service
            emails = [u.email for u in users]
            subject = params.get("subject", "[Security] LLM Agent Alert")
            email_service.send(
                from_email=INCIDENT_EMAIL_FROM,
                to=emails,
                subject=subject,
                body=message,
            )
        return {"ok": True, "sent_to": len(users) if users else 0}

    elif channel == "sms":
        users = _resolve_users(targets, require_phone=True)
        for user in (users or []):
            try:
                from mojo.apps.account.models.phone_hub import PhoneHub
                PhoneHub.send_sms(user.phone_number, message)
            except Exception:
                logger.exception("LLM send_alert SMS failed for user %s", user.pk)
        return {"ok": True, "sent_to": len(users) if users else 0}

    elif channel == "notify":
        users = _resolve_users(targets)
        for user in (users or []):
            try:
                from mojo.apps.account.models.notification import Notification
                Notification.send(
                    title="[Security] LLM Agent Alert",
                    body=message,
                    user=user,
                    kind="security",
                )
            except Exception:
                logger.exception("LLM send_alert notify failed for user %s", user.pk)
        return {"ok": True, "sent_to": len(users) if users else 0}

    return {"ok": False, "error": f"Unknown channel: {channel}"}


def _normalize_field_name(field_name):
    if field_name and field_name.startswith("metadata."):
        return field_name[9:]
    return field_name or ""


def _rule_signature(category, handler, rules_list):
    """Canonical signature for dedup.

    Uses JSON canonical form so LLM-supplied `value` strings containing
    separator characters cannot collide with unrelated rule combinations.
    rules_list is the incoming tool-call payload (dicts with field/comparator/value/value_type).
    """
    tuples = []
    for r in (rules_list or []):
        tuples.append([
            _normalize_field_name(r.get("field", "")),
            r.get("comparator", "==") or "==",
            str(r.get("value", "")),
            r.get("value_type", "str") or "str",
        ])
    tuples.sort()
    return ujson.dumps([category or "", handler or "", tuples])


def _rule_signature_from_ruleset(ruleset):
    """Same signature computed from a persisted RuleSet + its child Rules."""
    tuples = []
    for r in ruleset.rules.all():
        tuples.append([
            _normalize_field_name(r.field_name or ""),
            r.comparator or "==",
            str(r.value or ""),
            r.value_type or "str",
        ])
    tuples.sort()
    return ujson.dumps([ruleset.category or "", ruleset.handler or "", tuples])


def _find_matching_proposed_ruleset(category, signature):
    """Scan llm_proposed RuleSets in this category for a signature match."""
    from mojo.apps.incident.models import RuleSet
    candidates = RuleSet.objects.filter(category=category, metadata__llm_proposed=True)
    for rs in candidates:
        if _rule_signature_from_ruleset(rs) == signature:
            return rs
    return None


def _find_open_proposal_ticket(category):
    """Find any open rule-proposal ticket for this category.

    Replaces the time-windowed dedup — if there's an open proposal ticket
    linked to a pending ruleset in the same category, the new proposal
    is a duplicate regardless of timing.
    """
    from mojo.apps.incident.models import Ticket, RuleSet

    open_tickets = (
        Ticket.objects
        .filter(category="llm_review")
        .exclude(status__in=TICKET_CLOSED_STATUSES)
        .order_by("-modified")
    )
    for ticket in open_tickets:
        ruleset_id = (ticket.metadata or {}).get("ruleset_id")
        if not ruleset_id:
            continue
        try:
            rs = RuleSet.objects.get(pk=ruleset_id)
            if rs.category == category and not rs.is_active:
                return ticket, rs
        except RuleSet.DoesNotExist:
            continue
    return None, None


def _tool_create_rule(params):
    from mojo.apps.incident.models import RuleSet, Rule, Ticket

    category = params["category"]
    handler = params["handler"]
    rules_payload = params.get("rules") or []
    signature = _rule_signature(category, handler, rules_payload)

    # Check exact signature match against ACTIVE rules only
    existing = _find_matching_proposed_ruleset(category, signature)
    if existing is not None and existing.is_active:
        return {
            "ok": True,
            "ruleset_id": existing.pk,
            "deduplicated": True,
            "already_active": True,
        }

    # Check for any open proposal ticket in this category (replaces time-window dedup)
    open_ticket, pending_rs = _find_open_proposal_ticket(category)
    if open_ticket is not None and pending_rs is not None:
        pending_meta = pending_rs.metadata or {}
        occurrence_count = int(pending_meta.get("occurrence_count") or 1) + 1
        pending_meta["occurrence_count"] = occurrence_count
        pending_rs.metadata = pending_meta
        pending_rs.save(update_fields=["metadata"])

        _append_ticket_note(
            open_ticket,
            f"Pattern seen again — total observations: {occurrence_count}. "
            f"Reasoning from latest sighting: {params['reasoning']}",
        )
        return {
            "ok": True,
            "ruleset_id": pending_rs.pk,
            "ticket_id": open_ticket.pk,
            "deduplicated": True,
            "occurrence_count": occurrence_count,
        }

    # No match — create a new RuleSet and approval ticket with action block.
    metadata = {
        "llm_proposed": True,
        "llm_reasoning": params["reasoning"],
        "occurrence_count": 1,
    }
    if params.get("min_count"):
        metadata["min_count"] = params["min_count"]
    if params.get("window_minutes"):
        metadata["window_minutes"] = params["window_minutes"]
    if params.get("delete_on_resolution"):
        metadata["delete_on_resolution"] = True

    ruleset = RuleSet.objects.create(
        name=params["name"],
        category=category,
        handler=handler,
        bundle_by=params.get("bundle_by", 4),
        bundle_minutes=params.get("bundle_minutes", 30),
        priority=params.get("priority", 50),
        is_active=False,
        metadata=metadata,
    )

    for i, rule_data in enumerate(rules_payload):
        Rule.objects.create(
            parent=ruleset,
            name=rule_data.get("name", ""),
            index=i,
            field_name=_normalize_field_name(rule_data.get("field", "")),
            comparator=rule_data.get("comparator", "=="),
            value=rule_data.get("value", ""),
            value_type=rule_data.get("value_type", "str"),
        )

    delete_note = ""
    if metadata.get("delete_on_resolution"):
        delete_note = "\n**Delete on Resolution**: Yes (noise pattern — incidents auto-deleted when resolved/closed)\n"

    # Create the ticket with llm_enabled and requires_approval metadata
    ticket_metadata = {
        "llm_linked": True,
        "llm_enabled": True,
        "ruleset_id": ruleset.pk,
        "requires_approval": True,
    }
    ticket = Ticket.objects.create(
        title=f"[Rule Proposal] {params['name']}",
        description=f"Rule proposal for pattern in category '{category}'",
        priority=3,
        category="llm_review",
        metadata=ticket_metadata,
    )

    # Create the first note with an action block for approval
    note_text = (
        f"I've detected a recurring pattern and propose a new rule:\n\n"
        f"**Name**: {params['name']}\n"
        f"**Category**: {category}\n"
        f"**Handler**: {handler}\n"
        f"{delete_note}"
        f"**Reasoning**: {params['reasoning']}"
    )
    action_note_meta = {
        "action": {
            "type": "approval",
            "handler": "incident.rule_approval",
            "label": f"Approve rule proposal \"{params['name']}\"?",
            "context": {
                "target": {"model": "incident.RuleSet", "pk": ruleset.pk},
            },
        }
    }
    note = _append_ticket_note(ticket, note_text)
    if note:
        note.metadata = action_note_meta
        note.save(update_fields=["metadata"])

    return {"ok": True, "ruleset_id": ruleset.pk, "ticket_id": ticket.pk}


def _tool_update_rule_memory(params):
    from mojo.apps.incident.models import RuleSet

    ruleset = RuleSet.objects.get(pk=params["ruleset_id"])
    if not ruleset.metadata:
        ruleset.metadata = {}

    existing = ruleset.metadata.get("agent_memory", "")
    if existing:
        ruleset.metadata["agent_memory"] = existing + "\n" + params["memory"]
    else:
        ruleset.metadata["agent_memory"] = params["memory"]

    ruleset.save(update_fields=["metadata"])
    return {"ok": True, "ruleset_id": ruleset.pk}


def _tool_merge_incidents(params):
    from mojo.apps.incident.models import Incident

    target = Incident.objects.get(pk=params["target_incident_id"])
    incident_ids = params["incident_ids"]
    if not incident_ids:
        return {"ok": False, "error": "No incident IDs provided"}

    # Filter to only merge same-category, same-scope, non-resolved incidents
    mergeable = Incident.objects.filter(
        pk__in=incident_ids, category=target.category, scope=target.scope,
    ).exclude(pk=target.pk).exclude(status__in=["resolved", "ignored"])

    merge_ids = list(mergeable.values_list("pk", flat=True))
    if not merge_ids:
        return {"ok": True, "merged": 0, "note": "No eligible incidents to merge"}

    target.on_action_merge(merge_ids)
    return {"ok": True, "merged": len(merge_ids), "target_incident_id": target.pk}


def _tool_query_open_incidents(params):
    from mojo.apps.incident.models import Incident
    from django.db.models import Count

    criteria = {"status__in": ["new", "open", "investigating"]}
    if params.get("category"):
        criteria["category"] = params["category"]

    limit = min(params.get("limit", 50), 100)
    incidents = (
        Incident.objects.filter(**criteria)
        .annotate(event_count=Count("events"))
        .order_by("-created")[:limit]
    )

    return [
        {
            "id": i.pk,
            "status": i.status,
            "priority": i.priority,
            "category": i.category,
            "source_ip": i.source_ip,
            "hostname": i.hostname,
            "created": str(i.created),
            "title": i.title,
            "event_count": i.event_count,
            "rule_set_id": i.rule_set_id,
        }
        for i in incidents
    ]


CONTEXT_ALLOWED_MODELS = {
    "incident.RuleSet", "incident.Incident", "incident.Event",
    "incident.Ticket", "account.GeoLocatedIP",
}


def _tool_add_ticket_note(params):
    from mojo.apps.incident.models import Ticket

    ticket = Ticket.objects.get(pk=params["ticket_id"])
    note = _append_ticket_note(ticket, params["note"])

    references = list(params.get("references") or [])

    if params.get("incident_id"):
        from mojo.apps.incident.models import Incident
        if Incident.objects.filter(pk=params["incident_id"]).exists():
            references.append({
                "model": "incident.Incident",
                "pk": params["incident_id"],
                "label": f"Incident #{params['incident_id']}",
            })

    if note and references:
        valid_refs = [
            ref for ref in references
            if ref.get("model") in CONTEXT_ALLOWED_MODELS
        ]
        if valid_refs:
            note.metadata = {
                "action": {
                    "type": "context",
                    "references": valid_refs,
                }
            }
            note.save(update_fields=["metadata"])

    return {"ok": True, "ticket_id": ticket.pk, "note_id": note.pk if note else None}


def _tool_request_approval(params):
    from mojo.apps.incident.models import Ticket, TicketNote

    ticket = Ticket.objects.get(pk=params["ticket_id"])

    action_meta = {
        "action": {
            "type": "approval",
            "handler": params["handler"],
            "label": params["label"],
            "context": params["context"],
        }
    }

    note = _append_ticket_note(
        ticket,
        f"Requesting approval: {params['label']}\n\nReasoning: {params['reasoning']}",
    )
    if note:
        note.metadata = action_meta
        note.save(update_fields=["metadata"])

    return {"ok": True, "ticket_id": ticket.pk, "note_id": note.pk if note else None}


def _tool_suggest_rule_update(params):
    from mojo.apps.incident.models import RuleSet, Ticket

    ruleset = RuleSet.objects.get(pk=params["ruleset_id"])

    # Dedup: check for an existing open update-suggestion ticket for this ruleset
    existing_ticket = (
        Ticket.objects
        .filter(
            category="llm_review",
            metadata__ruleset_id=ruleset.pk,
            metadata__update_suggestion=True,
        )
        .exclude(status__in=TICKET_CLOSED_STATUSES)
        .order_by("-modified")
        .first()
    )

    if existing_ticket:
        _append_ticket_note(
            existing_ticket,
            f"Additional update suggestion:\n\n{params['reasoning']}",
        )
        return {
            "ok": True,
            "ticket_id": existing_ticket.pk,
            "ruleset_id": ruleset.pk,
            "deduplicated": True,
        }

    # Build current rules description for comparison
    current_rules = []
    for r in ruleset.rules.all().order_by("index"):
        current_rules.append({
            "name": r.name,
            "field_name": r.field_name,
            "comparator": r.comparator,
            "value": r.value,
            "value_type": r.value_type,
        })

    incident = None
    if params.get("incident_id"):
        try:
            from mojo.apps.incident.models import Incident
            incident = Incident.objects.get(pk=params["incident_id"])
        except Exception:
            pass

    ticket_metadata = {
        "llm_linked": True,
        "llm_enabled": True,
        "ruleset_id": ruleset.pk,
        "update_suggestion": True,
    }
    ticket = Ticket.objects.create(
        title=f"[Rule Update] {ruleset.name}",
        description=f"Suggested changes to RuleSet #{ruleset.pk}",
        priority=3,
        category="llm_review",
        incident=incident,
        metadata=ticket_metadata,
    )

    action_note_meta = {
        "action": {
            "type": "approval",
            "handler": "incident.rule_update",
            "label": f"Update RuleSet \"{ruleset.name}\"?",
            "context": {
                "target": {"model": "incident.RuleSet", "pk": ruleset.pk},
                "proposed_rules": params["proposed_rules"],
                "current_rules": current_rules,
            },
        }
    }

    note_text = (
        f"I suggest updating an existing rule to cover this pattern:\n\n"
        f"**RuleSet**: #{ruleset.pk} \"{ruleset.name}\"\n"
        f"**Reasoning**: {params['reasoning']}\n\n"
        f"Current rules: {len(current_rules)} condition(s)\n"
        f"Proposed rules: {len(params['proposed_rules'])} condition(s)"
    )
    note = _append_ticket_note(ticket, note_text)
    if note:
        note.metadata = action_note_meta
        note.save(update_fields=["metadata"])

    return {"ok": True, "ticket_id": ticket.pk, "ruleset_id": ruleset.pk}


TOOL_DISPATCH = {
    "query_events": _tool_query_events,
    "query_event_counts": _tool_query_event_counts,
    "query_ip_history": _tool_query_ip_history,
    "query_related_incidents": _tool_query_related_incidents,
    "query_incident_events": _tool_query_incident_events,
    "update_incident": _tool_update_incident,
    "block_ip": _tool_block_ip,
    "create_ticket": _tool_create_ticket,
    "add_note": _tool_add_note,
    "send_alert": _tool_send_alert,
    "create_rule": _tool_create_rule,
    "update_rule_memory": _tool_update_rule_memory,
    "add_ticket_note": _tool_add_ticket_note,
    "request_approval": _tool_request_approval,
    "suggest_rule_update": _tool_suggest_rule_update,
    "merge_incidents": _tool_merge_incidents,
    "query_open_incidents": _tool_query_open_incidents,
}


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------

def _call_claude(messages, system_prompt, tools=None):
    """Call Claude API with tool use. Returns the response as a dict."""
    return llm.call(messages, system=system_prompt, tools=tools or TOOLS)


def _run_agent_loop(messages, system_prompt, max_iterations=15, tools=None):
    """
    Run the agent loop: call Claude, execute tools, feed results back,
    repeat until Claude stops calling tools.
    """
    for _ in range(max_iterations):
        result = _call_claude(messages, system_prompt, tools=tools)
        stop_reason = result.get("stop_reason")

        # Add assistant response to messages
        messages.append({"role": "assistant", "content": result["content"]})

        if stop_reason != "tool_use":
            # Agent is done
            return result

        # Process tool calls
        tool_results = []
        for block in result["content"]:
            if block.get("type") != "tool_use":
                continue

            tool_name = block["name"]
            tool_input = block["input"]
            tool_id = block["id"]

            try:
                handler = TOOL_DISPATCH.get(tool_name)
                if not handler:
                    tool_result = {"error": f"Unknown tool: {tool_name}"}
                else:
                    tool_result = handler(tool_input)
            except Exception as e:
                logger.exception("LLM tool %s failed", tool_name)
                tool_result = {"error": str(e)}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": ujson.dumps(tool_result),
            })

        messages.append({"role": "user", "content": tool_results})

    logger.warning("LLM agent hit max iterations (%d)", max_iterations)
    return None


def _build_system_prompt(ruleset=None):
    """Build the full system prompt including rule-specific context."""
    parts = [SYSTEM_PROMPT]

    if ruleset:
        agent_prompt = (ruleset.metadata or {}).get("agent_prompt")
        if agent_prompt:
            parts.append(f"\n## Rule-Specific Instructions\n{agent_prompt}")

        agent_memory = (ruleset.metadata or {}).get("agent_memory")
        if agent_memory:
            parts.append(f"\n## Your Past Learnings for This Rule Type\n{agent_memory}")

    return "\n".join(parts)


def _build_pending_proposals_section(category):
    """Return a prompt section listing pending rule proposals for this category."""
    from mojo.apps.incident.models import RuleSet
    pending = list(
        RuleSet.objects.filter(
            category=category,
            metadata__llm_proposed=True,
            is_active=False,
        ).order_by("-modified")[:5]
    )
    if not pending:
        return ""
    lines = ["\n## Pending Rule Proposals for This Category"]
    lines.append("A rule has already been proposed for this category and is awaiting human approval.")
    lines.append("Do NOT call create_rule if the existing proposal covers the same attack pattern.")
    lines.append("The create_rule tool will deduplicate automatically, but avoiding the call is preferred.\n")
    for rs in pending:
        meta = rs.metadata or {}
        count = meta.get("occurrence_count", 1)
        reasoning = (meta.get("llm_reasoning") or "")[:200]
        lines.append(f"- **RuleSet #{rs.pk}** \"{rs.name}\" — seen {count} time(s)")
        if reasoning:
            lines.append(f"  Reasoning: {reasoning}")
    return "\n".join(lines)


def _build_open_tickets_section(category):
    """Return a prompt section listing open LLM tickets for this incident category."""
    from mojo.apps.incident.models import Ticket
    if not category:
        return ""
    tickets = list(
        Ticket.objects.filter(
            category="llm_review",
            metadata__llm_linked=True,
            incident__category=category,
            incident__isnull=False,
            incident__status__in=["new", "open", "investigating"],
        )
        .exclude(status__in=TICKET_CLOSED_STATUSES)
        .exclude(metadata__has_key="update_suggestion")
        .exclude(metadata__has_key="requires_approval")
        .select_related("incident")
        .order_by("-modified")[:10]
    )
    if not tickets:
        return ""
    lines = ["\n## Open Tickets for This Category"]
    lines.append("A ticket already exists for this incident category.")
    lines.append("Do NOT call create_ticket if an existing ticket covers the same pattern.")
    lines.append("Use add_ticket_note with the ticket_id and incident_id to append your findings instead.\n")
    for t in tickets:
        inc = t.incident
        lines.append(
            f"- **Ticket #{t.pk}** \"{t.title}\" — incident #{inc.pk}, "
            f"priority={t.priority}, created={t.created}"
        )
    return "\n".join(lines)


def _build_active_rules_section(category):
    """Return a prompt section listing active rules for this category."""
    from mojo.apps.incident.models import RuleSet
    active = list(
        RuleSet.objects.filter(category=category, is_active=True)
        .order_by("priority")[:5]
    )
    if not active:
        return ""
    lines = ["\n## Active Rules for This Category"]
    lines.append("These rules are currently live. If an existing rule covers a similar pattern")
    lines.append("but missed this event, use suggest_rule_update instead of creating a new rule.\n")
    for rs in active:
        rules = list(rs.rules.all().order_by("index"))
        conditions = ", ".join(
            f"{r.field_name} {r.comparator} {r.value}" for r in rules
        ) or "no conditions"
        lines.append(f"- **RuleSet #{rs.pk}** \"{rs.name}\" — handler: {rs.handler}")
        lines.append(f"  Conditions: {conditions}")
    return "\n".join(lines)


def _build_incident_message(event, incident):
    """Build the user message with incident context."""
    parts = [
        "A new incident needs your attention. Please investigate and take action.\n",
        f"## Incident #{incident.pk}" if incident else "## Event",
        f"- **Category**: {event.category}",
        f"- **Level**: {event.level}",
        f"- **Scope**: {event.scope}",
        f"- **Source IP**: {event.source_ip or 'N/A'}",
        f"- **Hostname**: {event.hostname or 'N/A'}",
        f"- **Title**: {event.title or 'N/A'}",
        f"- **Details**: {event.details or 'N/A'}",
        f"- **Country**: {event.country_code or 'N/A'}",
    ]

    if incident:
        parts.extend([
            f"- **Incident ID**: {incident.pk}",
            f"- **Status**: {incident.status}",
            f"- **Priority**: {incident.priority}",
            f"- **State**: {incident.state}",
        ])

    # Include event metadata
    metadata = event.metadata or {}
    if metadata:
        dedup_count = metadata.get("dedup_count")
        if dedup_count and dedup_count > 1:
            parts.append(f"- **Duplicate count**: {dedup_count} (this event represents {dedup_count} identical events)")

        # Include relevant metadata keys
        skip_keys = {"dedup_count", "server", "request_ip", "http_path", "http_protocol",
                     "http_method", "http_query_string", "http_user_agent", "http_host"}
        extra = {k: v for k, v in metadata.items() if k not in skip_keys}
        if extra:
            parts.append(f"\n## Event Metadata\n```json\n{ujson.dumps(extra, indent=2)}\n```")

    if incident and incident.rule_set_id:
        parts.append(f"\n- **RuleSet ID**: {incident.rule_set_id}")

    active_section = _build_active_rules_section(event.category)
    if active_section:
        parts.append(active_section)

    pending_section = _build_pending_proposals_section(event.category)
    if pending_section:
        parts.append(pending_section)

    tickets_section = _build_open_tickets_section(event.category)
    if tickets_section:
        parts.append(tickets_section)

    parts.append("\nPlease investigate this incident. Start by setting it to 'investigating', "
                 "then use the available tools to gather context before making a decision.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Job entry points
# ---------------------------------------------------------------------------

def execute_llm_handler(job):
    """
    Job function: triage an incident via the LLM agent.

    The job engine calls func(job) where job is a Job model instance.
    The actual data is in job.payload with keys:
        event_id: ID of the Event
        incident_id: ID of the Incident
        ruleset_id: ID of the RuleSet that triggered this (optional)
    """
    if not _get_llm_api_key():
        logger.warning("LLM handler called but LLM_HANDLER_API_KEY not configured")
        return

    payload = job.payload
    event_id = payload.get("event_id")
    incident_id = payload.get("incident_id")
    ruleset_id = payload.get("ruleset_id")

    # Load event
    try:
        from mojo.apps.incident.models import Event
        event = Event.objects.get(pk=event_id)
    except Exception:
        logger.exception("LLM handler: failed to load event %s", event_id)
        return

    # Load incident
    incident = None
    if incident_id:
        try:
            from mojo.apps.incident.models import Incident
            incident = Incident.objects.get(pk=incident_id)
        except Exception:
            logger.warning("LLM handler: incident %s not found", incident_id)

    # Load ruleset for custom prompt
    ruleset = None
    if ruleset_id:
        try:
            from mojo.apps.incident.models import RuleSet
            ruleset = RuleSet.objects.get(pk=ruleset_id)
        except Exception:
            pass

    # Build prompt and run agent
    system_prompt = _build_system_prompt(ruleset)
    user_message = _build_incident_message(event, incident)

    messages = [{"role": "user", "content": user_message}]

    try:
        result = _run_agent_loop(messages, system_prompt)
        if incident:
            # Extract final text response for the history
            text_parts = []
            if result and result.get("content"):
                for block in result["content"]:
                    if block.get("type") == "text":
                        text_parts.append(block["text"])
            if text_parts:
                summary = "\n".join(text_parts)[:2000]
                incident.add_history("handler:llm",
                    note=f"[LLM Agent] Triage complete: {summary}")
    except Exception:
        logger.exception("LLM agent failed for event %s", event_id)
        if incident:
            incident.add_history("handler:llm",
                note="[LLM Agent] Triage failed due to an error")


def _build_analysis_message(incident):
    """Build a rich user message for analysis mode with pre-loaded context."""
    from mojo.apps.incident.models import Event, Incident

    parts = [
        "## Analysis Request\n",
        "An admin has requested deep analysis of the following incident.\n",
        f"## Target Incident #{incident.pk}",
        f"- **Category**: {incident.category}",
        f"- **Status**: {incident.status}",
        f"- **Priority**: {incident.priority}",
        f"- **Source IP**: {incident.source_ip or 'N/A'}",
        f"- **Hostname**: {incident.hostname or 'N/A'}",
        f"- **Title**: {incident.title or 'N/A'}",
        f"- **Details**: {incident.details or 'N/A'}",
        f"- **Created**: {incident.created}",
        f"- **RuleSet ID**: {incident.rule_set_id or 'None'}",
    ]

    # Pre-load events
    events = Event.objects.filter(incident=incident).order_by("-created")[:50]
    if events:
        parts.append(f"\n## Events in This Incident ({events.count()} shown)\n")
        for e in events:
            parts.append(
                f"- [{e.pk}] {e.created} | level={e.level} | ip={e.source_ip} | "
                f"{e.title or ''} | {(e.details or '')[:200]}"
            )

    # Pre-load related open incidents
    related_criteria = {"status__in": ["new", "open", "investigating"], "category": incident.category}
    related = Incident.objects.filter(**related_criteria).exclude(pk=incident.pk).order_by("-created")[:20]
    if related:
        parts.append(f"\n## Related Open Incidents (same category: {incident.category})\n")
        for r in related:
            event_count = r.events.count()
            parts.append(
                f"- [#{r.pk}] status={r.status} priority={r.priority} ip={r.source_ip} "
                f"events={event_count} | {r.title or 'N/A'}"
            )

    parts.append(
        "\nPlease analyze this incident, merge related incidents if appropriate, "
        "and propose a rule to auto-handle this pattern."
    )

    return "\n".join(parts)


def execute_llm_analysis(job):
    """
    Job function: deep analysis of an incident via the LLM agent.

    Triggered by the 'analyze' POST_SAVE_ACTION on Incident.
    The agent investigates the incident, merges related incidents,
    and proposes rulesets for auto-handling.

    job.payload keys:
        incident_id: ID of the Incident to analyze
    """
    if not _get_llm_api_key():
        logger.warning("LLM analysis called but LLM_HANDLER_API_KEY not configured")
        return

    payload = job.payload
    incident_id = payload.get("incident_id")

    try:
        from mojo.apps.incident.models import Incident
        incident = Incident.objects.get(pk=incident_id)
    except Exception:
        logger.exception("LLM analysis: failed to load incident %s", incident_id)
        return

    # Load ruleset for custom prompt context
    ruleset = None
    if incident.rule_set_id:
        try:
            from mojo.apps.incident.models import RuleSet
            ruleset = RuleSet.objects.get(pk=incident.rule_set_id)
        except Exception:
            pass

    # Build prompt with analysis-specific instructions
    system_parts = [ANALYSIS_PROMPT]
    if ruleset:
        agent_prompt = (ruleset.metadata or {}).get("agent_prompt")
        if agent_prompt:
            system_parts.append(f"\n## Rule-Specific Instructions\n{agent_prompt}")
        agent_memory = (ruleset.metadata or {}).get("agent_memory")
        if agent_memory:
            system_parts.append(f"\n## Your Past Learnings for This Rule Type\n{agent_memory}")
    system_prompt = "\n".join(system_parts)

    user_message = _build_analysis_message(incident)
    messages = [{"role": "user", "content": user_message}]

    # Use all tools (base + analysis-specific)
    all_tools = TOOLS + ANALYSIS_TOOLS

    try:
        result = _run_agent_loop(messages, system_prompt, tools=all_tools)

        # Store analysis result
        text_parts = []
        if result and result.get("content"):
            for block in result["content"]:
                if block.get("type") == "text":
                    text_parts.append(block["text"])

        if not incident.metadata:
            incident.metadata = {}
        incident.metadata["llm_analysis"] = {
            "summary": "\n".join(text_parts)[:3000] if text_parts else "Analysis completed",
        }
        incident.metadata["analysis_in_progress"] = False
        incident.save(update_fields=["metadata"])

        if text_parts:
            summary = "\n".join(text_parts)[:2000]
            incident.add_history("handler:llm",
                note=f"[LLM Agent] Analysis complete: {summary}")
    except Exception:
        logger.exception("LLM analysis failed for incident %s", incident_id)
        # Clear in-progress flag
        try:
            if not incident.metadata:
                incident.metadata = {}
            incident.metadata["analysis_in_progress"] = False
            incident.save(update_fields=["metadata"])
            incident.add_history("handler:llm",
                note="[LLM Agent] Analysis failed due to an error")
        except Exception:
            pass




def execute_llm_ticket_reply(job):
    """
    Job function: re-invoke the LLM when a human replies to an llm_linked ticket.

    The job engine calls func(job) where job is a Job model instance.
    The actual data is in job.payload with keys:
        ticket_id: ID of the Ticket
        note_id: ID of the new TicketNote that triggered this
    """
    if not _get_llm_api_key():
        return

    payload = job.payload
    ticket_id = payload.get("ticket_id")

    try:
        from mojo.apps.incident.models import Ticket, TicketNote
        ticket = Ticket.objects.get(pk=ticket_id)
    except Exception:
        logger.exception("LLM ticket reply: failed to load ticket %s", ticket_id)
        return

    if not ticket.is_llm_enabled():
        return

    # Build conversation from all notes
    notes = TicketNote.objects.filter(parent=ticket).order_by("created")

    conversation = [
        f"## Ticket #{ticket.pk}: {ticket.title}\n",
        f"- **Status**: {ticket.status}",
        f"- **Priority**: {ticket.priority}",
        f"- **Category**: {ticket.category}",
    ]

    if ticket.incident:
        conversation.append(f"- **Incident ID**: {ticket.incident_id}")

    conversation.append("\n## Conversation History\n")

    for note in notes:
        speaker = "LLM Agent" if note.note and note.note.startswith("[LLM Agent]") else f"Human ({note.user.username if note.user else 'unknown'})"
        conversation.append(f"**{speaker}** ({note.created}):\n{note.note}\n")

    conversation.append(
        "\nA human has responded to this ticket. Please review their response "
        "and continue the investigation. If they've approved a proposed action, "
        "execute it. If they have questions, answer them."
    )

    # Load ruleset for prompt context if incident is linked
    ruleset = None
    if ticket.incident and ticket.incident.rule_set_id:
        try:
            from mojo.apps.incident.models import RuleSet
            ruleset = RuleSet.objects.get(pk=ticket.incident.rule_set_id)
        except Exception:
            pass

    system_prompt = _build_system_prompt(ruleset)
    messages = [{"role": "user", "content": "\n".join(conversation)}]

    try:
        result = _run_agent_loop(messages, system_prompt)

        # Post LLM's response as a ticket note
        if result and result.get("content"):
            text_parts = []
            for block in result["content"]:
                if block.get("type") == "text":
                    text_parts.append(block["text"])
            if text_parts:
                response_text = "\n".join(text_parts)[:5000]
                from django.contrib.auth import get_user_model
                User = get_user_model()
                system_user = User.objects.filter(is_superuser=True, is_active=True).first()
                if system_user:
                    TicketNote.objects.create(
                        parent=ticket,
                        user=system_user,
                        note=f"[LLM Agent] {response_text}",
                        group=ticket.group,
                    )
    except Exception:
        logger.exception("LLM ticket reply failed for ticket %s", ticket_id)
