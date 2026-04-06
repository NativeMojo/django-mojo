"""Planning domain tools — create and update multi-step execution plans."""
import uuid
from mojo.apps.assistant import tool


@tool(
    name="create_plan",
    domain="planning",
    permission="view_admin",
    core=True,
    description=(
        "Create a multi-step plan for complex requests that require 3+ tool calls "
        "across different areas. The plan is shown to the user as a progress tracker.\n\n"
        "Each step can optionally include a tool name and input for parallel execution. "
        "Steps marked parallel=true with a tool and tool_input will be executed concurrently "
        "by the system — you do not need to call them individually.\n\n"
        "Steps without a tool field (like a final synthesis step) run sequentially after "
        "all parallel steps complete — you handle these yourself.\n\n"
        "Do NOT create a plan for simple single-tool queries."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short title for the plan (e.g. 'Security Audit (24h)').",
            },
            "steps": {
                "type": "array",
                "description": "List of plan steps.",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "What this step does.",
                        },
                        "parallel": {
                            "type": "boolean",
                            "description": "If true, this step can run concurrently with other parallel steps. Default false.",
                        },
                        "tool": {
                            "type": "string",
                            "description": "Tool name to execute for this step. Only for parallel steps.",
                        },
                        "tool_input": {
                            "type": "object",
                            "description": "Input parameters for the tool. Only used when tool is specified.",
                        },
                    },
                    "required": ["description"],
                },
            },
        },
        "required": ["title", "steps"],
    },
)
def _tool_create_plan(params, user):
    plan_id = str(uuid.uuid4())
    title = params.get("title", "Plan")
    raw_steps = params.get("steps", [])

    if not raw_steps:
        return {"error": "Plan must have at least one step"}

    if len(raw_steps) > 20:
        return {"error": "Plan cannot have more than 20 steps"}

    steps = []
    for i, step in enumerate(raw_steps, 1):
        s = {
            "id": i,
            "description": step.get("description", ""),
            "status": "pending",
            "summary": None,
            "parallel": step.get("parallel", False),
        }
        if step.get("tool"):
            s["tool"] = step["tool"]
            s["tool_input"] = step.get("tool_input", {})
        steps.append(s)

    plan = {
        "plan_id": plan_id,
        "title": title,
        "steps": steps,
    }

    # Plan is stored in conversation metadata by the agent loop (meta-tool handler)
    return plan


@tool(
    name="update_plan",
    domain="planning",
    permission="view_admin",
    core=True,
    description=(
        "Update the status of a plan step. Call this before starting a step "
        "(status='in_progress') and after completing it (status='done' with a summary).\n\n"
        "Statuses: pending, in_progress, done, skipped.\n"
        "The progress is shown to the user in real time."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "step_id": {
                "type": "integer",
                "description": "The step ID to update.",
            },
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "done", "skipped"],
                "description": "New status for the step.",
            },
            "summary": {
                "type": "string",
                "description": "Brief summary of what was found/done. Include when marking done.",
            },
        },
        "required": ["step_id", "status"],
    },
)
def _tool_update_plan(params, user):
    # The actual plan update is handled by the agent loop (meta-tool handler)
    # which has access to the conversation. This handler just validates input
    # and returns the update for the agent loop to apply.
    step_id = params.get("step_id")
    status = params.get("status")
    summary = params.get("summary")

    if not step_id or not status:
        return {"error": "step_id and status are required"}

    valid_statuses = {"pending", "in_progress", "done", "skipped"}
    if status not in valid_statuses:
        return {"error": f"Invalid status '{status}'. Must be one of: {', '.join(sorted(valid_statuses))}"}

    return {
        "step_id": step_id,
        "status": status,
        "summary": summary,
        "updated": True,
    }
