"""Admin Security authority, policy safety, and compatibility contracts."""

from datetime import timedelta
from copy import deepcopy
import importlib
import uuid

from testit import helpers as th


PREFIX = f"admin-security-{uuid.uuid4().hex[:10]}"
IPSET_PREFIX = f"as_{PREFIX[-10:]}"


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
    IPSet.objects.filter(name__startswith=IPSET_PREFIX).delete()
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


@th.django_unit_test("Admin Security routes use server-derived authority and configured freshness")
def test_route_authority(opts):
    import inspect
    from types import SimpleNamespace
    from mojo import errors as merrors
    from mojo.apps.account.models import User
    views = importlib.import_module("mojo.apps.incident.rest.admin_security")
    from mojo.apps.incident.rest import ipset as ipset_views
    from mojo.apps.incident.handlers import ticket_actions
    from mojo.apps.incident.services import admin_security
    assert views.on_admin_security.__url__ == ("GET", "admin/security")
    assert views.on_admin_security_action.__url__ == (
        "POST", "admin/security/action")
    assert not getattr(views.on_admin_security, "_mojo_denies_key_backed_session", False), (
        "validated API credentials must not be rejected before authority is derived")
    assert not getattr(
        views.on_admin_security_action, "_mojo_denies_key_backed_session", False), (
        "validated per-user API keys may carry global operator authority")
    assert views.on_admin_security_action._mojo_requires_fresh_auth
    assert views.on_admin_security_action._mojo_fresh_auth_seconds is None
    assert ipset_views.on_ipset_action.__url__ == ("POST", "ipset/action")
    assert not getattr(ipset_views.on_ipset_action, "_mojo_denies_key_backed_session", False)
    assert ipset_views.on_ipset_action._mojo_requires_fresh_auth
    ticket_authority = inspect.getsource(ticket_actions._has_global_authority)
    assert "require_fresh(request)" in ticket_authority
    assert "seconds=600" not in ticket_authority, (
        "ticket-governed security actions must use configured freshness")
    actor = User.objects.get(pk=opts.security_operator)
    machine_request = SimpleNamespace(
        user=actor, user_api_key=SimpleNamespace(pk=23, label="operator"),
        api_key=None, group_token=None, bearer="bearer", META={})
    assert ticket_actions._has_global_authority(
        SimpleNamespace(active_request=machine_request),
        "incident.rule_approval") == actor, (
            "validated per-user API keys must retain ticket action authority")
    try:
        views._translate(lambda: (_ for _ in ()).throw(
            admin_security.SecurityActionError(
                "revision changed", code="stale_revision", status=409)))
    except merrors.ValueException as error:
        assert error.status == 409 and error.code == "stale_revision"
    else:
        assert False, "the REST adapter must retain typed security error codes"


@th.django_unit_test("legacy rule CRUD remains while governed rows stay protected")
def test_generic_compatibility(opts):
    from mojo.apps.incident.models import IPSet, Rule, RuleSet
    for model in (RuleSet, Rule):
        assert model.RestMeta.CAN_CREATE is True
        assert model.RestMeta.CAN_UPDATE is True
        assert model.RestMeta.CAN_DELETE is True
        assert model.RestMeta.DENY_AI is True
    assert getattr(IPSet.RestMeta, "CAN_CREATE", True) is True
    assert IPSet.RestMeta.CAN_DELETE is False
    assert "is_enabled" in IPSet.RestMeta.NO_SAVE_FIELDS


@th.django_unit_test("IPSet action claims desired state before an out-of-tx checked wait")
def test_ipset_action_checked_and_revision_fenced(opts):
    from django.db import connection
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import IPSet
    from mojo.apps.incident.services import admin_security

    actor = User.objects.get(pk=opts.security_operator)
    row = IPSet.objects.create(
        name=f"{IPSET_PREFIX}_gov", kind="custom", source="manual",
        data="192.0.2.0/24")

    def partial(name, cidrs, present=True):
        assert not connection.in_atomic_block, \
            "checked IPSet wait held the desired-state transaction"
        return {"status": "partial", "ok": False,
                "error": {"code": "missing_host",
                          "message": "host receipt missing"}}

    result = admin_security.apply_action({
        "action": "ipset.enable", "ipset_id": row.pk,
        "expected_modified": row.modified.isoformat(),
        "confirm": f"ENABLE IPSET {row.pk}"}, actor,
        reconcile_ipset=partial)
    row.refresh_from_db()
    assert row.is_enabled is True, \
        "governed action lost desired enable state"
    assert result["data"]["enforcement_ok"] is False
    assert result["data"]["error_code"] == "missing_host"

    stale = row.modified.isoformat()
    row.description = "concurrent edit"
    row.save(update_fields=["description", "modified"])
    with th.assert_raises(admin_security.SecurityActionError):
        admin_security.apply_action({
            "action": "ipset.sync", "ipset_id": row.pk,
            "expected_modified": stale,
            "confirm": f"SYNC IPSET {row.pk}"}, actor)


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
            "I*x\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}*y$",
            "foo|bar", "a|b", "(abc)",
            "(?:a|aa)(?:a|aa)(?:a|aa)(?:a|aa)$"):
        adjacent = _policy(rules=[{
            "field": "details", "operator": "regex", "value": pattern,
            "value_type": "str"}])
        with th.assert_raises(rule_validation.RuleValidationError):
            rule_validation.normalize_ruleset(adjacent)
    assert rule_validation.validate_regex("a+b+").pattern == "a+b+", (
        "adjacent repetitions with disjoint literal domains should remain safe")
    assert rule_validation.validate_regex(r"^node-[A-Z0-9]+$").search(
        "node-A12"), "simple literal, class, repetition, and anchor syntax stays safe"
    assert rule_validation.validate_regex(r"^[|]+\|$").search("|||"), (
        "pipe literals in classes and escapes must not be treated as alternation")
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


@th.django_unit_test("legacy rules retain established evaluation and dispatch")
def test_legacy_runtime_fails_closed(opts):
    from mojo.apps.incident.models import Event, Rule, RuleSet
    event = Event.objects.create(category=f"{PREFIX}:legacy", level=9)
    row = RuleSet.objects.create(
        name="legacy", category=event.category, handler="job://os.system")
    condition = Rule.objects.create(
        parent=row, field_name="level", comparator="regex", value="(",
        value_type="int")
    assert condition.check_rule(event) is False
    unrolled = Rule.objects.create(
        parent=row, field_name="category", comparator="regex",
        value="(?:a|aa)(?:a|aa)(?:a|aa)(?:a|aa)$", value_type="str")
    assert unrolled.check_rule(event) is False, (
        "legacy execution must reject unrolled ambiguous alternation")
    published = []

    def publish(*args, **kwargs):
        published.append((args, kwargs))

    assert row.run_handler(event, publisher=publish) is True
    assert len(published) == 1
    payload = published[0][0][1]
    assert payload["execution_mode"] == "legacy"
    assert "handler_schema" not in payload


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


@th.django_unit_test("new envelope preserves evidence while scrubbing authentication secrets")
def test_complete_secret_scrubbed_overview(opts):
    from mojo.apps.incident.models import Event, IPSet, RuleSet
    from mojo.apps.incident.services import admin_security
    event = Event.objects.create(category=f"{PREFIX}:secret", metadata={
        "evidence": "fixture-secret", "command": "rm fixture",
        "password": "credential-secret"})
    ruleset = RuleSet.objects.create(
        name="raw", category=f"{PREFIX}:raw", handler="job://secret.module")
    ipset = IPSet.objects.create(name=f"{IPSET_PREFIX}_raw", kind="custom", source="manual",
                                 source_key="fixture-key", data="8.8.8.8/32")
    result = admin_security.overview({"limit": 100, "sections": "overview"})
    rendered = str(result)
    assert result["schema_version"] == 3
    assert "fixture-key" not in rendered
    metrics = result["sections"]["overview"]["data"]
    assert metrics["accuracy"]["current"] == "exact_current_rows"
    assert metrics["accuracy"]["resolution_rate"] == "unavailable"
    for section in result["sections"].values():
        assert {"status", "observed_at", "cutoff", "window", "truncated", "data"} <= set(section)

    event_detail = admin_security.overview({
        "sections": "events", "event_id": event.pk})["sections"]["events"]["data"][0]
    metadata = event_detail["metadata"]["chunk"]
    assert "fixture-secret" in metadata and "rm fixture" in metadata, (
        "authorized event detail must preserve operational evidence")
    assert "credential-secret" not in metadata and "[redacted secret]" in metadata, (
        "only authentication secrets should be scrubbed")
    rule_detail = admin_security.overview({
        "sections": "rules", "ruleset_id": ruleset.pk})["sections"]["rules"]["data"][0]
    assert "job://secret.module" in rule_detail["handler"]["chunk"]
    ipset_detail = admin_security.overview({
        "sections": "ipsets", "ipset_id": ipset.pk})["sections"]["ipsets"]["data"][0]
    assert "8.8.8.8/32" in ipset_detail["data"]["chunk"]
    assert "fixture-key" not in str(ipset_detail)

    checked = {
        "status": "partial", "ok": False,
        "expected_hosts": ["edge-a"], "responded_hosts": ["edge-a"],
        "succeeded_hosts": [], "failed_hosts": ["edge-a"],
        "missing_hosts": [], "desired": {"present": True, "count": 1,
                                           "digest": "a" * 64},
        "fence": 4, "error": {"code": "runner_error",
                                "message": "ipset command failed"},
        "checked": {"results": [{"host": "edge-a", "runner_id": "runner-a",
                                   "status": "failed", "error": "exit 1"}]},
    }
    proof_detail = admin_security._bounded_ipset(
        ipset, admin_security.SecurityAuthority("global", "internal", None),
        checked, [{"host": "edge-a", "started": "boot-a"}],
        "2026-08-10T17:10:00Z")
    assert "runner-a" in proof_detail["checked_proof"]["chunk"]
    assert "ipset command failed" in proof_detail["checked_proof"]["chunk"]


@th.django_unit_test("schema v3 advertises governed actions and bounded checked receipts")
def test_action_schema_and_checked_receipt_projection(opts):
    from types import SimpleNamespace
    from mojo.apps.incident.services import admin_security

    schemas = admin_security._action_schemas()
    assert set(schemas) == set(admin_security.ACTIONS)
    for action, schema in schemas.items():
        assert schema["additional_properties"] is False
        assert schema["properties"]["action"]["const"] == action
        assert {"confirm"} <= set(schema["properties"])
    row = SimpleNamespace(
        pk=91, modified=None, name="safe_set", kind="custom",
        description="safe", is_enabled=True, cidr_count=2,
        last_synced=None, sync_error="raw provider exception text")
    result = {
        "status": "partial", "ok": False, "fence": 7,
        "desired": {"name": "safe_set", "present": True, "count": 2,
                    "digest": "a" * 64, "cidrs": ["8.8.8.8/32"]},
        "error": {"code": "missing_host", "message": "raw broker exception"},
        "checked": {
            "expected_hosts": ["edge-a", "edge-b", "8.8.8.8"],
            "responded_hosts": ["edge-a"], "succeeded_hosts": ["edge-a"],
            "failed_hosts": [], "missing_hosts": ["edge-b"],
            "expected_roster": [{"host": "edge-a", "started": "secret-incarnation"}],
            "results": [{"host": "edge-a", "runner_id": "secret-runner",
                         "result": {"cidrs": ["8.8.8.8/32"]}}],
        },
    }
    projected = admin_security._safe_ipset(
        row, result=result, observation_cutoff="2026-08-10T17:10:00Z")
    assert projected["enforcement"]["status"] == "missing"
    assert projected["enforcement"]["expected_host_ids"] == ["edge-a", "edge-b"]
    rendered = str(projected)
    for secret in ("8.8.8.8/32", "secret-incarnation", "secret-runner",
                   "raw broker exception", "raw provider exception text"):
        assert secret not in rendered
    stale = dict(result, error={"code": "generation_superseded"})
    assert admin_security._safe_ipset(row, result=stale)[
        "enforcement_status"] == "stale", (
            "a superseded generation must not be mislabeled as merely missing")

    desired = {"name": "safe_set", "present": True, "count": 2,
               "digest": "a" * 64}
    roster = [{"host": "edge-a", "started": "incarnation-a"},
              {"host": "edge-b", "started": "incarnation-b"}]
    direct = {
        "status": "verified", "ok": True, "expected_hosts": ["edge-a", "edge-b"],
        "expected_roster": roster, "desired": desired, "fence": 8,
        "fingerprint": "b" * 64,
        "observations": [{
            "schema": "mojo.firewall.semantic", "version": 1, "kind": "set",
            "identity": "safe_set", "fence": 8, "fingerprint": "b" * 64,
            "host": host, "started": started, "desired": desired,
        } for host, started in (("edge-a", "incarnation-a"),
                                ("edge-b", "incarnation-b"))],
    }
    verified = admin_security._safe_ipset(row, result=direct, roster=roster)
    assert verified["enforcement_status"] == "verified", verified
    assert verified["enforcement"]["responded_host_ids"] == ["edge-a", "edge-b"]
    assert verified["enforcement"]["succeeded_host_ids"] == ["edge-a", "edge-b"]

    checked = deepcopy(direct)
    checked["checked"] = {
        "schema": "mojo.jobs.execute-checked", "version": 2,
        "status": "verified", "expected_hosts": ["edge-a", "edge-b"],
        "expected_roster": roster, "responded_hosts": ["edge-a", "edge-b"],
        "succeeded_hosts": ["edge-a", "edge-b"], "failed_hosts": [],
        "missing_hosts": [], "anomalies": [],
        "results": [{
            "host": host, "runner_id": f"secret-runner-{host}",
            "started": started, "status": "success", "error": None,
            "result": {"schema": "mojo.firewall.semantic", "version": 1,
                       "kind": "set", "desired": desired,
                       "observed": desired, "ok": True},
        } for host, started in (("edge-a", "incarnation-a"),
                                ("edge-b", "incarnation-b"))],
    }
    assert admin_security._safe_ipset(
        row, result=checked, roster=roster)["enforcement_status"] == "verified"

    contradictions = {}
    partial = deepcopy(direct)
    partial["status"] = "partial"
    contradictions["partial"] = (partial, "partial")
    bare = {"status": "verified", "ok": True, "desired": desired}
    contradictions["malformed"] = (bare, "partial")
    for expected_status, field in (("missing", "missing_hosts"),
                                   ("partial", "failed_hosts"),
                                   ("partial", "anomalies")):
        value = deepcopy(checked)
        value["checked"][field] = (["edge-b"] if field != "anomalies"
                                    else ["unexpected_reply"])
        if field == "missing_hosts":
            value["checked"]["responded_hosts"] = ["edge-a"]
            value["checked"]["succeeded_hosts"] = ["edge-a"]
        elif field == "failed_hosts":
            value["checked"]["succeeded_hosts"] = ["edge-a"]
        contradictions[field] = (value, expected_status)
    for case, (value, expected_status) in contradictions.items():
        projection = admin_security._safe_ipset(row, result=value, roster=roster)
        assert projection["enforcement_status"] == expected_status, (case, projection)
        assert projection["enforcement_ok"] is False, (case, projection)
        assert projection["error_code"] == "fleet_unverified", (case, projection)


@th.django_unit_test("Assistant rule mutations use configured freshness and previews")
def test_assistant_registry_contract(opts):
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    for name in ("create_rule", "update_ruleset", "delete_ruleset",
                 "manage_security_recommendation"):
        entry = registry[name]
        assert entry["mutates"] is True
        assert entry["fresh_auth_seconds"] is None
    assert registry["create_rule"]["preview"] is not None
    assert registry["update_ruleset"]["preview"] is not None
    assert registry["delete_ruleset"]["preview"] is not None
