import hashlib
import json
import uuid

from testit import helpers as th


PREFIX = f"mojosec_learning_test_{uuid.uuid4().hex[:12]}"
PASSWORD = "MojoSecLearning##1"


def _features(kind="web.probe", count=3, severity="high"):
    return {
        "feature_schema": "replay_features_v1",
        "event": {"kind": kind, "count": count, "severity": severity},
    }


def _payload_digest(features):
    payload = json.dumps(
        features["event"], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _policy(decision="flag"):
    return {
        "schema": "mojosec.policy-proposal.v1",
        "detectors": [{
            "kind": "web.probe", "decision": decision,
            "minimum_count": 2, "minimum_severity": "warning",
        }],
    }


@th.django_unit_setup()
def setup_mojosec_learning(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.incident.models import (
        Event, Incident, MojoSecReceipt)
    from mojo.helpers import dates

    author = User.objects.create_user(
        f"{PREFIX}@example.test", PASSWORD, username=f"{PREFIX}_author")
    author.add_permission("manage_security")
    author.add_permission("view_security")
    author.save()
    disposable = User.objects.create_user(
        f"{PREFIX}_delete@example.test", PASSWORD, username=f"{PREFIX}_delete_author")
    disposable.add_permission("manage_security")
    disposable.save()
    group = Group.objects.create(name=PREFIX, kind="organization")
    group.add_member(author)
    api_key, token = ApiKey.create_for_group(
        group, f"{PREFIX}_key", permissions={"security": True})
    override_key, override_token = ApiKey.create_for_group(
        group, f"{PREFIX}_override_key", permissions={}, user=author,
        override_user=True)
    from mojo.apps.account.services import group_token

    event = Event.objects.create(
        category="mojosec.learning.test", scope="mojosec", level=8,
        title="Learning replay fixture")
    features_one = _features()
    receipt_one = MojoSecReceipt.objects.create(
        api_key=api_key, event=event, sensor_id=f"{PREFIX}_sensor",
        wire_event_id="a" * 64, payload_digest=_payload_digest(features_one),
        sensor_policy_revision="fixture-policy", publish_state="published",
        published_at=dates.utcnow(), replay_features=features_one)
    features_two = _features(count=1)
    receipt_two = MojoSecReceipt.objects.create(
        api_key=api_key, event=event, sensor_id=f"{PREFIX}_sensor",
        wire_event_id="b" * 64, payload_digest=_payload_digest(features_two),
        sensor_policy_revision="fixture-policy", publish_state="published",
        published_at=dates.utcnow(), replay_features=features_two)
    incident = Incident.objects.create(
        category="mojosec.learning.test", scope="mojosec", title="Learning incident")
    incident_features = _features(kind="auth.sudo_command")
    incident_receipt = MojoSecReceipt.objects.create(
        api_key=api_key, event=event, incident=incident, sensor_id=f"{PREFIX}_sensor",
        wire_event_id="c" * 64, payload_digest=_payload_digest(incident_features),
        sensor_policy_revision="fixture-policy", publish_state="published",
        published_at=dates.utcnow(), replay_features=incident_features)

    opts.learning_author = author
    opts.learning_disposable = disposable
    opts.learning_api_key = api_key
    opts.learning_api_token = token
    opts.learning_override_api_key = override_key
    opts.learning_override_api_token = override_token
    opts.learning_group_token = group_token.mint(author, group)
    opts.learning_receipt_one = receipt_one
    opts.learning_receipt_two = receipt_two
    opts.learning_incident = incident
    opts.learning_incident_receipt = incident_receipt


@th.django_unit_test()
def test_mojosec_policy_validator_is_bounded_and_non_executable(opts):
    from mojo.apps.incident.services import mojosec_learning

    normalized = mojosec_learning.validate_policy_content(_policy())
    th.assert_eq(normalized["detectors"][0]["decision"], "flag",
                 "the pure validator should retain an allowlisted fixed decision")
    for forbidden in ("regex", "url", "handler", "job", "action"):
        invalid = _policy()
        invalid["detectors"][0][forbidden] = "https://example.test/run"
        with th.assert_raises(mojosec_learning.MojoSecLearningError):
            mojosec_learning.validate_policy_content(invalid)

    server_policy = {
        "schema": "mojosec.policy-proposal.v1",
        "detectors": [{
            "kind": "web.error", "decision": "flag",
            "minimum_severity": "high",
        }],
    }
    replay_features = _features(kind="web.error", count=1, severity="critical")
    evaluated = mojosec_learning.evaluate_features(server_policy, [{
        "id": 1, "payload_digest": _payload_digest(replay_features),
        "replay_features": replay_features,
    }])
    th.assert_eq(evaluated["metrics"]["flagged"], 0,
                 "host critical severity must not override server web.error level 5")
    th.assert_eq(evaluated["metrics"]["unmatched"], 1,
                 "effective replay severity must be derived from server KIND_POLICY")


@th.django_unit_test()
def test_mojosec_feedback_exact_subject_and_reversal_chain(opts):
    from mojo.apps.incident.models import (
        MojoSecDetectorFeedback, MojoSecDetectorFeedbackHead)
    from mojo.apps.incident.services import mojosec_learning

    with th.assert_raises(mojosec_learning.MojoSecLearningError):
        mojosec_learning.create_feedback(
            opts.learning_author, "confirmed_threat")
    with th.assert_raises(mojosec_learning.MojoSecLearningError):
        mojosec_learning.create_feedback(
            opts.learning_author, "confirmed_threat",
            receipt_id=opts.learning_receipt_one.pk,
            manual_exemplar={"kind": "web.probe", "count": 3, "severity": "high"})

    manual = {"kind": "web.probe", "count": 3, "severity": "high"}
    first = mojosec_learning.create_feedback(
        opts.learning_author, "confirmed_threat", manual_exemplar=manual,
        note=PREFIX)
    second = mojosec_learning.create_feedback(
        opts.learning_author, "benign_noise", manual_exemplar=manual,
        reverses_id=first.pk, note=f"{PREFIX} corrected")
    third = mojosec_learning.create_feedback(
        opts.learning_author, "unknown", manual_exemplar=manual,
        reverses_id=second.pk, note=f"{PREFIX} needs evidence")
    th.assert_eq(third.reverses_id, second.pk,
                 "a reversal must append a new link rather than mutate history")
    th.assert_eq(third.subject_id_snapshot, first.subject_id_snapshot,
                 "every reversal must preserve the original immutable subject snapshot")
    th.assert_eq(MojoSecDetectorFeedback.objects.filter(
        subject_key=first.subject_key, current_subject_head__isnull=False).count(), 1,
        "a reversal chain must expose exactly one current disposition")
    first.note = "mutation"
    with th.assert_raises(ValueError):
        first.save()
    head = MojoSecDetectorFeedbackHead.objects.get(subject_key=first.subject_key)
    with th.assert_raises(ValueError):
        MojoSecDetectorFeedbackHead.objects.filter(pk=head.pk).update(current=first)
    with th.assert_raises(ValueError):
        MojoSecDetectorFeedbackHead.objects.filter(pk=head.pk).delete()
    head.current = None
    with th.assert_raises(ValueError):
        head.save()
    other = mojosec_learning.create_feedback(
        opts.learning_author, "unknown",
        manual_exemplar={"kind": "system.oom", "count": 1, "severity": "high"},
        note=f"{PREFIX} other subject")
    with th.assert_raises(ValueError):
        head.advance(other)


@th.django_unit_test()
def test_mojosec_feedback_survives_subject_and_author_deletion(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Incident, MojoSecDetectorFeedback, MojoSecReceipt
    from mojo.apps.incident.services import mojosec_learning

    receipt_feedback = mojosec_learning.create_feedback(
        opts.learning_disposable, "operational_failure",
        receipt_id=opts.learning_receipt_two.pk, note=PREFIX)
    receipt_pk = opts.learning_receipt_two.pk
    author_pk = opts.learning_disposable.pk
    MojoSecReceipt.objects.filter(pk=receipt_pk).delete()
    User.objects.filter(pk=author_pk).delete()
    receipt_feedback.refresh_from_db()
    th.assert_true(receipt_feedback.receipt_id is None and receipt_feedback.author_id is None,
                   "prunable receipt and author links must SET_NULL without deleting feedback")
    th.assert_eq(receipt_feedback.subject_id_snapshot, str(receipt_pk),
                 "receipt identity must remain in an immutable scalar snapshot")
    th.assert_eq(receipt_feedback.author_id_snapshot, author_pk,
                 "author identity must remain in an immutable scalar snapshot")
    reversal = mojosec_learning.create_feedback(
        opts.learning_author, "unknown", reverses_id=receipt_feedback.pk,
        note=f"{PREFIX} post-prune reversal")
    th.assert_eq(reversal.subject_id_snapshot, str(receipt_pk),
                 "a reversal must inherit a pruned subject's durable identity snapshot")

    with th.assert_raises(mojosec_learning.MojoSecLearningError):
        mojosec_learning.create_feedback(
            opts.learning_author, "missed_incomplete",
            incident_id=opts.learning_incident.pk, note=PREFIX)
    incident_feedback = mojosec_learning.create_feedback(
        opts.learning_author, "missed_incomplete",
        receipt_id=opts.learning_incident_receipt.pk,
        incident_id=opts.learning_incident.pk, note=PREFIX)
    incident_pk = opts.learning_incident.pk
    Incident.objects.filter(pk=incident_pk).delete()
    incident_feedback.refresh_from_db()
    th.assert_true(incident_feedback.incident_id is None,
                   "incident lifecycle deletion must not erase its human disposition")
    th.assert_eq(incident_feedback.incident_id_snapshot, incident_pk,
                 "linked incident context must remain in an immutable scalar snapshot")


@th.django_unit_test()
def test_mojosec_replay_requires_explicit_unique_receipts_and_is_deterministic(opts):
    from mojo.apps.incident.models import Incident, MojoSecPolicyEvaluation, RuleSet
    from mojo.apps.incident.services import mojosec_learning

    proposal = mojosec_learning.create_policy_proposal(
        opts.learning_author, _policy(), summary=f"{PREFIX} draft")
    shadow = mojosec_learning.create_policy_proposal(
        opts.learning_author, _policy(), summary=f"{PREFIX} shadow",
        status="shadow", supersedes=proposal.pk)
    with th.assert_raises(mojosec_learning.MojoSecLearningError):
        mojosec_learning.evaluate_proposal(opts.learning_author, proposal.pk)
    with th.assert_raises(mojosec_learning.MojoSecLearningError):
        mojosec_learning.evaluate_proposal(
            opts.learning_author, proposal.pk,
            receipt_ids=[opts.learning_receipt_one.pk, opts.learning_receipt_one.pk])

    incident_count = Incident.objects.count()
    ruleset_count = RuleSet.objects.count()
    ids = [opts.learning_incident_receipt.pk, opts.learning_receipt_one.pk]
    first = mojosec_learning.evaluate_proposal(
        opts.learning_author, shadow.pk, mode="shadow", receipt_ids=list(reversed(ids)))
    second = mojosec_learning.evaluate_proposal(
        opts.learning_author, shadow.pk, mode="shadow", receipt_ids=ids)
    th.assert_eq(first.sample_digest, second.sample_digest,
                 "receipt ordering must be canonical and deterministic")
    th.assert_eq(first.result_digest, second.result_digest,
                 "the same explicit evidence set must produce the same result digest")
    th.assert_eq(first.metrics["evaluator"]["schema"], "mojosec.offline-evaluator",
                 "persisted replay metrics must identify the evaluator schema")
    th.assert_true(bool(first.metrics["evaluator"]["kind_policy_registry_digest"]),
                   "replay provenance must bind the server KIND_POLICY registry")
    th.assert_eq(MojoSecPolicyEvaluation.objects.filter(proposal=shadow).count(), 2,
                 "explicit shadow calls should persist only bounded evaluation summaries")
    first.assert_integrity()
    with th.assert_raises(ValueError):
        MojoSecPolicyEvaluation.objects.filter(pk=first.pk).update(sample_count=0)
    with th.assert_raises(ValueError):
        MojoSecPolicyEvaluation.objects.filter(pk=first.pk).delete()
    first.metrics = {"tampered": True}
    with th.assert_raises(ValueError):
        first.save()
    th.assert_eq(Incident.objects.count(), incident_count,
                 "offline shadow evaluation must never create an incident")
    th.assert_eq(RuleSet.objects.count(), ruleset_count,
                 "policy proposals must never modify manually-authored live RuleSets")
    with th.assert_raises(mojosec_learning.MojoSecLearningError):
        mojosec_learning.evaluate_proposal(
            opts.learning_author, proposal.pk,
            receipt_ids=[opts.learning_receipt_one.pk])
    rejected = mojosec_learning.create_policy_proposal(
        opts.learning_author, _policy(), summary=f"{PREFIX} rejected",
        status="rejected", supersedes=shadow.pk)
    with th.assert_raises(mojosec_learning.MojoSecLearningError):
        mojosec_learning.evaluate_proposal(
            opts.learning_author, rejected.pk,
            receipt_ids=[opts.learning_receipt_one.pk])
    with th.assert_raises(mojosec_learning.MojoSecLearningError):
        mojosec_learning.create_policy_proposal(
            opts.learning_author, _policy(), supersedes=rejected.pk)
    from mojo.helpers import dates
    mojosec_learning.prune_learning_evaluations(
        now=first.created + dates.timedelta(days=91))
    th.assert_true(not MojoSecPolicyEvaluation.objects.filter(proposal=shadow).exists(),
                   "expired bounded evaluation summaries should be prunable")
    th.assert_true(type(shadow).objects.filter(pk=shadow.pk).exists(),
                   "evaluation retention must preserve immutable proposal history")


@th.django_unit_test()
def test_mojosec_learning_rejects_api_key_authors_and_reports_bounded_metrics(opts):
    from mojo.apps.incident.services import mojosec_learning

    with th.assert_raises(mojosec_learning.MojoSecLearningError):
        mojosec_learning.create_policy_proposal(opts.learning_api_key, _policy())
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = opts.learning_api_token
    opts.client.is_authenticated = True
    denied = opts.client.post(
        "/api/incident/mojosec/proposal", {"content": _policy()})
    th.assert_eq(denied.status_code, 403,
                 "a globally permissioned ApiKey must still be denied from learning writes")
    override_denied = opts.client.post(
        "/api/incident/mojosec/proposal", {"content": _policy()},
        headers={"Authorization": f"apikey {opts.learning_override_api_token}"})
    th.assert_eq(override_denied.status_code, 403,
                 "an override-user ApiKey must not turn its linked human into a global caller")
    group_token_denied = opts.client.post(
        "/api/incident/mojosec/proposal", {"content": _policy()},
        headers={"Authorization": f"grouptoken {opts.learning_group_token}"})
    th.assert_eq(group_token_denied.status_code, 403,
                 "a group-scoped token must never reach the global learning control plane")
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    logged_in = opts.client.login(f"{PREFIX}@example.test", PASSWORD)
    th.assert_true(logged_in, "the global human security operator should authenticate")
    accepted = opts.client.post("/api/incident/mojosec/feedback", {
        "disposition": "expected_administrative",
        "manual_exemplar": {"kind": "system.oom", "count": 1, "severity": "high"},
        "note": f"{PREFIX} REST authorization",
    })
    th.assert_eq(accepted.status_code, 200,
                 "a global human security operator should append bounded feedback")
    metrics = mojosec_learning.detector_metrics(opts.learning_author, days=30, limit=100)
    th.assert_true("detectors" in metrics,
                   "detector metrics should be derived from bounded receipt and feedback rows")
    th.assert_true("fleet" not in metrics and "sensor_health" not in metrics,
                   "learning metrics must not fabricate fleet or sensor-health truth")
    th.assert_true(bool(metrics["installations"]),
                   "bounded metrics should expose stable installation strata")
    th.assert_true(all(
        "group" not in row and "tenant" not in row
        and row["receipts"] <= metrics["per_installation_sample_limit"]
        and row["feedback"] <= metrics["per_installation_sample_limit"]
        for row in metrics["installations"]),
        "installation strata must remain bounded and must not carry tenant identity")

    from mojo.apps.assistant import get_registry
    registry = get_registry()
    th.assert_true("propose_mojosec_policy" not in registry,
                   "learning must remain human-only until structural approval exists")
    th.assert_true("create_rule" in registry,
                   "the learning prototype must not remove ordinary incident triage tools")


@th.django_unit_test()
def test_mojosec_learning_history_guards_queryset_mutation_and_detects_tamper(opts):
    from mojo.apps.incident.models import (
        MojoSecDetectorFeedback, MojoSecDetectorFeedbackHead,
        MojoSecPolicyEvaluation, MojoSecPolicyProposal)
    from mojo.apps.incident.services import mojosec_learning

    feedback = mojosec_learning.create_feedback(
        opts.learning_author, "unknown",
        manual_exemplar={"kind": "web.denied", "count": 77, "severity": "warning"},
        note=f"{PREFIX} immutable feedback")
    proposal = mojosec_learning.create_policy_proposal(
        opts.learning_author, _policy(), summary=f"{PREFIX} immutable proposal")
    th.assert_true(all(
        getattr(model.RestMeta, "DENY_AI", False)
        for model in (
            MojoSecDetectorFeedback, MojoSecDetectorFeedbackHead,
            MojoSecPolicyProposal, MojoSecPolicyEvaluation)),
        "every learning model must be excluded from generic assistant model tools")
    with th.assert_raises(ValueError):
        MojoSecDetectorFeedback.objects.filter(pk=feedback.pk).update(note="changed")
    with th.assert_raises(ValueError):
        MojoSecDetectorFeedback.objects.filter(pk=feedback.pk).delete()
    with th.assert_raises(ValueError):
        MojoSecPolicyProposal.objects.bulk_update([proposal], ["summary"])
    with th.assert_raises(ValueError):
        MojoSecPolicyProposal.objects.bulk_create([
            MojoSecPolicyProposal(
                created_by=opts.learning_author, content=_policy(),
                content_digest="0" * 64, summary=f"{PREFIX} bulk")])

    # This named manager is the documented migration/DB-administration escape
    # hatch. Simulate out-of-band tampering and prove services fail closed.
    MojoSecPolicyProposal.maintenance_objects.filter(pk=proposal.pk).update(
        summary="tampered out of band")
    with th.assert_raises(ValueError):
        mojosec_learning.evaluate_proposal(
            opts.learning_author, proposal.pk,
            receipt_ids=[opts.learning_receipt_one.pk])

    MojoSecDetectorFeedback.maintenance_objects.filter(pk=feedback.pk).update(
        note="tampered feedback out of band")
    with th.assert_raises(ValueError):
        mojosec_learning.detector_metrics(opts.learning_author, days=30, limit=100)


@th.django_unit_test()
def test_mojosec_first_feedback_label_is_concurrency_safe(opts):
    import threading
    from django.db import close_old_connections
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import MojoSecDetectorFeedback
    from mojo.apps.incident.services import mojosec_learning

    barrier = threading.Barrier(2)
    results = []
    exemplar = {"kind": "web.error", "count": 99, "severity": "critical"}

    def label(disposition):
        close_old_connections()
        try:
            author = User.objects.get(pk=opts.learning_author.pk)
            barrier.wait(timeout=5)
            row = mojosec_learning.create_feedback(
                author, disposition, manual_exemplar=exemplar,
                note=f"{PREFIX} concurrent {disposition}")
            results.append(("created", row.pk))
        except Exception as err:
            results.append(("error", type(err).__name__))
        finally:
            close_old_connections()

    threads = [
        threading.Thread(target=label, args=("confirmed_threat",)),
        threading.Thread(target=label, args=("benign_noise",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    th.assert_true(all(not thread.is_alive() for thread in threads),
                   "concurrent first-label writers must not deadlock")
    created = [item for item in results if item[0] == "created"]
    th.assert_eq(len(created), 1,
                 f"the unique locked subject head must admit one first label: {results}")
    subject_rows = MojoSecDetectorFeedback.objects.filter(
        subject_key=f"manual:{mojosec_learning._digest(exemplar)}")
    th.assert_eq(subject_rows.count(), 1,
                 "a first-label race must leave one immutable history row")
