# Assistant Memory

**Type**: request
**Status**: planned
**Date**: 2026-04-05
**Priority**: high

## Description

Add a persistent memory system to the assistant that stores compact, critical knowledge across conversations. Three tiers of memory — global (platform identity), user (portable personal context), and group (tenant-specific rules) — stored in Redis hashes with strict size limits. The LLM can read and write memories during conversations, and admins can manage them via REST.

## Context

The assistant currently has no knowledge that persists between conversations. Every new conversation starts cold — the LLM doesn't know what platform it's managing, what the operational rules are, or what the user cares about. Conversation history provides within-session context, but nothing carries over.

This feature gives the assistant a compact, long-term knowledge store. It learns what matters over time — "this is a healthcare SaaS on AWS", "never block 10.0.0.0/8 — that's our internal network", "PCI events go to the security team" — and recalls it automatically in future conversations.

The memory must stay compact and focused. It's not a general-purpose database — it stores only critical facts about the system and rules the assistant needs to do its job well.

## Design

### Three-Tier Scoping

| Tier | Redis Key | Who Writes | Injected When | Purpose |
|------|-----------|------------|---------------|---------|
| **Global** | `assistant:memory:global` | Superusers only (LLM or REST) | Every conversation | Platform identity, tech stack, environment, universal rules |
| **User** | `assistant:memory:user:<user_id>` | The user (LLM or REST) | Conversations by that user | Personal context that follows the user across groups |
| **Group** | `assistant:memory:group:<group_id>` | Group admins (LLM or REST) | Conversations in group context | Tenant-specific rules, group-specific knowledge |

### Storage Format

Redis hash per tier. Each field is a memory entry:

```
HSET assistant:memory:global "platform" "Healthcare SaaS (HIPAA-compliant) running on AWS us-east-1"
HSET assistant:memory:global "rule:internal_ips" "Never block 10.0.0.0/8 or 172.16.0.0/12 — internal network"
HSET assistant:memory:global "rule:pci_escalation" "PCI-related events must be escalated to security team immediately"
```

Keys are short slugs. Values are plain text — one fact per entry, kept to 1-2 sentences. No JSON nesting, no structured schemas. The LLM writes natural language; compactness is enforced by size limits.

### Size Limits

| Tier | Max Entries | Max Entry Size | Max Total Size |
|------|-------------|----------------|----------------|
| Global | 50 | 500 chars | ~25 KB |
| User | 30 | 500 chars | ~15 KB |
| Group | 40 | 500 chars | ~20 KB |

These limits keep the injected context well under 2K tokens per tier (~6K tokens worst case for all three tiers combined). The LLM enforces limits at write time — if at capacity, it must delete or update an existing entry before adding a new one.

### System Prompt Injection

At conversation start, all applicable memories are loaded and injected into the system prompt as a `## Memory` section:

```
## Memory

### Platform
- platform: Healthcare SaaS (HIPAA-compliant) running on AWS us-east-1
- rule:internal_ips: Never block 10.0.0.0/8 or 172.16.0.0/12 — internal network

### Your Notes
- preferred_channel: User prefers Slack notifications over email for non-critical alerts

### Group: Acme Corp
- rule:deploy_window: Deployments only between 2-4am UTC on weekdays
```

Empty tiers are omitted. The format is simple key-value so the LLM can reference entries by key when updating or deleting.

### LLM Tools

Three new tools in the `memory` domain:

**`read_memory`** — Read all memories for the current context (already injected, but useful for explicit recall)
- Permission: `view_admin`
- Params: `tier` (optional — "global", "user", "group"; defaults to all)
- Returns: dict of key-value pairs per tier

**`write_memory`** — Store or update a memory entry
- Permission: `view_admin` (user/group tier), `superuser` (global tier)
- Params: `tier`, `key` (slug), `value` (plain text)
- Mutates: True (LLM confirms before writing)
- Validates: size limits, key format (lowercase, alphanumeric + colons/underscores)

**`delete_memory`** — Remove a memory entry
- Permission: same as write
- Params: `tier`, `key`
- Mutates: True

### System Prompt Guidance

The system prompt instructs the LLM on when to write memories:

- **Do store**: Platform facts, environment details, operational rules, safety constraints, recurring user preferences
- **Don't store**: Ephemeral data, conversation-specific context, anything already in tool results, secrets or credentials
- **Keep it compact**: One fact per entry. If you can look it up with a tool, don't memorize it.
- **Prune proactively**: Before writing, check if an existing entry covers it. Update rather than duplicate.

## Acceptance Criteria

- Global, user, and group memory tiers stored in Redis hashes
- LLM tools: `read_memory`, `write_memory`, `delete_memory` with permission gating
- Global tier requires superuser; user/group tiers require view_admin
- Memory injected into system prompt at conversation start
- Size limits enforced (max entries, max chars per entry)
- REST endpoints for manual CRUD by admins
- Memory survives Redis restarts if persistence is configured (standard Redis behavior)
- Empty tiers don't bloat the system prompt

## Investigation

**What exists**:
- `mojo/helpers/redis/` — `get_adapter()` provides `hset`, `hget`, `hgetall`, `hdel` — exactly what's needed
- `mojo/apps/assistant/services/agent.py` — system prompt construction and tool-calling loop
- `mojo/apps/assistant/services/__init__.py` — tool registration pattern (`register_tool()`)
- `Conversation.metadata` JSONField exists but is unused — could reference memory tier keys but shouldn't duplicate Redis data

**What changes**:
- New file: `mojo/apps/assistant/services/memory.py` — memory read/write/delete logic, size validation, prompt injection helper
- New file: `mojo/apps/assistant/services/tools/memory.py` — LLM tool definitions (read/write/delete)
- Modified: `mojo/apps/assistant/services/agent.py` — inject memory into system prompt at conversation start
- Modified: `mojo/apps/assistant/services/__init__.py` — register memory tools
- New file: `mojo/apps/assistant/rest/memory.py` — REST endpoints for manual CRUD
- Modified: `mojo/apps/assistant/rest/__init__.py` — register memory REST routes

**Constraints**:
- Redis is optional in django-mojo — memory features must degrade gracefully (no memory if no Redis)
- Memory content must never include secrets (passwords, API keys, tokens)
- Global tier writes need superuser check, not just permission check
- Size limits must be enforced at the service layer, not just in LLM instructions

**Related files**:
- `mojo/helpers/redis/adapter.py` — RedisAdapter with hash operations
- `mojo/apps/assistant/services/agent.py` — system prompt + tool loop
- `mojo/apps/assistant/services/tools/` — existing tool domain pattern
- `mojo/apps/assistant/rest/assistant.py` — existing REST pattern

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `LLM_ADMIN_MEMORY_ENABLED` | `True` | Feature flag for memory (requires LLM_ADMIN_ENABLED + Redis) |
| `LLM_ADMIN_MEMORY_GLOBAL_MAX` | `50` | Max entries in global tier |
| `LLM_ADMIN_MEMORY_USER_MAX` | `30` | Max entries in user tier |
| `LLM_ADMIN_MEMORY_GROUP_MAX` | `40` | Max entries in group tier |
| `LLM_ADMIN_MEMORY_ENTRY_MAX_CHARS` | `500` | Max characters per memory value |

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| `GET` | `/api/assistant/memory` | List all memories for current context (all tiers) | `view_admin` |
| `GET` | `/api/assistant/memory/<tier>` | List memories for a specific tier | `view_admin` |
| `POST` | `/api/assistant/memory/<tier>` | Create/update a memory entry | `view_admin` / `superuser` for global |
| `DELETE` | `/api/assistant/memory/<tier>/<key>` | Delete a memory entry | `view_admin` / `superuser` for global |

## Tests Required

- Memory CRUD via LLM tools (write, read, delete across all tiers)
- Size limit enforcement (reject when at max entries, reject oversized values)
- Permission gating (non-superuser can't write global, user can write own tier)
- System prompt injection (verify memory appears in prompt, empty tiers omitted)
- Graceful degradation when Redis unavailable
- REST CRUD for all tiers with permission checks
- Memory isolation (user A can't read user B's memories, group A can't see group B's)

## Open Questions

- **TTL on memories?** Should entries expire after N days of not being referenced, or persist indefinitely? Leaning toward no TTL — explicit deletion only — but worth considering auto-expiry for user tier.
- **Group context**: How does the assistant know which group context to use? Likely from `request.group` or a `group_id` param on the conversation. Needs alignment with how group context flows through the assistant today.

## Out of Scope

- Embedding/vector search over memories (this is key-value, not semantic search)
- Memory sharing between users (each tier is isolated)
- Memory versioning or audit trail (keep it simple — Redis hash, no history)
- Operational patterns and traffic baselines (future enhancement if memory proves useful)
- Auto-learning without LLM confirmation (the LLM always decides what to store)

## Analysis

### Why Memory Is Worth Building

**The cold-start problem is real and costly.** Every conversation starts from zero. An admin who's triaged 50 incidents still has to say "don't block our internal IPs" on incident 51. The assistant doesn't know if it's managing a healthcare platform or a gaming site, whether infrastructure is AWS or bare metal, or that `10.0.0.0/8` is the internal network.

**Institutional knowledge evaporates.** When an admin learns through conversation that "PCI events always go to the security team" or "deploys at 2am cause expected job failures", that knowledge dies with the session. Memory makes the assistant accumulate organizational wisdom across admins and across time.

**Safety rules compound over time.** "Never block this IP range", "always escalate PCI events", "the CEO's account is user 42" — rules that prevent costly mistakes and are easy to forget across sessions.

### Risks and Mitigations

**1. Stale memories are worse than no memories.** A memory saying "primary DB is on RDS us-east-1" is helpful — until the team migrates to us-west-2 and nobody updates the memory. The assistant then gives wrong advice with confidence. Unlike having no memory (where it says "I don't know"), stale memory produces confident wrong answers.

→ *Mitigation*: Store rules and identity (changes slowly), not operational state (changes fast). "Don't store what you can look up with a tool" guidance. Nightly cleanup job validates and prunes stale entries.

**2. Memory poisoning is a persistent prompt injection vector.** If an admin writes bad info to global memory, every future conversation is influenced. The LLM treats memories as trusted context with no way to evaluate legitimacy.

→ *Mitigation*: Superuser-only on global tier. `mutates=True` on LLM writes. Secret-pattern detection at the service layer. Nightly cleanup audits for suspicious patterns.

**3. The LLM will over-trust its own memories.** LLMs treat system prompt content as ground truth. Memory creates a feedback loop where past judgments constrain future reasoning.

→ *Mitigation*: System prompt guidance: "memories are hints — verify with tools when the memory is load-bearing for a decision." Nightly cleanup adds a `last_verified` timestamp so the LLM can see how old a memory is.

**4. Debugging surface area grows.** When the assistant gives a weird answer, you now need to check conversation history, tool results, AND injected memories. The cause might be a memory written months ago by a different admin.

→ *Mitigation*: `read_memory` tool + REST endpoints make memories inspectable. Cleanup job logs what it prunes so there's a trail.

**5. Token budget pressure.** System prompt is ~1,200 tokens today. Worst case, memory adds ~6K tokens. In practice most deployments will have 5-15 global memories — well under the cap.

→ *Mitigation*: Strict size limits enforced at service layer. Nightly cleanup prunes entries that push totals toward limits.

**6. Scope creep is almost guaranteed.** "Just platform identity and rules" is clean. But once memory exists, pressure to store more is constant. Each addition is individually reasonable but collectively turns compact knowledge into bloated context.

→ *Mitigation*: The strict size limits (50/30/40 entries × 500 chars) are features, not limitations. Resist relaxing them. The cleanup job enforces these even if the write path has bugs.

### Robustness Strategy

The risks above are manageable if we build the guardrails as first-class features, not afterthoughts:

1. **Strict size limits at the service layer** — not just LLM instructions, code-enforced caps
2. **Superuser gating on global tier** — prevents casual or accidental poisoning
3. **Secret detection on writes** — reject values matching `sk-`, `password=`, key patterns
4. **"Verify, don't trust" system prompt guidance** — memories are context, not commands
5. **Nightly cleanup job** — the most important robustness feature (see below)
6. **REST visibility** — admins can inspect and manage what's stored at any time

### Nightly Memory Cleanup Job

A scheduled job (`assistant_memory_cleanup`) runs nightly via the jobs framework. Its responsibilities:

**Staleness detection**: Each memory entry stores a `_meta` hash field with `created` and `last_touched` timestamps. The cleanup job flags entries older than N days (configurable, default 90) that haven't been touched. "Touched" means read during a conversation (the memory service bumps the timestamp when injecting into the system prompt) or explicitly updated.

**Orphan cleanup**: Delete user-tier memories for deleted users. Delete group-tier memories for deleted groups. These accumulate silently when users/groups are removed.

**Size enforcement**: If any tier exceeds its max entries (possible via race conditions or config changes lowering the limit), prune the oldest untouched entries to bring it back under the cap.

**Suspicious pattern scan**: Check all memory values against a deny list of patterns that shouldn't be in memory: API keys, passwords, tokens, SQL fragments, prompt injection attempts (e.g., "ignore previous instructions"). Log warnings and optionally auto-delete.

**Stats logging**: Log per-tier counts, total sizes, oldest entries, and any actions taken. This creates a paper trail for debugging "why did the assistant say that?"

| Setting | Default | Purpose |
|---|---|---|
| `LLM_ADMIN_MEMORY_STALE_DAYS` | `90` | Days before an untouched memory is flagged for review |
| `LLM_ADMIN_MEMORY_AUTO_PRUNE_STALE` | `False` | Auto-delete stale entries (default: log-only) |

The job runs as a standard `mojo.apps.jobs` scheduled task — same pattern as other nightly maintenance jobs in the framework.

## Plan

**Status**: planned
**Planned**: 2026-04-05

### Objective

Add a three-tier persistent memory system (global / user / group) to the assistant, stored in Redis hashes with strict size limits, LLM tools, REST endpoints, and a nightly cleanup job for robustness.

### Steps

1. **`mojo/apps/assistant/models/conversation.py`** — Add optional `group` FK (`null=True, blank=True, on_delete=SET_NULL`) to Conversation. This establishes group context for memory injection and is useful beyond just memory. Run `bin/create_testproject` after.

2. **`mojo/apps/assistant/services/memory.py`** (new) — Core memory service:
   - `read_memories(user, group=None, tier=None)` — returns dict of key-value pairs per tier via `HGETALL`
   - `write_memory(user, tier, key, value, group=None)` — validates size limits, key format, secret patterns. Superuser check for global tier. Writes via `HSET`. Stores `_meta` field with `created`/`last_touched` timestamps.
   - `delete_memory(user, tier, key, group=None)` — permission check + `HDEL`
   - `build_memory_prompt(user, group=None)` — loads all applicable tiers, formats as markdown section, bumps `last_touched` on read entries. Returns `""` when Redis unavailable or no memories.
   - `cleanup_memories()` — called by nightly job. Handles staleness, orphans, size enforcement, suspicious pattern scan.
   - All functions catch Redis exceptions and return graceful fallbacks (empty dict, empty string, error dict).

3. **`mojo/apps/assistant/services/tools/memory.py`** (new) — Three LLM tools following existing TOOLS list pattern:
   - `read_memory`: permission `view_admin`, returns all tiers
   - `write_memory`: permission `view_admin` (handler checks `user.is_superuser` for global tier), `mutates=True`
   - `delete_memory`: same permission model, `mutates=True`
   - Handlers receive `(params, user)`, call memory service, return result dicts

4. **`mojo/apps/assistant/services/tools/__init__.py`** — Add `from . import memory` and `_register_domain("memory", memory.TOOLS)`

5. **`mojo/apps/assistant/services/agent.py`** — Two changes:
   - `_get_system_prompt()` → `_get_system_prompt(user, group=None)`: calls `build_memory_prompt()` and appends to system prompt. Returns base prompt unchanged when memory is empty or disabled.
   - Add memory guidance block to `SYSTEM_PROMPT`: when to store, when not to, "verify don't trust" rule, how to reference entries by key.
   - Update both `run_assistant()` and `run_assistant_ws()` to pass `user` and `conversation.group` to `_get_system_prompt()`.

6. **`mojo/apps/assistant/rest/memory.py`** (new) — REST endpoints:
   - `GET /api/assistant/memory` — list all tiers for current user + request.group
   - `GET /api/assistant/memory/<tier>` — list one tier
   - `POST /api/assistant/memory/<tier>` — write entry (params: `key`, `value`). Superuser gate on global.
   - `DELETE /api/assistant/memory/<tier>/<key>` — delete entry. Superuser gate on global.
   - All require `view_admin` via `@md.requires_perms('view_admin')`

7. **`mojo/apps/assistant/rest/__init__.py`** — Add `from .memory import *`

8. **`mojo/apps/assistant/rest/assistant.py`** + **`handler.py`** — Pass `group` when creating conversations:
   - REST: `Conversation.objects.create(user=user, title=title, group=request.group)`
   - WS handler: accept `group_id` in data, resolve to Group, pass to Conversation creation

9. **`mojo/apps/assistant/jobs.py`** (new) — Nightly cleanup job:
   - Register as a scheduled job via the jobs framework
   - `assistant_memory_cleanup()`: staleness detection, orphan cleanup, size enforcement, suspicious pattern scan, stats logging
   - Uses `logit.get_logger("assistant", "assistant.log")` for all output

### Design Decisions

- **Redis hash per tier**: One `HGETALL` per tier = 2-3 Redis calls to load all memory. No scanning, no pattern matching.
- **Group FK on Conversation**: Enables group-scoped memory and is generally useful for multi-tenant assistant usage. Optional (`null=True`) — superuser conversations don't need a group.
- **No TTL, cleanup job instead**: Explicit delete + nightly job is more predictable than TTL expiry. Admins won't be surprised by disappearing memories. The job handles the staleness problem that TTL would solve, but with visibility (logging) and control (configurable thresholds).
- **`_meta` hash field for timestamps**: Store `{"created": ..., "last_touched": ...}` as a JSON string in a `_meta` field of the same Redis hash. The cleanup job reads this to determine staleness. The memory service bumps `last_touched` when injecting into system prompt. Keeps everything in one hash — no secondary data structures.
- **Secret detection at service layer**: Regex patterns for API keys, passwords, tokens, connection strings. Defense in depth — LLM instructions + code enforcement.
- **`mutates=True` on write/delete tools**: Forces the LLM to confirm with the user before storing or removing memories. Prevents autonomous memory writes during normal tool-calling flows.
- **Cleanup job defaults to log-only for stale entries**: `LLM_ADMIN_MEMORY_AUTO_PRUNE_STALE=False` means stale entries are logged as warnings, not auto-deleted. Admins opt into auto-pruning once they trust the system. Orphans and size violations are always cleaned.

### Edge Cases

- **Redis unavailable**: `build_memory_prompt()` returns `""`, tools return `{"error": "Memory not available"}`. No crash, assistant works normally without memory context.
- **Concurrent writes**: Redis `HSET` is atomic per field. Two conversations writing different keys is safe. Same key = last-write-wins (acceptable).
- **User with no group**: User tier always works. Group tier not loaded when `conversation.group` is None.
- **Migration on existing data**: Group FK is nullable, existing conversations get `group=None`.
- **Deleted user/group**: Orphan cleanup in nightly job catches these. Memory service also handles gracefully — if user/group doesn't exist, that tier is skipped.
- **Config change lowers max entries**: Nightly job prunes oldest untouched entries to bring tiers under the new limit.

### Testing

- `tests/test_assistant/3_test_memory.py` — Memory CRUD (write/read/delete all tiers), size limit enforcement (reject at max entries, reject oversized values), permission gating (non-superuser blocked from global), key format validation, secret pattern rejection, graceful Redis failure, isolation between users/groups, `build_memory_prompt` output format, stale timestamp tracking
- `tests/test_assistant/4_test_memory_cleanup.py` — Nightly job: orphan cleanup for deleted users/groups, stale entry detection, size enforcement pruning, suspicious pattern detection, stats logging
- `tests/test_assistant/2_test_conversations.py` — Update for group FK on conversation creation

### Docs

- `docs/django_developer/assistant/README.md` — New "Memory" section: tiers, settings, system prompt injection, LLM tool reference, cleanup job, robustness model
- `docs/web_developer/assistant/README.md` — REST memory endpoints: request/response format, permission requirements
