# Assistant Learned Skills

Skills are reusable multi-step procedures stored in the database that the assistant can recall and replay. A user teaches the assistant a workflow once; the assistant finds and executes it on future requests.

## Model: `Skill`

**File**: `mojo/apps/assistant/models/skill.py`

```
Skill
├── tier          CharField  — "global", "user", or "group"
├── name          CharField  — short descriptive name, max 128 chars
├── description   TextField  — what the skill does
├── triggers      JSONField  — list of phrase strings that should surface this skill
├── steps         JSONField  — ordered list of step dicts
├── auto_execute  BooleanField  — skip confirmation prompt if True (default False)
├── is_active     BooleanField  — soft-delete / pause flag (default True)
├── metadata      JSONField  — reserved for future use
├── user          FK → account.User   — set for tier="user", null otherwise
├── group         FK → account.Group  — set for tier="group", null otherwise
├── created       DateTimeField
└── modified      DateTimeField
```

**Unique constraint**: `(tier, user, group, name)` — one skill per name per scope.

### Tiers

| Tier | Scope | `user` FK | `group` FK |
|---|---|---|---|
| `global` | All users with `assistant` permission | null | null |
| `user` | A single user's personal skills | set | null |
| `group` | Shared across a group | null | set |

### Step Format

Each element of `steps` is a dict:

| Field | Required | Description |
|---|---|---|
| `tool` | Yes | Tool name to call (e.g. `"query_jobs"`) |
| `description` | Yes | Human-readable summary of what this step does |
| `params` | No | Dict of fixed parameters passed to the tool |
| `condition` | No | Expression string evaluated against the previous step's result (e.g. `"previous_step.count > 0"`) |

Example step list:

```json
[
    {
        "tool": "query_jobs",
        "description": "Find failed jobs in the last 24 hours",
        "params": {"status": "failed", "minutes": 1440}
    },
    {
        "tool": "query_job_logs",
        "description": "Get logs for the first failed job",
        "condition": "previous_step.count > 0"
    }
]
```

### RestMeta

```python
class RestMeta:
    VIEW_PERMS = ["view_admin", "assistant", "owner"]
    SAVE_PERMS = ["view_admin"]
    OWNER_FIELD = "user"
    CAN_DELETE = True
    GRAPHS = {
        "default": {
            "fields": ["id", "tier", "name", "description", "auto_execute", "is_active", "created", "modified"],
        },
        "detail": {
            "fields": ["id", "tier", "name", "description", "triggers", "steps",
                       "auto_execute", "is_active", "metadata", "created", "modified"],
            "graphs": {"user": "basic"},
        },
    }
```

`"owner"` in `VIEW_PERMS` means personal (`tier="user"`) skills are auto-filtered to the requesting user.

---

## Service API

**Module**: `mojo.apps.assistant.services.skills`

### `find_skills(user, query, group=None)`

Search for active skills matching a query string. Searches `name`, `description`, and `triggers` using `icontains`. Returns up to 5 results with full step definitions.

```python
from mojo.apps.assistant.services.skills import find_skills

results = find_skills(user, "rebuild sales reports", group=group)
# Returns: [{"id": 1, "tier": "user", "name": "...", "steps": [...], ...}]
```

Returns an empty list when no match is found or when skills are disabled.

### `get_skill(user, skill_id, group=None)`

Load a single skill by primary key with permission check. Returns the full detail dict (same shape as `find_skills` results) or an error dict.

```python
from mojo.apps.assistant.services.skills import get_skill

result = get_skill(user, skill_id=42, group=group)
# Returns: {"id": 42, "tier": "user", "name": "...", "steps": [...], ...}
# Or: {"error": "Skill 42 not found"}
```

User-tier skills are scoped to their owner — a non-superuser cannot load another user's skill by ID.

### `save_skill(user, tier, name, description, triggers, steps, group=None, auto_execute=False)`

Create or update a skill. Skills with the same name in the same scope are updated (upsert).

```python
from mojo.apps.assistant.services.skills import save_skill

result = save_skill(
    user,
    tier="user",
    name="rebuild sales reports",
    description="Finds failed report jobs and retries them",
    triggers=["rebuild sales reports", "regenerate monthly reports"],
    steps=[
        {"tool": "query_jobs", "description": "Find failed report jobs", "params": {"status": "failed"}},
        {"tool": "retry_job", "description": "Retry the failed job"},
    ],
)
# Returns: {"message": "Skill 'rebuild sales reports' saved", "skill": {...}}
# Or: {"error": "..."}
```

Returns a dict with either `message` + `skill` (summary view) or `error`.

### `update_skill(user, skill_id, group=None, **fields)`

Partial update of an existing skill by ID. Only the keyword arguments provided are written — all other fields are left unchanged. Accepted field names: `name`, `description`, `triggers`, `steps`, `auto_execute`, `is_active`.

```python
from mojo.apps.assistant.services.skills import update_skill

result = update_skill(user, skill_id=42, description="Updated description", auto_execute=True)
# Returns: {"message": "Skill 'rebuild sales reports' updated", "skill": {...}}
# Or: {"error": "..."}
```

The same permission rules as `save_skill` apply. User-tier skills are owner-only (non-superusers cannot update another user's skill).

### `build_skill_catalog(user, group=None)`

Build a markdown-formatted catalog of all accessible active skills for injection into the agent system prompt. Returns an empty string when no skills exist or when skills are disabled.

```python
from mojo.apps.assistant.services.skills import build_skill_catalog

catalog = build_skill_catalog(user, group=group)
# Returns:
# "- **rebuild sales reports** (ID: 42, user): Finds failed report jobs and retries them | Triggers: rebuild sales reports, regenerate monthly reports"
```

This is called automatically by `_get_system_prompt` in `agent.py` at the start of every conversation turn. The catalog is injected into the `{skill_catalog}` placeholder in `SYSTEM_PROMPT`.

### `list_skills(user, group=None, tier=None)`

List all active skills the user can read. Returns summaries grouped by tier (no step details).

```python
from mojo.apps.assistant.services.skills import list_skills

result = list_skills(user, group=group)
# Returns: {"user": [{...}, ...], "global": [{...}]}
```

Pass `tier` to filter to a single tier. Empty tiers are omitted.

### `delete_skill(user, skill_id)`

Delete a skill by ID. The skill owner, group members with `assistant` permission, or a superuser can delete.

```python
from mojo.apps.assistant.services.skills import delete_skill

result = delete_skill(user, skill_id=42)
# Returns: {"message": "Skill 'rebuild sales reports' deleted"}
# Or: {"error": "..."}
```

---

## Permissions

| Operation | Tier | Required |
|---|---|---|
| Read | `global` | `assistant` permission or superuser |
| Read | `user` | `assistant` permission (own skills only); superuser can read any |
| Read | `group` | Any group member (with `check_parents=True`) |
| Write | `global` | `assistant` permission or superuser |
| Write | `user` | `assistant` permission or superuser |
| Write | `group` | Group member with `assistant` permission on their `Member` record |
| Delete | any | Same as write, or owner of the specific skill |

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `LLM_ADMIN_SKILLS_ENABLED` | `True` | Feature flag. Set to `False` to disable all skill tools and service calls. |
| `LLM_ADMIN_SKILLS_MAX_PER_USER` | `20` | Max active skills per user (tier="user"). |
| `LLM_ADMIN_SKILLS_MAX_PER_GROUP` | `30` | Max active skills per group (tier="group"). |
| `LLM_ADMIN_SKILLS_MAX_GLOBAL` | `20` | Max active global skills (tier="global"). |
| `LLM_ADMIN_SKILLS_MAX_STEPS` | `10` | Max steps per skill. |

---

## Assistant Tools

Five core tools expose skills to the LLM. All require `assistant` permission and are in the `skills` domain.

| Tool | Mutates | Description |
|---|---|---|
| `find_skill` | No | Load a skill by ID (from the catalog), or search by keywords. Returns full step definitions. |
| `save_skill` | Yes | Create or update a skill (full upsert by name). |
| `update_skill` | Yes | Partial update of a skill by ID — only the provided fields are changed. |
| `list_skills` | No | List all accessible skills, grouped by tier (no step details). |
| `delete_skill` | Yes | Delete a skill by ID. |

All five are `core=True` tools — they are always available to the LLM without calling `load_tools`.

### `find_skill` — ID lookup and keyword search

`find_skill` accepts two mutually exclusive inputs:

- `skill_id` (integer) — load a specific skill by its ID. Calls `get_skill()` internally. Use this when the skill is already known from the catalog injected in the system prompt.
- `query` (string) — keyword search across name, description, and triggers. Calls `find_skills()` internally.

When `skill_id` is provided, `query` is ignored.

### `update_skill` — partial update

`update_skill` takes a required `skill_id` plus any subset of `name`, `description`, `triggers`, `steps`, `auto_execute`, `is_active`. Fields omitted from the call are not changed. This is the preferred tool when the user wants to adjust one attribute of an existing skill without rewriting the whole thing (contrast with `save_skill`, which always replaces the full definition when the name matches).

### Agent Behavior (System Prompt)

At the start of each conversation turn, `_get_system_prompt()` calls `build_skill_catalog()` and injects the result into the `{skill_catalog}` placeholder in `SYSTEM_PROMPT`. The catalog lists every accessible active skill with its ID, tier, description, and trigger phrases.

The system prompt instructs the LLM to:

- Recognize skills from the injected catalog and call `find_skill(skill_id=<id>)` to load full steps when the user's request matches.
- Fall back to `find_skill(query=...)` for keyword search when no catalog match is obvious.
- Ask before executing steps unless `auto_execute` is true (marked `AUTO-EXECUTE` in the catalog).
- Execute each step in order using the referenced tools, evaluating conditions against the previous step's result.

---

## Extending

To add a new skill tier or custom scope, extend `_can_read_tier` / `_can_write_tier` in `mojo/apps/assistant/services/skills.py`. The `_scope_filter` and `_scoped_queryset` helpers also need updating for any new scope type.

To store additional per-skill data, write to the `metadata` JSONField. The `detail` graph exposes it.
