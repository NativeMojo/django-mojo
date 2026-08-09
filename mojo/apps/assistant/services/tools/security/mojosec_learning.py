"""Proposal-only MojoSec learning tool; it cannot touch live policy."""

from mojo.apps.assistant import tool


@tool(
    name="propose_mojosec_policy",
    domain="security",
    permission="manage_security",
    mutates=True,
    description=(
        "Create an immutable draft MojoSec detector-policy proposal for human review. "
        "This never activates RuleSet policy, dispatches a handler, evaluates live events, "
        "or performs any security action. Content accepts only allowlisted detector kinds "
        "and fixed count/severity predicates."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string", "maxLength": 500},
            "content": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "schema": {"type": "string", "enum": ["mojosec.policy-proposal.v1"]},
                    "detectors": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 24,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "kind": {"type": "string"},
                                "decision": {"type": "string", "enum": ["flag", "ignore"]},
                                "minimum_count": {"type": "integer", "minimum": 1, "maximum": 10000},
                                "minimum_severity": {
                                    "type": "string",
                                    "enum": ["info", "warning", "high", "critical"],
                                },
                            },
                            "required": ["kind", "decision"],
                        },
                    },
                },
                "required": ["schema", "detectors"],
            },
        },
        "required": ["content"],
    },
)
def _tool_propose_mojosec_policy(params, user):
    from mojo.apps.incident.services import mojosec_learning

    try:
        proposal = mojosec_learning.create_policy_proposal(
            user, params.get("content"), summary=params.get("summary", ""), status="draft")
    except (mojosec_learning.MojoSecLearningError, ValueError) as err:
        return {"error": str(err)}
    return {
        "proposal_id": proposal.pk,
        "lineage_id": str(proposal.lineage_id),
        "revision": proposal.revision,
        "status": proposal.status,
        "content_digest": proposal.content_digest,
        "non_executable": True,
        "next_step": "A human security operator may explicitly replay or shadow-evaluate it.",
    }
