# Assistant Skill Discovery is Fundamentally Broken

**Type**: bug
**Status**: planned
**Date**: 2026-05-07
**Severity**: critical

## Description

The AI Assistant's skill discovery mechanism has multiple compounding failures that make skills effectively unusable. A user asking for "Group lock activity" cannot find a skill named "Group Lock Activity Report" — a near-exact match. The root cause is not a single bug but a flawed architecture: the system relies entirely on the LLM deciding to call `find_skill` with good keywords, the search itself is brittle, there is no fallback, and no skills are ever surfaced proactively.

## Context

Skills are one of the assistant's flagship features — reusable multi-step procedures that make the assistant more capable over time. If skills can't be reliably found and executed, the entire feature is dead weight. Users invest effort creating skills and then lose trust when the assistant can't find them. This is a "works in demo, fails in practice" situation.

## Problems Identified

### 1. No skill awareness at conversation start
The system prompt tells the LLM that skills exist and to use `find_skill` "when a user's request sounds like a stored procedure" (`agent.py:443`), but the LLM has **zero knowledge of what skills actually exist**. It doesn't know there's a "Group Lock Activity Report" skill, so it can't recognize that the user's request matches one. The LLM is guessing blind.

### 2. find_skills search is too brittle
`find_skills()` (`skills.py:139-175`) splits the query into keywords and does OR'd `icontains` across name, description, and triggers. This should have matched "Group lock activity" against "Group Lock Activity Report" (all three words match the name). If it didn't, possible causes:
- The LLM sent a reformulated query that dropped key words
- The skill's tier scoping filtered it out (wrong group context)
- `_scoped_queryset` excluded the skill due to permission logic

But even when the text search works, it's still fragile — it can't handle synonyms, abbreviations, or intent-based queries ("show me lock data for this week").

### 3. No fallback from find_skill to list_skills
When `find_skill` returns empty, the LLM gets `{"message": "No matching skills found", "results": []}` (`tools/skills.py:35`) and has no instruction to try `list_skills` as a fallback. The system prompt doesn't mention this pattern. The LLM gives up and asks the user for help — exactly backwards.

### 4. LLM decides IF and WHEN to search — unreliable
Skill discovery is entirely LLM-initiated. The system prompt says to use `find_skill` when something "sounds like a stored procedure," but most user requests don't sound like stored procedures — they sound like normal requests that happen to have a skill for them. The LLM has to guess that a skill might exist before it bothers looking.

### 5. auto_execute skills are never proactively matched
Skills with `auto_execute=True` are meant to run automatically when triggered, but there is no mechanism to match them against the user's message before the LLM loop starts. Auto-execute only works if the LLM happens to find the skill via `find_skill` and notices the flag.

## Fix Approach

Simple: inject all accessible skills into the system prompt at conversation start. The skill count is small, the LLM is already good at matching intent to descriptions — we're just not giving it the catalog. No complicated pre-matching, no semantic search, no fallback chains.

1. `_get_system_prompt()` queries all accessible skills (name, description, triggers, auto_execute flag) and appends a skill catalog to the `## Skills` section
2. The LLM sees what exists and naturally matches user intent to skill names/descriptions
3. `find_skill` becomes "load this skill's full step definitions so I can execute it" — not a blind search
4. System prompt instructions updated to tell the LLM: "Here are the available skills. When the user's request matches one, call `find_skill` with the skill name to load its steps and execute them."

## Acceptance Criteria

- All accessible skills (name, description, triggers, auto_execute) are listed in the system prompt at conversation start
- The LLM can match "Group lock activity" to "Group Lock Activity Report" without any tool call
- `find_skill` reliably loads step definitions when called with a skill name the LLM already knows about
- auto_execute skills are clearly marked in the catalog so the LLM knows to run them without confirmation
- No regression in conversations that don't involve skills (catalog section is small/absent when no skills exist)

## Investigation

**Likely root cause**: Architectural — skill discovery relies 100% on LLM judgment with no pre-matching, no skill awareness injection, and no fallback chain. The individual components (search, tool definitions, system prompt) each have gaps that compound into total failure.

**Confidence**: confirmed (by code analysis — the gaps are structural and visible in the code paths)

**Code path**:
- System prompt skills section: `services/agent.py:441-449` — no skill catalog injected
- Conversation startup: `services/agent.py:1086-1165` — no skill pre-matching
- `find_skills()`: `services/skills.py:139-175` — keyword-only, no fallback
- `find_skill` tool: `services/tools/skills.py:28-39` — returns empty with no suggestions
- `list_skills` tool: `services/tools/skills.py:139-148` — never called automatically

**Regression test**: not feasible — requires running LLM + database with skill fixtures

**Related files**:
- `mojo/apps/assistant/services/agent.py` — `_get_system_prompt()` needs to query and inject skill catalog
- `mojo/apps/assistant/services/skills.py` — needs `build_skill_catalog()`, `get_skill()`, `update_skill()` functions
- `mojo/apps/assistant/services/tools/skills.py` — `find_skill` tool reworked, new `update_skill` tool
- `mojo/apps/assistant/services/agent.py:441-449` — system prompt skills section needs rewrite

## Plan

**Status**: planned
**Planned**: 2026-05-07

### Objective

Inject a skill catalog into the system prompt so the LLM knows what skills exist, and add ID-based loading + partial updates so the LLM can explore and modify skills.

### Steps

1. `mojo/apps/assistant/services/skills.py` — Add `build_skill_catalog(user, group=None)`
   - Reuses `_scoped_queryset` for tier-scoped permission filtering
   - Returns markdown string listing all accessible active skills: ID, name, description, triggers, auto_execute flag
   - Returns `""` if skills disabled or no skills exist
   - Follows same pattern as `build_memory_prompt()` in memory.py

2. `mojo/apps/assistant/services/skills.py` — Add `get_skill(user, skill_id, group=None)`
   - Load a single skill by ID with permission check
   - Returns full detail dict (`_skill_to_detail`) or error
   - Used by the updated `find_skill` tool for ID-based loading

3. `mojo/apps/assistant/services/skills.py` — Add `update_skill(user, skill_id, group=None, **fields)`
   - Partial update by ID — only updates provided fields
   - Accepts any subset of: `name`, `description`, `triggers`, `steps`, `auto_execute`, `is_active`
   - Permission check via `_can_write_tier` (same as save_skill)
   - Validates only the fields being changed (reuses existing validators)
   - Name change checks uniqueness within scope

4. `mojo/apps/assistant/services/tools/skills.py` — Rework `find_skill` tool
   - Add optional `skill_id` param (integer) alongside existing `query` (string)
   - When `skill_id` provided: call `get_skill()` for exact load
   - When `query` provided: existing keyword search via `find_skills()`
   - Update description: "Load a skill's full details by ID, or search by keywords. Use skill_id when you know the skill from the catalog. Use query for keyword search."

5. `mojo/apps/assistant/services/tools/skills.py` — Add `update_skill` tool
   - `core=True`, `mutates=True`, domain `"skills"`, permission `"assistant"`
   - Input: `skill_id` (required), plus optional `name`, `description`, `triggers`, `steps`, `auto_execute`, `is_active`
   - Calls `update_skill()` service function
   - Description: "Update part of an existing skill. Pass only the fields you want to change."

6. `mojo/apps/assistant/services/agent.py:441-449` — Rewrite `## Skills` system prompt section
   - Replace static text with a `{skill_catalog}` placeholder
   - New instructions: "Here are the skills available to you. When a user's request matches a skill, call `find_skill` with the skill's ID to load its steps and execute them. If auto_execute is true, run without asking. Use `update_skill` to modify individual fields. Use `save_skill` to create new skills."
   - When no skills exist: "No skills stored yet. Users can teach you procedures with `save_skill`."

7. `mojo/apps/assistant/services/agent.py:574-593` — Update `_get_system_prompt()`
   - Import and call `build_skill_catalog(user, group)`
   - Inject catalog into the skills section of the prompt (before memory injection)
   - If skills disabled or no user: skip injection

### Design Decisions

- **Catalog in system prompt, not a tool call**: Avoids a round-trip on every conversation. Max 70 skills across tiers (20 user + 30 group + 20 global) at ~50 tokens each = ~3500 tokens worst case.
- **Summary format in catalog (no steps)**: ID, name, description, triggers, auto_execute. Steps loaded via `find_skill` when needed. Keeps prompt lean.
- **Overload `find_skill` with `skill_id`**: KISS — one tool the LLM already knows, two ways to identify a skill. No new tool to learn for loading.
- **Separate `update_skill` tool**: Partial updates are a different operation from create/upsert. `save_skill` stays as the full-create tool, `update_skill` handles edits. Clear intent.
- **Reuse `_scoped_queryset` and `_can_write_tier`**: No new permission paths. Same security model.
- **`find_skill` keeps keyword search**: Still useful as a fallback if the LLM wants to search beyond the catalog (e.g., a skill was created mid-conversation).

### Edge Cases

- **No skills exist**: Catalog section is minimal ("No skills stored yet"). No wasted tokens.
- **Skills disabled**: `build_skill_catalog()` returns `""`. System prompt omits catalog.
- **Mid-conversation skill creation**: New skill won't appear in catalog until next conversation. Acceptable — LLM already knows about it from the `save_skill` response.
- **Name change collision**: `update_skill` with a new name checks uniqueness in scope. Returns error if name already taken.
- **Partial steps update**: `update_skill` replaces the full `steps` array (not individual steps). LLM must load current steps, modify, and send the full list. This is simpler than step-level indexing.
- **Custom system prompt**: If `LLM_ADMIN_SYSTEM_PROMPT` is set, catalog is appended (same as memory behavior).
- **No user context**: `_get_system_prompt(user=None)` skips catalog injection.

### Testing

- `build_skill_catalog` output format and tier scoping → `tests/test_assistant/skills.py`
- `get_skill` by ID with permission checks → `tests/test_assistant/skills.py`
- `update_skill` partial updates (each field individually, combo) → `tests/test_assistant/skills.py`
- `update_skill` permission denied for wrong tier → `tests/test_assistant/skills.py`
- `update_skill` name collision detection → `tests/test_assistant/skills.py`
- `find_skill` tool with `skill_id` param → `tests/test_assistant/skills.py`
- System prompt contains catalog when skills exist, omits when empty → `tests/test_assistant/skills.py`

### Docs

- `docs/django_developer/assistant/skills.md` — Update discovery section: catalog injection, `find_skill` dual-mode, new `update_skill` tool
- `docs/web_developer/assistant/` — Update if REST skill endpoints are documented there
