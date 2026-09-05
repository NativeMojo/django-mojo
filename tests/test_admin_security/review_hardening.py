"""Regressions for the Admin Security release-blocker review."""

from datetime import timedelta
from types import SimpleNamespace
import json
import time
import uuid

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from testit import helpers as th


PREFIX = f"admin-security-review-{uuid.uuid4().hex[:10]}"
PASSWORD = "AdminSecurityReview##1"


def _operator():
    from mojo.apps.account.models import User

    user = User.objects.filter(username=f"{PREFIX}-operator").first()
    if user is None:
        user = User.objects.create_user(
            username=f"{PREFIX}-operator", email=f"{PREFIX}@example.test",
            password=PASSWORD)
    user.is_active = True
    user.add_permission("manage_security")
    user.save()
    return user


def _case(group, installation_key, suffix, last_seen):
    from mojo.apps.incident.models import MojoSecCase

    return MojoSecCase.objects.create(
        group=group, installation_key=installation_key,
        first_seen=last_seen, last_seen=last_seen,
        window_start=last_seen - timedelta(minutes=5), window_end=last_seen,
        sensor_id=f"sensor-{suffix}", sensor_kind="web",
        family="request", correlation_key=f"corr-{PREFIX}-{suffix}",
        window_key=f"window-{PREFIX}-{suffix}")


def _chunk_value(report, field, authority=None):
    from mojo.apps.incident.services import admin_security

    current = report[field]
    value = current["chunk"]
    while current["next_cursor"]:
        current = admin_security.overview(
            {"chunk_cursor": current["next_cursor"]},
            authority=authority)["chunk"]
        value += current["chunk"]
    return json.loads(value) if report[field]["encoding"] == "json" else value


@th.django_unit_test("generic compatibility CRUD stamps provenance and protects marker ownership")
def test_generic_compatibility_rest_boundary(opts):
    from mojo.apps.account.models import UserAPIKey
    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.apps.incident.models import Event, RuleSet
    from mojo.apps.incident.services import rule_validation

    user = _operator()
    assert opts.client.login(user.email, PASSWORD), "operator login failed"
    created = opts.client.post("/api/incident/event/ruleset", {
        "name": "intentional compatibility policy",
        "category": f"{PREFIX}:generic", "handler": "job://custom.audit",
        "metadata": {"operator_note": "powerful by design"},
    })
    assert created.status_code == 200, created.response
    row = RuleSet.objects.get(category=f"{PREFIX}:generic")
    assert rule_validation.policy_mode(row.metadata) == "compatibility", (
        "new generic creates need an immutable server-owned compatibility marker")
    audit = Event.objects.filter(
        category="security:admin_action", metadata__object_id=row.pk,
        metadata__action="ruleset.compatibility.create").order_by("-pk").first()
    assert audit is not None, "compatibility create audit was not emitted"
    assert audit.metadata["actor"] == {
        "credential_kind": "user", "user_id": user.pk}, (
        "generic policy writes need safe server-derived actor provenance")

    replaced = opts.client.post(f"/api/incident/event/ruleset/{row.pk}", {
        "metadata": {"__replace": True, "operator_note": "drop marker"}})
    assert replaced.status_code == 400, replaced.response
    non_dict = opts.client.post(
        f"/api/incident/event/ruleset/{row.pk}", {"metadata": ["drop marker"]})
    assert non_dict.status_code == 400, non_dict.response
    changed = opts.client.post(f"/api/incident/event/ruleset/{row.pk}", {
        "metadata": {"__replace": True,
                     rule_validation.COMPATIBILITY_METADATA_KEY: {"version": 2}},
    })
    assert changed.status_code == 400, changed.response
    row.refresh_from_db()
    assert rule_validation.policy_mode(row.metadata) == "compatibility", (
        "compatibility marker was removed by a client metadata replacement")

    forged = opts.client.post("/api/incident/event/ruleset", {
        "name": "forged", "category": f"{PREFIX}:forged",
        "metadata": {rule_validation.GOVERNED_METADATA_KEY: {"version": 1}},
    })
    assert forged.status_code == 400, forged.response

    stale_token = JWToken(user.get_auth_key()).create_access_token(
        uid=user.pk, auth_time=int(time.time()) - 900)
    stale = opts.client.post("/api/incident/event/ruleset", {
        "name": "stale", "category": f"{PREFIX}:stale",
    }, headers={
        "Authorization": f"Bearer {stale_token}",
        "X-Mojo-Test-Fresh-Auth-Window": "300",
    })
    assert stale.status_code == 440, stale.response
    stale_headers = {
        "Authorization": f"Bearer {stale_token}",
        "X-Mojo-Test-Fresh-Auth-Window": "300",
    }
    stale_update = opts.client.post(
        f"/api/incident/event/ruleset/{row.pk}",
        {"name": "must not change"}, headers=stale_headers)
    stale_delete = opts.client.delete(
        f"/api/incident/event/ruleset/{row.pk}", headers=stale_headers)
    stale_child = opts.client.post(
        "/api/incident/event/ruleset/rule",
        {"parent": row.pk, "field_name": "level", "value": "1"},
        headers=stale_headers)
    assert (stale_update.status_code, stale_delete.status_code,
            stale_child.status_code) == (440, 440, 440), (
        "configured freshness was not applied to every generic mutation path")

    package = UserAPIKey.create_for_user(user, label="compatibility automation")
    automated = opts.client.post("/api/incident/event/ruleset", {
        "name": "automated", "category": f"{PREFIX}:automated",
    }, headers={
        "Authorization": f"Bearer {package.token}",
        "X-Mojo-Test-Fresh-Auth-Window": "300",
    })
    assert automated.status_code == 200, automated.response
    automated_row = RuleSet.objects.get(category=f"{PREFIX}:automated")
    automated_audit = Event.objects.filter(
        category="security:admin_action", metadata__object_id=automated_row.pk,
        metadata__action="ruleset.compatibility.create").latest("pk")
    assert automated_audit.metadata["actor"] == {
        "credential_kind": "user_api_key", "user_id": user.pk,
        "user_api_key_id": package.id,
        "user_api_key_label": "compatibility automation"}, (
        "generic compatibility audit lost validated UserAPIKey provenance")


@th.django_unit_test("REST marker checks use locked persisted parents, not submitted deltas")
def test_persisted_marker_boundary_and_reparent(opts):
    from mojo.apps.incident.models import Rule, RuleSet
    from mojo.apps.incident.services import rule_validation

    user = _operator()
    assert opts.client.login(user.email, PASSWORD), "operator login failed"
    legacy = RuleSet.objects.create(
        name="grandfathered", category=f"{PREFIX}:grandfathered")
    compatible = RuleSet.objects.create(
        name="explicit compatibility", category=f"{PREFIX}:compatible",
        metadata=rule_validation.mark_compatible({"note": "retained"}))
    governed = RuleSet.objects.create(
        name="governed", category=f"{PREFIX}:governed",
        metadata=rule_validation.mark_governed())
    child = Rule.objects.create(
        parent=legacy, field_name="level", comparator=">=", value="5",
        value_type="int")

    ok = opts.client.post(
        f"/api/incident/event/ruleset/{legacy.pk}", {"name": "still legacy"})
    assert ok.status_code == 200, ok.response
    assert not rule_validation.has_reserved_marker(
        RuleSet.objects.get(pk=legacy.pk).metadata), "grandfather marker changed"

    ignored = opts.client.post(
        f"/api/incident/event/ruleset/rule/{child.pk}",
        {"parent": governed.pk})
    # The stock graph does not make ``parent`` writable, so a wire attempt is
    # harmlessly ignored. The model boundary below still guards direct/stale
    # framework instances in case a project exposes reparenting in a graph.
    assert ignored.status_code == 200, ignored.response
    child.refresh_from_db()
    assert child.parent_id == legacy.pk, "governed reparent was persisted"

    reparented = Rule.objects.get(pk=child.pk)
    reparented.parent = governed
    with th.assert_raises(Exception):
        reparented.on_rest_pre_save({}, False)

    stale = RuleSet.objects.get(pk=compatible.pk)
    RuleSet.objects.filter(pk=compatible.pk).update(
        metadata=rule_validation.mark_governed())
    stale.name = "stale overwrite"
    with th.assert_raises(Exception):
        stale.on_rest_pre_save({}, False)
    with th.assert_raises(Exception):
        stale.on_rest_pre_delete()

    stale_child = Rule.objects.get(pk=child.pk)
    Rule.objects.filter(pk=child.pk).update(parent=governed)
    with th.assert_raises(Exception):
        stale_child.on_rest_pre_save({}, False)
    with th.assert_raises(Exception):
        stale_child.on_rest_pre_delete()


@th.django_unit_test("malformed reserved policy markers fail closed")
def test_malformed_reserved_markers_fail_closed(opts):
    from mojo.apps.incident.models import Event, Rule, RuleSet
    from mojo.apps.incident.services import rule_validation

    event = Event.objects.create(category=f"{PREFIX}:marker", level=9)
    malformed = (
        {rule_validation.GOVERNED_METADATA_KEY: {"version": 999}},
        {rule_validation.GOVERNED_METADATA_KEY: "1"},
        {rule_validation.GOVERNED_METADATA_KEY: {"wrong": 1}},
        {rule_validation.COMPATIBILITY_METADATA_KEY: {"version": 999}},
    )
    for index, metadata in enumerate(malformed):
        row = RuleSet.objects.create(
            name=f"malformed-{index}", category=event.category,
            handler="job://must.not.run", metadata=metadata)
        condition = Rule.objects.create(
            parent=row, field_name="level", comparator=">=", value="1",
            value_type="int")
        summary = rule_validation.validation_summary(row)
        assert summary["status"] == "replacement_required" and not summary["legacy"], (
            "a malformed reserved marker was classified as legacy")
        assert condition.check_rule(event) is False, (
            "a condition with a malformed parent marker executed")
        assert row.run_handler(
            event, publisher=lambda *args, **kwargs: None) is False, (
            "a handler with a malformed parent marker executed")


@th.django_unit_test("configured Assistant freshness and actor provenance are server-derived")
def test_assistant_freshness_and_actor_context(opts):
    from mojo.apps.account.models import UserAPIKey
    from mojo.apps.account.services import fresh_auth
    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.apps.assistant import CONFIGURED_FRESH_AUTH, get_registry
    from mojo.apps.assistant.services import agent, approvals
    from mojo.apps.assistant.services.tools.security import rules as tools
    from testit.helpers import get_mock_request

    user = _operator()
    for name in ("create_rule", "update_ruleset", "delete_ruleset",
                 "manage_security_recommendation"):
        assert get_registry()[name]["fresh_auth_seconds"] == CONFIGURED_FRESH_AUTH, (
            f"{name} does not resolve deployment-configured freshness")
    assert approvals.resolve_fresh_auth_window(
        CONFIGURED_FRESH_AUTH, configured_window=0) is None, (
        "a disabled configured freshness gate did not resolve to no-op")
    assert approvals.resolve_fresh_auth_window(
        CONFIGURED_FRESH_AUTH, configured_window=300) == 300, (
        "an enabled configured freshness gate lost its window")

    stale = get_mock_request()
    stale.user = user
    stale.bearer = "bearer"
    stale.auth_token = SimpleNamespace(token=JWToken(
        user.get_auth_key()).create_access_token(
            uid=user.pk, auth_time=int(time.time()) - 900))
    assert fresh_auth.is_fresh(stale, seconds=300) is False, (
        "a stale interactive token passed freshness")
    stale.oauth_grant = SimpleNamespace(pk=1)
    assert fresh_auth.is_fresh(stale, seconds=300) is False, (
        "a stale OAuth token passed freshness")
    assert fresh_auth.is_fresh(stale, seconds=0) is True, (
        "the deployment off switch did not disable freshness")

    package = UserAPIKey.create_for_user(user, label="assistant automation")
    generated = UserAPIKey.objects.get(pk=package.id)
    machine = get_mock_request()
    machine.user = user
    machine.bearer = "bearer"
    machine.user_api_key = generated
    machine.api_key = None
    machine.group_token = None
    machine.oauth_grant = None
    assert fresh_auth.is_fresh(machine, seconds=300) is True, (
        "validated UserAPIKey was forced through interactive freshness")
    meta = agent._build_request_meta(machine)
    context = tools._actor_context(
        user, meta, untrusted={"credential_kind": "user", "user_id": 999})
    assert context == {
        "credential_kind": "user_api_key", "user_id": user.pk,
        "user_api_key_id": generated.pk,
        "user_api_key_label": "assistant automation"}, (
        "Assistant actor context trusted spoofed input or lost key provenance")

    entry = {"fresh_auth_seconds": CONFIGURED_FRESH_AUTH}
    required = SimpleNamespace(fresh_auth_seconds=300)
    with th.assert_raises(approvals.ApprovalRefused):
        approvals._require_fresh_auth(entry, required, user, stale)
    stale.oauth_grant = SimpleNamespace(pk=1)
    with th.assert_raises(approvals.ApprovalRefused):
        approvals._require_fresh_auth(entry, required, user, stale)
    approvals._require_fresh_auth(entry, required, user, machine)

    def policy(suffix):
        return {
            "name": f"Assistant {suffix}",
            "category": f"{PREFIX}:assistant:{suffix}",
            "handlers": [{"type": "notify", "permission": "manage_security"}],
            "rules": [{"field": "level", "operator": ">=", "value": 8,
                       "value_type": "int"}],
            "is_active": False,
        }

    machine_result = tools._tool_create_rule({
        "confirm": "CREATE RULESET", "ruleset": policy("machine"),
    }, user, request_meta=meta)
    interactive = get_mock_request()
    interactive.user = user
    interactive.bearer = "bearer"
    interactive.user_api_key = None
    interactive.api_key = None
    interactive.group_token = None
    interactive.oauth_grant = None
    interactive_result = tools._tool_create_rule({
        "confirm": "CREATE RULESET", "ruleset": policy("interactive"),
    }, user, request_meta=agent._build_request_meta(interactive))
    from mojo.apps.incident.models import Event
    machine_audit = Event.objects.filter(
        category="security:admin_action",
        metadata__object_id=machine_result["data"]["id"]).latest("pk")
    interactive_audit = Event.objects.filter(
        category="security:admin_action",
        metadata__object_id=interactive_result["data"]["id"]).latest("pk")
    assert machine_audit.metadata["actor"] == context, (
        "Assistant machine action audit lost its server-derived actor")
    assert interactive_audit.metadata["actor"] == {
        "credential_kind": "user", "user_id": user.pk}, (
        "Assistant interactive action audit lost its server-derived actor")


@th.django_unit_test("mutable-sort discovery cursors detect snapshot drift")
def test_mutable_sort_pagination_restarts(opts):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.incident.models import RuleSet
    from mojo.apps.incident.services import admin_security

    group = Group.objects.create(name=f"{PREFIX}-paging", kind="organization")
    key, _token = ApiKey.create_for_group(
        group, f"{PREFIX}-paging-key", permissions={"view_security": True})
    now = timezone.now()
    cases = [_case(group, key, str(index), now - timedelta(minutes=index))
             for index in range(4)]
    group_authority = admin_security.SecurityAuthority(
        "group", "api_key", None, group_id=group.pk)
    first = admin_security.overview(
        {"sections": "cases", "limit": 2}, authority=group_authority)
    cursor = first["sections"]["cases"]["next_cursor"]
    cases[-1].last_seen = now - timedelta(seconds=30)
    cases[-1].save(update_fields=["last_seen", "modified"])
    try:
        admin_security.overview(
            {"sections": "cases", "page_cursor": cursor},
            authority=group_authority)
    except admin_security.SecurityActionError as error:
        assert error.status == 409 and error.code == "stale_cursor", (
            "Case cursor drift did not return the typed restart conflict")
    else:
        assert False, "a changed Case sort key must restart pagination"

    rows = [RuleSet.objects.create(
        name=f"paging-{index}", category=f"{PREFIX}:paging:{index}",
        priority=index) for index in range(4)]
    first = admin_security.overview({"sections": "rules", "limit": 2})
    cursor = first["sections"]["rules"]["next_cursor"]
    rows[-1].priority = 0
    rows[-1].save(update_fields=["priority", "modified"])
    try:
        admin_security.overview({"sections": "rules", "page_cursor": cursor})
    except admin_security.SecurityActionError as error:
        assert error.status == 409 and error.code == "stale_cursor", (
            "RuleSet cursor drift did not return the typed restart conflict")
    else:
        assert False, "a changed RuleSet sort key must restart pagination"


@th.django_unit_test("secret scrubbing catches credential shapes without hiding operations")
def test_secret_scrubber_leak_and_preservation(opts):
    from mojo.apps.incident.services import admin_security_transport as transport

    evidence = {
        "credentials": {
            "username": "deploy-user", "host": "edge-a.example.test",
            "path": "/usr/bin/deploy", "secret_key": "secret-one",
            "aws_secret_access_key": "secret-two"},
        "headers": {"Host": "api.example.test", "X-Api-Key": "secret-three",
                    "Cookie": "sessionid=secret-four; theme=dark"},
        "cookies": {"session": "secret-five", "theme": "dark"},
        "credential": ["secret-nine", {
            "username": "backup-user", "password": "secret-ten"}],
        "url": ("https://deploy-user:secret-six@api.example.test/run"
                "?otp=secret-seven&X-Amz-Signature=secret-eleven&view=raw"),
        "error": "worker said Bearer is a deployment mode",
        "command": "/usr/bin/check --host api.example.test",
        "handler": "job://custom.audit?mode=raw",
        "mfa_code": "secret-eight",
    }
    rendered = transport.scrub(evidence)
    text = json.dumps(rendered, sort_keys=True)
    for secret in ("secret-one", "secret-two", "secret-three", "secret-four",
                   "secret-five", "secret-six", "secret-seven", "secret-eight",
                   "secret-nine", "secret-ten", "secret-eleven"):
        assert secret not in text, f"credential value leaked: {secret}"
    for retained in ("deploy-user", "edge-a.example.test", "/usr/bin/deploy",
                     "backup-user", "theme=dark", "view=raw",
                     "Bearer is a deployment mode",
                     "/usr/bin/check", "job://custom.audit"):
        assert retained in text, f"operational evidence was over-redacted: {retained}"


@th.django_unit_test("large legacy RuleSets remain listable and completely readable")
def test_large_legacy_ruleset_transport(opts):
    from mojo.apps.incident.models import Rule, RuleSet
    from mojo.apps.incident.services import admin_security

    long_name = "legacy-name-" + ("x" * 900)
    row = RuleSet.objects.create(
        name=long_name, category=f"{PREFIX}:large", handler="job://custom.audit")
    Rule.objects.bulk_create([Rule(
        parent=row, name=f"condition-{index}", index=index,
        field_name="level", comparator=">=", value=str(index),
        value_type="int") for index in range(40)])

    with CaptureQueriesContext(connection) as captured:
        listed = admin_security.overview({"sections": "rules", "limit": 100})
    summary = next(item for item in listed["sections"]["rules"]["data"]
                   if item["id"] == row.pk)
    assert summary["rule_count"] == 40, "legacy RuleSet count was capped"
    assert isinstance(summary["name"], str) and len(summary["name"]) <= 512, (
        "RuleSet discovery name was not bounded")
    assert not any(
        'FROM "incident_rule"' in query["sql"] and '"incident_rule"."value"' in query["sql"]
        for query in captured.captured_queries), (
        "a discovery summary must not prefetch every legacy child condition")

    detail = admin_security.overview({
        "sections": "rules", "ruleset_id": row.pk,
    })["sections"]["rules"]["data"][0]
    assert isinstance(detail["name"], dict) and detail["name"]["encoding"] == "text", (
        "long RuleSet name was not transported as a chunk")
    assert _chunk_value(detail, "name") == long_name, (
        "long RuleSet name was not completely readable")
    assert len(_chunk_value(detail, "rules")) == 40, (
        "legacy RuleSet detail lost conditions above the governed mutation cap")

    api_source = open(
        "mojo/apps/account/admin_portal_v2/assets/features/security/api.js",
        encoding="utf-8").read()
    assert "validChunk(row.name)" in api_source, (
        "Admin UI rejects chunked legacy names before expansion")
    assert "validChunk(row.rules)" in api_source, (
        "Admin UI rejects chunked legacy conditions before expansion")


@th.django_unit_test("Event discovery bounds titles while detail remains complete")
def test_event_title_bounds(opts):
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.services import admin_security

    title = "event-title-" + ("e" * 900)
    row = Event.objects.create(category=f"{PREFIX}:title", title=title)
    listed = admin_security.overview({"sections": "events", "limit": 100})
    summary = next(item for item in listed["sections"]["events"]["data"]
                   if item["id"] == row.pk)
    assert len(summary["title"]) <= 512, "Event discovery title was not bounded"
    detail = admin_security.overview({
        "sections": "events", "event_id": row.pk,
    })["sections"]["events"]["data"][0]
    assert _chunk_value(detail, "title") == title, (
        "Event detail did not retain the complete title")


@th.django_unit_test("every group-readable evidence path preserves exact tenant scope")
def test_exact_group_lists_details_chunks_nested_and_aliases(opts):
    from urllib.parse import quote

    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.incident.models import (
        Event, Incident, MojoSecExecutionAttempt, MojoSecRecommendation,
        MojoSecRecommendationTarget, MojoSecRecommendationTransition)
    from mojo.apps.incident.services import admin_security

    now = timezone.now()

    def evidence(suffix, ip):
        group = Group.objects.create(
            name=f"{PREFIX}-scope-{suffix}", kind="organization")
        key, token = ApiKey.create_for_group(
            group, f"{PREFIX}-scope-key-{suffix}",
            permissions={"view_security": True})
        case = _case(group, key, f"scope-{suffix}", now)
        case.samples = [{"tenant": suffix, "ip": ip}]
        case.observed_sources = [ip]
        case.breakdown = {"tenant": suffix}
        case.distinct_source_count = 1
        case.save(update_fields=[
            "samples", "observed_sources", "breakdown",
            "distinct_source_count", "modified"])
        incident = Incident.objects.create(
            group=group, category=f"{PREFIX}:scope:{suffix}",
            title=f"incident-{suffix}", details=f"evidence-{suffix}",
            metadata={"tenant": suffix}, source_ip=ip)
        event = Event.objects.create(
            group=group, incident=incident,
            category=f"{PREFIX}:scope:{suffix}", title=f"event-{suffix}",
            details=(f"tenant={suffix} path=/var/log/{suffix} " * 1200),
            metadata={"tenant": suffix}, source_ip=ip)
        recommendation = MojoSecRecommendation.objects.create(
            group=group, installation_key=key, case=case,
            action=MojoSecRecommendation.ACTION_BLOCK_IP,
            reason_code=f"scope_{suffix}", explanation=f"explanation-{suffix}",
            confidence="high", urgency="high", requested_scope="installation",
            requested_ttl_seconds=300,
            idempotency_key=uuid.uuid4().hex,
            expires_at=now + timedelta(hours=1), target_count=1,
            validated_count=1, approval_note=f"approval-{suffix}",
            collateral=f"collateral-{suffix}")
        target = MojoSecRecommendationTarget.objects.create(
            recommendation=recommendation, ip=ip,
            validation_state="validated", outcome="applied", attempts=1)
        transition = MojoSecRecommendationTransition.objects.create(
            recommendation=recommendation, transition="proposed",
            reason=f"reason-{suffix}", from_state="", to_state="proposed",
            actor_id_snapshot=0, target_count=1, validated_count=1,
            protected_count=0, executed_count=0, failed_count=0,
            reversed_count=0)
        attempt = MojoSecExecutionAttempt.objects.create(
            recommendation=recommendation, target=target, attempt_number=1,
            started_at=now, finished_at=now, outcome="applied",
            detail=f"attempt-{suffix}")
        return {
            "group": group, "key": key, "token": token, "case": case,
            "incident": incident, "event": event,
            "recommendation": recommendation, "target": target,
            "transition": transition, "attempt": attempt,
        }

    own = evidence("a", "203.0.113.20")
    foreign = evidence("b", "198.51.100.20")
    authority = admin_security.SecurityAuthority(
        "group", "api_key", own["key"], group_id=own["group"].pk)
    foreign_authority = admin_security.SecurityAuthority(
        "group", "api_key", foreign["key"], group_id=foreign["group"].pk)
    sections = {
        "cases": ("case_id", "case"),
        "incidents": ("incident_id", "incident"),
        "events": ("event_id", "event"),
        "recommendations": ("recommendation_id", "recommendation"),
    }
    details = {}
    for section, (parameter, key) in sections.items():
        listed = admin_security.overview(
            {"section": section, "limit": 100}, authority=authority)
        ids = {item["id"] for item in listed["sections"][section]["data"]}
        assert own[key].pk in ids and foreign[key].pk not in ids, section
        detail = admin_security.overview(
            {"section": section, parameter: own[key].pk},
            authority=authority)["sections"][section]["data"]
        hidden = admin_security.overview(
            {"section": section, parameter: foreign[key].pk},
            authority=authority)["sections"][section]["data"]
        assert len(detail) == 1 and detail[0]["id"] == own[key].pk, section
        assert hidden == [], f"foreign {section} detail disclosed existence"
        details[section] = detail[0]

    chunk_fields = {
        "cases": ("samples", "observed_sources", "breakdown"),
        "incidents": ("title", "details", "metadata"),
        "events": ("title", "details", "metadata"),
        "recommendations": (
            "explanation", "approval_note", "collateral", "targets",
            "transitions", "attempts"),
    }
    decoded = {}
    for section, fields in chunk_fields.items():
        decoded[section] = {
            field: _chunk_value(details[section], field, authority=authority)
            for field in fields}
    assert decoded["recommendations"]["targets"][0]["id"] == own["target"].pk, (
        "recommendation target detail crossed scope or disappeared")
    assert decoded["recommendations"]["transitions"][0]["id"] == own["transition"].pk, (
        "recommendation transition detail crossed scope or disappeared")
    assert decoded["recommendations"]["attempts"][0]["id"] == own["attempt"].pk, (
        "recommendation attempt detail crossed scope or disappeared")
    assert foreign["target"].pk not in {
        item["id"] for item in decoded["recommendations"]["targets"]}, (
        "foreign recommendation target appeared in nested detail")

    cross_cursor = details["events"]["details"]["next_cursor"]
    assert cross_cursor, "large event evidence must exercise a resumed chunk"
    try:
        admin_security.overview(
            {"chunk_cursor": cross_cursor}, authority=foreign_authority)
    except admin_security.SecurityActionError as error:
        assert error.code == "invalid_cursor" and error.status == 400, (
            "cross-scope chunk replay did not fail as an invalid cursor")
    else:
        assert False, "a signed detail cursor must stay bound to its exact group"

    headers = {"Authorization": f"apikey {own['token']}"}
    for section, (parameter, key) in sections.items():
        listed = opts.client.get(
            f"/api/incident/admin/security?section={section}",
            headers=headers)
        assert listed.status_code == 200, listed.response
        ids = {item["id"] for item in
               listed.response.data["sections"][section]["data"]}
        assert own[key].pk in ids and foreign[key].pk not in ids, (
            f"wire {section} list crossed exact group scope")
        hidden = opts.client.get(
            f"/api/incident/admin/security?section={section}"
            f"&{parameter}={foreign[key].pk}", headers=headers)
        assert hidden.status_code == 200, hidden.response
        assert hidden.response.data["sections"][section]["data"] == [], (
            f"wire foreign {section} detail disclosed existence")

    cross_wire = opts.client.get(
        "/api/incident/admin/security?chunk_cursor=" + quote(cross_cursor),
        headers={"Authorization": f"apikey {foreign['token']}"})
    assert cross_wire.status_code == 400, cross_wire.response
    for path, payload in (
            ("/api/incident/admin/security/action", {"action": "not-real"}),
            ("/api/incident/ipset/action", {"action": "sync"}),
            ("/api/incident/mojosec/recommendation-action",
             {"action": "approve"})):
        denied = opts.client.post(path, payload, headers=headers)
        assert denied.status_code == 403, (path, denied.response)
