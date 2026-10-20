"""DNS setup discovery and real MCP -> PendingAction -> handler dispatch.

The fake replaces only the DNS setup service's external-world result. Exact
local authority and target resolution use the real service, and approval
creation/resolution use the real projected registry. No provider call is made.
"""

TESTIT_TIER = "extended"

import uuid
from unittest import mock

from testit import helpers as th


TOOL_NAMES = {
    "list_dns_setup_groups", "get_dns_validation_plan", "connect_dns_validation",
    "check_dns_validation", "request_dns_wildcard_certificate",
}
WRITERS = TOOL_NAMES - {"list_dns_setup_groups", "get_dns_validation_plan"}
EMAIL = "dns-tools-operator@assistant.test"
LIMITED_EMAIL = "dns-tools-viewer@assistant.test"
RESOURCE = "https://dns-tools.example/api/assistant/mcp"


@th.django_unit_setup()
def setup_dns_tools(opts):
    from mojo.apps.account.models import Group, OAuthClient, User
    from mojo.apps.account.services.oauth_server import tokens
    from mojo.apps.dnsman.models import AcmeDelegation, Domain

    User.objects.filter(email__in=[EMAIL, LIMITED_EMAIL]).delete()
    OAuthClient.objects.filter(client_id="testit-dns-tools").delete()
    Domain.objects.filter(name="dns-tools.assistant.test").delete()
    Group.objects.filter(name="testit-assistant-dns-tools").delete()
    opts.dns_user = User.objects.create_user(
        username=EMAIL, email=EMAIL, password="TestitDns1!")
    opts.dns_user.is_email_verified = True
    opts.dns_user.save()
    opts.dns_user.add_permission(["view_admin", "view_dns", "manage_dns"])
    opts.dns_limited = User.objects.create_user(
        username=LIMITED_EMAIL, email=LIMITED_EMAIL, password="TestitDns1!")
    opts.dns_limited.add_permission("view_admin")
    opts.dns_group = Group.objects.create(name="testit-assistant-dns-tools")
    opts.dns_domain = Domain.objects.create(
        name="dns-tools.assistant.test", group=opts.dns_group,
        provider="godaddy", status="active")
    opts.dns_row = AcmeDelegation.objects.create(
        domain=opts.dns_domain, group=opts.dns_group,
        tenant_uuid=opts.dns_group.get_uuid(), tenant_name=opts.dns_group.name,
        domain_name=opts.dns_domain.name, connection_mode="shared",
        hub_allocation_ref="fixed.hub.example", hub_consumer_project_uuid=str(uuid.uuid4()),
        source_name="_acme-challenge.dns-tools.assistant.test",
        target_name="fixed.hub.example", state="pending")
    client = OAuthClient.objects.create(
        client_id="testit-dns-tools", client_name="DNS Tools Test", kind="dcr",
        redirect_uris=["http://127.0.0.1:8539/callback"])
    opts.dns_grant = tokens.create_grant(opts.dns_user, client, ["mcp"], RESOURCE, 1700000000)


class _Setup:
    def __init__(self, opts):
        from mojo.apps.dnsman.services import delegation_setup

        self.real = delegation_setup
        self.SetupError = delegation_setup.SetupError
        self.calls = []
        self.reads = 0
        self.consumer = opts.dns_row.hub_consumer_project_uuid
        self.trust = "active"
        self.row = opts.dns_row
        self.opts = opts

    def group_choices(self, actor, **kwargs):
        return self.real.group_choices(actor, **kwargs)

    def resolve_target(self, **kwargs):
        return self.real.resolve_target(**kwargs)

    def resolve_connection(self, value, **kwargs):
        return self.real.resolve_connection(value, **kwargs)

    def plan(self, target, allocation_ref=None, actor=None):
        self.reads += 1
        self.real.resolve_target(domain=target.domain.pk, actor=actor)
        writable = self.real.can_access(actor, target.group, write=True)
        return {
            "version": 1, "checked_at": "2026-10-20T21:00:00Z",
            "domain": {"id": target.domain.pk, "name": target.name,
                       "provider": target.domain.provider,
                       "group": {"id": target.group.pk, "name": target.group.name}},
            "profile": {"name": "apex_wildcard", "names": [target.name, "*." + target.name]},
            "hub": {"state": "ready", "protocol_version": 2,
                    "consumer_project_uuid": self.consumer},
            "connection": {"id": self.row.pk, "mode": "shared", "state": "pending",
                           "client_ref": str(self.row.client_ref),
                           "allocation_ref": self.row.hub_allocation_ref,
                           "participant_ref": self.row.hub_participant_ref,
                           "verified_at": None},
            "trust": {"state": self.trust, "can_publish": self.trust == "active",
                      "can_withdraw": True},
            "alias": {"state": "match", "source": self.row.source_name,
                      "target": self.row.target_name, "observed_target": self.row.target_name,
                      "one_hop": True, "checked_at": "2026-10-20T21:00:00Z"},
            "certificate": None,
            "renewal": {"state": "not_requested", "next_attempt_at": None,
                        "attempts": 0, "error_code": None, "cleanup_pending": False,
                        "job": {"id": None, "state": None}, "worker_version": None},
            "capabilities": {"group_choices": writable, "can_connect": writable,
                             "can_restart": writable, "can_check": writable and self.trust == "active",
                             "can_request": writable and self.trust == "active"},
            "next_action": {"code": "check", "actor": "project_operator"},
            "connection_request": None,
            "operation": {"action": "plan", "outcome": "ready", "changed": False},
        }

    def _write(self, action, target, actor):
        self.real.resolve_target(domain=target.domain.pk, actor=actor, write=True)
        self.calls.append(action)
        dto = self.plan(target, actor=actor)
        dto["operation"] = {"action": action, "outcome": "completed", "changed": True}
        return dto

    def connect(self, target, allocation_ref, actor=None, restart=False):
        return self._write("connect", target, actor)

    def check(self, row, actor=None):
        target = self.real.resolve_target(domain=row.domain_id, actor=actor, write=True)
        return self._write("check", target, actor)

    def request(self, row, actor=None):
        target = self.real.resolve_target(domain=row.domain_id, actor=actor, write=True)
        return self._write("request", target, actor)


def _call(opts, name, args):
    from mojo.apps.assistant.mcp import server
    from mojo.apps.assistant.services import agent

    req = th.get_mock_request(user=opts.dns_user, path="/api/assistant/mcp", method="POST")
    req.oauth_grant = opts.dns_grant
    return server.call_tool(
        name, args, opts.dns_user, opts.dns_grant, agent._build_request_meta(req),
        hide_infrastructure=False, _reporter=lambda *_args, **_kwargs: None)


def _approve(opts, action_id):
    from mojo.apps.assistant.services import approvals

    return approvals.resolve(opts.dns_user, action_id, "approve",
                             _reporter=lambda *_args, **_kwargs: None)


@th.django_unit_test("DNS tools are autodiscovered, projected over MCP and gated by DNS authority")
def test_dns_discovery(opts):
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.mcp import server
    from mojo.apps.dnsman import assistant_tools

    setup = _Setup(opts)
    with mock.patch.object(assistant_tools, "_service", return_value=setup):
        visible = {row["name"]: row for row in server.list_tools(opts.dns_user, False)}
        limited = {row["name"] for row in server.list_tools(opts.dns_limited, False)}
    assert TOOL_NAMES <= set(visible), "All five DNS tools must be discoverable through the MCP projection"
    assert not (TOOL_NAMES & limited), "view_admin alone must never reveal DNS setup tools"
    for name in WRITERS:
        entry = get_registry()[name]
        assert entry["mutates"] and entry["preview"] is not None, f"{name} must use a bound PendingAction preview"
        assert entry.get("owner_state") is None, f"{name} must never exempt itself from approval"
        assert visible[name]["annotations"]["readOnlyHint"] is False, f"{name} must advertise mutation"
    assert not any("grant" in name or "key" in name for name in TOOL_NAMES), \
        "The DNS setup surface must not add a trust or credential capability"


@th.django_unit_test("a real MCP DNS read returns the shared DTO without mutations")
def test_dns_mcp_read(opts):
    from mojo.apps.dnsman import assistant_tools

    setup = _Setup(opts)
    with mock.patch.object(assistant_tools, "_service", return_value=setup):
        result = _call(opts, "get_dns_validation_plan", {"domain": opts.dns_domain.pk})
    assert not result["isError"], f"An authorized readiness read must succeed, got {result}"
    assert result["structuredContent"]["version"] == 1, "MCP must return the same readiness version"
    assert not setup.calls, "A plan must never dispatch a DNS setup mutation"


@th.django_unit_test("view-only DNS authority can discover readiness but cannot prepare a mutation")
def test_dns_view_only_discovery(opts):
    from mojo.apps.assistant.mcp import server
    from mojo.apps.dnsman import assistant_tools

    setup = _Setup(opts)
    opts.dns_limited.add_permission("view_dns")
    try:
        with mock.patch.object(assistant_tools, "_service", return_value=setup):
            names = {row["name"] for row in server.list_tools(opts.dns_limited, False)}
            assert "get_dns_validation_plan" in names, "A DNS viewer must be able to discover the readiness read"
            dto = assistant_tools.get_dns_validation_plan({"domain": opts.dns_domain.pk}, opts.dns_limited)
            assert dto["capabilities"]["can_connect"] is False, "A view-only result must never advertise a mutation"
            try:
                assistant_tools._preview("connect", {"domain": opts.dns_domain.pk,
                    "allocation_ref": "fixed.hub.example"}, opts.dns_limited)
            except (ValueError, PermissionError):
                pass
            else:
                assert False, "A DNS viewer must not create a mutating approval"
    finally:
        opts.dns_limited.remove_permission("view_dns")


@th.django_unit_test("each DNS writer proposes over MCP and runs once through real approval resolution")
def test_dns_mcp_approval_dispatch(opts):
    from mojo.apps.assistant.models import PendingAction
    from mojo.apps.dnsman import assistant_tools

    setup = _Setup(opts)
    requests = [
        ("connect_dns_validation", {"domain": opts.dns_domain.pk,
                                    "allocation_ref": "fixed.hub.example"}, "connect"),
        ("check_dns_validation", {"delegation": opts.dns_row.pk}, "check"),
        ("request_dns_wildcard_certificate", {"delegation": opts.dns_row.pk}, "request"),
    ]
    with mock.patch.object(assistant_tools, "_service", return_value=setup):
        for name, args, action in requests:
            proposed = _call(opts, name, args)["structuredContent"]
            assert proposed.get("status") == "approval_required", f"{name} must propose before dispatch, got {proposed}"
            before = len(setup.calls)
            result = _approve(opts, proposed["action_id"])
            assert result["block"]["state"] == "completed", f"Approved {name} must complete, got {result}"
            assert setup.calls[before:] == [action], f"{name} must dispatch exactly its chosen action once"
            row = PendingAction.objects.get(uuid=proposed["action_id"])
            assert row.result["operation"]["action"] == action, f"{name} must retain the exact readiness result"


@th.django_unit_test("changed consumer identity, alias or trust invalidates the real bound approval")
def test_dns_stale_preview_refuses(opts):
    from mojo.apps.assistant.services import approvals
    from mojo.apps.dnsman import assistant_tools

    for change in ("consumer", "trust"):
        setup = _Setup(opts)
        with mock.patch.object(assistant_tools, "_service", return_value=setup):
            proposed = _call(opts, "connect_dns_validation", {
                "domain": opts.dns_domain.pk, "allocation_ref": "fixed.hub.example",
                "restart": change == "trust"})["structuredContent"]
            assert proposed.get("status") == "approval_required", f"A valid DNS proposal must exist before {change} changes"
            setattr(setup, change, str(uuid.uuid4()) if change == "consumer" else "revoked")
            try:
                _approve(opts, proposed["action_id"])
            except approvals.ApprovalRefused as exc:
                assert exc.code == approvals.CODE_PRECONDITION, f"A changed {change} must fail the bound preview"
            else:
                assert False, f"A changed {change} must not execute a previously approved request"
        assert setup.calls == [], f"A stale {change} proposal must never reach the mutation handler"


@th.django_unit_test("foreign targets and lost DNS authority cannot create or execute a DNS approval")
def test_dns_target_and_permission_gates(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.assistant.services import approvals
    from mojo.apps.dnsman import assistant_tools
    from mojo.apps.dnsman.models import Domain
    from tests.test_dnsman._helpers import make_group_member

    user, _email, _password, group = make_group_member(["manage_dns", "view_dns"])
    user.add_permission("view_admin")
    foreign_group = Group.objects.create(name="dns-tools-foreign-" + uuid.uuid4().hex[:8])
    foreign = Domain.objects.create(name="foreign-" + uuid.uuid4().hex[:8] + ".example",
                                    group=foreign_group, provider="godaddy")
    setup = _Setup(opts)
    try:
        with mock.patch.object(assistant_tools, "_service", return_value=setup):
            try:
                assistant_tools._preview("connect", {"domain": foreign.pk,
                    "allocation_ref": "fixed.hub.example"}, user)
            except (ValueError, PermissionError):
                pass
            else:
                assert False, "A group DNS operator must not preview a foreign Domain"
            proposed = _call(opts, "connect_dns_validation", {
                "domain": opts.dns_domain.pk, "allocation_ref": "fixed.hub.example",
                "restart": True})["structuredContent"]
            assert proposed.get("status") == "approval_required", "A valid proposal must exist before authority is removed"
            opts.dns_user.remove_permission(["view_dns", "manage_dns"])
            try:
                _approve(opts, proposed["action_id"])
            except approvals.ApprovalRefused:
                pass
            else:
                assert False, "Lost DNS authority must refuse approval resolution"
    finally:
        opts.dns_user.add_permission(["view_dns", "manage_dns"])
        opts.dns_user.refresh_from_db()
        user.delete()
        group.delete()
        foreign.delete()
        foreign_group.delete()
    assert not setup.calls, "Foreign or stale-authority calls must never execute"


@th.django_unit_test("untrusted error details and forged credential/URL fields never enter the approval")
def test_dns_argument_and_error_redaction(opts):
    from mojo.apps.assistant.models import PendingAction
    from mojo.apps.dnsman import assistant_tools

    setup = _Setup(opts)
    secret = "DO-NOT-RENDER-FAKE-HUB-SECRET"
    with mock.patch.object(assistant_tools, "_service", return_value=setup):
        safe = assistant_tools._safe_error(RuntimeError(secret))
        proposed = _call(opts, "connect_dns_validation", {
            "domain": opts.dns_domain.pk, "allocation_ref": "fixed.hub.example",
            "hub_url": "https://evil.example", "api_key": secret,
            "restart": False})["structuredContent"]
    assert secret not in str(safe), "Arbitrary transport/provider exceptions must never render their content"
    row = PendingAction.objects.get(uuid=proposed["action_id"])
    assert set(row.args) == {"domain", "allocation_ref", "restart"}, \
        "Only the operator-visible schema arguments may be stored or dispatched"
    assert secret not in str(row.preview) + str(row.args) + str(proposed), \
        "Credential-shaped or unrecognized remote-agent inputs must not survive approval normalization"


@th.django_unit_test("a key-backed Assistant session cannot lose its underlying key confinement")
def test_dns_key_backed_session_refuses(opts):
    from mojo.apps.assistant.mcp import server
    from mojo.apps.assistant.models import PendingAction
    from mojo.apps.dnsman import assistant_tools

    setup = _Setup(opts)
    before = PendingAction.objects.filter(user=opts.dns_user).count()
    with mock.patch.object(assistant_tools, "_service", return_value=setup):
        read = assistant_tools.get_dns_validation_plan(
            {"domain": opts.dns_domain.pk}, opts.dns_user,
            request_meta={"key_backed": True})
        write = assistant_tools.connect_dns_validation(
            {"domain": opts.dns_domain.pk, "allocation_ref": "fixed.hub.example"},
            opts.dns_user, request_meta={"key_backed": True})
        proposed = server.call_tool("connect_dns_validation", {
            "domain": opts.dns_domain.pk, "allocation_ref": "fixed.hub.example"},
            opts.dns_user, opts.dns_grant, {"key_backed": True},
            hide_infrastructure=False, _reporter=lambda *_args, **_kwargs: None)
    assert read.get("error_code") == write.get("error_code") == "permission_denied", \
        "An override User must not replace the key's absent tenant authority in Assistant dispatch"
    assert setup.calls == [] and setup.reads == 0, "A key-backed Assistant call must refuse before any hub-backed service read or mutation"
    assert proposed["isError"] and PendingAction.objects.filter(user=opts.dns_user).count() == before, \
        "A key-backed mutating MCP proposal must refuse before preview I/O or approval creation"


@th.django_unit_test("approval re-preview uses current confined session rather than proposal authority")
def test_dns_resolution_current_session(opts):
    from mojo.apps.assistant.mcp import server
    from mojo.apps.assistant.services import approvals
    from mojo.apps.dnsman import assistant_tools
    setup = _Setup(opts)
    with mock.patch.object(assistant_tools, "_service", return_value=setup):
        for kind in ("api_key", "group_token"):
            proposed = _call(opts, "connect_dns_validation", {
                "domain": opts.dns_domain.pk, "allocation_ref": "fixed.hub.example", "restart": kind == "api_key"})["structuredContent"]
            assert proposed.get("status") == "approval_required", "ordinary consented OAuth session must propose a real action"
            reads = setup.reads
            try:
                approvals.resolve(opts.dns_user, proposed["action_id"], "approve",
                    request_meta={"key_backed": True, "credential_kind": kind},
                    _reporter=lambda *_args, **_kwargs: None)
            except approvals.ApprovalRefused:
                pass
            else:
                raise AssertionError("current confined authority must refuse a previously interactive proposal")
            assert setup.reads == reads and not setup.calls, "resolution must reject key/group-token context before reading the target or executing"
