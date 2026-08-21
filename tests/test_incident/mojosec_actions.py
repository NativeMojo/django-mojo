"""MojoSec recommendation lifecycle, single action owner, ticket path (#2105)."""

import datetime
import uuid
from unittest import mock

from testit import helpers as th


PREFIX = f"mojosec_action_test_{uuid.uuid4().hex[:10]}"
SENSOR = f"{PREFIX}-node-a"
# Deliberately global unicast (the validator rightly refuses RFC 5737
# documentation ranges as non-global); nothing here leaves the test DB —
# broadcast fan-out has no runners in unit tests.
SCANNER_IP = "203.0.114.201"
SECOND_IP = "203.0.114.202"
TICKET_IP = "203.0.114.203"
WHITELISTED_IP = "203.0.114.204"
REVERSE_IP = "203.0.114.206"
RACE_IP = "203.0.114.207"
CAMPAIGN_IPS = [f"198.51.107.{10 + index}" for index in range(6)]
TEST_IPS = ([SCANNER_IP, SECOND_IP, TICKET_IP, WHITELISTED_IP, REVERSE_IP,
             RACE_IP] + CAMPAIGN_IPS)


def _settings(opts, mode="authoritative", auto=False, include_host=True):
    from mojo.apps.incident.services import mojosec_correlation

    targets = [{
        "installation_key_id": opts.action_key.pk,
        "vhost_ids": [],
        "include_fim": True,
        "include_host": include_host,
        "require_registered_deployments": False,
        "mode": mode,
    }]
    original = mojosec_correlation.settings.get_static

    def get_static(name, default=None, **kwargs):
        if name == "MOJOSEC_CASE_SHADOW_TARGETS":
            return targets
        if name == "MOJOSEC_ACTION_AUTO_EXECUTE":
            return auto
        return original(name, default, **kwargs)
    return get_static


def _inline_execution():
    """Run queued executions synchronously — jobs stay out of unit tests."""
    from mojo.apps.incident.services import mojosec_actions

    return mock.patch.object(
        mojosec_actions, "_queue_execution",
        side_effect=lambda rec: mojosec_actions._execute(rec.pk))


def _case(opts, sensor_kind="web", family="wordpress", urgency="warning",
          urgency_reason="trusted_impossible_path", occurrences=20,
          sources=None, distinct_sources=None, resource_id="vhost:5150",
          key_suffix=None):
    from mojo.apps.incident.models import MojoSecCase
    from mojo.helpers import dates

    now = dates.utcnow()
    sources = sources if sources is not None else [SCANNER_IP]
    suffix = key_suffix or uuid.uuid4().hex[:8]
    return MojoSecCase.objects.create(
        group=opts.action_group, installation_key=opts.action_key,
        sensor_id=SENSOR, sensor_kind=sensor_kind, family=family,
        resource_id=resource_id, network="203.0.113.0/24",
        correlation_key=f"{PREFIX}-{suffix}",
        window_key=f"{PREFIX}-{suffix}-w",
        window_start=now, window_end=now, first_seen=now, last_seen=now,
        policy_version=1, evaluator_version=3,
        urgency=urgency, urgency_reason=urgency_reason,
        state="elevated" if urgency in ("high", "critical") else "observing",
        occurrence_count=occurrences, receipt_count=1,
        observed_sources=sources,
        distinct_source_count=(
            len(sources) if distinct_sources is None else distinct_sources))


@th.django_unit_setup()
def setup_mojosec_actions(opts):
    from mojo.apps.account.models import ApiKey, GeoLocatedIP, Group, User
    from mojo.apps.incident.models import (
        Event, Incident, MojoSecCase, MojoSecCaseTransition,
        MojoSecDeployment, MojoSecExecutionAttempt, MojoSecRecommendation,
        MojoSecRecommendationTarget, MojoSecRecommendationTransition,
        MojoSecReceipt, Ticket)

    stem = "mojosec_action_test_"
    # PROTECT FKs: deployments and recommendation rows must go before keys.
    MojoSecDeployment.objects.filter(
        installation_key__name__startswith=stem).delete()
    MojoSecExecutionAttempt.maintenance_objects.filter(
        recommendation__installation_key__name__startswith=stem).delete()
    MojoSecRecommendationTransition.maintenance_objects.filter(
        recommendation__installation_key__name__startswith=stem).delete()
    MojoSecRecommendationTarget.objects.filter(
        recommendation__installation_key__name__startswith=stem).delete()
    MojoSecRecommendation.objects.filter(
        installation_key__name__startswith=stem).delete()
    MojoSecCaseTransition.maintenance_objects.filter(
        case__installation_key__name__startswith=stem).delete()
    MojoSecReceipt.objects.filter(sensor_id__startswith=stem).delete()
    MojoSecCase.objects.filter(
        installation_key__name__startswith=stem).delete()
    Ticket.objects.filter(title__startswith=stem).delete()
    Incident.objects.filter(title__startswith=stem).delete()
    Event.objects.filter(metadata__mojosec__sensor_id=SENSOR).delete()
    User.objects.filter(username__startswith=stem).delete()
    ApiKey.objects.filter(name__startswith=stem).delete()
    Group.objects.filter(name__startswith=stem).delete()
    GeoLocatedIP.objects.filter(ip_address__in=TEST_IPS).delete()

    group = Group.objects.create(name=PREFIX, kind="organization")
    key, _token = ApiKey.create_for_group(
        group, f"{stem}key_{uuid.uuid4().hex[:6]}",
        permissions={"mojosec_ingest": True})
    key.metadata = {"protected": {"mojosec": {
        "enabled": True, "sensor_id": SENSOR, "allowed_versions": [1]}}}
    key.save(update_fields=["metadata"])
    approver = User.objects.create_user(
        f"{stem}approver@example.test", "MojoSecAction##1",
        username=f"{stem}approver")
    approver.is_active = True
    approver.is_email_verified = True
    approver.requires_mfa = False
    approver.add_permission("manage_security")
    approver.save()
    bystander = User.objects.create_user(
        f"{stem}bystander@example.test", "MojoSecAction##1",
        username=f"{stem}bystander")
    bystander.is_active = True
    bystander.is_email_verified = True
    bystander.requires_mfa = False
    bystander.add_permission("view_security")
    bystander.save()
    whitelisted = GeoLocatedIP.geolocate(WHITELISTED_IP, auto_refresh=False)
    whitelisted.whitelist(reason="operator egress")
    opts.action_group = group
    opts.action_key = key
    opts.action_approver = approver
    opts.action_bystander = bystander


@th.django_unit_test()
def test_single_source_proposal_is_idempotent_and_auto_gated(opts):
    from mojo.apps.incident.models import MojoSecRecommendation
    from mojo.apps.incident.services import mojosec_actions, mojosec_correlation

    case = _case(opts, occurrences=18)
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts, auto=False)), \
            mock.patch.object(mojosec_actions, "_record_metric"), \
            _inline_execution():
        result = mojosec_actions.action_sweep()
        th.assert_eq(result["proposed"], 1,
                     f"one qualifying case must propose once: {result}")
        recommendation = MojoSecRecommendation.objects.get(case=case)
        th.assert_eq(recommendation.state, "proposed",
                     "with the master flag off nothing may auto-execute")
        th.assert_eq(
            (recommendation.action, recommendation.target_count,
             recommendation.validated_count),
            ("temporary_block_ip", 1, 1),
            "the proposal must carry exactly the case's one source")
        again = mojosec_actions.action_sweep()
        th.assert_eq(again["proposed"], 0,
                     f"a re-sweep must refresh, never duplicate: {again}")
        th.assert_eq(
            MojoSecRecommendation.objects.filter(case=case).count(), 1,
            "the open-recommendation constraint must hold")


@th.django_unit_test()
def test_auto_execution_applies_a_bounded_temporary_block(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import MojoSecRecommendation
    from mojo.apps.incident.services import mojosec_actions, mojosec_correlation

    case = _case(opts, sources=[SECOND_IP], key_suffix="auto")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts, auto=True)), \
            mock.patch.object(mojosec_actions, "_record_metric"), \
            _inline_execution():
        mojosec_actions.action_sweep()
    recommendation = MojoSecRecommendation.objects.get(case=case)
    th.assert_eq(recommendation.state, "executed",
                 "a single validated high-confidence IP may auto-execute")
    target = recommendation.targets.get()
    th.assert_eq(target.outcome, "applied",
                 "the auto-approved target must actually apply")
    geo = GeoLocatedIP.objects.get(ip_address=SECOND_IP)
    th.assert_true(geo.block_active,
                   "the block must be live on the GeoLocatedIP row")
    th.assert_true(geo.blocked_until is not None,
                   "an automatic block must always carry a TTL — never permanent")
    th.assert_true(f"mojosec:rec:{recommendation.pk}" in geo.blocked_reason,
                   "the block reason must name the owning recommendation")
    attempt = recommendation.attempts.get()
    th.assert_eq(attempt.outcome, "applied",
                 "execution must append exactly one attempt row")


@th.django_unit_test()
def test_campaign_set_requires_approval_and_records_protected_skips(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import MojoSecRecommendation
    from mojo.apps.incident.services import mojosec_actions, mojosec_correlation

    sources = CAMPAIGN_IPS + ["10.0.0.8", "127.0.0.1", WHITELISTED_IP]
    campaign = _case(
        opts, sensor_kind="campaign", family="wordpress", urgency="high",
        urgency_reason="distributed_campaign", occurrences=500,
        sources=sources, resource_id="vhost:5151", key_suffix="campaign")
    # One member IP is already blocked by a legacy rule.
    pre = GeoLocatedIP.geolocate(CAMPAIGN_IPS[0], auto_refresh=False)
    pre.block(reason="legacy:ruleset", ttl=600, broadcast=False)
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts, auto=True)), \
            mock.patch.object(mojosec_actions, "_record_metric"), \
            _inline_execution():
        mojosec_actions.action_sweep()
        recommendation = MojoSecRecommendation.objects.get(case=campaign)
        th.assert_eq(recommendation.state, "proposed",
                     "multi-IP sets must never auto-execute, flag or no flag")
        th.assert_eq(recommendation.requested_scope, "group",
                     "campaign scope is the tenant group")
        protected = recommendation.targets.filter(
            validation_state="protected").values_list("ip", flat=True)
        th.assert_eq(set(protected), {"10.0.0.8", "127.0.0.1", WHITELISTED_IP},
                     "private/loopback/whitelisted targets are recorded skips")
        th.assert_eq(recommendation.validated_count, len(CAMPAIGN_IPS),
                     "only public unprotected IPs validate")
        mojosec_actions.approve(
            recommendation, opts.action_approver, note="ship it")
        with th.assert_raises(ValueError):
            # The state machine refuses a second approval outright.
            mojosec_actions.approve(recommendation, opts.action_approver)
        recommendation.refresh_from_db()
        th.assert_eq(recommendation.state, "executed",
                     "an approved set must execute every validated target")
        th.assert_eq(recommendation.approved_by_id, opts.action_approver.pk,
                     "the approver identity must be recorded")
        outcomes = dict(recommendation.targets.values_list("ip", "outcome"))
        th.assert_eq(outcomes[CAMPAIGN_IPS[0]], "pre_existing",
                     "an already-active block is pre-existing, not applied")
        pre_target = recommendation.targets.get(ip=CAMPAIGN_IPS[0])
        th.assert_true("legacy:ruleset" in (pre_target.prior_reason or ""),
                       "pre-existing outcome must snapshot the prior reason")
        for ip in CAMPAIGN_IPS[1:]:
            th.assert_eq(outcomes[ip], "applied",
                         f"validated target {ip} must apply")
        th.assert_eq(outcomes[WHITELISTED_IP], "pending",
                     "protected targets are never executed")


@th.django_unit_test()
def test_two_recommendations_on_one_ip_disambiguate(opts):
    from mojo.apps.incident.models import MojoSecRecommendation
    from mojo.apps.incident.services import mojosec_actions, mojosec_correlation

    first = _case(opts, sources=[RACE_IP], key_suffix="race-a",
                  resource_id="vhost:5152")
    second = _case(opts, sources=[RACE_IP], key_suffix="race-b",
                   resource_id="vhost:5153")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts, auto=True)), \
            mock.patch.object(mojosec_actions, "_record_metric"), \
            _inline_execution():
        mojosec_actions.action_sweep()
    outcomes = sorted(
        MojoSecRecommendation.objects.filter(
            case__in=(first, second)).values_list(
            "targets__outcome", flat=True))
    th.assert_eq(outcomes, ["applied", "pre_existing"],
                 "exactly one wins the row lock; the other records pre-existing")


@th.django_unit_test()
def test_ttl_expiry_and_operator_reversal(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import (
        MojoSecRecommendation, MojoSecRecommendationTarget)
    from mojo.apps.incident.services import mojosec_actions, mojosec_correlation
    from mojo.helpers import dates

    case = _case(opts, sources=[TICKET_IP], key_suffix="expiry",
                 resource_id="vhost:5154")
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts, auto=True)), \
            mock.patch.object(mojosec_actions, "_record_metric"), \
            _inline_execution():
        mojosec_actions.action_sweep()
        recommendation = MojoSecRecommendation.objects.get(case=case)
        target = recommendation.targets.get()
        th.assert_eq(target.outcome, "applied", "fixture must apply first")
        # Simulate the TTL passing; the central block sweep owns the actual
        # unblock, the action sweep records the terminal outcome.
        MojoSecRecommendationTarget.objects.filter(pk=target.pk).update(
            expires_at=dates.utcnow() - datetime.timedelta(seconds=5))
        result = mojosec_actions.action_sweep()
        th.assert_true(result["expired_targets"] >= 1,
                       f"the sweep must confirm the expiry: {result}")
        recommendation.refresh_from_db()
        target.refresh_from_db()
        th.assert_eq((recommendation.state, target.outcome),
                     ("expired", "expired"),
                     "an all-expired recommendation settles to expired")
        # Reversal of a fresh execution restores access (dedicated IP so no
        # earlier fixture can have pre-blocked it).
        case2 = _case(opts, sources=[REVERSE_IP], key_suffix="reverse",
                      resource_id="vhost:5155")
        mojosec_actions.action_sweep()
        rec2 = MojoSecRecommendation.objects.get(case=case2)
        target2 = rec2.targets.get()
        th.assert_eq(target2.outcome, "applied", "reversal fixture must apply")
        reversed_rec = mojosec_actions.reverse(
            rec2, opts.action_approver, note="false positive")
        th.assert_eq(reversed_rec.state, "reversed",
                     "operator reversal must land the reversed state")
        geo = GeoLocatedIP.objects.get(ip_address=REVERSE_IP)
        th.assert_true(not geo.block_active,
                       "reversal must actually restore access")
        th.assert_eq(reversed_rec.targets.get().outcome, "reversed",
                     "the target must record its reversal")


@th.django_unit_test()
def test_scope_and_injection_bounds(opts):
    from mojo.apps.incident.models import MojoSecRecommendation
    from mojo.apps.incident.services import mojosec_actions

    case = _case(opts, sources=[SCANNER_IP], key_suffix="scope",
                 resource_id="vhost:5156")
    with th.assert_raises(ValueError) as guard:
        mojosec_actions.propose(
            case, MojoSecRecommendation.ACTION_BLOCK_IP, "test", "test",
            "high", [SCANNER_IP], requested_scope="region")
    th.assert_true("1636" in str(guard.exception),
                   "the scope rejection must name the identity dependency")
    recommendation, _created = mojosec_actions.propose(
        case, MojoSecRecommendation.ACTION_BLOCK_IP, "test", "test",
        "high", ["not-an-ip", "203.0.113.0/24", "203.0.113.205 OR 1=1"])
    th.assert_eq(recommendation.validated_count, 0,
                 "garbage and CIDR strings must never become targets")


@th.django_unit_test()
def test_block_handler_suppression_is_scoped_to_routed_categories(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.handlers.event_handlers import BlockHandler
    from mojo.apps.incident.models import Event, Incident
    from mojo.apps.incident.services import mojosec_actions, mojosec_correlation

    GeoLocatedIP.objects.filter(ip_address=SCANNER_IP).delete()

    def make_event(category, metadata):
        event = Event(
            category=category, scope="mojosec", level=8,
            source_ip=SCANNER_IP, title=f"{PREFIX} owner test",
            details="owner test", metadata=metadata)
        event.save()
        return event

    routed = make_event("mojosec.web.probe", {
        "mojosec": {"installation_key_id": opts.action_key.pk,
                    "sensor_id": SENSOR}})
    incident = Incident.objects.create(
        title=f"{PREFIX} incident", category="mojosec.web.probe",
        status="open")
    Event.objects.filter(pk=routed.pk).update(incident=incident)
    routed.refresh_from_db()
    unrouted_auth = make_event("mojosec.auth.ssh_failure", {
        "mojosec": {"installation_key_id": opts.action_key.pk,
                    "sensor_id": SENSOR}})
    promoted = make_event("mojosec.case.promoted", {
        "mojosec_case": {"installation_key_id": opts.action_key.pk,
                         "case_id": 1}})
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts, include_host=False)), \
            mock.patch.object(mojosec_actions, "_record_metric"):
        handler = BlockHandler()
        th.assert_eq(handler.run(routed), False,
                     "block:// on a routed category must be suppressed")
        incident.refresh_from_db()
        th.assert_eq(incident.status, "open",
                     "suppression must never auto-resolve the incident")
        th.assert_true(not GeoLocatedIP.objects.filter(
            ip_address=SCANNER_IP, is_blocked=True).exists(),
            "suppression must not block")
        th.assert_eq(handler.run(promoted), False,
                     "block:// on a promoted case Event stays inert")
        # include_host is OFF: auth categories are not routed, so the
        # operator's block rule still fires and actually blocks.
        th.assert_eq(handler.run(unrouted_auth), True,
                     "unrouted categories keep their operator block rules")
        geo = GeoLocatedIP.objects.get(ip_address=SCANNER_IP)
        th.assert_true(geo.block_active,
                       "the unrouted-category block must actually apply")
        geo.unblock(broadcast=False)
    with mock.patch.object(
            mojosec_correlation.settings, "get_static",
            side_effect=_settings(opts, mode="shadow")), \
            mock.patch.object(mojosec_actions, "_record_metric"):
        th.assert_eq(
            mojosec_actions.owns_enforcement(routed), False,
            "shadow installations keep today's per-receipt blocking")


@th.django_unit_test()
def test_recommendation_rest_contract_and_permissions(opts):
    from mojo.decorators.limits import clear_rate_limits
    from mojo.apps.incident.models import MojoSecRecommendation
    from mojo.apps.incident.services import mojosec_actions

    case = _case(opts, sources=[SECOND_IP], key_suffix="rest",
                 resource_id="vhost:5157")
    recommendation, _created = mojosec_actions.propose(
        case, MojoSecRecommendation.ACTION_BLOCK_IP,
        "repeated_impossible_paths", "rest fixture", "high", [SECOND_IP])

    opts.client.logout()
    clear_rate_limits(ip="127.0.0.1", key="login")
    th.assert_true(
        opts.client.login(
            f"mojosec_action_test_bystander@example.test", "MojoSecAction##1"),
        "the view-only fixture must log in")
    listed = opts.client.get("/api/incident/mojosec/recommendation?state=proposed")
    th.assert_eq(listed.status_code, 200,
                 f"view_security must read recommendations: {listed.response}")
    th.assert_true(any(row["id"] == recommendation.pk
                       for row in listed.response.data),
                   "the proposed recommendation must be listed")
    detail = opts.client.get(
        f"/api/incident/mojosec/recommendation/{recommendation.pk}")
    th.assert_eq(detail.status_code, 200,
                 f"detail must serialize: {detail.response}")
    th.assert_eq(detail.response.data["targets"][0]["ip"], SECOND_IP,
                 "detail must expose the bounded target rows")
    denied = opts.client.post(
        "/api/incident/mojosec/recommendation-action",
        {"recommendation_id": recommendation.pk, "action": "approve"})
    th.assert_true(denied.status_code in (401, 403),
                   f"view-only users must never approve: {denied.status_code}")
    recommendation.refresh_from_db()
    th.assert_eq(recommendation.state, "proposed",
                 "a denied approval must not move the state machine")

    opts.client.logout()
    clear_rate_limits(ip="127.0.0.1", key="login")
    th.assert_true(
        opts.client.login(
            f"mojosec_action_test_approver@example.test", "MojoSecAction##1"),
        "the approver fixture must log in")
    bad = opts.client.post(
        "/api/incident/mojosec/recommendation-action",
        {"recommendation_id": recommendation.pk, "action": "widen"})
    th.assert_true(bad.status_code >= 400,
                   "unknown actions must be rejected")
    approved = opts.client.post(
        "/api/incident/mojosec/recommendation-action",
        {"recommendation_id": recommendation.pk, "action": "approve",
         "note": "ok"})
    th.assert_eq(approved.status_code, 200,
                 f"manage_security may approve: {approved.response}")
    th.assert_true(approved.response.data["state"] in
                   ("approved", "executing", "executed"),
                   "approval must advance the lifecycle")
    th.assert_eq(approved.response.data["approved_by"],
                 opts.action_approver.username,
                 "the approver identity must be recorded and exposed")

    registered = opts.client.post(
        "/api/incident/mojosec/deployment",
        {"installation_key_id": opts.action_key.pk,
         "deployment_id": "promo-rest-deploy-1", "ttl_seconds": 3600})
    th.assert_eq(registered.status_code, 200,
                 f"manage_security may pre-register deployments: {registered.response}")
    rows = opts.client.get(
        f"/api/incident/mojosec/deployment?installation_key_id={opts.action_key.pk}")
    th.assert_eq(rows.status_code, 200,
                 f"deployment list must serialize: {rows.response}")
    th.assert_true(any(row["deployment_id"] == "promo-rest-deploy-1"
                       for row in rows.response.data),
                   "the registered deployment must be listed")
    opts.client.logout()


@th.django_unit_test()
def test_ticket_approve_block_regression(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.handlers.ticket_actions import _handler_block_confirm
    from mojo.apps.incident.models import Ticket, TicketNote

    GeoLocatedIP.objects.filter(ip_address=TICKET_IP).delete()

    def make_ticket():
        ticket = Ticket.objects.create(
            title=f"{PREFIX} block confirm", status="open",
            group=opts.action_group)
        note = TicketNote.objects.create(
            parent=ticket, user=opts.action_approver, note="approve",
            group=opts.action_group)
        return ticket, note

    # The regression: on the old code this reported success and resolved
    # the ticket while IPSet.block_ip raised and no block ever applied.
    ticket, note = make_ticket()
    _handler_block_confirm(ticket, note, "approve",
                           {"ip": TICKET_IP, "reason": "scanner"})
    ticket.refresh_from_db()
    geo = GeoLocatedIP.objects.get(ip_address=TICKET_IP)
    th.assert_true(geo.block_active,
                   "an approved ticket block must actually block")
    th.assert_eq(ticket.status, "resolved",
                 "a block that applied may resolve the ticket")
    th.assert_true(geo.blocked_until is not None,
                   "the ticket path must never mint a permanent block")
    geo.unblock(broadcast=False)

    # A protected target is refused and the ticket stays open.
    ticket2, note2 = make_ticket()
    _handler_block_confirm(ticket2, note2, "approve",
                           {"ip": "10.1.2.3", "reason": "scanner"})
    ticket2.refresh_from_db()
    th.assert_eq(ticket2.status, "open",
                 "a refused block must never resolve the ticket as success")

    # An approver without security permissions is refused outright.
    ticket3 = Ticket.objects.create(
        title=f"{PREFIX} block confirm unpriv", status="open",
        group=opts.action_group)
    note3 = TicketNote.objects.create(
        parent=ticket3, user=opts.action_bystander, note="approve",
        group=opts.action_group)
    _handler_block_confirm(ticket3, note3, "approve",
                           {"ip": TICKET_IP, "reason": "scanner"})
    ticket3.refresh_from_db()
    th.assert_eq(ticket3.status, "open",
                 "an unprivileged approver must be refused")
    th.assert_true(not GeoLocatedIP.objects.get(
        ip_address=TICKET_IP).block_active,
        "an unprivileged approval must not block")
