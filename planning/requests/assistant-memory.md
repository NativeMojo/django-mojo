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

| Tier | Redis Key | Who Reads | Who Writes | Injected When | Purpose |
|------|-----------|-----------|------------|---------------|---------|
| **Global** | `assistant:memory:global` | `assistant` perm, or superuser | `assistant` perm | Every conversation | Platform identity, tech stack, environment, universal rules |
| **User** | `assistant:memory:user:<user_id>` | The user themselves, or superuser | The user themselves, or superuser | Conversations by that user | Personal context that follows the user across groups |
| **Group** | `assistant:memory:group:<group_id>` | Any member of the group | Members with `assistant` perm on their Member | Conversations in group context | Tenant-specific rules, group-specific knowledge |

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

### Guided Onboarding

When global memory is empty (no entries in `assistant:memory:global`), the system prompt includes a onboarding section that instructs the LLM to proactively ask the user about their platform. This only fires when global memory has zero entries — once the first memory is written, the onboarding prompt is replaced by the normal memory section.

The onboarding prompt asks about:
- What kind of application/platform this is
- Infrastructure and environment (cloud provider, regions, etc.)
- Any critical safety rules ("never block these IPs", "always escalate PCI events")
- Key operational patterns worth remembering

The LLM stores answers as global memories using the `write_memory` tool (with `mutates=True` confirmation). After the first conversation with a user who has `assistant` permission, the platform has baseline context for all future conversations.

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
- Permission: `assistant`
- Params: `tier` (optional — "global", "user", "group"; defaults to all)
- Returns: dict of key-value pairs per tier
- Backend enforces tier-level access: global requires `assistant` perm, user tier limited to own or superuser, group tier requires group membership

**`write_memory`** — Store or update a memory entry
- Permission: `assistant`
- Params: `tier`, `key` (slug), `value` (plain text)
- Mutates: True (LLM confirms before writing)
- Validates: size limits, key format (lowercase, alphanumeric + colons/underscores)
- Backend enforces: global requires `assistant` perm, user tier limited to own or superuser, group tier requires `assistant` perm on Member

**`delete_memory`** — Remove a memory entry
- Permission: `assistant`
- Params: `tier`, `key`
- Mutates: True
- Same tier-level enforcement as write

### System Prompt Guidance

The system prompt gives the LLM clear instructions for each memory tier — what belongs there, when to write, and when not to.

#### Global Memory (platform-wide context)

**What to store**: Facts about the platform that every admin needs to know across every conversation.
- Platform identity: "Healthcare SaaS (HIPAA-compliant) on AWS us-east-1"
- Infrastructure rules: "Never block 10.0.0.0/8 or 172.16.0.0/12 — internal network"
- Escalation rules: "PCI-related events must go to the security team immediately"
- Environment facts: "Production runs on 3 app servers behind ALB, staging is single-instance"

**When to write**: When a user states a fact about the platform, infrastructure, or operational rules that would be useful in every future conversation regardless of who's asking. These are slow-changing, universal truths.

**When NOT to write**: Anything user-specific, group-specific, temporary, or queryable via tools. "We had an outage yesterday" is not a global memory — it's an incident in the database.

#### User Memory (personal context)

**What to store**: Preferences and context specific to this user that carry across conversations.
- Communication preferences: "Prefers Slack notifications over email for non-critical alerts"
- Focus areas: "Primarily monitors the auth service and login pipeline"
- Working patterns: "Usually investigates incidents during US East business hours"
- Shorthand: "When I say 'the dashboard', I mean the Grafana board at /d/api-latency"

**When to write**: When a user explicitly states a preference, or when you notice a recurring pattern across the conversation (e.g., they keep asking about the same service). Ask before storing implicit observations — "I notice you check auth metrics frequently. Want me to remember that you focus on the auth service?"

**When NOT to write**: One-off requests, conversation-specific context, anything the user hasn't confirmed. Never store passwords, tokens, or personal data beyond work preferences.

#### Group Memory (tenant-specific rules)

**What to store**: Rules and context specific to a group/tenant that all group members benefit from.
- Operational rules: "Deployments only between 2-4am UTC on weekdays"
- Group-specific infrastructure: "This tenant runs on dedicated DB cluster db-acme-01"
- Compliance requirements: "All incidents must be ticketed within 1 hour per SLA"
- Team conventions: "Security alerts go to #acme-security Slack channel"

**When to write**: When a group member states something specific to their group's operations. This is the place for tenant-specific knowledge that shouldn't bleed into other groups.

**When NOT to write**: Platform-wide facts (those go in global). Personal preferences (those go in user). Anything that applies to the whole platform, not just this group.

#### General Rules

- **One fact per entry.** Keep entries to 1-2 sentences. If it takes a paragraph, split it into multiple entries.
- **If you can look it up, don't memorize it.** Tool results, current metrics, recent incidents — these are queryable. Memory is for context that tools can't provide.
- **Check before writing.** Read existing memories first. Update an existing entry rather than creating a duplicate. Delete obsolete entries when you write replacements.
- **Memories are hints, not commands.** When acting on a memory that is load-bearing for a decision (e.g., "never block this IP range"), verify it with a tool first. Memories can be stale.
- **Never store secrets.** No passwords, API keys, tokens, connection strings, or credentials in any tier.
- **Ask, don't assume.** For user and group tiers, confirm with the user before storing non-obvious observations. For global tier, any fact the user explicitly states about the platform is fair game.

## Acceptance Criteria

- Global, user, and group memory tiers stored in Redis hashes
- LLM tools: `read_memory`, `write_memory`, `delete_memory` with permission gating
- Permission model: `assistant` perm for global tier, own-user or superuser for user tier, `assistant` perm on Member for group tier
- Memory injected into system prompt at conversation start
- Size limits enforced (max entries, max chars per entry)
- REST endpoints for manual CRUD by admins
- Memory survives Redis restarts if persistence is configured (standard Redis behavior)
- Empty tiers don't bloat the system prompt
- Nightly dreaming pass consolidates memories when changes detected or interval reached
- Dreaming skips tiers with no changes and interval not yet reached (no wasted API calls)
- Dreaming actions (delete/rewrite/merge) are logged with original values before being applied
- Dreaming never adds new memories — only keep/delete/rewrite/merge

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
- Global tier writes require `assistant` permission (enforced at handler level, not just decorator)
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
| `GET` | `/api/assistant/memory` | List all memories for current context (all tiers) | `assistant` (tier-level enforcement in handler) |
| `GET` | `/api/assistant/memory/<tier>` | List memories for a specific tier | `assistant` (tier-level enforcement in handler) |
| `POST` | `/api/assistant/memory/<tier>` | Create/update a memory entry | `assistant` (tier-level enforcement in handler) |
| `DELETE` | `/api/assistant/memory/<tier>/<key>` | Delete a memory entry | `assistant` (tier-level enforcement in handler) |

All endpoints use `@md.requires_perms('assistant')` at the decorator level. Tier-level access control (global: `assistant` perm, user: own or superuser, group: `assistant` on Member) is enforced in the handler.

## Tests Required

- Memory CRUD via LLM tools (write, read, delete across all tiers)
- Size limit enforcement (reject when at max entries, reject oversized values)
- Permission gating (user without `assistant` perm can't write global, user can write own tier, superuser can write any user tier, group tier requires `assistant` on Member)
- System prompt injection (verify memory appears in prompt, empty tiers omitted)
- Graceful degradation when Redis unavailable
- REST CRUD for all tiers with permission checks
- Memory isolation (user A can't read user B's memories, group A can't see group B's)
- Dreaming skips when no changes and interval not reached
- Dreaming runs when memory changed since last dream
- Dreaming runs when `DREAM_INTERVAL` days passed (even without changes)
- Dreaming consolidation actions applied or logged based on `DREAM_AUTO_APPLY`
- Unparseable LLM response during dreaming is logged and skipped (no changes applied)

## Resolved Questions

- **TTL on memories?** No TTL. Explicit deletion + nightly dreaming handles semantic expiry (e.g., "deploy freeze until March 5th" detected as expired by dreaming pass). `STALE_DAYS` (default 90) flags untouched entries for review but doesn't auto-delete unless `AUTO_PRUNE_STALE=True`.
- **Group context**: Resolved by step 1 (group FK on Conversation) and step 8 (pass `request.group` when creating conversations via REST, accept `group_id` via WS). Group memory tier is loaded based on `conversation.group`.

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

**2. Memory poisoning is a persistent prompt injection vector.** If someone writes bad info to global memory, every future conversation is influenced. The LLM treats memories as trusted context with no way to evaluate legitimacy.

→ *Mitigation*: Global tier gated by `assistant` permission (not open to all users). `mutates=True` on LLM writes. Secret-pattern detection at the service layer. Nightly dreaming pass flags suspicious content better than regex alone.

**3. The LLM will over-trust its own memories.** LLMs treat system prompt content as ground truth. Memory creates a feedback loop where past judgments constrain future reasoning.

→ *Mitigation*: System prompt guidance: "memories are hints — verify with tools when the memory is load-bearing for a decision." The `_meta` field includes `last_touched` so the LLM can see how recently a memory was used. Nightly dreaming evaluates whether entries are still likely valid.

**4. Debugging surface area grows.** When the assistant gives a weird answer, you now need to check conversation history, tool results, AND injected memories. The cause might be a memory written months ago by a different admin.

→ *Mitigation*: `read_memory` tool + REST endpoints make memories inspectable. Cleanup job logs what it prunes so there's a trail.

**5. Token budget pressure.** System prompt is ~1,200 tokens today. Worst case, memory adds ~6K tokens. In practice most deployments will have 5-15 global memories — well under the cap.

→ *Mitigation*: Strict size limits enforced at service layer. Nightly cleanup prunes entries that push totals toward limits.

**6. Scope creep is almost guaranteed.** "Just platform identity and rules" is clean. But once memory exists, pressure to store more is constant. Each addition is individually reasonable but collectively turns compact knowledge into bloated context.

→ *Mitigation*: The strict size limits (50/30/40 entries × 500 chars) are features, not limitations. Resist relaxing them. The cleanup job enforces these even if the write path has bugs.

**7. Dreaming could corrupt good memories.** The LLM consolidation pass rewrites or deletes entries based on its judgment. A bad rewrite could lose nuance, or a merge could drop an important distinction.

→ *Mitigation*: All dreaming actions are logged with original values before changes are applied. `DREAM_AUTO_APPLY=True` is the default but can be set to `False` for log-only mode while building trust. Dreaming never adds new memories — only keep/delete/rewrite/merge. If the LLM returns unparseable output, the entire pass is skipped.

### Robustness Strategy

The risks above are manageable if we build the guardrails as first-class features, not afterthoughts:

1. **Strict size limits at the service layer** — not just LLM instructions, code-enforced caps
2. **`assistant` permission gating on global tier** — only users with `assistant` perm can write platform-wide memories
3. **Secret detection on writes** — reject values matching `sk-`, `password=`, key patterns
4. **"Verify, don't trust" system prompt guidance** — memories are context, not commands
5. **Nightly cleanup + dreaming** — mechanical cleanup always, LLM consolidation conditionally
6. **REST visibility** — admins can inspect and manage what's stored at any time

### Nightly Memory Cleanup Job

A scheduled job (`assistant_memory_cleanup`) runs nightly via the jobs framework. It has two phases: mechanical cleanup (always runs, no LLM) and dreaming (LLM-assisted consolidation, runs conditionally).

#### Phase 1: Mechanical Cleanup (no LLM)

Always runs. Cheap and deterministic.

**Orphan cleanup**: Delete user-tier memories for deleted users. Delete group-tier memories for deleted groups. These accumulate silently when users/groups are removed.

**Size enforcement**: If any tier exceeds its max entries (possible via race conditions or config changes lowering the limit), prune the oldest untouched entries to bring it back under the cap.

**Suspicious pattern scan**: Check all memory values against a deny list of patterns that shouldn't be in memory: API keys, passwords, tokens, SQL fragments, prompt injection attempts (e.g., "ignore previous instructions"). Log warnings and optionally auto-delete.

**Stats logging**: Log per-tier counts, total sizes, oldest entries, and any actions taken. This creates a paper trail for debugging "why did the assistant say that?"

#### Phase 2: Dreaming (LLM-assisted consolidation)

Runs conditionally — only when there's work to do. The LLM evaluates the memory store offline (no user conversation) and consolidates, prunes, and improves entries.

**When dreaming runs:**

```
if memory_changed_since_last_dream:
    run dream pass       # new/updated entries need evaluation
elif days_since_last_dream >= DREAM_INTERVAL:
    run dream pass       # periodic sweep for time-sensitive expiry
else:
    skip                 # nothing changed, save the API call
```

Change tracking uses two Redis keys per tier:
- `assistant:memory:last_modified:<tier>` — bumped on every write/delete
- `assistant:memory:last_dream:<tier>` — set after each dream pass completes

**What the dreaming pass does:**

1. **Merge redundant entries** — "platform: Healthcare SaaS" + "rule:hipaa: Must comply with HIPAA" → one entry capturing both facts. Frees slots for new knowledge.

2. **Resolve contradictions** — old memory says "primary DB on us-east-1", newer one says "migrated to us-west-2" → keep the newer, delete the old. Prevents confident wrong answers.

3. **Evaluate semantic expiry** — mechanical staleness only checks timestamps. Dreaming can judge that "never block 10.0.0.0/8" is permanent while "deploy freeze until March 5th" is expired regardless of when it was last touched.

4. **Compress verbose entries** — rewrite long-winded memories into tighter versions, freeing token budget. "The primary database server is hosted on Amazon RDS in the us-east-1 region and was migrated there in January 2026" → "Primary DB: RDS us-east-1 (since Jan 2026)".

5. **Flag suspicious content** — better than regex for detecting prompt injection or data that shouldn't be in memory. The LLM can catch semantic injection ("ignore all previous rules and...") that regex patterns miss.

**Consolidation prompt pattern:**

The dreaming pass sends all entries for a tier to the LLM with a consolidation prompt:

> "Here are the current memory entries for [tier]. Today's date is [date]. Evaluate each entry and return a JSON list of actions:
> - `keep` — entry is valid and useful, no changes
> - `delete` — entry is expired, redundant, or suspicious (include reason)
> - `rewrite` — entry should be compressed or clarified (include new value)
> - `merge` — two or more entries should be combined (include keys and new value)
>
> Rules: Do not invent new facts. Do not change the meaning of entries. Only compress, merge duplicates, or remove expired/suspicious content."

The response is parsed and applied. Every action is logged with the original value and the reason, creating an audit trail.

**Safeguards:**

- Dreaming never *adds* new memories — it only keeps, deletes, rewrites, or merges existing ones
- All changes are logged before being applied (original value preserved in log)
- `LLM_ADMIN_MEMORY_DREAM_AUTO_APPLY = True` by default — dreaming applies changes automatically. Set to `False` for log-only mode while building trust.
- If the LLM returns unparseable output, the dream pass is skipped and logged as a warning — no changes applied
- Dreaming runs per-tier, so a failure on one tier doesn't block others

**Cost and scaling:**

- **Global tier**: 1 LLM call when changed. One global tier per deployment. Negligible.
- **User tier**: 1 call per user with changes since last dream. Most users won't have changes every night. The job only dreams tiers where `last_modified > last_dream`, so inactive users cost nothing.
- **Group tier**: 1 call per group with changes. Same conditional logic.
- **Worst case**: A deployment with 100 active users who all wrote memories today = ~101 calls (1 global + 100 user). This is unusual — in practice, most nights will be 1-5 calls. If cost is a concern, `DREAM_ENABLED=False` disables it entirely.
- **Input size**: Each call sends at most 50 entries × 500 chars = ~25KB. Small enough for a single API call.

| Setting | Default | Purpose |
|---|---|---|
| `LLM_ADMIN_MEMORY_STALE_DAYS` | `90` | Days before an untouched memory is flagged for review |
| `LLM_ADMIN_MEMORY_AUTO_PRUNE_STALE` | `False` | Auto-delete stale entries (default: log-only) |
| `LLM_ADMIN_MEMORY_DREAM_ENABLED` | `True` | Enable LLM-assisted memory consolidation |
| `LLM_ADMIN_MEMORY_DREAM_INTERVAL` | `7` | Days between periodic dream passes (even without changes) |
| `LLM_ADMIN_MEMORY_DREAM_AUTO_APPLY` | `True` | Apply dreaming changes automatically (False = log-only) |

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
   - `write_memory(user, tier, key, value, group=None)` — validates size limits, key format, secret patterns. Enforces tier-level permissions (`assistant` perm for global, own-user or superuser for user tier, `assistant` on Member for group tier). Writes via `HSET`. Stores `_meta` field with `created`/`last_touched` timestamps. Calls `mark_modified(tier)` on success.
   - `delete_memory(user, tier, key, group=None)` — permission check + `HDEL`
   - `build_memory_prompt(user, group=None)` — loads all applicable tiers, formats as markdown section, bumps `last_touched` on read entries. Returns `""` when Redis unavailable or no memories.
   - `cleanup_mechanical()` — called by nightly job phase 1. Handles orphans, size enforcement, suspicious pattern scan.
   - `dream_tier(tier, redis_key)` — called by nightly job phase 2. Loads entries, sends to LLM with consolidation prompt, returns list of actions. Does NOT apply changes — the job decides based on `DREAM_AUTO_APPLY`.
   - `mark_modified(tier)` — bumps `assistant:memory:last_modified:<tier>` timestamp. Called by write/delete.
   - `should_dream(tier)` — checks `last_modified` vs `last_dream` timestamps and `DREAM_INTERVAL`. Returns bool.
   - All functions catch Redis exceptions and return graceful fallbacks (empty dict, empty string, error dict).

3. **`mojo/apps/assistant/services/tools/memory.py`** (new) — Three LLM tools following existing TOOLS list pattern:
   - `read_memory`: permission `assistant`, returns applicable tiers based on user/group context
   - `write_memory`: permission `assistant`, `mutates=True`. Handler enforces tier-level access: global requires `assistant` perm, user tier is own or superuser, group tier requires `assistant` on Member.
   - `delete_memory`: permission `assistant`, `mutates=True`. Same tier-level enforcement.
   - Handlers receive `(params, user)`, call memory service, return result dicts

4. **`mojo/apps/assistant/services/tools/__init__.py`** — Add `from . import memory` and `_register_domain("memory", memory.TOOLS)`

5. **`mojo/apps/assistant/services/agent.py`** — Three changes:
   - `_get_system_prompt()` → `_get_system_prompt(user, group=None)`: calls `build_memory_prompt()` and appends to system prompt. Returns base prompt unchanged when memory is disabled or Redis unavailable.
   - Add memory guidance block to `SYSTEM_PROMPT`: when to store, when not to, "verify don't trust" rule, how to reference entries by key.
   - Add guided onboarding: when `build_memory_prompt()` returns empty AND global tier has zero entries, inject onboarding prompt instead of memory section. Instructs the LLM to ask about platform identity, infrastructure, and safety rules. Replaced by normal memory section once first global memory is written.
   - Update both `run_assistant()` and `run_assistant_ws()` to pass `user` and `conversation.group` to `_get_system_prompt()`.

6. **`mojo/apps/assistant/rest/memory.py`** (new) — REST endpoints:
   - `GET /api/assistant/memory` — list all tiers for current user + request.group
   - `GET /api/assistant/memory/<tier>` — list one tier
   - `POST /api/assistant/memory/<tier>` — write entry (params: `key`, `value`). Tier-level enforcement in handler.
   - `DELETE /api/assistant/memory/<tier>/<key>` — delete entry. Tier-level enforcement in handler.
   - All require `assistant` via `@md.requires_perms('assistant')`

7. **`mojo/apps/assistant/rest/__init__.py`** — Add `from .memory import *`

8. **`mojo/apps/assistant/rest/assistant.py`** + **`handler.py`** — Pass `group` when creating conversations:
   - REST: `Conversation.objects.create(user=user, title=title, group=request.group)`
   - WS handler: accept `group_id` in data, resolve to Group, pass to Conversation creation

9. **`mojo/apps/assistant/jobs.py`** (new) — Nightly cleanup job with two phases:
   - Register as a scheduled job via the jobs framework
   - Phase 1 (`_mechanical_cleanup()`): orphan cleanup, size enforcement, suspicious pattern scan, stats logging
   - Phase 2 (`_dream_pass(tier)`): conditional LLM-assisted consolidation — checks `last_modified` vs `last_dream` timestamps and `DREAM_INTERVAL` to decide whether to run. Sends tier entries to LLM with consolidation prompt, parses actions (keep/delete/rewrite/merge), applies or logs based on `DREAM_AUTO_APPLY` setting.
   - `assistant_memory_cleanup()` runs phase 1 always, then phase 2 per tier conditionally
   - Uses `logit.get_logger("assistant", "assistant.log")` for all output

### Design Decisions

- **Redis hash per tier**: One `HGETALL` per tier = 2-3 Redis calls to load all memory. No scanning, no pattern matching.
- **Group FK on Conversation**: Enables group-scoped memory and is generally useful for multi-tenant assistant usage. Optional (`null=True`) — superuser conversations don't need a group.
- **No TTL, cleanup job instead**: Explicit delete + nightly job is more predictable than TTL expiry. Admins won't be surprised by disappearing memories. The job handles the staleness problem that TTL would solve, but with visibility (logging) and control (configurable thresholds).
- **Dreaming only when needed**: The dream pass skips tiers with no changes since the last run, avoiding unnecessary API calls. A periodic interval (`DREAM_INTERVAL`, default 7 days) catches time-sensitive entries that expire semantically even though the data hasn't changed (e.g., "deploy freeze until March 5th").
- **Dreaming never adds**: The consolidation prompt only allows keep/delete/rewrite/merge — no inventing new memories. This prevents the feedback loop of the LLM generating its own context.
- **`_meta` hash field for timestamps**: Store `{"created": ..., "last_touched": ...}` as a JSON string in a `_meta` field of the same Redis hash. The cleanup job reads this to determine staleness. The memory service bumps `last_touched` when injecting into system prompt. Keeps everything in one hash — no secondary data structures.
- **`assistant` permission as the gate**: All memory access goes through the `assistant` permission — consistent with other assistant features. No `view_admin` or `security` checks. Tier-level enforcement (own-user for user tier, Member-level `assistant` for group tier) happens in the handler, not the decorator.
- **Superuser can read any user's memories**: For debugging and admin oversight. Superuser can also write to any user's tier.
- **Group tier uses Member permissions**: `assistant` permission on the Member record, not the User. This means group memory access is scoped to group membership — a user with system-level `assistant` perm but no group membership can't touch group memory.
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

- `tests/test_assistant/3_test_memory.py` — Memory CRUD (write/read/delete all tiers), size limit enforcement (reject at max entries, reject oversized values), permission gating (user without `assistant` perm blocked from global, user can't write other user's tier, superuser can write any user tier, group tier requires `assistant` on Member), key format validation, secret pattern rejection, graceful Redis failure, isolation between users/groups, `build_memory_prompt` output format (including empty-tier omission and onboarding prompt when global is empty), stale timestamp tracking (`last_touched` bumped on read, `last_modified` bumped on write)
- `tests/test_assistant/4_test_memory_cleanup.py` — Phase 1: orphan cleanup for deleted users/groups, stale entry detection, size enforcement pruning, suspicious pattern detection, stats logging. Phase 2 (dreaming): skip when no changes and interval not reached, run when changes detected, run when interval reached even without changes, parse LLM consolidation response (keep/delete/rewrite/merge actions), auto-apply vs log-only mode, unparseable LLM response skips gracefully, `last_dream` timestamp updated after successful pass
- `tests/test_assistant/2_test_conversations.py` — Update for group FK on conversation creation

### Docs

- `docs/django_developer/assistant/README.md` — New "Memory" section: tiers, permission model, settings, system prompt injection, guided onboarding, LLM tool reference, cleanup job (mechanical + dreaming), robustness model
- `docs/web_developer/assistant/README.md` — REST memory endpoints: request/response format, tier-level permission requirements, CRUD examples
