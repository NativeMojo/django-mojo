# LLM ticket reply: FK violation creating IncidentHistory for deleted incident

**Type**: bug
**Status**: done
**Date**: 2026-04-20
**Severity**: medium

## Description
When a human replies to an LLM-linked ticket, the job
`execute_llm_ticket_reply` re-invokes the LLM. During tool execution the
LLM sometimes tries to attach history to an incident (e.g. 5078) whose
row has already been deleted. `Incident.add_history()` then fails with a
FK integrity error on `incident_incidenthistory.parent_id`.

The failure is swallowed by the `try/except` in `add_history`
([incident.py:96-110](mojo/apps/incident/models/incident.py:96)), so the
request does not crash — but the history entry is lost and the error log
fires on every reply.

```
psycopg.errors.ForeignKeyViolation: insert or update on table
"incident_incidenthistory" violates foreign key constraint
"incident_incidenthis_parent_id_4eb743a8_fk_incident_"
DETAIL:  Key (parent_id)=(5078) is not present in table "incident_incident".
```

## Context
- Ticket.incident uses `on_delete=SET_NULL`
  ([ticket.py:41](mojo/apps/incident/models/ticket.py:41)), so an
  incident can disappear while tickets (and their LLM conversation
  transcripts) continue to reference it by id.
- The LLM conversation built in `execute_llm_ticket_reply`
  ([llm_agent.py:1150-1152](mojo/apps/incident/handlers/llm_agent.py:1150))
  includes `Incident ID: <id>` from when the incident still existed.
  The model then calls tools (`_tool_update_incident`, `_tool_add_note`,
  `_tool_block_ip`, `_tool_create_ticket`) with that stale id.
- Known deletion paths that can remove the incident between turns:
  - `Incident.check_delete_on_resolution()` when ruleset has
    `delete_on_resolution` ([incident.py:112-139](mojo/apps/incident/models/incident.py:112))
  - `Incident.on_action_merge()` deletes merged incidents
    ([incident.py:249](mojo/apps/incident/models/incident.py:249))
- The FK insert has `parent_id=5078`, meaning the in-memory object still
  carried a pk but the row was gone at insert time. Likely mechanism:
  a prior turn / concurrent flow deleted the row; the LLM tool
  `Incident.objects.get(pk=5078)` on this turn either races with that
  delete, or the object was cached/passed across a boundary.

## Acceptance Criteria
- Replying on an LLM ticket whose linked incident no longer exists does
  not produce a FK integrity error in `incident.log`.
- LLM tools (`_tool_update_incident`, `_tool_add_note`, `_tool_block_ip`,
  `_tool_create_ticket`) handle a missing incident gracefully and return
  a sensible result to the model instead of attempting to write history.
- `Incident.add_history()` is defensive: it does not attempt the insert
  when `self.pk` no longer corresponds to a live row (prevents noise
  from any caller, not just LLM).
- Regression test covers: ticket with `llm_linked=True`, its incident
  deleted, human posts a note → job runs without logging an exception
  and without raising.

## Investigation
**Likely root cause**: Callers hold a stale `Incident` instance (pk set,
row deleted). `add_history` blindly inserts `IncidentHistory(parent=self)`
and hits the FK constraint. The catch-all `except Exception` hides it
from callers but logs on every occurrence.

**Confidence**: high (code path and FK error are consistent; exact
deletion trigger for incident 5078 cannot be confirmed without
production data).

**Code path**:
- [ticket.py:89-99](mojo/apps/incident/models/ticket.py:89) — TicketNote.on_rest_saved publishes the job
- [llm_agent.py:1118-1199](mojo/apps/incident/handlers/llm_agent.py:1118) — execute_llm_ticket_reply builds conversation, runs agent loop
- [llm_agent.py:488-514](mojo/apps/incident/handlers/llm_agent.py:488) — _tool_update_incident calls add_history then may delete via check_delete_on_resolution
- [llm_agent.py:581-586](mojo/apps/incident/handlers/llm_agent.py:581) — _tool_add_note: get + add_history with no existence re-check
- [incident.py:84-110](mojo/apps/incident/models/incident.py:84) — add_history swallows the FK error

**Regression test**: not written — requires a working jobs worker /
queue fixture and LLM mock to reproduce cleanly. Feasible as a unit
test against `add_history` directly (construct an `Incident`, save,
delete the row out from under it, call `add_history`, assert no
exception and no row inserted).

**Related files**:
- `mojo/apps/incident/models/incident.py` (`add_history`)
- `mojo/apps/incident/handlers/llm_agent.py` (`_tool_update_incident`,
  `_tool_add_note`, `_tool_block_ip`, `_tool_create_ticket`,
  `execute_llm_ticket_reply`)
- `tests/test_incident/llm_agent.py` (add regression)
