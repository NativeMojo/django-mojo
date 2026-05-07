"""Skill tools — find, save, list, and delete learned skills."""
from mojo.apps.assistant import tool


@tool(
    name="find_skill",
    domain="skills",
    permission="assistant",
    core=True,
    description=(
        "Load a skill's full details (including steps) by ID, or search by "
        "keywords. Use skill_id when you know the skill from the catalog. "
        "Use query for keyword search. Returns full step definitions so "
        "you can replay them."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "integer",
                "description": "Load a specific skill by its ID (from the skill catalog).",
            },
            "query": {
                "type": "string",
                "description": "Search keywords (e.g., 'rebuild sales reports'). Used when skill_id is not provided.",
            },
        },
    },
)
def _tool_find_skill(params, user):
    from mojo.apps.assistant.services.skills import find_skills, get_skill

    group = getattr(user, "_assistant_group", None)
    skill_id = params.get("skill_id")

    if skill_id is not None:
        if not isinstance(skill_id, int):
            return {"error": "skill_id must be an integer"}
        result = get_skill(user, skill_id, group=group)
        if "error" in result:
            return result
        return {
            "message": f"Loaded skill '{result['name']}'. Review the steps and execute them if relevant.",
            "skill": result,
        }

    query = params.get("query", "")
    results = find_skills(user, query, group=group)
    if not results:
        return {"message": "No matching skills found.", "results": []}
    return {
        "message": f"Found {len(results)} matching skill(s). Review the steps and execute them if relevant.",
        "results": results,
    }


@tool(
    name="save_skill",
    domain="skills",
    permission="assistant",
    core=True,
    mutates=True,
    description=(
        "Save a new skill or update an existing one. A skill is a reusable "
        "multi-step procedure with trigger phrases. When the user teaches you "
        "a procedure and says to remember it, save it as a skill.\n\n"
        "Each step must have:\n"
        "- tool: the tool name to call\n"
        "- description: what this step does\n"
        "- params: parameters to pass to the tool (optional)\n"
        "- condition: when to execute this step, e.g., 'previous_step.count > 0' (optional)\n\n"
        "If a skill with the same name already exists in the same scope, it is updated."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tier": {
                "type": "string",
                "enum": ["global", "user", "group"],
                "description": "Scope: global (all users), user (personal), group (team).",
            },
            "name": {
                "type": "string",
                "description": "Short, descriptive name (e.g., 'rebuild sales reports').",
            },
            "description": {
                "type": "string",
                "description": "What the skill does — 1-2 sentences.",
            },
            "triggers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Phrases that should trigger this skill (e.g., ['rebuild sales reports', 'regenerate monthly reports']).",
            },
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "description": "Tool name to call."},
                        "params": {"type": "object", "description": "Parameters for the tool call."},
                        "condition": {"type": "string", "description": "Condition for executing this step."},
                        "description": {"type": "string", "description": "What this step does."},
                    },
                    "required": ["tool", "description"],
                },
                "description": "Ordered list of steps to execute.",
            },
            "auto_execute": {
                "type": "boolean",
                "description": "If true, execute without asking for confirmation when matched. Default false.",
            },
        },
        "required": ["tier", "name", "description", "steps"],
    },
)
def _tool_save_skill(params, user):
    from mojo.apps.assistant.services.skills import save_skill

    group = getattr(user, "_assistant_group", None)
    return save_skill(
        user,
        tier=params["tier"],
        name=params["name"],
        description=params["description"],
        triggers=params.get("triggers", []),
        steps=params["steps"],
        group=group,
        auto_execute=params.get("auto_execute", False),
    )


@tool(
    name="list_skills",
    domain="skills",
    permission="assistant",
    core=True,
    description=(
        "List all learned skills accessible to you, grouped by tier "
        "(global, user, group). Returns summaries without step details. "
        "Use find_skill to get full step definitions for a specific skill."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tier": {
                "type": "string",
                "enum": ["global", "user", "group"],
                "description": "Filter by tier. Omit to see all tiers.",
            },
        },
    },
)
def _tool_list_skills(params, user):
    from mojo.apps.assistant.services.skills import list_skills

    group = getattr(user, "_assistant_group", None)
    tier = params.get("tier")
    result = list_skills(user, group=group, tier=tier)
    if not result:
        return {"message": "No skills stored"}
    total = sum(len(v) for v in result.values())
    return {"message": f"{total} skill(s) found", "skills": result}


@tool(
    name="update_skill",
    domain="skills",
    permission="assistant",
    core=True,
    mutates=True,
    description=(
        "Update part of an existing skill. Pass only the fields you want to "
        "change — everything else stays the same. Use find_skill first to "
        "see the current definition if needed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "integer",
                "description": "The ID of the skill to update.",
            },
            "name": {
                "type": "string",
                "description": "New name for the skill.",
            },
            "description": {
                "type": "string",
                "description": "New description.",
            },
            "triggers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Replacement trigger phrases (replaces entire list).",
            },
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "description": "Tool name to call."},
                        "params": {"type": "object", "description": "Parameters for the tool call."},
                        "condition": {"type": "string", "description": "Condition for executing this step."},
                        "description": {"type": "string", "description": "What this step does."},
                    },
                    "required": ["tool", "description"],
                },
                "description": "Replacement steps (replaces entire list).",
            },
            "auto_execute": {
                "type": "boolean",
                "description": "Whether to execute without confirmation.",
            },
            "is_active": {
                "type": "boolean",
                "description": "Enable or disable the skill.",
            },
        },
        "required": ["skill_id"],
    },
)
def _tool_update_skill(params, user):
    from mojo.apps.assistant.services.skills import update_skill

    group = getattr(user, "_assistant_group", None)
    skill_id = params.pop("skill_id")
    if not isinstance(skill_id, int):
        return {"error": "skill_id must be an integer"}
    return update_skill(user, skill_id, group=group, **params)


@tool(
    name="delete_skill",
    domain="skills",
    permission="assistant",
    core=True,
    mutates=True,
    description=(
        "Delete a learned skill by its ID. You must be the skill owner or "
        "an admin. Use list_skills first to find the skill ID."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "integer",
                "description": "The ID of the skill to delete.",
            },
        },
        "required": ["skill_id"],
    },
)
def _tool_delete_skill(params, user):
    from mojo.apps.assistant.services.skills import delete_skill

    return delete_skill(user, params["skill_id"])
