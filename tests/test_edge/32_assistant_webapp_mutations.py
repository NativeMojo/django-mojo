"""Assistant `webapp` mutations at PROPOSAL time, plus the two executions whose
idempotency is load-bearing.

Proposal behaviour is what the approval gate acts on: a raising `preview` means
no PendingAction is ever created, and the bound `revision` is what makes a
follow-up model turn unable to substitute another tenant, resource or choice.
These tests drive `preview` and the handlers directly with a stub approval, so
they need none of #2569's transport.

In `tests/test_edge` for the same reason as 31: the edge fixtures write `EDGE_*`
settings, and this package is already serial.
"""

import uuid
from unittest import mock

from objict import objict
from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup, declare_pools, declare_release_buckets, make_certificate,
    make_domain, make_group, make_group_member, make_release, make_upstream,
    make_user, make_vhost, make_webapp, with_setting,
)


def _preview(name, params, user):
    from mojo.apps.assistant import get_registry

    return get_registry()[name]["preview"](params, user)


def _summarize(name, params, user):
    from mojo.apps.assistant import get_registry

    return get_registry()[name]["summarize"](params, user)


def _execute(name, params, user, approval):
    from mojo.apps.assistant import get_registry

    return get_registry()[name]["handler"](params, user, approval=approval)


def _read(name, params, user):
    """A read tool's handler, which must RETURN a refusal and never raise."""
    from mojo.apps.assistant import get_registry

    return get_registry()[name]["handler"](params, user)


def _stub_approval(revision=""):
    """What `approvals.resolve` hands a handler: a consumed record."""
    return objict(uuid=uuid.uuid4(), revision=revision, conversation=None,
                  group=None)


def _refusal(name, params, user):
    try:
        _preview(name, params, user)
    except (ValueError, PermissionError) as exc:
        return exc
    return None


@th.django_unit_setup()
def setup_assistant_webapp_mutations(opts):
    cleanup()
    declare_pools()
    declare_release_buckets()

    opts.group = make_group("edge-asmut")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.certificate, label="www",
                            kind="site_api")
    opts.webapp = make_webapp(opts.group, slug=f"mu{uuid.uuid4().hex[:6]}",
                              vhost=opts.vhost)
    opts.live = make_release(opts.webapp, "v1", status="uploaded")
    opts.pending = make_release(opts.webapp, "v2pending", status="pending")
    opts.upstream = make_upstream(group=opts.group)

    opts.other_group = make_group("edge-asmutother")
    opts.other_webapp = make_webapp(
        opts.other_group, slug=f"om{uuid.uuid4().hex[:6]}")
    opts.other_release = make_release(
        opts.other_webapp, "otherv1", status="uploaded")

    opts.manager, _, _, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=opts.group)
    opts.manager.add_permission(["view_admin"])
    opts.manager.save()

    opts.outsider, _, _ = make_user(["view_admin"])


@th.django_unit_test("every mutating preview refuses a cross-tenant app")
def test_previews_refuse_cross_tenant(opts):
    from mojo.apps.assistant import get_registry

    day2 = [name for name, entry in get_registry().items()
            if entry["domain"] == "webapp" and entry["mutates"]
            and "webapp" in (entry["definition"]["input_schema"]
                             .get("properties") or {})]
    assert len(day2) == 11, f"expected 11 app-scoped mutating tools, got {day2}"
    for name in day2:
        params = {"webapp": opts.other_webapp.pk, "reason": "cross tenant probe",
                  "hostname": "www.example.com", "vhost": 1, "release": 1,
                  "certificate": 1, "path_prefix": "/api", "upstream": "x",
                  "pool": "default"}
        error = _refusal(name, params, opts.manager)
        assert error is not None, \
            f"{name} proposed a write against another tenant's app"
        assert "available to you" in str(error), (
            f"{name} disclosed that a foreign app exists rather than giving "
            f"the shared refusal: {error}")


@th.django_unit_test("a lost grant refuses at proposal, so no card is ever created")
def test_previews_refuse_a_lost_grant(opts):
    from mojo.apps.account.models import GroupMember

    params = {"webapp": opts.webapp.pk, "reason": "grant removed mid-flight"}
    assert _refusal("take_webapp_offline", params, opts.manager) is None, \
        "setup did not leave the manager able to propose"

    member = GroupMember.objects.get(user=opts.manager, group=opts.group)
    member.permissions = {"view_dns": True}
    member.save(update_fields=["permissions", "modified"])
    try:
        error = _refusal("take_webapp_offline", params, opts.manager)
        assert error is not None, \
            "an operator whose WebApp grant was removed still proposed a write"
    finally:
        member.permissions = {"manage_webapp": True, "manage_dns": True}
        member.save(update_fields=["permissions", "modified"])


@th.django_unit_test("a deactivated workspace refuses at proposal")
def test_previews_refuse_an_inactive_group(opts):
    params = {"webapp": opts.webapp.pk, "reason": "workspace deactivated"}
    opts.group.is_active = False
    opts.group.save(update_fields=["is_active", "modified"])
    try:
        error = _refusal("take_webapp_offline", params, opts.manager)
        assert error is not None, \
            "a write was proposed against an app in a deactivated workspace"
    finally:
        opts.group.is_active = True
        opts.group.save(update_fields=["is_active", "modified"])


@th.django_unit_test("a missing or too-short reason is refused before anything is bound")
def test_reason_is_required_and_bounded(opts):
    for reason in (None, "", "  ", "no"):
        params = {"webapp": opts.webapp.pk}
        if reason is not None:
            params["reason"] = reason
        error = _refusal("delete_webapp", params, opts.manager)
        assert error is not None, \
            f"delete_webapp accepted reason={reason!r} and bound a card anyway"
        assert "reason is required" in str(error), \
            f"the reason refusal changed wording: {error}"

    long_reason = "x" * 500
    preview = _preview("delete_webapp",
                       {"webapp": opts.webapp.pk, "reason": long_reason},
                       opts.manager)
    assert len(preview["details"]["reason"]) == 300, \
        "the bound reason was not capped at 300 characters"


@th.django_unit_test("rollback binds from/to/version and refuses an unverified version")
def test_rollback_preview_binds_and_refuses(opts):
    from mojo.apps.edge.services import releases

    releases.promote(opts.webapp, opts.live)
    opts.webapp.refresh_from_db()
    later = make_release(opts.webapp, "v3", status="uploaded")

    preview = _preview("rollback_webapp",
                       {"webapp": opts.webapp.pk, "release": later.pk,
                        "reason": "the new build is bad"}, opts.manager)
    assert preview["revision"] == (
        f"app:{opts.webapp.pk}|from:{opts.webapp.current_release_id}"
        f"|to:{later.pk}|version:{later.version}"), \
        f"rollback did not bind the exact from/to/version: {preview['revision']}"

    unverified = _refusal("rollback_webapp",
                          {"webapp": opts.webapp.pk, "release": opts.pending.pk,
                           "reason": "roll back please"}, opts.manager)
    assert unverified is not None and "never verified" in str(unverified), (
        "an abandoned, unverified upload was proposed for rollback: "
        f"{unverified}")

    foreign = _refusal("rollback_webapp",
                       {"webapp": opts.webapp.pk,
                        "release": opts.other_release.pk,
                        "reason": "roll back please"}, opts.manager)
    assert foreign is not None and "belongs to this app" in str(foreign), \
        f"a release from another app was accepted for rollback: {foreign}"


@th.django_unit_test("take_webapp_offline binds the address, its kind and the alias count")
def test_offline_preview_binds_addresses(opts):
    from mojo.apps.edge.models import Vhost

    alias = make_vhost(opts.domain, opts.certificate,
                       label=f"al{uuid.uuid4().hex[:6]}", kind="site_api",
                       alias_of=opts.webapp)
    try:
        preview = _preview("take_webapp_offline",
                           {"webapp": opts.webapp.pk, "reason": "maintenance window"},
                           opts.manager)
        assert preview["revision"] == (
            f"app:{opts.webapp.pk}|vhost:{opts.vhost.pk}"
            f"|host:{opts.vhost.server_name}|kind:site_api|aliases:1"), \
            f"offline did not bind the address, kind and alias count: {preview['revision']}"
        assert "kept" in _summarize("take_webapp_offline",
                                    {"webapp": opts.webapp.pk}, opts.manager), \
            "the offline card does not say the app and its versions are kept"
    finally:
        Vhost.objects.filter(pk=alias.pk).delete()


@th.django_unit_test("the offline card matches what teardown really does, for both serving kinds")
def test_offline_card_matches_what_teardown_really_does(opts):
    # `webapp_lifecycle.take_offline` deletes the primary vhost for BOTH
    # serving kinds — `site` and `site_api` — and every alias, in one locked
    # transaction, so the card may promise that the address stops serving for
    # either kind. The kind is still bound so the card describes the address
    # that was there.
    from mojo.apps.edge.models import Vhost

    static_vhost = make_vhost(opts.domain, opts.certificate,
                              label=f"st{uuid.uuid4().hex[:6]}", kind="site")
    static_app = make_webapp(opts.group, slug=f"st{uuid.uuid4().hex[:5]}",
                             vhost=static_vhost)
    api_vhost = make_vhost(opts.domain, opts.certificate,
                           label=f"ap{uuid.uuid4().hex[:6]}", kind="site_api")
    api_app = make_webapp(opts.group, slug=f"ap{uuid.uuid4().hex[:5]}",
                          vhost=api_vhost)
    params = {"reason": "seasonal shutdown"}

    served_from_build = _preview(
        "take_webapp_offline", dict(params, webapp=static_app.pk), opts.manager)
    api_backed = _preview(
        "take_webapp_offline", dict(params, webapp=api_app.pk), opts.manager)

    for label, preview, vhost in (("build-served", served_from_build, static_vhost),
                                  ("API-backed", api_backed, api_vhost)):
        assert preview["details"]["address_stops_serving"] is True, \
            f"a {label} address was not reported as stopping: {preview}"
        assert "stop serving" in preview["summary"] and "KEEPS" not in preview["summary"], \
            f"the {label} card does not say the address stops serving: {preview['summary']}"
        assert f"kind:{vhost.kind}" in preview["revision"], \
            f"the address kind was not bound for the {label} app: {preview['revision']}"

    # ...and execution does exactly that for both kinds.
    for app, vhost in ((static_app, static_vhost), (api_app, api_vhost)):
        outcome = _execute("take_webapp_offline",
                           dict(params, webapp=app.pk), opts.manager,
                           _stub_approval())
        assert outcome["address_stopped_serving"] is True and outcome["note"] is None, \
            f"a {vhost.kind} teardown reported a caveat it does not have: {outcome}"
        assert not Vhost.objects.filter(pk=vhost.pk).exists(), \
            f"a `{vhost.kind}` primary was not actually deleted"


@th.django_unit_test("a malformed operation id is refused exactly like an unknown one")
def test_malformed_operation_id_is_indistinguishable(opts):
    from mojo.apps.assistant.services.tools.webapp import common

    unknown = str(uuid.uuid4())
    malformed = ("not-a-uuid", "", "   ", "12345", None,
                 "'; DROP TABLE edge_web_app; --")

    # The READ path must RETURN a refusal, never raise: an escaping
    # ValidationError becomes a severity-6 assistant:error incident with a
    # traceback on every call, which the model can trigger at will.
    baseline = _read("get_webapp_setup_status", {"operation_id": unknown},
                     opts.manager)
    assert baseline == {"error": common.NO_OPERATION}, \
        f"an unknown operation id did not give the shared refusal: {baseline}"
    for value in malformed:
        result = _read("get_webapp_setup_status", {"operation_id": value},
                       opts.manager)
        assert result == baseline, (
            f"operation_id={value!r} is distinguishable from an unknown id — "
            f"an oracle for which id shapes are real: {result}")

    # ...and the PREVIEW path must raise the same refusal, not a generic
    # precondition message that reads differently from a real not-found.
    for tool_name, extra in (("answer_webapp_setup_step",
                              {"step": "address", "choice": {"label": "app"}}),
                             ("cancel_webapp_setup", {"reason": "stop this"})):
        unknown_error = _refusal(tool_name,
                                 dict(extra, operation_id=unknown), opts.manager)
        assert unknown_error is not None and str(unknown_error) == common.NO_OPERATION, \
            f"{tool_name} did not refuse an unknown id with the shared text: {unknown_error}"
        for value in malformed:
            error = _refusal(tool_name, dict(extra, operation_id=value),
                             opts.manager)
            assert error is not None, \
                f"{tool_name} accepted operation_id={value!r}"
            assert isinstance(error, common.Refused), (
                f"{tool_name} let a non-refusal exception escape for "
                f"operation_id={value!r}: {type(error).__name__}: {error}")
            assert str(error) == str(unknown_error), (
                f"{tool_name} distinguishes operation_id={value!r} from an "
                f"unknown id: {error}")


@th.django_unit_test("an app with no address has nothing to take offline")
def test_offline_refuses_an_app_with_no_address(opts):
    site = make_webapp(opts.group, slug=f"noaddr{uuid.uuid4().hex[:5]}")
    error = _refusal("take_webapp_offline",
                     {"webapp": site.pk, "reason": "nothing to do"}, opts.manager)
    assert error is not None and "nothing to take offline" in str(error), \
        f"an app with no address still produced an offline card: {error}"


@th.django_unit_test("delete binds the slug and what goes with it")
def test_delete_preview_binds_counts(opts):
    site = make_webapp(opts.group, slug=f"del{uuid.uuid4().hex[:5]}",
                       vhost=make_vhost(opts.domain, opts.certificate,
                                        label=f"dl{uuid.uuid4().hex[:6]}"))
    make_release(site, "d1", status="uploaded")
    make_release(site, "d2", status="uploaded")

    preview = _preview("delete_webapp",
                       {"webapp": site.pk, "reason": "decommissioned"},
                       opts.manager)
    assert preview["revision"] == (
        f"app:{site.pk}|slug:{site.slug}|releases:2|addresses:1"), \
        f"delete did not bind the slug and counts: {preview['revision']}"
    assert "cannot be undone" in preview["summary"], \
        f"the delete card does not say it is irreversible: {preview['summary']}"


@th.django_unit_test("set_webapp_serving binds both the old and the new values")
def test_serving_preview_binds_both_sides(opts):
    preview = _preview("set_webapp_serving",
                       {"webapp": opts.webapp.pk, "pool": "staging", "spa": False},
                       opts.manager)
    assert preview["revision"] == (
        f"app:{opts.webapp.pk}|pool:{opts.vhost.pool}->staging"
        f"|spa:{bool(opts.vhost.spa)}->False"), \
        f"serving did not bind both sides of the change: {preview['revision']}"

    nothing = _refusal("set_webapp_serving", {"webapp": opts.webapp.pk},
                       opts.manager)
    assert nothing is not None, \
        "a serving card was created with nothing to change"

    undeclared = _refusal("set_webapp_serving",
                          {"webapp": opts.webapp.pk, "pool": "not-a-pool"},
                          opts.manager)
    assert undeclared is not None, \
        "an undeclared node pool reached an approval card"


@th.django_unit_test("route previews reuse the service's own prefix and upstream gates")
def test_route_previews_use_service_gates(opts):
    preview = _preview("add_webapp_route",
                       {"webapp": opts.webapp.pk, "path_prefix": "api",
                        "upstream": str(opts.upstream.pk)}, opts.manager)
    assert preview["revision"] == (
        f"app:{opts.webapp.pk}|prefix:/api|upstream:{opts.upstream.pk}"), \
        f"the route preview did not bind the cleaned prefix: {preview['revision']}"

    unknown = _refusal("add_webapp_route",
                       {"webapp": opts.webapp.pk, "path_prefix": "/api",
                        "upstream": "999000"}, opts.manager)
    assert unknown is not None, \
        "a destination this app cannot send to reached an approval card"

    absent = _refusal("remove_webapp_route",
                      {"webapp": opts.webapp.pk, "path_prefix": "/nothing-here"},
                      opts.manager)
    assert absent is not None and "isn't set up" in str(absent), \
        f"removing a route that does not exist produced a card: {absent}"


@th.django_unit_test("the setup step binds the revision, cursor and choice, and refuses purchase")
def test_setup_step_binds_and_refuses_purchase(opts):
    from mojo import errors as me
    from mojo.apps.edge.models import WebAppOnboardingOperation
    from mojo.apps.edge.services import webapp_onboarding
    from mojo.apps.assistant.services.tools.webapp import common

    operation = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.manager, web_app=opts.webapp,
        origin=webapp_onboarding.ASSISTANT_ORIGIN,
        replay_fingerprint=uuid.uuid4().hex, cursor="address",
        state={"profile": {"slug": opts.webapp.slug}, "choices": {}})
    choice = {"label": "app", "domain": opts.domain.pk}
    params = {"operation_id": str(operation.operation_id), "step": "address",
              "choice": choice}

    preview = _preview("answer_webapp_setup_step", params, opts.manager)
    assert preview["revision"] == (
        f"op:{operation.operation_id}|rev:{operation.revision}"
        f"|cursor:address|choice:{common.choice_digest(choice)}"), \
        f"the setup step did not bind revision, cursor and choice: {preview['revision']}"
    assert common.bound_value(
        objict(revision=preview["revision"]), "rev") == str(operation.revision), \
        "the bound revision cannot be read back at execution"

    wrong_step = _refusal(
        "answer_webapp_setup_step",
        dict(params, step="github"), opts.manager)
    assert wrong_step is not None and "current setup step" in str(wrong_step), \
        f"a step that is not the cursor produced a card: {wrong_step}"

    for money in ({"label": "app", "purchase": 3},
                  {"label": "app", "confirm_token": "one-use"}):
        error = _refusal("answer_webapp_setup_step",
                         dict(params, choice=money), opts.manager)
        assert error is not None and "Buying a domain" in str(error), \
            f"the tool layer let a purchase choice through: {error}"

    # ...and the SERVICE refuses it too, so the exclusion does not depend on
    # which caller happens to be wired up.
    with th.assert_raises(me.PermissionDeniedException):
        webapp_onboarding.choose_for_actor(
            operation, opts.manager, webapp_onboarding.ASSISTANT_ORIGIN,
            {"revision": operation.revision, "step": "address",
             "choice": {"label": "app", "purchase": 3}})


@th.django_unit_test("a portal-started setup cannot be continued from chat")
def test_setup_step_refuses_a_portal_operation(opts):
    from mojo.apps.edge.models import WebAppOnboardingOperation

    operation = WebAppOnboardingOperation.objects.create(
        group=opts.group, actor=opts.manager, web_app=opts.webapp,
        origin="https://admin.example.com",
        replay_fingerprint=uuid.uuid4().hex, cursor="github",
        state={"profile": {"slug": opts.webapp.slug}, "choices": {}})
    error = _refusal("answer_webapp_setup_step",
                     {"operation_id": str(operation.operation_id),
                      "step": "github", "choice": {"skip": True}}, opts.manager)
    assert error is not None and "Admin portal" in str(error), \
        f"a portal setup was continued from chat: {error}"


@th.django_unit_test("start_webapp_setup accepts no group_intent and binds the profile")
def test_start_setup_binds_profile_and_refuses_group_intent(opts):
    from mojo.apps.assistant import get_registry
    from mojo.apps.assistant.services import approvals
    from mojo.apps.edge.services import webapp_destination
    from tests.test_edge._helpers import RELEASE_BUCKET

    schema = get_registry()["start_webapp_setup"]["definition"]["input_schema"]
    assert "group_intent" not in (schema.get("properties") or {}), (
        "start_webapp_setup offers group_intent; a workspace created as a side "
        "effect could not be bound into the approval at proposal time")
    normalized = approvals.normalize_args(
        schema, {"group": opts.group.pk, "slug": "chatapp",
                 "bucket": RELEASE_BUCKET, "group_intent": "new"})
    assert "group_intent" not in normalized, \
        f"group_intent survived argument normalization: {normalized}"

    def previewed():
        return _preview("start_webapp_setup",
                        {"group": opts.group.pk, "slug": "chatapp",
                         "bucket": RELEASE_BUCKET}, opts.manager)

    # The installation-level backstop the create endpoint runs: with no serving
    # destination configured there is nothing to point an address at, and the
    # preview refuses with that plain steer instead of creating an app.
    # Extended AWS setup coverage deliberately leaves a valid protected
    # BASE_URL behind. This serial package still runs after that package under
    # --all, so isolate the absence this assertion means to prove through the
    # service's documented seams instead of depending on global DB order.
    with mock.patch.object(webapp_destination, "_override", return_value=""), \
            mock.patch.object(webapp_destination, "_base_url", return_value=""):
        unready = _refusal("start_webapp_setup",
                           {"group": opts.group.pk, "slug": "chatapp",
                            "bucket": RELEASE_BUCKET}, opts.manager)
    assert unready is not None and "System Setup" in str(unready), (
        "an installation with no serving destination still produced a setup "
        f"card: {unready}")

    preview = with_setting(
        "EDGE_WEBAPP_CNAME_TARGET", "edge-assistant-target.example.net",
        previewed)
    assert preview["revision"] == (
        f"group:{opts.group.pk}|slug:chatapp|env:production"
        f"|bucket:{RELEASE_BUCKET}"), \
        f"start did not bind the exact workspace and profile: {preview['revision']}"

    bad_slug = with_setting(
        "EDGE_WEBAPP_CNAME_TARGET", "edge-assistant-target.example.net",
        lambda: _refusal("start_webapp_setup",
                         {"group": opts.group.pk, "slug": "Not A Slug",
                          "bucket": RELEASE_BUCKET}, opts.manager))
    assert bad_slug is not None, "an invalid slug reached an approval card"


@th.django_unit_test("revoking a deploy key is idempotent on the approval uuid")
def test_revoke_key_uses_the_approval_as_its_operation_id(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.edge.models import WebAppKeyOperation
    from mojo.apps.edge.services import webapp_keys

    site = make_webapp(opts.group, slug=f"key{uuid.uuid4().hex[:5]}")
    _, key, _, _ = webapp_keys.link(site)
    site.refresh_from_db()
    approval = _stub_approval(revision=f"app:{site.pk}|key:{key.pk}")
    params = {"webapp": site.pk, "reason": "the key leaked"}

    first = _execute("revoke_webapp_deploy_key", params, opts.manager, approval)
    second = _execute("revoke_webapp_deploy_key", params, opts.manager, approval)

    assert first["replayed"] is False and second["replayed"] is True, (
        "a second execution of the same approval was not a replay receipt: "
        f"{first} vs {second}")
    assert first["key"] == {"linked": False, "active": False}, \
        f"the key was not unlinked and deactivated: {first}"
    assert "token" not in str(first) + str(second), \
        "the revoke result carried a token-shaped value"
    receipts = WebAppKeyOperation.objects.filter(
        web_app=site, operation_id=approval.uuid, action="revoke").count()
    assert receipts == 1, \
        f"the same approval wrote {receipts} revoke receipts, not one"
    assert ApiKey.objects.get(pk=key.pk).is_active is False, \
        "the deploy key stayed active after being revoked"


@th.django_unit_test("a key-less app has nothing to revoke")
def test_revoke_key_refuses_without_a_key(opts):
    site = make_webapp(opts.group, slug=f"nok{uuid.uuid4().hex[:5]}")
    error = _refusal("revoke_webapp_deploy_key",
                     {"webapp": site.pk, "reason": "no key here"}, opts.manager)
    assert error is not None and "no deploy key" in str(error), \
        f"an app with no deploy key still produced a revoke card: {error}"


@th.django_unit_test("taking an app offline through the tool leaves the app and its versions")
def test_offline_execution_keeps_the_app(opts):
    from mojo.apps.edge.models import Vhost, WebApp

    vhost = make_vhost(opts.domain, opts.certificate,
                       label=f"off{uuid.uuid4().hex[:6]}")
    site = make_webapp(opts.group, slug=f"off{uuid.uuid4().hex[:5]}", vhost=vhost)
    release = make_release(site, "o1", status="uploaded")

    result = _execute("take_webapp_offline",
                      {"webapp": site.pk, "reason": "seasonal shutdown"},
                      opts.manager, _stub_approval())

    assert result["status"] == "offline", f"the tool did not report offline: {result}"
    assert WebApp.objects.filter(pk=site.pk).exists(), "the app was deleted"
    assert not Vhost.objects.filter(pk=vhost.pk).exists(), \
        "the address kept serving after the app went offline"
    from mojo.apps.edge.models import WebAppRelease
    assert WebAppRelease.objects.filter(pk=release.pk).exists(), \
        "going offline destroyed a version"


@th.django_unit_test("deleting an app through the tool removes it and reports the slug")
def test_delete_execution_removes_everything(opts):
    from mojo.apps.edge.models import Vhost, WebApp

    vhost = make_vhost(opts.domain, opts.certificate,
                       label=f"dx{uuid.uuid4().hex[:6]}", kind="site_api")
    site = make_webapp(opts.group, slug=f"dx{uuid.uuid4().hex[:5]}", vhost=vhost)

    result = _execute("delete_webapp",
                      {"webapp": site.pk, "reason": "customer offboarded"},
                      opts.manager, _stub_approval())

    assert result["deleted"] is True and result["slug"] == site.slug, \
        f"delete did not report what it removed: {result}"
    assert not WebApp.objects.filter(pk=site.pk).exists(), "the app row survived"
    assert not Vhost.objects.filter(pk=vhost.pk).exists(), \
        "the address survived the delete"


@th.django_unit_test("the handler re-checks authority even with an approval in hand")
def test_execution_re_checks_authority(opts):
    from mojo.apps.edge.models import WebApp

    # The gate re-runs authorize and preview against the freshly re-read User
    # before dispatch, so this path is unreachable in production. The handler
    # checks anyway — three independent gates — and fails closed rather than
    # acting on a forged or stale approval.
    error = None
    try:
        _execute("take_webapp_offline",
                 {"webapp": opts.webapp.pk, "reason": "should not run"},
                 opts.outsider, _stub_approval())
    except (ValueError, PermissionError) as exc:
        error = exc
    assert error is not None, \
        "an operator with no WebApp authority executed a write from an approval"
    opts.webapp.refresh_from_db()
    assert opts.webapp.vhost_id == opts.vhost.pk, \
        "the refused execution still took the app offline"
    assert WebApp.objects.filter(pk=opts.webapp.pk).exists(), \
        "the refused execution deleted the app"
