import copy
import gzip
import json
import os

from testit import helpers as th


SENSOR_ID = "mojosec-receiver-test"


def _golden_batch():
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "test_mojosec", "golden", "batch_v1.json")
    with open(path, encoding="utf-8") as handle:
        batch = json.load(handle)
    batch["sensor_id"] = SENSOR_ID
    return batch


def _use_apikey(opts, token):
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True


@th.django_unit_setup()
def setup_mojosec_receiver(opts):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.incident.models import Event, MojoSecReceipt, RuleSet

    MojoSecReceipt.objects.filter(sensor_id=SENSOR_ID).delete()
    Event.objects.filter(metadata__mojosec__sensor_id=SENSOR_ID).delete()
    RuleSet.objects.filter(name__startswith="Receiver test").delete()
    ApiKey.objects.filter(name__startswith="mojosec_receiver_test").delete()
    Group.objects.filter(name="mojosec_receiver_test_group").delete()

    group = Group.objects.create(name="mojosec_receiver_test_group", kind="organization")
    key, token = ApiKey.create_for_group(
        group, "mojosec_receiver_test_authorized",
        permissions={"mojosec_ingest": True})
    key.metadata = {
        "mojosec": {
            "enabled": True,
            "sensor_id": SENSOR_ID,
            "allowed_versions": [1],
        },
    }
    key.save(update_fields=["metadata"])
    plain, plain_token = ApiKey.create_for_group(
        group, "mojosec_receiver_test_plain", permissions={"security": True})

    opts.mojosec_token = token
    opts.mojosec_plain_token = plain_token


@th.django_unit_test()
def test_mojosec_endpoint_accepts_gzip_and_acks_each_event(opts):
    from mojo.apps.incident.models import Event, MojoSecReceipt

    batch = _golden_batch()
    batch["events"][0]["id"] = "d" * 64
    batch["events"][1]["id"] = "e" * 64
    body = gzip.compress(json.dumps(batch).encode("utf-8"))
    _use_apikey(opts, opts.mojosec_token)
    response = opts.client.post(
        "/api/incident/mojosec/batch",
        data=body,
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )

    th.assert_eq(response.status_code, 200,
                 f"an enrolled MojoSec key should ingest a gzip batch: {response.response}")
    th.assert_eq(response.response.schema, "mojosec.ack",
                 "the receiver must return the shared acknowledgement schema without wrappers")
    th.assert_eq(len(response.response.results), 2,
                 "the receiver must acknowledge every event in the batch")
    th.assert_eq(
        MojoSecReceipt.objects.filter(sensor_id=SENSOR_ID, publish_state="published").count(), 2,
        "accepted acknowledgements must have durable published receipts")
    th.assert_eq(
        Event.objects.filter(metadata__mojosec__sensor_id=SENSOR_ID).count(), 2,
        "the batch should create one bounded central Event projection per wire event")

    probe = Event.objects.get(metadata__mojosec__event_id="e" * 64)
    th.assert_eq(probe.source_ip, "198.51.100.7",
                 "an eligible detector kind should promote its validated source IP")
    th.assert_true("attributes" not in probe.metadata["mojosec"],
                   "untrusted sensor attributes must stay out of LLM-visible Event metadata")
    th.assert_true(probe.incident_id is None,
                   "host recommendations must not create incidents without an exact central RuleSet")


@th.django_unit_test()
def test_mojosec_replay_is_idempotent_and_digest_conflicts_reject(opts):
    from mojo.apps.incident.services import mojosec

    batch = _golden_batch()
    from mojo.apps.account.models import ApiKey
    key = ApiKey.objects.get(name="mojosec_receiver_test_authorized")

    first = mojosec.ingest_batch(key, batch)
    th.assert_eq([row["status"] for row in first["results"]], ["accepted", "accepted"],
                 "new valid evidence should be accepted after central publication")
    second = mojosec.ingest_batch(key, batch)
    th.assert_eq(
        [row["status"] for row in second["results"]], ["duplicate", "duplicate"],
        "replaying published event ids must return duplicate without republishing")

    changed = copy.deepcopy(batch)
    changed["events"][0]["summary"] = "different evidence under the same id"
    conflict = mojosec.ingest_batch(key, changed)
    th.assert_eq(conflict["results"][0]["status"], "rejected",
                 "one event id must never be accepted with a different canonical digest")


@th.django_unit_test()
def test_mojosec_requires_permission_and_enrolled_sensor_identity(opts):
    batch = _golden_batch()
    _use_apikey(opts, opts.mojosec_plain_token)
    denied = opts.client.post("/api/incident/mojosec/batch", batch)
    th.assert_eq(denied.status_code, 403,
                 "an ordinary API key must not reach the host-security ingestion endpoint")

    _use_apikey(opts, opts.mojosec_token)
    wrong = copy.deepcopy(batch)
    wrong["sensor_id"] = "another-sensor"
    denied = opts.client.post("/api/incident/mojosec/batch", wrong)
    th.assert_eq(denied.status_code, 403,
                 "the wire sensor id must match the profile enrolled on the API key")


@th.django_unit_test()
def test_mojosec_parser_rejects_duplicate_json_and_concatenated_gzip(opts):
    from django.test import RequestFactory
    from mojo.apps.incident.services import mojosec

    request = RequestFactory().post(
        "/api/incident/mojosec/batch",
        data=b'{"schema":"mojosec.batch","schema":"duplicate"}',
        content_type="application/json")
    with th.assert_raises(mojosec.MojoSecIngestError):
        mojosec.parse_request_batch(request)

    raw = json.dumps(_golden_batch()).encode("utf-8")
    request = RequestFactory().post(
        "/api/incident/mojosec/batch",
        data=gzip.compress(raw) + gzip.compress(raw),
        content_type="application/json",
        HTTP_CONTENT_ENCODING="gzip")
    with th.assert_raises(mojosec.MojoSecIngestError):
        mojosec.parse_request_batch(request)


@th.django_unit_test()
def test_mojosec_exact_policy_ignores_broad_scope_and_default_llm(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.models import Incident, RuleSet
    from mojo.apps.incident.services import mojosec

    key = ApiKey.objects.get(name="mojosec_receiver_test_authorized")
    batch = _golden_batch()
    batch["events"] = [batch["events"][0]]
    batch["events"][0]["id"] = "f" * 64
    RuleSet.objects.create(
        category="mojosec", name="Receiver test broad scope", handler=None, priority=1)
    ack = mojosec.ingest_batch(key, batch)
    th.assert_eq(ack["results"][0]["status"], "accepted",
                 "a valid event should be stored even without an exact action policy")
    th.assert_true(
        not Incident.objects.filter(events__metadata__mojosec__sensor_id=SENSOR_ID).exists(),
        "a broad scope rule and the level fallback must not action MojoSec evidence")

    exact = copy.deepcopy(batch)
    exact["events"][0]["id"] = "g" * 64
    RuleSet.objects.create(
        category="mojosec.auth.ssh_login", name="Receiver test exact category",
        handler=None, priority=1)
    ack = mojosec.ingest_batch(key, exact)
    th.assert_eq(ack["results"][0]["status"], "accepted",
                 "an exact server-owned category rule should publish successfully")
    th.assert_true(
        Incident.objects.filter(events__metadata__mojosec__event_id="g" * 64).exists(),
        "only the exact central category policy should promote evidence to an incident")
