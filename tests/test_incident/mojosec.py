import copy
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import os
from unittest import mock

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
        "protected": {
            "mojosec": {
                "enabled": True,
                "sensor_id": SENSOR_ID,
                "allowed_versions": [1],
            },
        },
    }
    key.save(update_fields=["metadata"])
    plain, plain_token = ApiKey.create_for_group(
        group, "mojosec_receiver_test_plain", permissions={"security": True})
    manager, manager_token = ApiKey.create_for_group(
        group, "mojosec_receiver_test_manager", permissions={"manage_group": True})

    opts.mojosec_token = token
    opts.mojosec_plain_token = plain_token
    opts.mojosec_manager_token = manager_token
    opts.mojosec_key_id = key.pk


@th.django_unit_test()
def test_mojosec_endpoint_accepts_gzip_and_acks_each_event(opts):
    from mojo.apps.incident.models import Event, MojoSecReceipt

    batch = _golden_batch()
    batch["events"][0]["id"] = "d" * 64
    batch["events"][1]["id"] = "e" * 64
    batch["events"][1]["count"] = 1
    batch["events"][1]["attributes"].update({
        "request_uri": "/wp-login.php?token=must-stay-protected",
        "host": "EXAMPLE.invalid",
        "referrer": "https://example.invalid/private?secret=must-not-project",
        "user_agent": "curl/8.9 Authorization: Bearer must-not-project",
        "request_time": "1.250",
        "upstream_status": "502, 200",
        "upstream_response_time": "1.100, 0.100",
    })
    sudo_secret = "receipt-only-short-secret"
    sudo_command = (
        "/usr/bin/curl -H 'Authorization: Bearer auth-secret' "
        f"mysql -pdb-secret {sudo_secret}")
    sudo_event = copy.deepcopy(batch["events"][0])
    sudo_event.update({
        "id": "0" * 64, "kind": "auth.sudo_command", "severity": "high",
        "summary": "Privileged sudo command executed", "count": 1,
        "recommendation": "review",
        "attributes": {
            "source_ip": "192.0.2.44", "actor": "deploy", "target_user": "root",
            "tty": "pts/3", "boot_id": "a" * 32, "audit_session": 93,
            "attribution_provenance": "audit_session", "cwd": "/opt/api",
            "command_path": "/usr/bin/curl",
            "command_sha256": hashlib.sha256(sudo_command.encode()).hexdigest(),
            "command": sudo_command,
        },
    })
    batch["events"].append(sudo_event)
    poisoned_event = copy.deepcopy(batch["events"][1])
    poisoned_event.update({
        "id": "6" * 64, "kind": "web.error", "severity": "warning",
        "summary": "Web request returned a server error", "count": 1,
        "recommendation": "review",
        "attributes": {
            "source_ip": "198.51.100.61", "status": 500,
            "request_uri": {"hidden": "receipt-path-secret"},
            "user_agent": ["receipt-ua-secret"],
            "referrer": {"hidden": "https://ref.invalid/receipt-referrer-secret"},
            "host": ["receipt-host-secret.invalid"],
            "method": {"hidden": "RECEIPT-METHOD-SECRET"},
            "request_id": ["receipt-request-id-secret"],
        },
    })
    batch["events"].append(poisoned_event)
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
    th.assert_eq(len(response.response.results), 4,
                 "the receiver must acknowledge every event in the batch")
    th.assert_eq(
        MojoSecReceipt.objects.filter(sensor_id=SENSOR_ID, publish_state="published").count(), 4,
        "accepted acknowledgements must have durable published receipts")
    th.assert_eq(
        Event.objects.filter(metadata__mojosec__sensor_id=SENSOR_ID).count(), 4,
        "the batch should create one bounded central Event projection per wire event")

    probe = Event.objects.get(metadata__mojosec__event_id="e" * 64)
    login = Event.objects.get(metadata__mojosec__event_id="d" * 64)
    receipt = MojoSecReceipt.objects.get(wire_event_id="e" * 64)
    sudo_receipt = MojoSecReceipt.objects.get(wire_event_id="0" * 64)
    sudo_projected = Event.objects.get(metadata__mojosec__event_id="0" * 64)
    poisoned_receipt = MojoSecReceipt.objects.get(wire_event_id="6" * 64)
    poisoned_projected = Event.objects.get(metadata__mojosec__event_id="6" * 64)
    th.assert_eq(probe.source_ip, "198.51.100.7",
                 "an eligible detector kind should promote its validated source IP")
    th.assert_eq(login.source_ip, "192.0.2.20",
                 "a source-bearing SSH event should promote its validated source")
    th.assert_true("attributes" not in probe.metadata["mojosec"],
                   "untrusted sensor attributes must stay out of LLM-visible Event metadata")
    th.assert_true("sensor_policy_revision" not in probe.metadata["mojosec"],
                   "free-form sensor policy labels must be represented by digest in Event metadata")
    th.assert_eq(receipt.replay_features["event"]["attributes"]["path"], "/wp-login.php",
                 "bounded raw features should remain available for deterministic offline replay")
    th.assert_in("must-stay-protected",
                 receipt.replay_features["event"]["attributes"]["request_uri"],
                 "raw bounded request evidence must remain in the DENY_AI receipt")
    projected = json.dumps(probe.metadata, sort_keys=True)
    th.assert_true("must-stay-protected" not in projected and
                   "must-not-project" not in projected,
                   "raw request secrets must never enter Event metadata")
    th.assert_eq(
        poisoned_receipt.replay_features["event"]["attributes"]["request_uri"],
        {"hidden": "receipt-path-secret"},
        "protected receipts must retain protocol-valid non-string raw evidence")
    th.assert_eq(
        poisoned_receipt.replay_features["event"]["attributes"]["user_agent"],
        ["receipt-ua-secret"],
        "protected receipts must preserve the original raw UA value")
    poisoned_visible = json.dumps(poisoned_projected.metadata, sort_keys=True)
    for secret in ("receipt-path-secret", "receipt-ua-secret",
                   "receipt-referrer-secret", "receipt-host-secret",
                   "RECEIPT-METHOD-SECRET", "receipt-request-id-secret"):
        th.assert_true(secret not in poisoned_visible,
                       f"central projection stringified non-string raw field {secret}")
    th.assert_in(sudo_secret, sudo_receipt.replay_features["event"]["attributes"]["command"],
                 "bounded raw sudo context must remain only in the protected receipt")
    sudo_visible = json.dumps(sudo_projected.metadata, sort_keys=True)
    for secret in (sudo_secret, "auth-secret", "db-secret"):
        th.assert_true(secret not in sudo_visible and
                       hashlib.sha256(secret.encode()).hexdigest()[:12] not in sudo_visible,
                       "sudo secrets and per-token digests must not enter Event metadata")
    th.assert_eq(
        sudo_projected.metadata["mojosec"]["evidence"]["command"],
        {"family": "network_client", "detail": "<redacted>"},
        "Event evidence should retain only a server-owned command family")
    th.assert_true(hashlib.sha256(sudo_command.encode()).hexdigest() not in sudo_visible and
                   "/usr/bin/curl" not in sudo_visible and "/opt/api" not in sudo_visible,
                   "command digest, executable, and working path must remain receipt-only")
    th.assert_eq(probe.metadata["mojosec"]["evidence"]["referrer_origin"],
                 "https://example.invalid",
                 "central projection should retain only the validated HTTP origin")
    th.assert_eq(probe.metadata["mojosec"]["evidence"]["user_agent"]["family"], "curl",
                 "central projection should structure the UA family")
    th.assert_in("curl/8.9", probe.metadata["mojosec"]["evidence"]["user_agent"]["display"],
                 "central projection should retain a useful scrubbed UA display")
    th.assert_true(receipt.RestMeta.DENY_AI,
                   "the model holding raw replay features must be denied to generic AI queries")
    th.assert_true(
        not receipt.RestMeta.CAN_CREATE
        and not receipt.RestMeta.CAN_UPDATE
        and not receipt.RestMeta.CAN_DELETE,
        "generic receipt REST routes must be read-only regardless of caller permissions")
    th.assert_eq(receipt.handler_state, MojoSecReceipt.HANDLER_NONE,
                 "a publication without a selected handler needs no outbox job")
    th.assert_true(probe.incident_id is None,
                   "host recommendations must not create incidents without an exact central RuleSet")
    th.assert_eq(probe.metadata["mojosec"]["sensor_id"], SENSOR_ID,
                 "central events must retain the server-validated host identity")
    th.assert_eq(probe.metadata["mojosec"]["installation_key_id"], receipt.api_key_id,
                 "central events must expose a non-secret installation identity")
    th.assert_true(probe.group_id is None,
                   "host identity must not be confused with customer tenant attribution")
    activity_fields = Event.RestMeta.GRAPHS["activity"]["fields"]
    th.assert_true("metadata" in activity_fields and "source_ip" in activity_fields,
                   "the activity graph must serialize rich MojoSec evidence and canonical source IP")


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
def test_manage_group_cannot_rewrite_server_owned_sensor_enrollment(opts):
    from mojo.apps.account.models import ApiKey

    _use_apikey(opts, opts.mojosec_manager_token)
    response = opts.client.post(
        f"/api/group/apikey/{opts.mojosec_key_id}",
        {"metadata": {"protected": {"mojosec": {
            "enabled": True, "sensor_id": "attacker-host", "allowed_versions": [1],
        }}}},
    )
    th.assert_true(response.status_code in (401, 403),
                   "manage_group must not mutate protected sensor enrollment")
    key = ApiKey.objects.get(pk=opts.mojosec_key_id)
    th.assert_eq(key.metadata["protected"]["mojosec"]["sensor_id"], SENSOR_ID,
                 "a denied generic REST write must leave host identity unchanged")


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
def test_mojosec_stream_parser_bounds_plain_gzip_and_content_length(opts):
    from mojo.apps.incident.services import mojosec

    class StreamRequest:
        def __init__(self, body, length=None, encoding=""):
            self.raw = body
            self.META = {"CONTENT_TYPE": "application/json"}
            if length is not None:
                self.META["CONTENT_LENGTH"] = str(length)
            if encoding:
                self.META["HTTP_CONTENT_ENCODING"] = encoding

        def read(self, size):
            return self.raw[:size]

        @property
        def body(self):
            raise AssertionError("bounded receiver must not materialize request.body")

    raw = json.dumps(_golden_batch()).encode("utf-8")
    parsed = mojosec.parse_request_batch(StreamRequest(raw))
    th.assert_eq(parsed["sensor_id"], SENSOR_ID,
                 "an absent Content-Length must still be safely bounded and accepted")
    parsed = mojosec.parse_request_batch(StreamRequest(gzip.compress(raw), encoding="gzip"))
    th.assert_eq(parsed["schema"], "mojosec.batch",
                 "gzip streams must use the same bounded parser")
    with th.assert_raises(mojosec.MojoSecIngestError):
        mojosec.parse_request_batch(StreamRequest(raw, length=len(raw) - 1))


@th.django_unit_test()
def test_mojosec_middleware_never_parses_or_logs_sensitive_body(opts):
    from django.http import HttpResponse
    from django.test import RequestFactory
    from mojo.middleware.mojo import MojoMiddleware
    from mojo.middleware import logging as request_logging

    raw = json.dumps(_golden_batch()).encode("utf-8")
    request = RequestFactory().post(
        "/api/incident/mojosec/batch/", data=raw, content_type="application/json")
    observed = {}

    def downstream(child):
        observed["marked"] = child._mojosec_sensitive_body
        observed["data"] = dict(child.DATA)
        observed["body_cached"] = hasattr(child, "_body")
        return HttpResponse(b"ok")

    MojoMiddleware(downstream)(request)
    th.assert_true(observed["marked"],
                   "the exact batch route must be marked sensitive before generic parsing")
    th.assert_eq(observed["data"], {},
                 "generic request.DATA must remain empty for sensor evidence")
    th.assert_true(not observed["body_cached"],
                   "MojoMiddleware must leave the request stream unmaterialized")

    queued = []
    logger = request_logging.LoggerMiddleware(lambda child: HttpResponse(b"ok"))
    logger.can_log = lambda child: True
    logger.queue_log = lambda *args: queued.append(args)
    old_db = request_logging.LOGIT_DB_ALL
    old_file = request_logging.LOGIT_FILE_ALL
    try:
        request_logging.LOGIT_DB_ALL = True
        request_logging.LOGIT_FILE_ALL = True
        logger.log_request(request)
    finally:
        request_logging.LOGIT_DB_ALL = old_db
        request_logging.LOGIT_FILE_ALL = old_file
    th.assert_eq(len(queued), 2,
                 "broad request logging may emit only fixed metadata summaries")
    th.assert_true(all(SENSOR_ID not in item[2] for item in queued),
                   "LOGIT_DB_ALL and file logging must never copy sensor evidence")
    th.assert_true(all("mojosec_batch" in item[2] for item in queued),
                   "sensitive log entries must be recognizable metadata-only summaries")


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


@th.django_unit_test()
def test_mojosec_server_registry_ignores_host_escalation_and_victim_ip(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.services import mojosec

    key = ApiKey.objects.get(name="mojosec_receiver_test_authorized")
    batch = _golden_batch()
    claimed = batch["events"][0]
    claimed.update({
        "id": "1" * 64, "kind": "web.error", "severity": "critical",
        "recommendation": "block_ip", "count": 100,
    })
    claimed["attributes"]["source_ip"] = "192.0.2.55"
    batch["events"] = [claimed]
    ack = mojosec.ingest_batch(key, batch)
    th.assert_eq(ack["results"][0]["status"], "accepted",
                 "valid evidence remains reportable despite untrusted recommendations")
    event = Event.objects.get(metadata__mojosec__event_id="1" * 64)
    th.assert_eq(event.level, 5,
                 "host critical severity cannot exceed the server-owned kind policy")
    th.assert_eq(event.source_ip, "192.0.2.55",
                 "known source-bearing web kinds should promote canonical IP evidence")
    th.assert_true(event.incident_id is None,
                   "an advisory block claim cannot action outside the exact central registry")
    th.assert_eq(event.metadata["mojosec"]["sensor_severity"], "critical",
                 "the raw severity claim remains advisory evidence")

    probe = copy.deepcopy(claimed)
    probe.update({"id": "2" * 64, "kind": "web.probe", "count": 1})
    batch["events"] = [probe]
    mojosec.ingest_batch(key, batch)
    event = Event.objects.get(metadata__mojosec__event_id="2" * 64)
    th.assert_eq(event.source_ip, "192.0.2.55",
                 "truthful fingerprinting permits even one canonical probe source")


@th.django_unit_test()
def test_mojosec_idempotency_is_scoped_to_installation_key(opts):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.incident.models import MojoSecReceipt
    from mojo.apps.incident.services import mojosec

    group = Group.objects.get(name="mojosec_receiver_test_group")
    second, token = ApiKey.create_for_group(
        group, "mojosec_receiver_test_second", permissions={"mojosec_ingest": True})
    second.metadata = copy.deepcopy(
        ApiKey.objects.get(name="mojosec_receiver_test_authorized").metadata)
    second.save(update_fields=["metadata"])
    batch = _golden_batch()
    batch["events"] = [batch["events"][0]]
    batch["events"][0]["id"] = "3" * 64
    first = mojosec.ingest_batch(
        ApiKey.objects.get(name="mojosec_receiver_test_authorized"), batch)
    other = mojosec.ingest_batch(second, batch)
    th.assert_eq(first["results"][0]["status"], "accepted",
                 "the first installation should claim its own wire id")
    th.assert_eq(other["results"][0]["status"], "accepted",
                 "another authenticated installation may use the same wire id")
    th.assert_eq(MojoSecReceipt.objects.filter(wire_event_id="3" * 64).count(), 2,
                 "deduplication identity must include the authenticated API key")


@th.django_unit_test()
def test_mojosec_concurrent_duplicate_creates_one_receipt(opts):
    from django.db import close_old_connections
    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.models import MojoSecReceipt
    from mojo.apps.incident.services import mojosec

    key_id = ApiKey.objects.get(name="mojosec_receiver_test_authorized").pk
    batch = _golden_batch()
    sensor_event = batch["events"][0]
    sensor_event["id"] = "5" * 64
    digest = mojosec._payload_digest(sensor_event)

    def create(unused):
        close_old_connections()
        try:
            receipt, created = mojosec._create_receipt(
                ApiKey.objects.get(pk=key_id), batch, sensor_event, digest)
            return receipt.pk, created
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    th.assert_eq(len(set(row[0] for row in results)), 1,
                 "concurrent copies must converge on one durable receipt")
    th.assert_eq(sorted(row[1] for row in results), [False, True],
                 "exactly one concurrent caller may create the installation event")
    th.assert_eq(MojoSecReceipt.objects.filter(
        api_key_id=key_id, wire_event_id="5" * 64).count(), 1,
        "the database uniqueness boundary must enforce concurrent idempotency")


@th.django_unit_test()
def test_mojosec_handler_outbox_is_durable_and_retryable(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.models import MojoSecReceipt, RuleSet
    from mojo.apps.incident.services import mojosec

    key = ApiKey.objects.get(name="mojosec_receiver_test_authorized")
    RuleSet.objects.create(
        category="mojosec.auth.ssh_failure", name="Receiver test outbox",
        handler="notify://security", priority=50)
    batch = _golden_batch()
    batch["events"] = [batch["events"][0]]
    batch["events"][0].update({"id": "4" * 64, "kind": "auth.ssh_failure", "count": 8})

    # Scoped to incident publishes and forwarding the rest: test modules run
    # as parallel threads, and an unscoped side_effect here would make OTHER
    # modules' real publishes raise "queue unavailable" mid-window.
    _incident_publish = lambda c: str(c.get("func", "")).startswith("mojo.apps.incident.")

    with th.capture_publishes(_incident_publish,
                              side_effect=RuntimeError("queue unavailable")):
        ack = mojosec.ingest_batch(key, batch)
    th.assert_eq(ack["results"][0]["status"], "retry",
                 "the receiver must not acknowledge required work before durable queueing")
    receipt = MojoSecReceipt.objects.get(wire_event_id="4" * 64, api_key=key)
    th.assert_eq(receipt.handler_state, MojoSecReceipt.HANDLER_FAILED,
                 "a queue failure must remain visible and replayable")

    with th.capture_publishes(_incident_publish, result="a" * 32) as calls:
        replay = mojosec.ingest_batch(key, batch)
    th.assert_eq(replay["results"][0]["status"], "duplicate",
                 "request replay must recover an already-published receipt")
    receipt.refresh_from_db()
    th.assert_eq(receipt.handler_state, MojoSecReceipt.HANDLER_QUEUED,
                 "successful durable queueing must advance the outbox")
    th.assert_eq(calls[0]["idempotency_key"], f"mojosec-handler:{receipt.pk}",
                 "outbox queue retries must use a stable receipt identity")

    with mock.patch.object(RuleSet, "run_handler", return_value=True) as run:
        mojosec._dispatch_receipt_handlers(receipt.pk)
        mojosec._dispatch_receipt_handlers(receipt.pk)
    receipt.refresh_from_db()
    th.assert_eq(receipt.handler_state, MojoSecReceipt.HANDLER_DISPATCHED,
                 "the dispatcher marks completion only after child jobs are published")
    prefix = run.call_args.kwargs["idempotency_prefix"]
    th.assert_eq(run.call_count, 1,
                 "a crash/retry after the dispatched mark must not republish child jobs")
    th.assert_true(prefix.startswith(f"mojosec:{receipt.pk}:") and len(prefix) < 64,
                   "handler child jobs need one bounded stable idempotency prefix")


@th.django_unit_test()
def test_mojosec_receipt_pruning_keeps_pending_outbox_rows(opts):
    from datetime import timedelta
    from mojo.apps.incident.models import MojoSecReceipt
    from mojo.apps.incident.services import mojosec

    published = MojoSecReceipt.objects.filter(
        sensor_id=SENSOR_ID, publish_state="published").first()
    th.assert_true(published is not None,
                   "the pruning test needs one published receipt from prior ingestion")
    pending = MojoSecReceipt.objects.create(
        api_key=published.api_key,
        sensor_id=SENSOR_ID,
        wire_event_id="9" * 64,
        payload_digest="8" * 64,
        replay_features={"event": {}},
    )
    queued = MojoSecReceipt.objects.create(
        api_key=published.api_key,
        sensor_id=SENSOR_ID,
        wire_event_id="7" * 64,
        payload_digest="6" * 64,
        publish_state=MojoSecReceipt.PUBLISH_PUBLISHED,
        published_at=published.published_at,
        handler_state=MojoSecReceipt.HANDLER_QUEUED,
        replay_features={"event": {}},
    )
    future = published.published_at + timedelta(days=46)
    mojosec.prune_receipts(now=future)
    th.assert_true(not MojoSecReceipt.objects.filter(pk=published.pk).exists(),
                   "published receipts older than the retry margin should be pruned")
    th.assert_true(MojoSecReceipt.objects.filter(pk=pending.pk).exists(),
                   "pending outbox rows must never be removed by age retention")
    th.assert_true(MojoSecReceipt.objects.filter(pk=queued.pk).exists(),
                   "queued handler work must never be removed by age retention")
