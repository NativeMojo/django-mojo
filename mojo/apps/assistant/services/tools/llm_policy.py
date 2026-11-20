"""AI policy domain — see and activate a deployed LLM safety policy.

A change to ``LLM_SAFETY_POLICY`` in the deployment file takes effect only once
an active superuser activates it (``llm_safety.activate_with_summary``); until
then every guarded call refuses ``policy_mixed``. These two tools let the
agent that changed the policy activate it itself and tell the owner, in a
short TL;DR, what the change does: features added or removed, models and
settings that moved, and limits raised or lowered.

A deliberate exception to "never easier through chat than the portal" (the
owner's decision): the Admin's **Activate deployed policy** asks for a fresh
sign-in, this tool does not, so an MCP execution grant runs it directly and a
proposal-only grant or the in-app chat makes an approval card. The activation
is bound to the policy summarized: ``hash`` must be the deployed hash, so a
card or a summary read before another deploy, or on a node with another
policy, cannot activate a different one. Active superusers only, re-checked at
call time.
"""

from mojo.apps.assistant import tool

from .cloud.common import can_system_admin, is_system_admin, refuse


RELAY = ("Tell the person who owns this installation what changed, using the "
         "tldr and the changes list, in plain words.")


def _superuser_refusal(user):
    if not is_system_admin(user):
        return refuse("The AI safety policy can only be changed by an active "
                      "superuser account.", "permission_denied")
    return None


def _unreadable(exc):
    return refuse(
        "The deployed AI policy cannot be read, so it cannot be activated: "
        f"{exc.code}. Fix the deployment file first.", exc.code)


def _summarize_activation(params, user):
    from mojo.apps.account.services import llm_safety

    try:
        change = llm_safety.policy_change(user)
    except Exception:
        return "Activate the AI safety policy now in the deployment file."
    wanted = str(params.get("hash") or "").strip()
    if wanted != change["hash"]:
        return ("Activate the AI safety policy now in the deployment file. The "
                "hash given is not the deployed one, so this will be refused.")
    return (f"Activate AI policy {change['hash'][:12]}. {change['tldr']}")[:500]


@tool(
    name="get_llm_policy_change",
    domain="ai_policy",
    permission="admin",
    description=(
        "Show whether the AI safety policy in the deployment file is active, "
        "and what activating it would change compared with the policy "
        "activated last: a one-line tldr plus a list of changes (features "
        "added or removed, models or settings changed, limits raised or "
        "lowered), and the deployed hash that activate_llm_policy needs. "
        "Read-only. Active superusers only."),
    input_schema={"type": "object", "properties": {}},
    authorize=can_system_admin,
)
def _tool_get_llm_policy_change(params, user):
    from mojo.apps.account.services import llm_safety

    denied = _superuser_refusal(user)
    if denied is not None:
        return denied
    try:
        change = llm_safety.policy_change(user)
    except llm_safety.LLMSafetyError as exc:
        return _unreadable(exc)
    return {
        "active": change["active"],
        "record_needed": change["record_needed"],
        "tldr": change["tldr"],
        "changes": change["changes"],
        "first_recorded": change["first"],
        "hash": change["hash"],
    }


@tool(
    name="activate_llm_policy",
    domain="ai_policy",
    permission="admin",
    mutates=True,
    summarize=_summarize_activation,
    description=(
        "Activate the AI safety policy now in the deployment file, so guarded "
        "AI calls stop refusing with policy_mixed after a policy change. Call "
        "get_llm_policy_change first and pass its hash; the activation is "
        "refused (policy_changed) if the deployed policy is no longer that one. "
        "Returns a tldr and the list of what the activation changed; you MUST "
        "then tell the installation's owner that tldr. Use it after deploying a "
        "policy change, inside the emergency-stop sequence the LLM safety "
        "runbook gives. Activating an already active policy changes nothing. "
        "Active superusers only."),
    input_schema={
        "type": "object",
        "properties": {
            "hash": {
                "type": "string",
                "description": "The deployed policy hash from get_llm_policy_change.",
            },
        },
        "required": ["hash"],
    },
    authorize=can_system_admin,
)
def _tool_activate_llm_policy(params, user):
    from mojo.apps.account.services import llm_safety

    denied = _superuser_refusal(user)
    if denied is not None:
        return denied
    wanted = str(params.get("hash") or "").strip()
    if not wanted:
        return refuse("Pass the hash get_llm_policy_change returned.",
                      "invalid_request")
    try:
        result = llm_safety.activate_with_summary(
            user, expected_hash=wanted, via="assistant")
    except llm_safety.LLMSafetyError as exc:
        return _unreadable(exc)
    except llm_safety.PolicyChangedError:
        return refuse(
            "The deployed AI policy is not the one that hash names (another "
            "deploy, or a node with a different policy). Nothing was activated. "
            "Read get_llm_policy_change again.", "policy_changed")
    return {
        "activated": True,
        "already_active": result["already_active"],
        "tldr": result["tldr"],
        "changes": result["changes"],
        "first_recorded": result["first"],
        "hash": result["hash"],
        "next": RELAY,
    }
