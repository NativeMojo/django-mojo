"""Security domain tools — split by sub-domain, self-registering via @tool."""
from . import incidents  # noqa: F401
from . import events  # noqa: F401
from . import tickets  # noqa: F401
from . import rules  # noqa: F401
from . import ips  # noqa: F401

# Re-export handler functions so existing imports from the package path still work.
from .incidents import (  # noqa: F401
    _tool_query_incidents, _tool_get_incident, _tool_get_incident_events,
    _tool_get_incident_timeline, _tool_update_incident,
    _tool_bulk_update_incidents, _tool_merge_incidents,
)
from .events import (  # noqa: F401
    _tool_query_events, _tool_query_event_counts, _tool_get_event,
)
from .tickets import (  # noqa: F401
    _tool_query_tickets, _tool_get_ticket, _tool_create_ticket,
    _tool_update_ticket, _tool_add_ticket_note,
)
from .rules import (  # noqa: F401
    _tool_query_rulesets, _tool_get_ruleset, _tool_create_rule,
    _tool_add_rule_condition, _tool_update_ruleset, _tool_delete_ruleset,
)
from .ips import (  # noqa: F401
    _tool_query_ip_history, _tool_block_ip, _tool_unblock_ip,
    _tool_whitelist_ip, _tool_unwhitelist_ip,
    _tool_query_blocked_ips, _tool_query_ipsets,
)
