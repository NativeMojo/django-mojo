"""
Tests for the LLM incident analysis feature:
- on_action_analyze publishes the correct job
- Rejects when LLM_HANDLER_API_KEY not set
- Rejects when analysis already in progress
- merge_incidents tool
- query_open_incidents tool
- Full analysis agent loop (mocked Claude)
"""
from testit import helpers as th
from unittest.mock import patch


def _claude_response(stop_reason, content):
    """Build a dict matching the Claude API response shape."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-20250514",
        "stop_reason": stop_reason,
        "content": content,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def _tool_use_block(tool_id, name, input_data):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": input_data}


def _text_block(text):
    return {"type": "text", "text": text}


def _cleanup():
    """Clean up test data before each test."""
    from mojo.apps.incident.models import Event, Incident, RuleSet
    from mojo.apps.jobs.models import Job
    Event.objects.filter(category__startswith="analyze_test").delete()
    Incident.objects.filter(category__startswith="analyze_test").delete()
    RuleSet.objects.filter(category__startswith="analyze_test").delete()
    Job.objects.filter(channel="default").delete()


@th.django_unit_test("Analyze action: publishes job with correct payload")
def test_analyze_action_publishes_job(opts):
    from mojo.apps.incident.models import Incident
    from mojo.apps.jobs.models import Job

    _cleanup()

    incident = Incident.objects.create(
        priority=8, state=0, status="new",
        category="analyze_test", scope="global",
        title="Test incident for analysis",
        source_ip="10.0.0.50",
    )

    with patch("mojo.helpers.settings.settings.get", side_effect=lambda k, d=None: "test-key" if k == "LLM_HANDLER_API_KEY" else d):
        result = incident.on_action_analyze(None)

    assert result["status"] is True, f"Expected status=True, got {result}"

    # Verify the job was published
    job = Job.objects.filter(
        func="mojo.apps.incident.handlers.llm_agent.execute_llm_analysis"
    ).order_by("-created").first()
    assert job is not None, "Expected analysis job to be published"
    assert job.payload["incident_id"] == incident.pk, f"Expected incident_id={incident.pk}, got {job.payload}"

    # Verify in-progress flag was set
    incident.refresh_from_db()
    assert incident.metadata.get("analysis_in_progress") is True, "Expected analysis_in_progress=True in metadata"


@th.django_unit_test("Analyze action: rejects when no API key")
def test_analyze_action_no_api_key(opts):
    from mojo.apps.incident.models import Incident

    _cleanup()

    incident = Incident.objects.create(
        priority=5, state=0, status="new",
        category="analyze_test_nokey", scope="global",
        title="No API key test",
    )

    with patch("mojo.helpers.settings.settings.get", side_effect=lambda k, d=None: None if k == "LLM_HANDLER_API_KEY" else d):
        result = incident.on_action_analyze(None)

    assert result["status"] is False, f"Expected status=False, got {result}"
    assert "not configured" in result["error"], f"Expected config error, got {result['error']}"


@th.django_unit_test("Analyze action: rejects when already in progress")
def test_analyze_action_already_in_progress(opts):
    from mojo.apps.incident.models import Incident

    _cleanup()

    incident = Incident.objects.create(
        priority=5, state=0, status="new",
        category="analyze_test_double", scope="global",
        title="Double-click test",
        metadata={"analysis_in_progress": True},
    )

    with patch("mojo.helpers.settings.settings.get", side_effect=lambda k, d=None: "test-key" if k == "LLM_HANDLER_API_KEY" else d):
        result = incident.on_action_analyze(None)

    assert result["status"] is False, f"Expected status=False, got {result}"
    assert "already in progress" in result["error"], f"Expected in-progress error, got {result['error']}"


@th.django_unit_test("merge_incidents tool: merges same-category incidents")
def test_merge_incidents_tool(opts):
    from mojo.apps.incident.models import Event, Incident
    from mojo.apps.incident.handlers.llm_agent import _tool_merge_incidents

    _cleanup()

    # Create target incident with events
    target = Incident.objects.create(
        priority=8, state=0, status="new",
        category="analyze_test_merge", scope="global",
        title="Target incident",
    )
    Event.objects.create(
        category="analyze_test_merge", level=8,
        title="Event 1", incident=target,
    )

    # Create source incidents with events
    source1 = Incident.objects.create(
        priority=6, state=0, status="new",
        category="analyze_test_merge", scope="global",
        title="Source incident 1",
    )
    Event.objects.create(
        category="analyze_test_merge", level=6,
        title="Event 2", incident=source1,
    )

    source2 = Incident.objects.create(
        priority=7, state=0, status="open",
        category="analyze_test_merge", scope="global",
        title="Source incident 2",
    )
    Event.objects.create(
        category="analyze_test_merge", level=7,
        title="Event 3", incident=source2,
    )

    result = _tool_merge_incidents({
        "target_incident_id": target.pk,
        "incident_ids": [source1.pk, source2.pk],
    })

    assert result["ok"] is True, f"Expected ok=True, got {result}"
    assert result["merged"] == 2, f"Expected 2 merged, got {result['merged']}"

    # All events should be on target now
    target.refresh_from_db()
    assert target.events.count() == 3, f"Expected 3 events on target, got {target.events.count()}"

    # Source incidents should be deleted
    assert not Incident.objects.filter(pk=source1.pk).exists(), "Source 1 should be deleted"
    assert not Incident.objects.filter(pk=source2.pk).exists(), "Source 2 should be deleted"


@th.django_unit_test("merge_incidents tool: skips resolved/ignored and wrong category")
def test_merge_incidents_skips_ineligible(opts):
    from mojo.apps.incident.models import Incident
    from mojo.apps.incident.handlers.llm_agent import _tool_merge_incidents

    _cleanup()

    target = Incident.objects.create(
        priority=8, state=0, status="new",
        category="analyze_test_skip", scope="global",
        title="Target",
    )

    # Resolved — should be skipped
    resolved = Incident.objects.create(
        priority=5, state=0, status="resolved",
        category="analyze_test_skip", scope="global",
        title="Resolved one",
    )

    # Wrong category — should be skipped
    wrong_cat = Incident.objects.create(
        priority=5, state=0, status="new",
        category="analyze_test_other", scope="global",
        title="Wrong category",
    )

    result = _tool_merge_incidents({
        "target_incident_id": target.pk,
        "incident_ids": [resolved.pk, wrong_cat.pk],
    })

    assert result["ok"] is True, f"Expected ok=True, got {result}"
    assert result["merged"] == 0, f"Expected 0 merged, got {result['merged']}"

    # Both should still exist
    assert Incident.objects.filter(pk=resolved.pk).exists(), "Resolved incident should still exist"
    assert Incident.objects.filter(pk=wrong_cat.pk).exists(), "Wrong category incident should still exist"


@th.django_unit_test("query_open_incidents tool: filters by status and category")
def test_query_open_incidents_tool(opts):
    from mojo.apps.incident.models import Incident
    from mojo.apps.incident.handlers.llm_agent import _tool_query_open_incidents

    _cleanup()

    # Create incidents with various statuses
    Incident.objects.create(
        priority=8, state=0, status="new",
        category="analyze_test_open", scope="global", title="New one",
    )
    Incident.objects.create(
        priority=6, state=0, status="open",
        category="analyze_test_open", scope="global", title="Open one",
    )
    Incident.objects.create(
        priority=5, state=0, status="resolved",
        category="analyze_test_open", scope="global", title="Resolved one",
    )
    Incident.objects.create(
        priority=7, state=0, status="new",
        category="analyze_test_open_other", scope="global", title="Different category",
    )

    # Query with category filter
    result = _tool_query_open_incidents({"category": "analyze_test_open"})
    assert len(result) == 2, f"Expected 2 open incidents, got {len(result)}"
    statuses = {r["status"] for r in result}
    assert statuses <= {"new", "open", "investigating"}, f"Got unexpected statuses: {statuses}"

    # Query without category filter — should include all open from both categories
    result_all = _tool_query_open_incidents({})
    open_test_ids = {r["id"] for r in result_all if r["category"].startswith("analyze_test_open")}
    assert len(open_test_ids) == 3, f"Expected 3 open incidents across categories, got {len(open_test_ids)}"


@th.django_unit_test("LLM analysis: full agent loop with merge and rule creation")
def test_llm_analysis_full_loop(opts):
    from mojo.apps.incident.models import Event, Incident, RuleSet
    from mojo.apps.incident.models.history import IncidentHistory
    from mojo.apps.jobs.models import Job
    from mojo.apps import jobs

    _cleanup()

    # Create target incident with events
    target = Incident.objects.create(
        priority=8, state=0, status="new",
        category="analyze_test_full", scope="global",
        title="SSH brute force from 10.0.0.77",
        source_ip="10.0.0.77",
        metadata={"analysis_in_progress": True},
    )
    Event.objects.create(
        category="analyze_test_full", level=8,
        title="SSH brute force", source_ip="10.0.0.77",
        incident=target,
    )

    # Create a related incident to merge
    related = Incident.objects.create(
        priority=7, state=0, status="new",
        category="analyze_test_full", scope="global",
        title="SSH brute force from 10.0.0.78",
        source_ip="10.0.0.78",
    )
    Event.objects.create(
        category="analyze_test_full", level=7,
        title="SSH brute force", source_ip="10.0.0.78",
        incident=related,
    )

    # Scripted Claude responses:
    # Turn 1: set investigating + query open incidents
    # Turn 2: merge related incident
    # Turn 3: create rule
    # Turn 4: resolve and summarize
    mock_responses = [
        _claude_response("tool_use", [
            _tool_use_block("t1", "update_incident", {
                "incident_id": target.pk,
                "status": "investigating",
                "note": "Starting analysis.",
            }),
            _tool_use_block("t2", "query_open_incidents", {
                "category": "analyze_test_full",
            }),
        ]),
        _claude_response("tool_use", [
            _tool_use_block("t3", "merge_incidents", {
                "target_incident_id": target.pk,
                "incident_ids": [related.pk],
            }),
        ]),
        _claude_response("tool_use", [
            _tool_use_block("t4", "create_rule", {
                "name": "Auto-block SSH brute force",
                "category": "analyze_test_full",
                "handler": "block://?ttl=3600",
                "bundle_by": 4,
                "bundle_minutes": 30,
                "reasoning": "Recurring SSH brute force pattern — block source IP.",
                "rules": [
                    {"field": "level", "operator": "gte", "value": "7"},
                ],
            }),
        ]),
        _claude_response("tool_use", [
            _tool_use_block("t5", "update_incident", {
                "incident_id": target.pk,
                "status": "resolved",
                "note": "Merged 1 incident, proposed auto-block rule.",
            }),
        ]),
        _claude_response("end_turn", [
            _text_block("Analysis complete. Merged 1 related incident. Proposed rule: auto-block SSH brute force."),
        ]),
    ]

    with patch("mojo.apps.incident.handlers.llm_agent._call_claude", side_effect=mock_responses):
        with patch("mojo.apps.incident.handlers.llm_agent._get_llm_api_key", return_value="test-key"):
            jobs.publish(
                "mojo.apps.incident.handlers.llm_agent.execute_llm_analysis",
                {"incident_id": target.pk},
                channel="default",
            )
            executed = th.run_pending_jobs(channel="default")

    assert executed >= 1, f"Expected at least 1 job executed, got {executed}"

    # Target incident should be resolved
    target.refresh_from_db()
    assert target.status == "resolved", f"Expected status=resolved, got {target.status}"

    # Analysis result in metadata
    assert target.metadata.get("llm_analysis") is not None, "Expected llm_analysis in metadata"
    assert "analysis_in_progress" in target.metadata, "Expected analysis_in_progress key"
    assert target.metadata["analysis_in_progress"] is False, "Expected analysis_in_progress=False after completion"

    # Related incident should be deleted (merged)
    assert not Incident.objects.filter(pk=related.pk).exists(), "Related incident should be merged (deleted)"

    # All events should be on target
    assert target.events.count() == 2, f"Expected 2 events on target, got {target.events.count()}"

    # A new ruleset should have been created (disabled)
    new_rule = RuleSet.objects.filter(category="analyze_test_full", name="Auto-block SSH brute force").first()
    assert new_rule is not None, "Expected new ruleset to be created"
    assert new_rule.metadata.get("disabled") is True, "Expected ruleset to be disabled"
    assert new_rule.metadata.get("llm_proposed") is True, "Expected ruleset to be LLM-proposed"

    # History should have analysis entries (at minimum the final summary)
    history = IncidentHistory.objects.filter(parent=target, kind="handler:llm")
    assert history.count() >= 1, f"Expected at least 1 LLM history entry, got {history.count()}"
