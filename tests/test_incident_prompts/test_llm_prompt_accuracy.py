"""
Regression for maestro item #1122: docs and the incident triage agent's
prompts claimed an automatic ingestion-time event dedup
(`INCIDENT_DEDUP_WINDOW_SECONDS`, `metadata.dedup_count`) that was never
implemented. There is deliberately NO ingestion dedup — every reported
occurrence is its own Event row, because flood detection counts rows
(RuleSet trigger_count/trigger_window/retrigger_every, incident event_count,
metrics, the LLM's query_event_counts). These tests pin that the false claim
stays out of the module source and out of the docs that carried it.
"""
from testit import helpers as th

# Doc files that carried the false dedup claim (repo-root relative).
DOC_SITES = [
    "docs/django_developer/security/README.md",
    "docs/web_developer/security/README.md",
    "docs/web_developer/logging/incidents.md",
]

# Tokens that only exist if the unimplemented feature is being claimed again.
# Deliberately narrow: the security README's "Tool Deduplication" section
# (ticket-layer dedup) is real and legitimately uses the word "deduplication".
FORBIDDEN_TOKENS = ["INCIDENT_DEDUP_WINDOW_SECONDS", "dedup_count"]


@th.django_unit_test("llm_agent source claims no ingestion dedup")
def test_llm_agent_source_has_no_ingestion_dedup_claim(opts):
    import inspect
    from mojo.apps.incident.handlers import llm_agent

    source = inspect.getsource(llm_agent)
    th.assert_true(
        "deduplicated at ingestion" not in source,
        "llm_agent claims events are deduplicated at ingestion — that "
        "mechanism does not exist (maestro #1122); rows ARE the volume signal")
    th.assert_true(
        "dedup_count" not in source,
        "llm_agent references metadata.dedup_count, which nothing writes — "
        "removed by maestro #1122; do not reintroduce without implementing "
        "ingestion dedup end-to-end (it would starve row-count detection)")
    th.assert_true(
        "bundle_by" in llm_agent.SYSTEM_PROMPT,
        "SYSTEM_PROMPT lost its RuleSet bundling guidance (bundle_by) — the "
        "#1122 cleanup must not delete the real noise-control instructions")


@th.django_unit_test("llm_agent prompt and schema describe operative thresholds")
def test_llm_agent_threshold_contract(opts):
    from mojo.apps.incident.handlers import llm_agent
    from mojo.apps.incident.models.rule import BundleBy

    create_rule = next(tool for tool in llm_agent.TOOLS if tool["name"] == "create_rule")
    properties = create_rule["input_schema"]["properties"]
    th.assert_eq(
        properties["min_count"].get("minimum"), 1,
        "create_rule.min_count must advertise the RuleSet field's positive minimum")
    th.assert_eq(
        properties["window_minutes"].get("minimum"), 1,
        "create_rule.window_minutes must advertise the RuleSet field's positive minimum")
    th.assert_true(
        "RuleSet.trigger_count" in properties["min_count"]["description"],
        "create_rule.min_count must name its canonical RuleSet.trigger_count mapping")
    th.assert_true(
        "RuleSet.trigger_window" in properties["window_minutes"]["description"],
        "create_rule.window_minutes must name its canonical RuleSet.trigger_window mapping")
    th.assert_eq(
        properties["bundle_by"].get("enum"),
        [value for value, _label in BundleBy.CHOICES],
        "create_rule.bundle_by enum must stay aligned with RuleSet bundle choices")

    for prompt_name in ("SYSTEM_PROMPT", "ANALYSIS_PROMPT"):
        prompt = getattr(llm_agent, prompt_name)
        th.assert_true(
            "enforced thresholds" in prompt,
            f"{prompt_name} must state that proposed thresholds are enforced")
        th.assert_true(
            "bundle_minutes" in prompt and "window_minutes" in prompt,
            f"{prompt_name} must explain the bundle/threshold reachability constraint")


@th.django_unit_test("incident docs claim no ingestion dedup")
def test_docs_have_no_ingestion_dedup_claim(opts):
    from pathlib import Path
    import mojo

    repo_root = Path(mojo.__file__).resolve().parent.parent
    for rel_path in DOC_SITES:
        doc = repo_root / rel_path
        th.assert_true(
            doc.is_file(),
            f"{rel_path} not found at {doc} — if the doc moved, update "
            "DOC_SITES in this test so the #1122 regression guard follows it")
        text = doc.read_text()
        for token in FORBIDDEN_TOKENS:
            th.assert_true(
                token not in text,
                f"{rel_path} claims the unimplemented ingestion dedup again "
                f"({token}) — see maestro #1122: there is no ingestion dedup; "
                "noise control is bundling + trigger thresholds + opt-in "
                "report_event_suppressed + pruning")
