"""Real job-history rows through the Assistant handlers and MCP dispatch."""
import uuid
from datetime import timedelta

import ujson

from testit import helpers as th


VIEWER_EMAIL = "job-history-viewer@testit.example"
DENIED_EMAIL = "job-history-denied@testit.example"
CLIENT_ID = "testit-job-history-client"
CHANNEL = "testit-job-history"
RESOURCE = "https://job-history.testit.example/api/assistant/mcp"


@th.django_unit_setup()
def setup_job_history(opts):
    from django.db.models import JSONField, Value
    from mojo.apps.account.models import OAuthClient, User
    from mojo.apps.account.services.oauth_server import tokens
    from mojo.apps.jobs.models import Job, JobEvent, JobLog
    from mojo.helpers import dates

    User.objects.filter(email__in=[VIEWER_EMAIL, DENIED_EMAIL]).delete()
    OAuthClient.objects.filter(client_id=CLIENT_ID).delete()
    Job.objects.filter(channel=CHANNEL).delete()

    opts.history_viewer = User.objects.create_user(
        username=VIEWER_EMAIL, email=VIEWER_EMAIL, password="TestPass1!")
    opts.history_denied = User.objects.create_user(
        username=DENIED_EMAIL, email=DENIED_EMAIL, password="TestPass1!")
    for user in (opts.history_viewer, opts.history_denied):
        user.add_permission("assistant")
        user.add_permission("view_admin")
    opts.history_viewer.add_permission("view_jobs")
    client = OAuthClient.objects.create(
        client_id=CLIENT_ID, kind="dcr", client_name="Job history regression",
        redirect_uris=["http://127.0.0.1:8500/cb"])
    opts.history_grant = tokens.create_grant(
        opts.history_viewer, client, ["mcp"], RESOURCE, 1700000000)
    opts.history_denied_grant = tokens.create_grant(
        opts.history_denied, client, ["mcp"], RESOURCE, 1700000000)

    opts.history_jobs = {
        name: Job.objects.create(id=uuid.uuid4().hex, channel=CHANNEL,
                                 func="testit.job_history", status="failed")
        for name in ("detail", "many", "empty", "falsey")
    }
    detail = opts.history_jobs["detail"]
    opts.history_event = JobEvent.objects.create(
        job=detail, channel=CHANNEL, event="failed",
        details={"z": [False, 0], "error": "échec", "context": {"b": 2, "a": 1}})
    opts.history_long_event = JobEvent.objects.create(
        job=detail, channel=CHANNEL, event="retry", details={"error": "é" * 600})
    opts.history_log = JobLog.objects.create(
        job=detail, channel=CHANNEL, kind="error", message="Settlement failed")
    opts.history_long_log = JobLog.objects.create(
        job=detail, channel=CHANNEL, kind="warn", message="é" * 600)

    # Set distinct creation times so ordering is independent of insert speed;
    # event occurrence times deliberately disagree with creation order.
    start = dates.subtract(days=1)
    opts.history_event_ids = []
    opts.history_log_ids = []
    for index in range(55):
        event = JobEvent.objects.create(
            job=opts.history_jobs["many"], channel=CHANNEL,
            event="running", details={"index": index})
        log = JobLog.objects.create(
            job=opts.history_jobs["many"], channel=CHANNEL,
            kind="info", message=f"Line {index}")
        created = start + timedelta(seconds=index)
        JobEvent.objects.filter(pk=event.pk).update(
            created=created, at=start - timedelta(seconds=index))
        JobLog.objects.filter(pk=log.pk).update(created=created)
        opts.history_event_ids.insert(0, event.pk)
        opts.history_log_ids.insert(0, log.pk)

    opts.history_falsey = {}
    for value, expected in (({}, "{}"), ([], "[]"), (False, "false"),
                            (0, "0"), ("", '""'), (None, "null")):
        # Value(None, JSONField()) stores JSON null, not SQL NULL.
        event = JobEvent.objects.create(
            job=opts.history_jobs["falsey"], channel=CHANNEL, event="failed",
            details=Value(value, output_field=JSONField()))
        opts.history_falsey[event.pk] = expected


def _mcp_call(opts, name, denied=False, job_name="detail"):
    from mojo.apps.assistant.mcp import server
    from mojo.apps.assistant.services import agent

    user = opts.history_denied if denied else opts.history_viewer
    grant = opts.history_denied_grant if denied else opts.history_grant
    request = th.get_mock_request(user=user, path="/api/assistant/mcp", method="POST")
    request.oauth_grant = grant
    reports = []

    def report(details, **kwargs):
        reports.append({"details": details, **kwargs})

    status, payload = server.handle(
        ujson.dumps({"jsonrpc": "2.0", "id": 8955, "method": "tools/call",
                     "params": {"name": name, "arguments": {
                         "job_id": opts.history_jobs[job_name].pk}}}),
        user, grant, agent._build_request_meta(request), "testit-job-history",
        hide_infrastructure=False, _reporter=report)
    assert status == 200, f"Valid tools/call must return HTTP 200: {payload}"
    assert payload.get("id") == 8955, f"Response must preserve request ID: {payload}"
    assert "result" in payload, f"Expected a tool result: {payload}"
    return payload["result"], reports


@th.tier("bug")
@th.django_unit_test()
def test_event_history_uses_real_fields_and_bounded_details(opts):
    from mojo.apps.assistant.services.tools.jobs import _tool_query_job_events

    rows = _tool_query_job_events(
        {"job_id": opts.history_jobs["detail"].pk}, opts.history_viewer)
    by_id = {row["id"]: row for row in rows}
    event = opts.history_event
    assert by_id[event.pk] == {
        "id": event.pk, "job_id": event.job_id, "kind": "failed",
        "message": '{"context":{"a":1,"b":2},"error":"échec","z":[false,0]}',
        "created": str(event.created),
    }, f"Events must retain their actual type and compact sorted details: {rows}"
    preview = by_id[opts.history_long_event.pk]["message"]
    assert preview == ('{"error":"' + "é" * 600 + '"}')[:500], \
        f"Event preview must be exactly the first 500 Unicode characters: {preview!r}"
    assert len(rows) == 2, f"Other jobs' events must not leak: {rows}"


@th.tier("bug")
@th.django_unit_test()
def test_log_history_uses_kind_as_level_and_bounds_messages(opts):
    from mojo.apps.assistant.services.tools.jobs import _tool_query_job_logs

    rows = _tool_query_job_logs(
        {"job_id": opts.history_jobs["detail"].pk}, opts.history_viewer)
    by_id = {row["id"]: row for row in rows}
    log = opts.history_log
    assert by_id[log.pk] == {
        "id": log.pk, "job_id": log.job_id, "level": "error",
        "message": "Settlement failed", "created": str(log.created),
    }, f"Log severity must come from the real kind field: {rows}"
    long_row = by_id[opts.history_long_log.pk]
    assert long_row["level"] == "warn", f"Keep the stored severity: {long_row}"
    assert long_row["message"] == "é" * 500, "Log messages must retain their 500-character cap"
    assert len(rows) == 2, f"Other jobs' logs must not leak: {rows}"


@th.tier("bug")
@th.django_unit_test()
def test_event_history_preserves_falsey_json_values(opts):
    from mojo.apps.assistant.services.tools.jobs import _tool_query_job_events

    rows = _tool_query_job_events(
        {"job_id": opts.history_jobs["falsey"].pk}, opts.history_viewer)
    assert {row["id"]: row["message"] for row in rows} == opts.history_falsey, \
        f"Empty containers, false, zero, empty string and JSON null must survive: {rows}"


@th.tier("bug")
@th.django_unit_test()
def test_empty_job_histories(opts):
    from mojo.apps.assistant.services.tools.jobs import (
        _tool_query_job_events, _tool_query_job_logs)

    for handler in (_tool_query_job_events, _tool_query_job_logs):
        rows = handler({"job_id": opts.history_jobs["empty"].pk}, opts.history_viewer)
        assert rows == [], f"An existing job without history must return []: {rows}"
    for name in ("query_job_events", "query_job_logs"):
        result, reports = _mcp_call(opts, name, job_name="empty")
        assert result["isError"] is False, f"Empty history is a successful MCP result: {result}"
        assert ujson.loads(result["content"][0]["text"]) == [], f"MCP must preserve []: {result}"
        assert "structuredContent" not in result, "MCP list results are text-only"
        assert reports == [], f"Empty reads must not emit error incidents: {reports}"


def _assert_history_bounds(opts, handler, ids):
    for limit, expected_count in ((None, 50), (3, 3), (50, 50), (100, 50), (0, 0)):
        params = {"job_id": opts.history_jobs["many"].pk}
        if limit is not None:
            params["limit"] = limit
        rows = handler(params, opts.history_viewer)
        assert [row["id"] for row in rows] == ids[:expected_count], \
            f"limit={limit} must return newest created rows within the 50 cap: {rows}"
        assert all(row["job_id"] == params["job_id"] for row in rows), \
            f"limit={limit} must not include another job's history: {rows}"


@th.tier("bug")
@th.django_unit_test()
def test_event_history_order_isolation_and_limits(opts):
    from mojo.apps.assistant.services.tools.jobs import _tool_query_job_events

    _assert_history_bounds(opts, _tool_query_job_events, opts.history_event_ids)


@th.tier("bug")
@th.django_unit_test()
def test_log_history_order_isolation_and_limits(opts):
    from mojo.apps.assistant.services.tools.jobs import _tool_query_job_logs

    _assert_history_bounds(opts, _tool_query_job_logs, opts.history_log_ids)


def _assert_mcp_history(opts, name, expected_id, field, expected_value):
    result, reports = _mcp_call(opts, name)
    assert result["isError"] is False, f"Populated history must succeed: {result}; reports={reports}"
    assert "structuredContent" not in result, "MCP arrays must remain text-only results"
    assert len(result["content"]) == 1 and result["content"][0]["type"] == "text", \
        f"Expected one text content block: {result}"
    rows = ujson.loads(result["content"][0]["text"])
    assert isinstance(rows, list) and len(rows) == 2, f"Expected both real history rows: {rows}"
    row = next(row for row in rows if row["id"] == expected_id)
    assert row[field] == expected_value, f"MCP must expose the real handler's diagnostic value: {row}"
    assert reports == [], f"Successful history reads must not report internal errors: {reports}"


@th.tier("bug")
@th.django_unit_test()
def test_mcp_event_history_uses_unmocked_handler(opts):
    _assert_mcp_history(
        opts, "query_job_events", opts.history_event.pk, "message",
        '{"context":{"a":1,"b":2},"error":"échec","z":[false,0]}')


@th.tier("bug")
@th.django_unit_test()
def test_mcp_log_history_uses_unmocked_handler(opts):
    _assert_mcp_history(opts, "query_job_logs", opts.history_log.pk, "level", "error")


@th.tier("bug")
@th.django_unit_test()
def test_mcp_histories_require_view_jobs(opts):
    for name in ("query_job_events", "query_job_logs"):
        result, reports = _mcp_call(opts, name, denied=True)
        assert result["isError"] is True, f"Missing view_jobs must deny {name}: {result}"
        assert ujson.loads(result["content"][0]["text"]) == {
            "error": f"Permission denied. You need 'view_jobs' to use {name}."
        }, f"Denied callers must see no history rows: {result}"
        assert any(report["category"] == "assistant:permission_denied" for report in reports), \
            f"Permission denial must retain its admin diagnostic trail: {reports}"
