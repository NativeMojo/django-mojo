from testit import helpers as th


PREFIX = "admin-security-compatibility"


@th.django_unit_setup()
def setup_compatibility(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.incident.models import Event, RuleSet

    Event.objects.filter(category__startswith=PREFIX).delete()
    RuleSet.objects.filter(category__startswith=PREFIX).delete()
    User.objects.filter(username__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()


@th.django_unit_test("Admin Security preserves API-first routes and legacy policy CRUD")
def test_api_first_route_and_legacy_crud_contract(opts):
    import importlib

    from mojo.apps.incident.models import Event, Rule, RuleSet
    views = importlib.import_module("mojo.apps.incident.rest.admin_security")
    event_views = importlib.import_module("mojo.apps.incident.rest.event")

    assert not getattr(views.on_admin_security, "_mojo_denies_key_backed_session", False), (
        "Admin Security reads must not impose a blanket human-only session gate")
    assert not getattr(
        views.on_admin_security_action, "_mojo_denies_key_backed_session", False), (
        "Admin Security mutations must recognize validated per-user machine credentials")
    assert getattr(views.on_admin_security_action, "_mojo_fresh_auth_seconds", None) is None, (
        "Admin Security mutations must use the configured freshness window")
    assert RuleSet.RestMeta.CAN_CREATE is True, (
        "legacy RuleSet REST creation must remain available")
    assert RuleSet.RestMeta.CAN_UPDATE is True, (
        "legacy RuleSet REST updates must remain available")
    assert RuleSet.RestMeta.CAN_DELETE is True, (
        "legacy RuleSet REST deletion must remain available")
    assert Rule.RestMeta.CAN_CREATE is True, (
        "legacy Rule REST creation must remain available")
    assert Rule.RestMeta.CAN_UPDATE is True, (
        "legacy Rule REST updates must remain available")
    assert Rule.RestMeta.CAN_DELETE is True, (
        "legacy Rule REST deletion must remain available")
    assert getattr(event_views.on_event_ruleset, "_mojo_secured_model", None) is RuleSet, (
        "RuleSet compatibility route must register its model-security contract")
    assert getattr(event_views.on_event_ruleset_rule, "_mojo_secured_model", None) is Rule, (
        "Rule compatibility route must register its model-security contract")

    event = Event(
        pk=991, level=8, scope="global", category="security:test",
        source_ip="203.0.113.7", hostname="worker-1", title="Blocked request",
        details="command=/usr/bin/check --target 203.0.113.7",
        metadata={"evidence": {"path": "/var/log/app.log"}},
    )
    projection = event.admin_security_projection()
    assert projection["source_ip"] == "203.0.113.7", (
        "authorized Admin Security evidence must retain source addresses")
    assert projection["details"].startswith("command="), (
        "authorized Admin Security evidence must retain operational details")
    assert projection["metadata"]["evidence"]["path"] == "/var/log/app.log", (
        "authorized Admin Security evidence must retain structured evidence")


@th.django_unit_test("markerless legacy handlers continue to publish after upgrade")
def test_markerless_legacy_handler_continues(opts):
    from mojo.apps.incident.models import Event, RuleSet

    category = "test:admin-security:legacy-handler"
    RuleSet.objects.filter(category=category).delete()
    row = RuleSet.objects.create(
        category=category, name="Legacy custom handler",
        handler="job://legacy.handlers.audit?mode=raw", is_active=True,
    )
    event = Event(pk=992, category=category, level=5)
    published = []

    result = row.run_handler(
        event, publisher=lambda *args, **kwargs: published.append((args, kwargs)))

    assert result is True, (
        "an unmarked legacy RuleSet must keep dispatching its established handler")
    assert len(published) == 1, "the legacy handler must enqueue exactly one job"
    payload = published[0][0][1]
    assert payload["execution_mode"] == "legacy", (
        "queued work must durably record that it uses the legacy execution path")
    assert "handler_schema" not in payload, (
        "legacy queued work must not claim governed-schema validation")


@th.django_unit_test("refresh exchange enforces purpose and stamps validated user API key provenance")
def test_refresh_purpose_and_user_api_key_provenance(opts):
    from testit.helpers import get_mock_request
    from mojo.apps.account.models import User, UserAPIKey
    from mojo.apps.account.services import fresh_auth
    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.decorators.limits import clear_rate_limits

    user = User.objects.create_user(
        username=f"{PREFIX}-token", email=f"{PREFIX}-token@example.test",
        password="AdminSecurityCompat##1")
    user.is_active = True
    user.add_permission("manage_security")
    user.save()
    package = JWToken(user.get_auth_key()).create(uid=user.pk, auth_time=1)
    clear_rate_limits(ip="127.0.0.1", key="refresh_token")
    denied = opts.client.post(
        "/api/refresh_token", {"refresh_token": package.access_token})
    assert denied.status_code == 401, (
        "an access token must never be accepted as a refresh credential")

    generated = UserAPIKey.create_for_user(user, label="automation")
    denied_key = opts.client.post(
        "/api/refresh_token", {"refresh_token": generated.token})
    assert denied_key.status_code == 401, (
        "a per-user API key must never mint an interactive token pair")
    accepted = opts.client.post(
        "/api/refresh_token", {"refresh_token": package.refresh_token})
    assert accepted.status_code == 200, (
        "a purpose-bound refresh token must retain the existing exchange")

    request = get_mock_request()
    request.bearer = "bearer"
    validated, error = User.validate_jwt(generated.token, request)
    assert error is None and validated.pk == user.pk
    assert request.user_api_key.pk == generated.id, (
        "positive provenance must name the validated server-side key record")
    assert fresh_auth.is_fresh(request, seconds=300) is True, (
        "a machine credential must not be forced through impossible interactive reauthentication")
    read = opts.client.get(
        "/api/incident/admin/security?sections=overview",
        headers={"Authorization": f"Bearer {generated.token}"})
    assert read.status_code == 200, (
        "a validated per-user API key must reach the Admin Security REST read")
    assert read.response.data["capabilities"]["credential_kind"] == "user_api_key", (
        "the REST response must report server-validated credential provenance")
    mutation = opts.client.post(
        "/api/incident/admin/security/action", {"action": "not-real"},
        headers={"Authorization": f"Bearer {generated.token}"})
    assert mutation.status_code == 400, (
        "a global per-user API key must reach action validation without a human-only or freshness refusal")


@th.django_unit_test("Admin Security group authority is derived from the credential and cannot cross tenants")
def test_exact_group_authority_and_scope(opts):
    from types import SimpleNamespace
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.services import admin_security

    group_a = Group.objects.create(name=f"{PREFIX}-group-a", kind="organization")
    group_b = Group.objects.create(name=f"{PREFIX}-group-b", kind="organization")
    event_a = Event.objects.create(
        group=group_a, category=f"{PREFIX}:group-a", source_ip="203.0.113.10")
    event_b = Event.objects.create(
        group=group_b, category=f"{PREFIX}:group-b", source_ip="198.51.100.10")
    key = SimpleNamespace(
        pk=71, group=group_a, group_id=group_a.pk, override_user=False,
        is_authenticated=True, has_permission=lambda perms: True)
    request = SimpleNamespace(
        user=key, api_key=key, group_token=None, user_api_key=None,
        oauth_grant=None, group=group_b, META={}, DATA={"group": group_b.pk})

    authority = admin_security.build_authority(request)
    assert authority.scope == "group" and authority.group_id == group_a.pk, (
        "the authenticated credential group must win over request-supplied group values")
    listed = admin_security.overview(
        {"sections": "events", "limit": 100}, authority=authority)
    ids = {row["id"] for row in listed["sections"]["events"]["data"]}
    assert event_a.pk in ids and event_b.pk not in ids, (
        "group evidence lists must stay inside the exact authenticated tenant")
    hidden = admin_security.overview(
        {"sections": "events", "event_id": event_b.pk}, authority=authority)
    assert hidden["sections"]["events"]["data"] == [], (
        "an out-of-scope direct lookup must not disclose object existence")

    api_key, raw_token = ApiKey.create_for_group(
        group_a, f"{PREFIX}-reader", permissions={"view_security": True})
    wire = opts.client.get(
        "/api/incident/admin/security?sections=events",
        headers={"Authorization": f"apikey {raw_token}"})
    assert wire.status_code == 200, (
        "a group API key with view permission must reach exact-group REST evidence: "
        f"{wire.status_code} {wire.response}")
    wire_ids = {row["id"] for row in wire.response.data["sections"]["events"]["data"]}
    assert event_a.pk in wire_ids and event_b.pk not in wire_ids, (
        "the wire API must keep the authenticated credential's exact group scope")
    foreign_scope = opts.client.get(
        "/api/incident/admin/security?sections=events&group=%s" % group_b.pk,
        headers={"Authorization": f"apikey {raw_token}"})
    assert foreign_scope.status_code == 403, (
        "a foreign client-supplied group must be refused before it can influence scope")
    global_denied = opts.client.get(
        "/api/incident/admin/security?sections=rules",
        headers={"Authorization": f"apikey {raw_token}"})
    assert global_denied.status_code == 403, (
        "group credentials must not read tenant-less global policy sections")
    api_key.delete()


@th.django_unit_test("evidence chunks preserve operations, scrub only secrets and bind cursors")
def test_secret_only_chunk_transport(opts):
    from mojo.apps.incident.services import admin_security
    from mojo.apps.incident.services import admin_security_transport as transport

    authority = admin_security.SecurityAuthority("global", "internal", None)
    operational = (
        "source=203.0.113.7 cidr=203.0.113.0/24 "
        "command=/usr/bin/firewall-check path=/var/log/security.log ")
    value = (operational * 300) + (
        "password=super-secret Authorization: Bearer bearer-secret "
        "auth_key=signing-secret \N{SNOWMAN}")
    first = transport.chunk(value, authority, "event", 91, "details", "r1")
    chunks = [first["chunk"]]
    cursor = first["next_cursor"]
    while cursor:
        page = transport.chunk(
            value, authority, "event", 91, "details", "r1", cursor=cursor)
        chunks.append(page["chunk"])
        cursor = page["next_cursor"]
    complete = "".join(chunks)
    assert operational in complete and "\N{SNOWMAN}" in complete, (
        "addresses, CIDRs, commands, paths and Unicode evidence must survive transport")
    for secret in ("super-secret", "bearer-secret", "signing-secret"):
        assert secret not in complete, f"authentication secret leaked: {secret}"
    assert complete.count(transport.REDACTED) == 3, (
        "secret scrubbing must be stable and limited to credential values")

    tampered = first["next_cursor"][:-1] + (
        "A" if first["next_cursor"][-1] != "A" else "B")
    with th.assert_raises(transport.TransportError):
        transport.read_cursor(tampered)
    cross_scope = admin_security.SecurityAuthority(
        "group", "api_key", None, group_id=999)
    with th.assert_raises(transport.TransportError):
        transport.chunk(
            value, cross_scope, "event", 91, "details", "r1",
            cursor=first["next_cursor"])
    with th.assert_raises(transport.TransportError):
        transport.chunk(
            value + "changed", authority, "event", 91, "details", "r1",
            cursor=first["next_cursor"])


@th.django_unit_test("rule reparenting advances both legacy aggregate revisions")
def test_reparent_advances_both_revisions(opts):
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.incident.models import Rule, RuleSet

    first = RuleSet.objects.create(name="first", category=f"{PREFIX}:first")
    second = RuleSet.objects.create(name="second", category=f"{PREFIX}:second")
    condition = Rule.objects.create(
        parent=first, field_name="level", comparator=">=", value="5",
        value_type="int")
    old = timezone.now() - timedelta(days=1)
    RuleSet.objects.filter(pk__in=(first.pk, second.pk)).update(modified=old)
    condition.parent = second
    condition.save()
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.modified > old and second.modified > old, (
        "moving a legacy condition must invalidate both aggregate revisions")


@th.django_unit_test("generic REST hooks separate legacy CRUD from governed lifecycle")
def test_governed_marker_lifecycle_hooks(opts):
    from mojo.apps.incident.models import Rule, RuleSet
    from mojo.apps.incident.services import rule_validation

    legacy = RuleSet.objects.create(name="legacy", category=f"{PREFIX}:hooks")
    legacy.on_rest_pre_save({}, False)
    legacy.metadata = rule_validation.mark_governed()
    with th.assert_raises(Exception):
        legacy.on_rest_pre_save({"metadata": {}}, False)

    governed = RuleSet.objects.create(
        name="governed", category=f"{PREFIX}:governed-hooks",
        metadata=rule_validation.mark_governed())
    with th.assert_raises(Exception):
        governed.on_rest_pre_save({}, False)
    with th.assert_raises(Exception):
        governed.on_rest_pre_delete()
    child = Rule(
        parent=governed, field_name="level", comparator=">=", value="5",
        value_type="int")
    with th.assert_raises(Exception):
        child.on_rest_pre_save({}, True)
