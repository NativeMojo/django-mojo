"""Admin Security authority, policy safety, and compatibility contracts."""

from datetime import timedelta
import uuid
from unittest import mock

from testit import helpers as th


PREFIX = f"admin-security-{uuid.uuid4().hex[:10]}"


def _policy(name="Governed policy", category=None, handlers=None, rules=None):
    return {
        "name": name, "category": category or f"{PREFIX}:event",
        "priority": 50, "bundle_minutes": 30, "bundle_by": 4,
        "bundle_by_rule_set": True, "match_by": 0,
        "handlers": handlers if handlers is not None else [
            {"type": "notify", "permission": "manage_security"}],
        "rules": rules if rules is not None else [{
            "name": "serious", "field": "level", "operator": ">=",
            "value": 8, "value_type": "int"}],
        "is_active": False,
    }


@th.django_unit_setup()
def setup_admin_security(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event, IPSet, RuleSet
    Event.objects.filter(category__startswith=PREFIX).delete()
    RuleSet.objects.filter(category__startswith=PREFIX).delete()
    IPSet.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__startswith=PREFIX).delete()
    operator = User.objects.create_user(
        username=f"{PREFIX}-operator", email=f"{PREFIX}@example.test",
        password="AdminSecurity##1")
    operator.is_active = True
    operator.add_permission("manage_security")
    operator.save()
    viewer = User.objects.create_user(
        username=f"{PREFIX}-viewer", email=f"viewer-{PREFIX}@example.test",
        password="AdminSecurity##1")
    viewer.is_active = True
    viewer.add_permission("view_security")
    viewer.save()
    opts.security_operator = operator.pk
    opts.security_viewer = viewer.pk


@th.django_unit_test("Admin Security routes pin human and fresh-auth authority")
def test_route_authority(opts):
    from mojo.apps.incident.rest import admin_security as views
    assert views.on_admin_security.__url__ == ("GET", "admin/security")
    assert views.on_admin_security_action.__url__ == (
        "POST", "admin/security/action")
    assert views.on_admin_security._mojo_denies_key_backed_session
    assert set(views.on_admin_security._mojo_required_permissions) == {
        "view_security", "manage_security", "security"}
    assert views.on_admin_security_action._mojo_denies_key_backed_session
    assert set(views.on_admin_security_action._mojo_required_permissions) == {
        "manage_security", "security"}
    assert views.on_admin_security_action._mojo_requires_fresh_auth
    assert views.on_admin_security_action._mojo_fresh_auth_seconds == 600


@th.django_unit_test("generic rule writes retire while IPSet stays available")
def test_generic_compatibility(opts):
    from mojo.apps.incident.models import IPSet, Rule, RuleSet
    for model in (RuleSet, Rule):
        assert model.RestMeta.CAN_CREATE is False
        assert model.RestMeta.CAN_UPDATE is False
        assert model.RestMeta.CAN_DELETE is False
        assert model.RestMeta.DENY_AI is True
    assert getattr(IPSet.RestMeta, "CAN_CREATE", True) is True
    assert IPSet.RestMeta.CAN_DELETE is True


@th.django_unit_test("typed rules reject arbitrary handlers and unsafe regular expressions")
def test_policy_validation(opts):
    from mojo.apps.incident.handlers.llm_agent import _tool_create_rule
    from mojo.apps.incident.services import rule_validation
    bad_handler = _policy(handlers=[{"type": "job", "path": "os.system"}])
    with th.assert_raises(rule_validation.RuleValidationError):
        rule_validation.normalize_ruleset(bad_handler)
    bad_regex = _policy(rules=[{
        "field": "details", "operator": "regex", "value": "(a+)+$",
        "value_type": "str"}])
    with th.assert_raises(rule_validation.RuleValidationError):
        rule_validation.normalize_ruleset(bad_regex)
    for pattern in (
            "a*a*a*a*a*b", "[ab]*[ab]*c", r"\d+\d+z",
            "(a*)(a*)(a*)b", "a*aa*aa*aa*$",
            "I*x\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}*y$"):
        adjacent = _policy(rules=[{
            "field": "details", "operator": "regex", "value": pattern,
            "value_type": "str"}])
        with th.assert_raises(rule_validation.RuleValidationError):
            rule_validation.normalize_ruleset(adjacent)
    assert rule_validation.validate_regex("a+b+").pattern == "a+b+", (
        "adjacent repetitions with disjoint literal domains should remain safe")
    for fleet_wide in (False, 0, 1, "true"):
        unsafe_scope = _policy(handlers=[{
            "type": "block", "ttl_seconds": 600,
            "fleet_wide": fleet_wide}])
        with th.assert_raises(rule_validation.RuleValidationError):
            rule_validation.normalize_ruleset(unsafe_scope)
    assert rule_validation.parse_handlers(
        "block://?ttl=600&fleet_wide=0") is None, (
        "stored block handlers may not claim a runtime-ignored local scope")
    refused = _tool_create_rule({
        "name": "raw", "category": f"{PREFIX}:raw-handler",
        "handler": "job://os.system", "reasoning": "must fail",
    })
    assert refused["error_code"] == "raw_handler_not_allowed"


@th.django_unit_test("public policy schema describes the complete governed aggregate")
def test_public_policy_schema(opts):
    from mojo.apps.incident.services import rule_validation

    schema = rule_validation.public_schema()
    aggregate = schema["aggregate"]
    properties = aggregate["properties"]
    assert aggregate["additional_properties"] is False, (
        "the aggregate schema must fail closed on unknown properties")
    assert {"name", "category", "priority", "bundle_minutes", "bundle_by",
            "bundle_by_rule_set", "match_by", "trigger_count",
            "trigger_window", "retrigger_every", "handlers", "rules",
            "delete_on_resolution", "is_active"} <= set(properties), (
        "schema-driven clients need every accepted aggregate property")
    assert aggregate["rejected_properties"]["description"], (
        "the unsupported description alias must be explicitly discoverable")
    assert aggregate["rejected_properties"]["match_type"], (
        "the rejected match_type alias must point clients to match_by")
    handlers = {row["type"]: row["arguments"] for row in schema["handlers"]}
    assert {"category", "maestro"} <= set(handlers["ticket"]), (
        "ticket category and maestro selection must be publicly discoverable")
    assert "note" in handlers["resolve"], (
        "the bounded resolve note must be publicly discoverable")
    assert schema["governance"]["revision_input"] == "expected_modified", (
        "clients need the optimistic-lock input name")
    assert properties["is_active"]["governed_write_value"] is False, (
        "create and replacement payloads must advertise inactive-only writes")
    for rejected_name in ("description", "match_type", "handler"):
        payload = _policy()
        payload[rejected_name] = "not canonical"
        with th.assert_raises(rule_validation.RuleValidationError):
            rule_validation.normalize_ruleset(payload)


@th.django_unit_test("durable handlers require the current governed job schema")
def test_durable_handler_schema(opts):
    from mojo.apps.incident.services import rule_validation

    safe = "notify://perm@manage_security"
    with th.assert_raises(rule_validation.RuleValidationError):
        rule_validation.normalize_queued_handler(safe, None, None)
    with th.assert_raises(rule_validation.RuleValidationError):
        rule_validation.normalize_queued_handler(
            "job://unsafe.callable", rule_validation.HANDLER_JOB_SCHEMA,
            rule_validation.HANDLER_JOB_SCHEMA_VERSION)
    canonical = rule_validation.normalize_queued_handler(
        safe, rule_validation.HANDLER_JOB_SCHEMA,
        rule_validation.HANDLER_JOB_SCHEMA_VERSION)
    assert canonical == safe, (
        "a current safe durable handler should canonicalize without widening")


@th.django_unit_test("malformed legacy rules no-match and cannot dispatch")
def test_legacy_runtime_fails_closed(opts):
    from mojo.apps.incident.models import Event, Rule, RuleSet
    event = Event.objects.create(category=f"{PREFIX}:legacy", level=9)
    row = RuleSet.objects.create(
        name="legacy", category=event.category, handler="job://os.system")
    condition = Rule.objects.create(
        parent=row, field_name="level", comparator="regex", value="(",
        value_type="int")
    assert condition.check_rule(event) is False
    with mock.patch("mojo.apps.jobs.publish") as publish:
        assert row.run_handler(event) is False
    assert not publish.called


@th.django_unit_test("RuleSet actions require version and typed confirmations")
def test_versioned_ruleset_actions(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.incident.services import admin_security
    actor = User.objects.get(pk=opts.security_operator)
    made = admin_security.apply_action({
        "action": "ruleset.create", "confirm": "CREATE RULESET",
        "ruleset": _policy()}, actor)["data"]
    row = RuleSet.objects.get(pk=made["id"])
    assert row.is_active is False
    stale = row.modified.isoformat()
    row.name = "changed elsewhere"
    row.save(update_fields=["name", "modified"])
    with th.assert_raises(admin_security.SecurityActionError):
        admin_security.apply_action({
            "action": "ruleset.activate", "ruleset_id": row.pk,
            "expected_modified": stale,
            "confirm": f"ACTIVATE RULESET {row.pk}"}, actor)
    row.refresh_from_db()
    assert row.is_active is False


@th.django_unit_test("catch-all activation needs its independent confirmation")
def test_catchall_confirmation(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.incident.services import admin_security
    actor = User.objects.get(pk=opts.security_operator)
    made = admin_security.apply_action({
        "action": "ruleset.create", "confirm": "CREATE RULESET",
        "ruleset": _policy(name="catchall", rules=[])}, actor)["data"]
    row = RuleSet.objects.get(pk=made["id"])
    base = {"action": "ruleset.activate", "ruleset_id": row.pk,
            "expected_modified": row.modified.isoformat(),
            "confirm": f"ACTIVATE RULESET {row.pk}"}
    with th.assert_raises(admin_security.SecurityActionError):
        admin_security.apply_action(base, actor)
    base["confirm_catch_all"] = f"ACTIVATE CATCH-ALL RULESET {row.pk}"
    result = admin_security.apply_action(base, actor)
    assert result["data"]["is_active"] is True


@th.django_unit_test("child writes advance the aggregate revision")
def test_child_bumps_parent_revision(opts):
    from django.utils import timezone
    from mojo.apps.incident.models import Rule, RuleSet
    row = RuleSet.objects.create(name="revision", category=f"{PREFIX}:revision")
    old = timezone.now() - timedelta(days=1)
    RuleSet.objects.filter(pk=row.pk).update(modified=old)
    Rule.objects.create(parent=row, field_name="level", comparator=">=",
                        value="8", value_type="int")
    row.refresh_from_db()
    assert row.modified > old


@th.django_unit_test("new envelope redacts raw security material and labels provenance")
def test_bounded_redacted_overview(opts):
    from mojo.apps.incident.models import Event, IPSet, RuleSet
    from mojo.apps.incident.services import admin_security
    Event.objects.create(category=f"{PREFIX}:secret", metadata={
        "evidence": "fixture-secret", "command": "rm fixture"})
    RuleSet.objects.create(
        name="raw", category=f"{PREFIX}:raw", handler="job://secret.module")
    IPSet.objects.create(name=f"{PREFIX}-set", kind="custom", source="manual",
                         source_key="fixture-key", data="8.8.8.8/32")
    result = admin_security.overview({"limit": 100})
    rendered = str(result)
    assert result["schema_version"] == 1
    assert "fixture-secret" not in rendered
    assert "fixture-key" not in rendered
    assert "8.8.8.8/32" not in rendered
    assert "job://secret.module" not in rendered
    metrics = result["sections"]["overview"]["data"]
    assert metrics["accuracy"]["current"] == "exact_current_rows"
    assert metrics["accuracy"]["resolution_rate"] == "unavailable"
    for section in result["sections"].values():
        assert {"status", "observed_at", "cutoff", "window", "truncated", "data"} <= set(section)


@th.django_unit_test("Assistant rule mutations bind fresh auth and previews")
def test_assistant_registry_contract(opts):
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    for name in ("create_rule", "update_ruleset", "delete_ruleset",
                 "manage_security_recommendation"):
        entry = registry[name]
        assert entry["mutates"] is True
        assert entry["fresh_auth_seconds"] == 600
    assert registry["create_rule"]["preview"] is not None
    assert registry["update_ruleset"]["preview"] is not None
    assert registry["delete_ruleset"]["preview"] is not None
