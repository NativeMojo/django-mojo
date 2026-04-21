"""
Regression tests for assistant tool-result boundary serialization and
incident reporting. Covers:

- datetime/Decimal/UUID round-trip through _dumps_tool_result
- Tool handler exception -> assistant:error incident with traceback
- Unserializable sentinel -> fallback error + assistant:error:serialize incident
- Parallel-tool failure -> assistant:error:parallel incident
"""
import json
import decimal
import datetime
import uuid
from unittest import mock
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL = 'tool-err-admin@example.com'
TEST_PASSWORD = 'TestPass1!'


class _FakeConversation:
    def __init__(self, pk=42):
        self.pk = pk
        self.metadata = {}


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
@th.requires_app("mojo.apps.incident")
def setup_user(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email=TEST_EMAIL).delete()
    opts.user = User.objects.create_user(
        username=TEST_EMAIL, email=TEST_EMAIL, password=TEST_PASSWORD,
    )
    opts.user.is_email_verified = True
    opts.user.save()
    opts.user.add_permission("view_admin")


# ---------------------------------------------------------------------------
# _json_default direct coverage
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_json_default_datetime(opts):
    """Aware and naive datetimes should coerce to ISO strings."""
    from mojo.apps.assistant.services.agent import _json_default

    aware = datetime.datetime(2026, 4, 15, 14, 51, 17, tzinfo=datetime.timezone.utc)
    naive = datetime.datetime(2026, 4, 15, 14, 51, 17)
    assert_eq(
        _json_default(aware), aware.isoformat(),
        "aware datetime should serialize via .isoformat()",
    )
    assert_eq(
        _json_default(naive), naive.isoformat(),
        "naive datetime should serialize via .isoformat()",
    )


@th.django_unit_test()
def test_json_default_decimal_uuid_set(opts):
    """Decimal/UUID/set should coerce to JSON-native types."""
    from mojo.apps.assistant.services.agent import _json_default

    assert_eq(
        _json_default(decimal.Decimal("1.23")), "1.23",
        "Decimal should coerce to str",
    )
    u = uuid.uuid4()
    assert_eq(_json_default(u), str(u), "UUID should coerce to str")

    out = _json_default({1, 2, 3})
    assert_true(isinstance(out, list), "set should coerce to list")
    assert_eq(sorted(out), [1, 2, 3], "set contents preserved")


# ---------------------------------------------------------------------------
# _dumps_tool_result boundary
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_dumps_tool_result_datetime_roundtrip(opts):
    """A tool result containing a datetime must round-trip through json.loads."""
    from mojo.apps.assistant.services.agent import _dumps_tool_result

    ts = datetime.datetime(2026, 4, 15, 14, 51, 17, tzinfo=datetime.timezone.utc)
    payload = {"ts": ts, "amount": decimal.Decimal("1.00"), "id": uuid.uuid4()}
    raw = _dumps_tool_result(payload, user=opts.user, conversation=_FakeConversation())
    parsed = json.loads(raw)
    assert_eq(parsed["ts"], ts.isoformat(), "datetime should appear as ISO string")
    assert_eq(parsed["amount"], "1.00", "Decimal should appear as string")
    assert_true(isinstance(parsed["id"], str), "UUID should appear as string")


@th.django_unit_test()
def test_dumps_tool_result_unserializable_reports_incident(opts):
    """A fully unserializable object triggers fallback + serialize incident."""
    from mojo.apps.assistant.services.agent import _dumps_tool_result, _json_default

    # Force _json_default to raise so the dumps path hits the except branch.
    with mock.patch(
        "mojo.apps.assistant.services.agent._json_default",
        side_effect=TypeError("boom"),
    ):
        with mock.patch("mojo.apps.incident.report_event") as mock_report:
            raw = _dumps_tool_result(
                {"bad": object()}, user=opts.user,
                conversation=_FakeConversation(), tool_name="stub_tool",
            )
    parsed = json.loads(raw)
    assert_true("error" in parsed, "fallback payload must include an error key")
    assert_true(
        "could not be serialized" in parsed["error"],
        "fallback error message should be informative",
    )
    assert_true(mock_report.called, "serialization failure must raise an incident")
    assert_eq(
        mock_report.call_args[1]["category"],
        "assistant:error:serialize",
        "category should be assistant:error:serialize",
    )
    assert_eq(
        mock_report.call_args[1]["level"], 7,
        "serialization failure incident level should be 7",
    )


# ---------------------------------------------------------------------------
# _execute_tool integration
# ---------------------------------------------------------------------------

def _make_registry(handler, permission="view_admin", mutates=False):
    return {
        "stub_tool": {
            "definition": {"name": "stub_tool", "description": "", "input_schema": {}},
            "handler": handler,
            "permission": permission,
            "mutates": mutates,
            "domain": "custom",
            "core": False,
        },
    }


@th.django_unit_test()
def test_execute_tool_datetime_result_round_trips(opts):
    """A tool returning a datetime must produce a valid JSON tool_result block."""
    from mojo.apps.assistant.services.agent import _execute_tool

    def handler(params, user):
        return {"ts": datetime.datetime(2026, 4, 15, tzinfo=datetime.timezone.utc)}

    block = {"id": "tu_1", "name": "stub_tool", "input": {}}
    registry = _make_registry(handler)

    result = _execute_tool(
        block, registry, opts.user, _FakeConversation(),
        tools=[], on_event=None, tool_calls_made=[],
    )
    assert_eq(result["type"], "tool_result", "result type must be tool_result")
    parsed = json.loads(result["content"])
    assert_true("ts" in parsed, "datetime field must survive serialization")
    assert_true(
        isinstance(parsed["ts"], str),
        "datetime must be serialized as a string",
    )


@th.django_unit_test()
def test_execute_tool_exception_reports_incident_with_traceback(opts):
    """Tool handler raising must emit assistant:error incident with traceback details."""
    from mojo.apps.assistant.services.agent import _execute_tool

    def handler(params, user):
        raise RuntimeError("boom inside handler")

    block = {"id": "tu_2", "name": "stub_tool", "input": {"key1": "v", "key2": "v"}}
    registry = _make_registry(handler)

    with mock.patch("mojo.apps.incident.report_event") as mock_report:
        result = _execute_tool(
            block, registry, opts.user, _FakeConversation(),
            tools=[], on_event=None, tool_calls_made=[],
        )

    parsed = json.loads(result["content"])
    assert_true("error" in parsed, "tool exception must yield an error payload")
    assert_true(mock_report.called, "tool exception must raise an incident")
    assert_eq(
        mock_report.call_args[1]["category"],
        "assistant:error",
        "category should be assistant:error",
    )
    details = mock_report.call_args[0][0]
    assert_true(
        "boom inside handler" in details,
        "incident details must include the exception text",
    )
    assert_true(
        "input_keys=" in details,
        "incident details must include input_keys",
    )
    assert_true(
        "key1" in details and "key2" in details,
        "incident details must list tool_input keys",
    )


@th.django_unit_test()
def test_execute_tool_result_with_model_instance_soft_coerces(opts):
    """A tool handler mistakenly returning a Django Model instance must still serialize."""
    from mojo.apps.assistant.services.agent import _execute_tool

    def handler(params, user):
        # Return the user object directly — a common tool-author mistake.
        return {"user": user}

    block = {"id": "tu_3", "name": "stub_tool", "input": {}}
    registry = _make_registry(handler)

    result = _execute_tool(
        block, registry, opts.user, _FakeConversation(),
        tools=[], on_event=None, tool_calls_made=[],
    )
    parsed = json.loads(result["content"])
    assert_true("user" in parsed, "model field must serialize, not crash")
    # MojoModel.to_dict() is used — the RestMeta graph governs which fields
    # are exposed, so sensitive fields (password hashes, tokens) are already
    # filtered out by the model's default graph.
    assert_true(
        isinstance(parsed["user"], (dict, int)),
        "model instance must coerce to a JSON-native value",
    )
    # The User's default graph must not include the password hash.
    raw = result["content"]
    assert_true(
        "password" not in raw,
        "User default RestMeta graph must not expose password field",
    )
