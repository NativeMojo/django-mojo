# Assistant Learned Skills

**Type**: request
**Status**: planned
**Date**: 2026-04-07
**Priority**: high

## Description
The assistant should be able to learn reusable skills from user interactions and recall them by natural language triggers. A skill is a named, multi-step procedure (tool calls, filters, parameters, and follow-up actions) that the assistant stores and replays when a user says something that matches the skill's intent.

Example: a user teaches the assistant "rebuild sales monthly reports" which means: query `sales.FeeTable` for changes in the last 30 days filtered to merchant groups, and if any exist, publish a job for `sales.asyncjobs.generate_report`. From then on, saying "rebuild sales monthly reports" triggers that stored procedure.

## Context
Today the assistant has a three-tier memory system (global/user/group) in Redis hashes. Memory entries are short text strings (max 500 chars) stored by explicit key — there is no semantic search, no structured procedure storage, and no trigger-matching. The LLM can memorize facts ("platform uses PostgreSQL") but cannot store executable recipes with tool sequences, parameters, and conditional logic.

This is a natural evolution of the memory system. Skills differ from memories in that they are structured (tool chain + parameters + conditions), matched by intent (not exact key), and executable (the assistant replays them, not just reads them).

## Design Decisions (Resolved)

### Storage: DB model (PostgreSQL)
Skills are stored as a Django model (`assistant.Skill`) with JSONFields for structured data (steps, triggers, conditions). No Redis storage for skills — Redis is used locally for testing and Valkey in production, and Valkey does not support Vector Similarity Search. The DB model is auditable, queryable, and becomes the future pgvector table when vector search is added later (just add an `embedding` column).

### Matching: Tool-based on-demand lookup (no prompt injection)
Skills are NOT injected into the system prompt on every turn. This avoids burning tokens when skills are not relevant and scales for wider release. Instead, the approach mirrors the existing two-tier tool loading pattern:

1. **System prompt gets one sentence**: "You have learned skills available. When a user request sounds like a stored procedure or they reference a skill by name, use `find_skill` to check."
2. **`find_skill` tool** (core, always loaded): The LLM passes the user's intent/keywords as a search string. The tool queries the DB against skill name, description, and trigger phrases using `icontains`. Returns matching skills with their full step definitions.
3. The LLM decides whether a match is relevant and executes the steps using its existing tool-calling capabilities — no special `run_skill` tool needed.

Token cost: ~50 tokens for the system prompt hint + one `find_skill` tool definition in the tool list. Only pays for actual skill data when the LLM decides to look.

**Future vector path**: When pgvector is added, swap the `icontains` query internals in `find_skill` for cosine similarity on the `embedding` column. The tool interface stays identical — the LLM never knows the difference.

### No vector infrastructure in v1
No pgvector, no Redis VSS, no embeddings. Keyword-based DB lookup is sufficient for a reasonable skill count per tier. Vector search is a future optimization when skill volume or matching precision demands it.

### Skill complexity: Linear steps only in v1
Skills are ordered step chains (step 1 -> step 2 -> step 3) with simple conditions ("if step 1 returns results, continue to step 2"). No loops, no branching, no parallel steps.

## Acceptance Criteria
- Users can teach the assistant a new skill via natural conversation ("remember this as a skill called...")
- Skills are stored in DB with: name, description, trigger phrases, tool chain (ordered steps with parameters), conditions, and tier (user/group/global)
- `find_skill` tool searches by keyword match against name, description, and trigger phrases
- When a skill matches, the assistant confirms before executing (unless marked auto-execute)
- Skills are scoped: user skills are private, group skills shared within a group, global skills available to all
- Users can list, view, edit, and delete their skills via tools
- Admins can manage global and group skills
- Zero token cost when skills are not relevant to the conversation

## Investigation

**What exists**:
- Three-tier Redis memory system (`mojo/apps/assistant/services/memory.py`) — key-value text entries, pattern to follow for tier scoping
- Memory tools (`read_memory`, `write_memory`, `delete_memory`) — core tools, pattern to follow for skill tools
- Two-tier tool loading via `load_tools` — exact pattern to follow (core tool triggers on-demand loading)
- Tool registry with domain-based loading (`mojo/apps/assistant/__init__.py`)
- System prompt injects memory as markdown — skills will NOT follow this pattern (too expensive at scale)

**What changes**:

| File | Change |
|---|---|
| `mojo/apps/assistant/models/skill.py` | New model: `Skill` with name, description, triggers (JSONField), steps (JSONField), tier, user FK, group FK, auto_execute flag, created, modified |
| `mojo/apps/assistant/services/tools/skills.py` | New core tools: `find_skill`, `save_skill`, `list_skills`, `delete_skill` |
| `mojo/apps/assistant/services/agent.py` | Add one-line skill hint to system prompt |
| `mojo/apps/assistant/rest/assistant.py` | Optional: REST endpoint for admin skill management (or admin-only via existing RestMeta) |

**Constraints**:
- Must not slow down the agent loop for non-skill conversations (tool-based, not prompt-injected)
- Skill execution must respect the same permission gates as manual tool use — a skill cannot call tools the user lacks permission for
- Skills must not bypass security — the LLM executes skill steps through normal tool-calling, so permission checks still apply per tool
- Skill storage must be auditable (who created, when, what it does)
- Production uses Valkey (no Redis VSS), local uses Redis — no vector features available cross-environment

**Related files**:
- `mojo/apps/assistant/services/memory.py` — tier scoping pattern to follow
- `mojo/apps/assistant/services/agent.py` — system prompt, tool execution loop
- `mojo/apps/assistant/services/tools/memory.py` — core tool pattern to follow
- `mojo/apps/assistant/services/tools/discovery.py` — `load_tools` pattern to follow
- `mojo/apps/assistant/models/conversation.py` — sibling model location

## Skill Model Shape

```
assistant.Skill
  user        FK(account.User, nullable)    — null for global skills
  group       FK(account.Group, nullable)   — null for non-group skills
  tier        CharField (global/user/group)
  name        CharField(128)                — human-readable name, unique per tier+owner
  description TextField                     — what the skill does, used in search
  triggers    JSONField                     — list of trigger phrases ["rebuild sales reports", "regenerate monthly reports"]
  steps       JSONField                     — ordered list of step dicts (see below)
  auto_execute BooleanField(default=False)  — skip confirmation on match
  is_active   BooleanField(default=True)
  metadata    JSONField(default=dict)       — extensible (future: embedding vector column)
  created     DateTimeField(auto_now_add)
  modified    DateTimeField(auto_now)
```

Step dict shape:
```json
{
  "tool": "query_model",
  "params": {"app_name": "sales", "model_name": "FeeTable", "filters": {"modified__gte": "-30d"}},
  "condition": "previous_step.count > 0",
  "description": "Check for FeeTable changes in last 30 days"
}
```

## Core Tools

| Tool | Type | Description |
|---|---|---|
| `find_skill` | core, read-only | Search skills by keyword against name, description, triggers. Returns matching skills with steps. |
| `save_skill` | core, mutates | Create or update a skill. Validates step structure, enforces tier permissions. |
| `list_skills` | core, read-only | List all skills accessible to the user (own + group + global). Summary view, no steps. |
| `delete_skill` | core, mutates | Delete a skill by ID. Owner or admin only. |

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `LLM_ADMIN_SKILLS_ENABLED` | `True` | Feature flag for skill tools |
| `LLM_ADMIN_SKILLS_MAX_PER_USER` | `20` | Max skills per user tier |
| `LLM_ADMIN_SKILLS_MAX_PER_GROUP` | `30` | Max skills per group tier |
| `LLM_ADMIN_SKILLS_MAX_GLOBAL` | `20` | Max global skills |
| `LLM_ADMIN_SKILLS_MAX_STEPS` | `10` | Max steps per skill |

## Tests Required
- Skill model CRUD — create, read, update, delete
- `find_skill` — keyword matching returns correct results, respects tier scoping
- `save_skill` — validates step structure, enforces limits, prevents duplicates
- `list_skills` — returns user + group + global skills, respects permissions
- `delete_skill` — owner can delete own, admin can delete any
- Tier scoping — user skills private, group skills visible to group members only
- Permission enforcement — skill steps cannot reference tools the user lacks access to
- Auto-execute flag — respected when set
- RestMeta permissions on Skill model — admin management

## Out of Scope
- Vector/embedding search (future pgvector migration)
- Scheduled/automatic skill execution (cron-triggered skills)
- Skill sharing/marketplace between tenants
- Visual skill builder UI
- Skill versioning/rollback
- Complex branching/looping in skill steps (v1 is linear only)
- Tool description embedding/semantic discovery (separate feature)

## Plan

**Status**: planned
**Planned**: 2026-04-07

### Objective
Add a `Skill` model and four core tools (`find_skill`, `save_skill`, `list_skills`, `delete_skill`) so the assistant can learn, store, and replay multi-step procedures on demand — following the same two-tier pattern as `load_tools`.

### Steps
1. `mojo/apps/assistant/models/skill.py` — New `Skill` model with user/group FKs, tier, name, description, triggers (JSONField list), steps (JSONField list), auto_execute, is_active, metadata, created, modified. RestMeta with OWNER_FIELD, VIEW_PERMS, CAN_DELETE. Unique constraint on (tier, user, group, name).
2. `mojo/apps/assistant/models/__init__.py` — Add `from .skill import Skill`
3. `mojo/apps/assistant/services/skills.py` — Service layer: `find_skills()` (Q filter on name/description/triggers scoped by tier), `save_skill()` (validate steps, enforce limits, upsert), `list_skills()` (summary view), `delete_skill()` (owner/admin check). Permission helpers follow memory.py pattern.
4. `mojo/apps/assistant/services/tools/skills.py` — Four core tools: `find_skill` (query string -> matching skills with steps), `save_skill` (tier/name/description/triggers/steps -> create/update), `list_skills` (optional tier filter -> summaries), `delete_skill` (skill_id -> remove). All `core=True`, permission `"assistant"`.
5. `mojo/apps/assistant/services/tools/__init__.py` — Add `from . import skills`
6. `mojo/apps/assistant/__init__.py` — Add `"skills"` to `DOMAIN_DESCRIPTIONS`
7. `mojo/apps/assistant/services/agent.py` — Add skills hint to SYSTEM_PROMPT (one paragraph after Memory section)
8. Run `bin/create_testproject` — generate migration for Skill model
9. `tests/test_assistant/21_test_skills.py` — Full test coverage (see Testing section)
10. `docs/django_developer/assistant/skills.md` — Model, service API, step format, settings
11. `docs/web_developer/assistant/skills.md` — How skills appear in assistant responses
12. `CHANGELOG.md` — New feature entry

### Design Decisions
- **Core tools, not domain tools**: All four skill tools are `core=True` — skills are foundational like memory, the LLM shouldn't need `load_tools` to access them.
- **Service layer separate from tools**: `services/skills.py` holds business logic; `services/tools/skills.py` is a thin adapter. Matches memory pattern.
- **DB only, no Redis**: Skills live in PostgreSQL. `find_skill` uses Django ORM Q objects with `icontains`. No Redis caching in v1 — skill counts are low enough. Future pgvector replaces `icontains` with embedding similarity.
- **Triggers as JSONField list**: `["rebuild sales reports", "regenerate monthly"]`. Searched with `triggers__icontains` on JSON text. Good enough for v1.
- **LLM executes steps itself**: No `run_skill` tool. `find_skill` returns step definitions, the LLM calls existing tools through normal tool-calling. Permission checks happen per-tool naturally.
- **Steps validated on save, not on find**: `save_skill` validates structure (tool + description required). Does NOT validate tool exists in registry (tools may be added/removed dynamically).
- **Upsert on duplicate name**: Same name in same scope (tier+user+group) updates existing skill rather than erroring.

### Edge Cases
- **Permission escalation**: Skill steps may reference tools the user can't access. Fine — tools fail with permission errors at execution time, same as manual calls.
- **Group skill without group context**: No `_assistant_group` means group skills not returned. Same as memory.
- **JSONField icontains on triggers**: Searches raw JSON string — `"rebuild"` matches `["rebuild sales reports"]`. Loose but appropriate; LLM evaluates relevance.
- **Stale tool references**: If a tool is removed, skill steps referencing it fail when called. LLM reports failure naturally.
- **Empty query to find_skill**: Returns empty list. LLM should pass meaningful keywords.

### Testing
- `tests/test_assistant/21_test_skills.py`:
  - Setup: admin, regular user, other user, group with members
  - CRUD: save, find, list, delete via service functions
  - Tier scoping: user private, group visible to members, global visible to all with assistant perm
  - Permissions: non-admin can't write global, non-member can't write group
  - Limits: enforce max skills per tier
  - Find: keyword matches name, description, triggers; scoped correctly
  - Duplicate: same name in same scope does upsert
  - Step validation: rejects steps without tool or description
  - Delete: owner can delete own, admin can delete any, non-owner cannot

### Docs
- `docs/django_developer/assistant/skills.md` — model, service API, step format, settings
- `docs/django_developer/assistant/README.md` — add skills to feature list
- `docs/web_developer/assistant/skills.md` — skills in assistant responses
- `CHANGELOG.md` — new feature entry
