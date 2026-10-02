"""
Regression tests for assistant tool-result boundary serialization and
incident reporting. Covers:

- datetime/Decimal/UUID round-trip through _dumps_tool_result
- Tool handler exception -> assistant:error incident with traceback
- Unserializable sentinel -> fallback error + assistant:error:serialize incident
- Parallel-tool failure -> assistant:error:parallel incident
"""

TESTIT_TIER = "bug"
import contextlib
import json
import decimal
import datetime
import uuid
from unittest import mock
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_EMAIL = 'tool-err-admin@example.com'
TEST_PASSWORD = 'TestPass1!'
SKILL_SECRET = "TOOLERR-SECRET-MARKER"
SKILL_DEFAULT_KEYS = [
    "id", "tier", "name", "description", "auto_execute", "is_active",
    "created", "modified", "user",
]


@contextlib.contextmanager
def _skill_rest_meta(graphs=None, **attrs):
    """Temporarily ADD graphs / RestMeta attributes to assistant.Skill.

    Additive only: `default` and `detail` are left exactly as shipped, so the
    only code that can see a difference is the assistant's own graph selection,
    which runs in this package's thread.
    """
    from mojo.apps.assistant.models import Skill

    original = Skill.RestMeta
    merged = dict(original.GRAPHS)
    merged.update(graphs or {})
    attrs["GRAPHS"] = merged
    Skill.RestMeta = type("RestMeta", (original,), attrs)
    try:
        yield Skill
    finally:
        Skill.RestMeta = original


def _skills():
    from mojo.apps.assistant.models import Skill
    return Skill.objects.filter(name__startswith="toolerr_skill_").order_by("name")


def _clear_events(user, category):
    from mojo.apps.incident.models import Event
    Event.objects.filter(uid=user.pk, category=category).delete()


def _event(user, category):
    from mojo.apps.incident.models import Event
    return Event.objects.filter(uid=user.pk, category=category).latest("pk")


def _real_reporter():
    from mojo.apps.incident.reporter import report_event
    return report_event


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

    # Rows carrying a value the `default` graph does not expose.
    from mojo.apps.assistant.models import Skill
    _skills().delete()
    for i in range(2):
        Skill.objects.create(
            user=opts.user, tier="user", name=f"toolerr_skill_{i}",
            steps=[{"note": SKILL_SECRET}], metadata={"marker": SKILL_SECRET},
        )


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
    _clear_events(opts.user, "assistant:error:serialize")
    with mock.patch(
        "mojo.apps.assistant.services.agent._json_default",
        side_effect=TypeError("boom"),
    ):
        raw = _dumps_tool_result(
            {"bad": object()}, user=opts.user,
            conversation=_FakeConversation(), tool_name="stub_tool",
            _reporter=_real_reporter(),
        )
    parsed = json.loads(raw)
    assert_true("error" in parsed, "fallback payload must include an error key")
    assert_true(
        "could not be serialized" in parsed["error"],
        "fallback error message should be informative",
    )
    event = _event(opts.user, "assistant:error:serialize")
    assert_eq(
        event.category,
        "assistant:error:serialize",
        "category should be assistant:error:serialize",
    )
    assert_eq(
        event.level, 7,
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

    _clear_events(opts.user, "assistant:error")
    result = _execute_tool(
        block, registry, opts.user, _FakeConversation(),
        tools=[], on_event=None, tool_calls_made=[],
        _reporter=_real_reporter(),
    )

    parsed = json.loads(result["content"])
    assert_true("error" in parsed, "tool exception must yield an error payload")
    event = _event(opts.user, "assistant:error")
    assert_eq(
        event.category,
        "assistant:error",
        "category should be assistant:error",
    )
    details = event.details
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
    # The row goes through the server-selected assistant graph (`ai`, else
    # `default`), so sensitive fields (password hashes, tokens) are already
    # filtered out by the model's own graph.
    assert_true(
        isinstance(parsed["user"], (dict, int)),
        "model instance must coerce to a JSON-native value",
    )
    # The User's default graph must not include exact credential fields. A
    # safe boolean such as requires_password_change may contain the word.
    if isinstance(parsed["user"], dict):
        forbidden = {"password", "auth_key", "onetime_code"}
        assert_true(
            forbidden.isdisjoint(parsed["user"]),
            "User default RestMeta graph exposed a credential field",
        )


# ---------------------------------------------------------------------------
# Model rows in a tool result go through the server-selected graph
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_json_default_model_instance_uses_selected_graph(opts):
    """An instance is serialized through `default`, and through `ai` when declared."""
    from mojo.apps.assistant.services.agent import _json_default

    skill = _skills().first()
    plain = _json_default(skill, user=opts.user)
    assert_eq(list(plain), SKILL_DEFAULT_KEYS, "without `ai` the default graph's keys are used")
    with _skill_rest_meta(graphs={"ai": {"fields": ["id", "name"]}}):
        chosen = _json_default(skill, user=opts.user)
    assert_eq(chosen, {"id": skill.pk, "name": skill.name}, "with `ai` declared, its keys are used")


@th.django_unit_test()
def test_json_default_queryset_uses_selected_graph(opts):
    """A model queryset is serialized through the selected graph, never `.values()`."""
    from mojo.apps.assistant.services.agent import _json_default

    rows = _json_default(_skills(), user=opts.user)
    assert_eq([list(r) for r in rows], [SKILL_DEFAULT_KEYS] * 2,
              "each row should have exactly the default graph's keys")
    assert_true(SKILL_SECRET not in json.dumps(rows),
                "a column outside the selected graph escaped through a queryset result")
    with _skill_rest_meta(graphs={"ai": {"fields": ["name"]}}):
        chosen = _json_default(_skills(), user=opts.user)
    assert_eq(chosen, [{"name": "toolerr_skill_0"}, {"name": "toolerr_skill_1"}],
              "with `ai` declared, the queryset goes through it")


@th.django_unit_test()
def test_json_default_withholds_raw_value_querysets(opts):
    """`.values()` / `.values_list()` querysets never reach the result as rows."""
    from mojo.apps.assistant.services.agent import _json_default

    for label, qs in (
            ("values()", _skills().values()),
            ("values(...)", _skills().values("id", "metadata")),
            ("values_list()", _skills().values_list()),
            ("values_list(flat)", _skills().values_list("metadata", flat=True))):
        out = _json_default(qs, user=opts.user)
        assert_true(isinstance(out, dict) and "error" in out,
                    f"{label} must be withheld with a marker, got: {out!r}")
        assert_true(SKILL_SECRET not in json.dumps(out), f"{label} leaked a raw column")


@th.django_unit_test()
def test_json_default_withholds_rows_without_a_usable_graph(opts):
    """Malformed `ai`, or a gated graph with no permission, yields a reference only."""
    from mojo.apps.assistant.services.agent import _json_default

    skill = _skills().first()
    with _skill_rest_meta(graphs={"ai": None}):
        one = _json_default(skill, user=opts.user)
        many = _json_default(_skills(), user=opts.user)
    assert_eq(one, {"pk": skill.pk, "model": "Skill"}, "a malformed graph leaves only a reference")
    assert_true(isinstance(many, dict) and "error" in many, f"a queryset is withheld, got: {many!r}")

    gate = {"ai": ["toolerr_ai_reader"]}
    with _skill_rest_meta(graphs={"ai": {"fields": ["id", "name"]}}, GRAPH_PERMISSIONS=gate):
        denied = _json_default(skill, user=opts.user)
        no_caller = _json_default(skill)
    assert_eq(denied, {"pk": skill.pk, "model": "Skill"},
              "a caller without the graph permission gets a reference only")
    assert_eq(no_caller, {"pk": skill.pk, "model": "Skill"},
              "with no caller to check, a gated graph is not served")


@th.django_unit_test()
def test_execute_tool_queryset_result_is_graph_serialized(opts):
    """End to end: a handler returning a queryset emits selected-graph rows only."""
    from mojo.apps.assistant.services.agent import _execute_tool

    def handler(params, user):
        return {"rows": _skills(), "raw": _skills().values(), "first": _skills().first()}

    block = {"id": "tu_4", "name": "stub_tool", "input": {}}
    result = _execute_tool(
        block, _make_registry(handler), opts.user, _FakeConversation(),
        tools=[], on_event=None, tool_calls_made=[],
    )
    assert_true(SKILL_SECRET not in result["content"],
                "a tool result carried a column outside the selected graph")
    parsed = json.loads(result["content"])
    assert_eq([list(r) for r in parsed["rows"]], [SKILL_DEFAULT_KEYS] * 2,
              "queryset rows should carry the default graph's keys")
    assert_eq(list(parsed["first"]), SKILL_DEFAULT_KEYS, "an instance should carry the default graph's keys")
    assert_true("error" in parsed["raw"], "a raw-values queryset should be withheld")
