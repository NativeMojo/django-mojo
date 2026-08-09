from testit import helpers as th


PREFIX = "mojosec_learning_test"
PASSWORD = "MojoSecLearning##1"


def _features(kind="web.probe", count=3, severity="high"):
    return {
        "feature_schema": "replay_features_v1",
        "event": {"kind": kind, "count": count, "severity": severity},
    }


def _policy(decision="flag"):
    return {
        "schema": "mojosec.policy-proposal.v1",
        "detectors": [{
            "kind": "web.probe", "decision": decision,
            "minimum_count": 2, "minimum_severity": "warning",
        }],
    }


def _delete_feedback_rows(Model):
    while Model.objects.filter(note__startswith=PREFIX).exists():
        leaf_ids = list(Model.objects.filter(
            note__startswith=PREFIX, reversed_by__isnull=True).values_list("id", flat=True))
        Model.objects.filter(pk__in=leaf_ids).delete()


@th.django_unit_setup()
def setup_mojosec_learning(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.incident.models import (
        Event, Incident, MojoSecDetectorFeedback, MojoSecPolicyEvaluation,
        MojoSecPolicyProposal, MojoSecReceipt)
    from mojo.helpers import dates

    MojoSecPolicyEvaluation.objects.filter(proposal__summary__startswith=PREFIX).delete()
    while MojoSecPolicyProposal.objects.filter(summary__startswith=PREFIX).exists():
        leaf_ids = list(MojoSecPolicyProposal.objects.filter(
            summary__startswith=PREFIX, superseded_by__isnull=True).values_list("id", flat=True))
        MojoSecPolicyProposal.objects.filter(pk__in=leaf_ids).delete()
    _delete_feedback_rows(MojoSecDetectorFeedback)
    MojoSecReceipt.objects.filter(sensor_id__startswith=PREFIX).delete()
    Incident.objects.filter(category="mojosec.learning.test").delete()
    Event.objects.filter(category="mojosec.learning.test").delete()
    ApiKey.objects.filter(name__startswith=PREFIX).delete()
    Group.objects.filter(name=PREFIX).delete()
    User.objects.filter(username__startswith=PREFIX).delete()

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
    api_key, token = ApiKey.create_for_group(
        group, f"{PREFIX}_key", permissions={"security": True})

    event = Event.objects.create(
        category="mojosec.learning.test", scope="mojosec", level=8,
        title="Learning replay fixture")
    receipt_one = MojoSecReceipt.objects.create(
        api_key=api_key, event=event, sensor_id=f"{PREFIX}_sensor",
        wire_event_id="a" * 64, payload_digest="1" * 64,
        sensor_policy_revision="fixture-policy", publish_state="published",
        published_at=dates.utcnow(), replay_features=_features())
    receipt_two = MojoSecReceipt.objects.create(
        api_key=api_key, event=event, sensor_id=f"{PREFIX}_sensor",
        wire_event_id="b" * 64, payload_digest="2" * 64,
        sensor_policy_revision="fixture-policy", publish_state="published",
        published_at=dates.utcnow(), replay_features=_features(count=1))
    incident = Incident.objects.create(
        category="mojosec.learning.test", scope="mojosec", title="Learning incident")
    incident_receipt = MojoSecReceipt.objects.create(
        api_key=api_key, event=event, incident=incident, sensor_id=f"{PREFIX}_sensor",
        wire_event_id="c" * 64, payload_digest="3" * 64,
        sensor_policy_revision="fixture-policy", publish_state="published",
        published_at=dates.utcnow(), replay_features=_features(kind="auth.sudo_command"))

    opts.learning_author = author
    opts.learning_disposable = disposable
    opts.learning_api_key = api_key
    opts.learning_api_token = token
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


@th.django_unit_test()
def test_mojosec_feedback_exact_subject_and_reversal_chain(opts):
    from mojo.apps.incident.models import MojoSecDetectorFeedback
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
        subject_key=first.subject_key, reversed_by__isnull=True).count(), 1,
        "a reversal chain must expose exactly one current disposition")
    first.note = "mutation"
    with th.assert_raises(ValueError):
        first.save()


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

    incident_feedback = mojosec_learning.create_feedback(
        opts.learning_author, "missed_incomplete",
        incident_id=opts.learning_incident.pk, note=PREFIX)
    incident_pk = opts.learning_incident.pk
    Incident.objects.filter(pk=incident_pk).delete()
    incident_feedback.refresh_from_db()
    th.assert_true(incident_feedback.incident_id is None,
                   "incident lifecycle deletion must not erase its human disposition")
    th.assert_eq(incident_feedback.subject_id_snapshot, str(incident_pk),
                 "incident identity must remain in an immutable scalar snapshot")


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
    th.assert_eq(MojoSecPolicyEvaluation.objects.filter(proposal=shadow).count(), 2,
                 "explicit shadow calls should persist only bounded evaluation summaries")
    th.assert_eq(Incident.objects.count(), incident_count,
                 "offline shadow evaluation must never create an incident")
    th.assert_eq(RuleSet.objects.count(), ruleset_count,
                 "policy proposals must never modify manually-authored live RuleSets")
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

    from mojo.apps.assistant import get_registry
    registry = get_registry()
    th.assert_true("propose_mojosec_policy" in registry,
                   "the assistant should expose proposal creation as its only learning write")
    th.assert_true("create_rule" in registry,
                   "the learning prototype must not remove ordinary incident triage tools")
    schema = registry["propose_mojosec_policy"]["definition"]["input_schema"]
    th.assert_true("status" not in schema["properties"],
                   "the assistant must not choose shadow/rejected status or activate policy")
