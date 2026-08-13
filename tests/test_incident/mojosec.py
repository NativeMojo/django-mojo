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


def _local_only_event(event_id="a9" * 32):
    event = copy.deepcopy(_golden_batch()["events"][0])
    event.update({
        "id": event_id, "kind": "auth.session_open", "severity": "info",
        "summary": "PAM service session opened", "count": 1,
        "recommendation": "none",
        "attributes": {
            "attribution_provenance": "none", "service": "systemd-user",
            "target_user": "www", "target_uid": 80, "opener_uid": 0,
            "producer_uid": 0, "producer_pid": 4123,
            "producer_comm": "(systemd)",
            "producer_exe": "/usr/lib/systemd/systemd",
            "systemd_unit": "user@80.service", "boot_id": "b" * 32,
            "audit_session": 44, "audit_loginuid": 80,
        },
    })
    event["first_seen"] = event["observed_at"]
    event["last_seen"] = event["observed_at"]
    return event


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
    from mojo.mojosec.evidence import build_evidence

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
    local_sudo_command = "/usr/bin/systemctl restart api.service --no-block"
    local_sudo_event = copy.deepcopy(sudo_event)
    local_sudo_event.update({
        "id": "ab" * 32,
        "attributes": {
            "source_ip": "198.51.100.99", "actor": "deploy", "target_user": "root",
            "tty": "pts/8", "boot_id": "b" * 32, "audit_session": 94,
            "attribution_provenance": "none", "cwd": "/opt/api",
            "command_path": "/usr/bin/systemctl",
            "command_sha256": hashlib.sha256(local_sudo_command.encode()).hexdigest(),
            "command": local_sudo_command,
        },
    })
    batch["events"].append(local_sudo_event)
    truncated_attributes = build_evidence("auth.sudo_command", {
        "source_ip": "192.0.2.45", "actor": "deploy", "target_user": "root",
        "tty": "pts/4", "boot_id": "c" * 32, "audit_session": 95,
        "attribution_provenance": "audit_session", "cwd": "/opt/api",
        "command_path": "/usr/bin/curl", "command": "x" * 2049,
    })
    truncated_sudo_event = copy.deepcopy(sudo_event)
    truncated_sudo_event.update({"id": "ac" * 32, "attributes": truncated_attributes})
    batch["events"].append(truncated_sudo_event)
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
    th.assert_eq(len(response.response.results), 6,
                 "the receiver must acknowledge every event in the batch")
    th.assert_eq(
        MojoSecReceipt.objects.filter(sensor_id=SENSOR_ID, publish_state="published").count(), 6,
        "accepted acknowledgements must have durable published receipts")
    th.assert_eq(
        Event.objects.filter(metadata__mojosec__sensor_id=SENSOR_ID).count(), 6,
        "the batch should create one bounded central Event projection per wire event")

    probe = Event.objects.get(metadata__mojosec__event_id="e" * 64)
    login = Event.objects.get(metadata__mojosec__event_id="d" * 64)
    receipt = MojoSecReceipt.objects.get(wire_event_id="e" * 64)
    sudo_receipt = MojoSecReceipt.objects.get(wire_event_id="0" * 64)
    sudo_projected = Event.objects.get(metadata__mojosec__event_id="0" * 64)
    local_sudo_receipt = MojoSecReceipt.objects.get(wire_event_id="ab" * 32)
    local_sudo_projected = Event.objects.get(metadata__mojosec__event_id="ab" * 32)
    truncated_sudo_receipt = MojoSecReceipt.objects.get(wire_event_id="ac" * 32)
    truncated_sudo_projected = Event.objects.get(metadata__mojosec__event_id="ac" * 32)
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
    th.assert_eq(sudo_receipt.replay_features["event"]["attributes"],
                 sudo_event["attributes"],
                 "the protected receipt must retain the byte-for-byte original full sudo evidence")
    sudo_evidence = sudo_projected.metadata["mojosec"]["evidence"]
    th.assert_eq(sudo_evidence["command"], sudo_command,
                 "the security-admin Event must expose the exact bounded sudo command")
    th.assert_eq((sudo_evidence["command_path"], sudo_evidence["cwd"],
                  sudo_evidence["actor"], sudo_evidence["target_user"],
                  sudo_evidence["tty"], sudo_evidence["boot_id"],
                  sudo_evidence["audit_session"], sudo_evidence["attribution"]),
                 ("/usr/bin/curl", "/opt/api", "deploy", "root", "pts/3",
                  "a" * 32, 93, "audit_session"),
                 "the admin Event must preserve complete validated execution context")
    th.assert_eq(sudo_evidence["command_family"], "network_client",
                 "server-owned command family may remain as additive evidence")
    th.assert_true(sudo_secret in json.dumps(sudo_evidence) and
                   "auth-secret" in json.dumps(sudo_evidence) and
                   "db-secret" in json.dumps(sudo_evidence),
                   "secret-looking command text must remain visible to authorized administrators")
    th.assert_eq(local_sudo_receipt.replay_features["event"]["attributes"],
                 local_sudo_event["attributes"],
                 "a receipt must retain even an unpromoted source claim for deterministic replay")
    th.assert_eq(local_sudo_projected.source_ip, None,
                 "a stray address with unattributed provenance must not populate Event.source_ip")
    th.assert_eq(local_sudo_projected.metadata["mojosec"]["evidence"]["attribution"],
                 "none", "local or unmapped sudo evidence must report explicit none attribution")
    th.assert_eq(local_sudo_projected.metadata["mojosec"]["evidence"]["command"],
                 local_sudo_command,
                 "unattributed sudo evidence must still retain the exact administrative command")
    th.assert_eq(truncated_sudo_receipt.replay_features["event"]["attributes"],
                 truncated_attributes,
                 "sensor-truncated evidence and its digest must remain byte-for-byte in replay")
    truncated_visible = truncated_sudo_projected.metadata["mojosec"]["evidence"]
    th.assert_eq(truncated_visible["command"], "x" * 2048,
                 "the accepted sensor prefix must project unchanged")
    th.assert_true(truncated_visible["command_truncated"] is True,
                   "the Event must explicitly identify a projected command prefix as truncated")
    th.assert_true("command_sha256" not in truncated_visible,
                   "full-value truncation digests must remain receipt-only")
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
    th.assert_eq(Event.RestMeta.VIEW_PERMS, ["view_security", "security"],
                 "rich sudo evidence must remain on the established security-admin Event surface")
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
def test_exact_local_only_receipts_are_eventless_idempotent_and_race_safe(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.models import Event, Incident, MojoSecReceipt
    from mojo.apps.incident.services import mojosec

    key = ApiKey.objects.get(name="mojosec_receiver_test_authorized")
    batch = _golden_batch()
    batch["events"] = [_local_only_event()]
    event_count = Event.objects.count()
    incident_count = Incident.objects.count()

    first = mojosec.ingest_batch(key, batch)
    second = mojosec.ingest_batch(key, batch)
    th.assert_eq(first["results"][0]["status"], "accepted",
                 "the first exact local-only identity must durably accept")
    th.assert_eq(second["results"][0]["status"], "duplicate",
                 "a published local-only identity must replay as duplicate")
    receipt = MojoSecReceipt.objects.get(api_key=key, wire_event_id="a9" * 32)
    th.assert_true(receipt.event_id is None and receipt.incident_id is None,
                   "a new local-only receipt must never create or link public evidence")
    th.assert_eq(receipt.handler_state, MojoSecReceipt.HANDLER_NONE,
                 "local-only terminal receipts must never enter handler dispatch")
    th.assert_eq(receipt.replay_features["feature_schema"], "local_only_receipt_v1",
                 "local-only receipts need a distinct non-learning compatibility schema")
    th.assert_eq(receipt.replay_features["event"], batch["events"][0],
                 "eventless compatibility receipts must retain complete protected raw evidence")
    th.assert_eq(Event.objects.count(), event_count,
                 "local-only central acceptance must create no Event")
    th.assert_eq(Incident.objects.count(), incident_count,
                 "local-only central acceptance must create no Incident")

    pending_event = Event.objects.create(
        category="mojosec.auth.session_open", scope="mojosec", level=2,
        title="pre-race local event")
    pending_wire = _local_only_event("a8" * 32)
    prior_replay = {
        "feature_schema": "replay_features_v1", "schema": "mojosec.batch",
        "version": 1, "sensor_id": "original-sensor",
        "policy_revision": "original-policy", "event": pending_wire,
        "effective": {"kind": "auth.session_open", "count": 1,
                      "level": 2, "severity": "info"},
        "protected_extra": {"audit": "must-survive"},
    }
    pending = MojoSecReceipt.objects.create(
        api_key=key, event=pending_event, sensor_id=SENSOR_ID,
        wire_event_id=pending_wire["id"],
        payload_digest=mojosec._payload_digest(pending_wire),
        sensor_policy_revision="original-policy", replay_features=prior_replay,
        publish_state=MojoSecReceipt.PUBLISH_PENDING,
        handler_state=MojoSecReceipt.HANDLER_PENDING,
    )
    batch["events"] = [pending_wire]
    converted = mojosec.ingest_batch(key, batch)
    pending.refresh_from_db()
    th.assert_eq(converted["results"][0]["status"], "accepted",
                 "an identical unpublished receipt must atomically terminalize")
    th.assert_eq(pending.event_id, pending_event.pk,
                 "pending compatibility conversion must preserve its Event pointer and row")
    th.assert_eq(pending.publish_state, MojoSecReceipt.PUBLISH_PUBLISHED,
                 "pending compatibility conversion must close publication under its row lock")
    th.assert_eq(pending.handler_state, MojoSecReceipt.HANDLER_NONE,
                 "pending compatibility conversion must close handler dispatch")
    for field, value in prior_replay.items():
        if field == "feature_schema":
            continue
        th.assert_eq(pending.replay_features[field], value,
                     f"pending conversion must preserve protected replay field {field}")
    th.assert_eq(pending.replay_features["disposition"], "local_only",
                 "pending conversion may add only its non-learning disposition marker")
    th.assert_eq(pending.sensor_policy_revision, "original-policy",
                 "a retry batch must not overwrite original receipt policy provenance")
    th.assert_true(Event.objects.filter(pk=pending_event.pk).exists(),
                   "compatibility conversion must never delete historical Event evidence")

    historical_event = Event.objects.create(
        category="mojosec.auth.session_open", scope="mojosec", level=2,
        title="published historical local event")
    historical_wire = _local_only_event("a7" * 32)
    historical = MojoSecReceipt.objects.create(
        api_key=key, event=historical_event, sensor_id=SENSOR_ID,
        wire_event_id=historical_wire["id"],
        payload_digest=mojosec._payload_digest(historical_wire),
        replay_features={"feature_schema": "replay_features_v1", "event": historical_wire},
        publish_state=MojoSecReceipt.PUBLISH_PUBLISHED,
        handler_state=MojoSecReceipt.HANDLER_DISPATCHED,
    )
    batch["events"] = [historical_wire]
    replayed = mojosec.ingest_batch(key, batch)
    historical.refresh_from_db()
    th.assert_eq(replayed["results"][0]["status"], "duplicate",
                 "historical published evidence must remain a duplicate")
    th.assert_eq(historical.event_id, historical_event.pk,
                 "historical published Event pointers must remain untouched")
    th.assert_eq(historical.handler_state, MojoSecReceipt.HANDLER_DISPATCHED,
                 "historical handler state must never be rewritten")

    batch["events"] = [pending_wire]
    changed = copy.deepcopy(batch)
    changed["events"][0]["attributes"]["producer_pid"] = 9999
    conflict = mojosec.ingest_batch(key, changed)
    th.assert_eq(conflict["results"][0]["status"], "rejected",
                 "a local-only identity reused for different evidence must reject")

    batch["events"] = [_local_only_event("a6" * 32)]
    with mock.patch.object(mojosec, "_accept_local_only",
                           side_effect=RuntimeError("database unavailable")):
        retry = mojosec.ingest_batch(key, batch)
    th.assert_eq(retry["results"][0]["status"], "retry",
                 "local-only persistence failure must retain sender retry semantics")


@th.django_unit_test()
def test_protocol_valid_local_only_near_match_keeps_ordinary_central_projection(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.models import Event, MojoSecReceipt
    from mojo.apps.incident.services import mojosec

    key = ApiKey.objects.get(name="mojosec_receiver_test_authorized")
    batch = _golden_batch()
    near_match = _local_only_event("15" * 32)
    near_match["attributes"]["source_ip"] = None
    batch["events"] = [near_match]
    result = mojosec.ingest_batch(key, batch)
    th.assert_eq(result["results"][0]["status"], "accepted",
                 "a protocol-valid explicit-null near match must follow ordinary publication")
    receipt = MojoSecReceipt.objects.get(api_key=key, wire_event_id="15" * 32)
    projected = Event.objects.get(pk=receipt.event_id)
    th.assert_eq(receipt.replay_features["feature_schema"], "replay_features_v1",
                 "ordinary near matches must retain the established learning schema")
    th.assert_true("source_ip" in receipt.replay_features["event"]["attributes"] and
                   receipt.replay_features["event"]["attributes"]["source_ip"] is None,
                   "ordinary protected replay must preserve explicit-null raw evidence")
    th.assert_eq(projected.metadata["mojosec"]["evidence"]["target_uid"], 80,
                 "ordinary near matches must keep the unchanged rich Event projection")


@th.django_unit_test()
def test_local_only_create_and_publication_terminalization_races_are_safe(opts):
    import threading

    from django.db import close_old_connections
    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.models import Event, MojoSecReceipt
    from mojo.apps.incident.services import mojosec

    create_wire = _local_only_event("14" * 32)
    create_batch = _golden_batch()
    create_batch["events"] = [create_wire]
    barrier = threading.Barrier(2)

    def create_once():
        close_old_connections()
        try:
            key = ApiKey.objects.get(name="mojosec_receiver_test_authorized")
            barrier.wait(timeout=5)
            return mojosec.ingest_batch(key, copy.deepcopy(create_batch))["results"][0]["status"]
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda unused: create_once(), range(2)))
    th.assert_eq(sorted(statuses), ["accepted", "duplicate"],
                 f"concurrent local-only creates must admit one identity once: {statuses}")
    created = MojoSecReceipt.objects.get(wire_event_id="14" * 32)
    th.assert_true(created.event_id is None,
                   "the concurrent winner must still create only an eventless receipt")

    key = ApiKey.objects.get(name="mojosec_receiver_test_authorized")
    race_event = Event.objects.create(
        category="mojosec.auth.session_open", scope="mojosec", level=2,
        title="publication race fixture")
    race_wire = _local_only_event("13" * 32)
    race_receipt = MojoSecReceipt.objects.create(
        api_key=key, event=race_event, sensor_id=SENSOR_ID,
        wire_event_id=race_wire["id"], payload_digest=mojosec._payload_digest(race_wire),
        sensor_policy_revision="race-original", publish_state="pending",
        handler_state="pending", replay_features={
            "feature_schema": "replay_features_v1", "event": race_wire,
            "policy_revision": "race-original", "protected_extra": "preserve",
        })
    race_batch = _golden_batch()
    race_batch["events"] = [race_wire]
    race_barrier = threading.Barrier(2)

    def publish_old():
        close_old_connections()
        try:
            receipt = MojoSecReceipt.objects.get(pk=race_receipt.pk)
            race_barrier.wait(timeout=5)
            locked, did_publish = mojosec._publish_receipt(receipt)
            return "published" if locked is not None and did_publish else "terminal"
        finally:
            close_old_connections()

    def terminalize_new():
        close_old_connections()
        try:
            local_key = ApiKey.objects.get(pk=key.pk)
            race_barrier.wait(timeout=5)
            return mojosec.ingest_batch(
                local_key, copy.deepcopy(race_batch))["results"][0]["status"]
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        old_future = pool.submit(publish_old)
        new_future = pool.submit(terminalize_new)
        old_result = old_future.result(timeout=15)
        new_result = new_future.result(timeout=15)
    race_receipt.refresh_from_db()
    th.assert_eq(race_receipt.publish_state, MojoSecReceipt.PUBLISH_PUBLISHED,
                 "either race winner must leave one terminal published receipt")
    th.assert_eq(race_receipt.event_id, race_event.pk,
                 "publication races must preserve the preexisting Event pointer")
    th.assert_true(Event.objects.filter(pk=race_event.pk).exists(),
                   "publication races must never delete the preexisting Event row")
    th.assert_true(
        (old_result == "published" and new_result == "duplicate") or
        (old_result == "terminal" and new_result == "accepted"),
        f"lock ordering must yield one safe publication outcome: {old_result}, {new_result}")
    if new_result == "accepted":
        th.assert_eq(race_receipt.replay_features["protected_extra"], "preserve",
                     "terminalization race winner must preserve protected replay provenance")


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


@th.django_unit_test()
def test_mojosec_poison_batch_is_terminally_rejected(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.models import MojoSecReceipt
    from mojo.apps.incident.services import mojosec

    key = ApiKey.objects.get(name="mojosec_receiver_test_authorized")

    poisoned = _golden_batch()
    poisoned["policy_revision"] = "rev" + chr(0)
    with mock.patch.object(mojosec, "logger") as log:
        ack = mojosec.ingest_batch(key, poisoned)
    th.assert_eq([row["status"] for row in ack["results"]],
                 ["rejected"] * len(poisoned["events"]),
                 "a poisoned policy_revision must terminally reject every event, never retry")
    th.assert_true(all("policy_revision" in row["reason"] for row in ack["results"]),
                   "the terminal rejection must name the poisoned batch field")
    poison_ids = [event["id"] for event in poisoned["events"]]
    th.assert_eq(MojoSecReceipt.objects.filter(
        api_key=key, wire_event_id__in=poison_ids).count(), 0,
        "a poisoned batch must create no receipt rows at all")
    th.assert_eq(log.warning.call_count, 1,
                 "a poisoned batch must write one warning line, not one per event")
    th.assert_eq(log.exception.call_count, 0,
                 "a poisoned batch must never write a stack trace")

    with mock.patch.object(mojosec, "logger") as log:
        again = mojosec.ingest_batch(key, poisoned)
    th.assert_eq([row["status"] for row in again["results"]],
                 ["rejected"] * len(poisoned["events"]),
                 "re-sending the poison batch must be statelessly terminal, never retry")

    batch = _golden_batch()
    template = batch["events"][0]
    nul_event = copy.deepcopy(template)
    nul_event["id"] = "5d" * 32
    nul_event["summary"] = "bad summary " + chr(0)
    surrogate_event = copy.deepcopy(template)
    surrogate_event["id"] = "6d" * 32
    surrogate_event["attributes"] = dict(
        surrogate_event["attributes"], note="bad attribute \udcff")
    clean_event = copy.deepcopy(template)
    clean_event["id"] = "7e" * 32
    batch["events"] = [nul_event, surrogate_event, clean_event]
    with mock.patch.object(mojosec, "logger") as log:
        mixed = mojosec.ingest_batch(key, batch)
    th.assert_eq([row["status"] for row in mixed["results"]],
                 ["rejected", "rejected", "accepted"],
                 "per-event poison must reject only the unstorable events")
    th.assert_true(all("unstorable" in row["reason"]
                       for row in mixed["results"][:2]),
                   "per-event rejections must carry the unstorable-text reason")
    th.assert_eq(MojoSecReceipt.objects.filter(
        api_key=key, wire_event_id__in=["5d" * 32, "6d" * 32]).count(), 0,
        "poison events must not persist receipt rows")
    th.assert_true(MojoSecReceipt.objects.filter(
        api_key=key, wire_event_id="7" * 64).exists(),
        "the clean event beside poison must still be accepted and persisted")
    th.assert_eq(log.exception.call_count, 0,
                 "per-event poison must never write stack traces")


@th.django_unit_test()
def test_mojosec_storage_errors_ack_rejected_not_retry(opts):
    from django.db import DataError
    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.services import mojosec

    key = ApiKey.objects.get(name="mojosec_receiver_test_authorized")
    batch = _golden_batch()
    batch["events"] = [copy.deepcopy(batch["events"][0])]
    batch["events"][0]["id"] = "8" * 64

    with mock.patch.object(mojosec, "_create_receipt",
                           side_effect=DataError("bad value")), \
            mock.patch.object(mojosec, "logger") as log:
        ack = mojosec.ingest_batch(key, batch)
    th.assert_eq(ack["results"][0]["status"], "rejected",
                 "a value-domain storage error must map to a terminal rejection")
    th.assert_eq(ack["results"][0]["reason"], "event could not be stored",
                 "the storage-error rejection must carry its reason")
    th.assert_eq(log.exception.call_count, 0,
                 "value-domain storage errors must log one line, not a stack trace")

    with mock.patch.object(mojosec, "_create_receipt",
                           side_effect=RuntimeError("transient outage")):
        control = mojosec.ingest_batch(key, batch)
    th.assert_eq(control["results"][0]["status"], "retry",
                 "genuinely transient failures must keep today's retry behavior")


@th.django_unit_test()
def test_mojosec_handler_attempt_cap_dead_letters(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.models import MojoSecReceipt, RuleSet
    from mojo.apps.incident.services import mojosec
    from mojo.helpers import dates
    from mojo.helpers.settings import settings

    key = ApiKey.objects.get(name="mojosec_receiver_test_authorized")
    cap = settings.get_static("MOJOSEC_HANDLER_MAX_ATTEMPTS", 100, kind="int")
    rule = RuleSet.objects.create(
        category="mojosec.auth.ssh_failure", name="Receiver test cap",
        handler="notify://security", priority=50)
    now = dates.utcnow()

    def receipt_at(event_id, digest, attempts):
        return MojoSecReceipt.objects.create(
            api_key=key, sensor_id=SENSOR_ID, wire_event_id=event_id,
            payload_digest=digest, rule_set=rule,
            publish_state=MojoSecReceipt.PUBLISH_PUBLISHED, published_at=now,
            handler_state=MojoSecReceipt.HANDLER_FAILED,
            handler_attempts=attempts, handler_job_id="e" * 32,
            replay_features={"event": {}})

    capped = receipt_at("a1" * 32, "a2" * 32, cap)
    below = receipt_at("b1" * 32, "b2" * 32, cap - 1)

    with mock.patch.object(RuleSet, "run_handler", return_value=True) as run:
        mojosec.replay_handler_outbox()
    capped.refresh_from_db()
    below.refresh_from_db()
    th.assert_eq(capped.handler_state, MojoSecReceipt.HANDLER_DEAD,
                 "a receipt at the attempt cap must be swept to the terminal dead state")
    th.assert_eq(below.handler_state, MojoSecReceipt.HANDLER_DISPATCHED,
                 "a receipt below the cap must still be re-dispatched")
    dispatched_pks = {call.kwargs["idempotency_prefix"]
                      for call in run.call_args_list}
    th.assert_true(all(f"mojosec:{capped.pk}:" not in prefix
                       for prefix in dispatched_pks),
                   "the dead-lettered receipt must never reach run_handler again")

    failing = receipt_at("c1" * 32, "c2" * 32, 0)
    with mock.patch.object(RuleSet, "run_handler",
                           side_effect=RuntimeError("jobs.publish down")):
        mojosec.replay_handler_outbox()
    failing.refresh_from_db()
    th.assert_eq(failing.handler_state, MojoSecReceipt.HANDLER_FAILED,
                 "a failing cron replay must return the receipt to the capped loop")
    th.assert_eq(failing.handler_attempts, 1,
                 "cron replays that fail must consume the attempt budget the cap is sized for")

    with mock.patch.object(RuleSet, "run_handler", return_value=True):
        mojosec.replay_handler_outbox()
    capped.refresh_from_db()
    th.assert_eq(capped.handler_state, MojoSecReceipt.HANDLER_DEAD,
                 "later cron passes must leave the dead row untouched")

    replay_batch = _golden_batch()
    replay_batch["events"] = [copy.deepcopy(replay_batch["events"][0])]
    replay_batch["events"][0]["id"] = capped.wire_event_id
    MojoSecReceipt.objects.filter(pk=capped.pk).update(
        payload_digest=mojosec._payload_digest(replay_batch["events"][0]))
    with mock.patch.object(RuleSet, "run_handler", return_value=True) as run:
        replay = mojosec.ingest_batch(key, replay_batch)
    th.assert_eq(replay["results"][0]["status"], "duplicate",
                 "replaying a dead receipt's event must ack duplicate without dispatching")
    th.assert_eq(run.call_count, 0,
                 "a dead receipt replay must never dispatch its handler")


@th.django_unit_test()
def test_mojosec_stale_queued_handler_recovers(opts):
    from datetime import timedelta
    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.models import MojoSecReceipt, RuleSet
    from mojo.apps.incident.services import mojosec
    from mojo.helpers import dates

    key = ApiKey.objects.get(name="mojosec_receiver_test_authorized")
    rule = RuleSet.objects.create(
        category="mojosec.auth.ssh_failure", name="Receiver test stale queue",
        handler="notify://security", priority=50)
    now = dates.utcnow()

    def queued_receipt(event_id, digest):
        return MojoSecReceipt.objects.create(
            api_key=key, sensor_id=SENSOR_ID, wire_event_id=event_id,
            payload_digest=digest, rule_set=rule,
            publish_state=MojoSecReceipt.PUBLISH_PUBLISHED, published_at=now,
            handler_state=MojoSecReceipt.HANDLER_QUEUED,
            handler_job_id="f" * 32, replay_features={"event": {}})

    stale = queued_receipt("d1" * 32, "d2" * 32)
    fresh = queued_receipt("e1" * 32, "e2" * 32)
    MojoSecReceipt.objects.filter(pk=stale.pk).update(
        modified=now - timedelta(seconds=3600))

    with mock.patch.object(RuleSet, "run_handler", return_value=True):
        mojosec.replay_handler_outbox()
    stale.refresh_from_db()
    fresh.refresh_from_db()
    th.assert_eq(stale.handler_state, MojoSecReceipt.HANDLER_DISPATCHED,
                 "a queued receipt whose dispatch job vanished must be recovered by age")
    th.assert_eq(fresh.handler_state, MojoSecReceipt.HANDLER_QUEUED,
                 "recently queued receipts must be left for their live dispatch job")

    broken = queued_receipt("f1" * 32, "f2" * 32)
    MojoSecReceipt.objects.filter(pk=broken.pk).update(
        modified=now - timedelta(seconds=3600))
    with mock.patch.object(RuleSet, "run_handler",
                           side_effect=RuntimeError("still down")):
        mojosec.replay_handler_outbox()
    broken.refresh_from_db()
    th.assert_eq(broken.handler_state, MojoSecReceipt.HANDLER_FAILED,
                 "a failed stale-queue recovery must enter the capped retry loop")
    th.assert_eq(broken.handler_attempts, 1,
                 "a failed stale-queue recovery must consume the attempt budget")


@th.django_unit_test()
def test_mojosec_pruned_event_receipt_terminates(opts):
    from datetime import timedelta
    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.models import MojoSecReceipt
    from mojo.apps.incident.services import mojosec
    from mojo.helpers import dates

    key = ApiKey.objects.get(name="mojosec_receiver_test_authorized")
    now = dates.utcnow()
    batch = _golden_batch()
    batch["events"] = [copy.deepcopy(batch["events"][0])]
    batch["events"][0]["id"] = "9a" * 32
    digest = mojosec._payload_digest(batch["events"][0])

    orphan = MojoSecReceipt.objects.create(
        api_key=key, sensor_id=SENSOR_ID, wire_event_id="9a" * 32,
        payload_digest=digest, event=None, replay_features={"event": {}})
    MojoSecReceipt.objects.filter(pk=orphan.pk).update(
        created=now - timedelta(days=2))

    with mock.patch.object(mojosec, "logger") as log:
        ack = mojosec.ingest_batch(key, batch)
    th.assert_eq(ack["results"][0]["status"], "rejected",
                 "a receipt whose Event was pruned must terminally reject, never retry")
    th.assert_true("pruned" in ack["results"][0]["reason"],
                   "the pruned-evidence rejection must carry its reason")
    orphan.refresh_from_db()
    th.assert_eq(orphan.publish_state, MojoSecReceipt.PUBLISH_DEAD,
                 "the pruned-evidence receipt must reach the terminal dead state")
    th.assert_eq(log.exception.call_count, 0,
                 "the pruned-evidence path must not write stack traces")

    swept = MojoSecReceipt.objects.create(
        api_key=key, sensor_id=SENSOR_ID, wire_event_id="9b" * 32,
        payload_digest="9c" * 32, event=None, replay_features={"event": {}})
    MojoSecReceipt.objects.filter(pk=swept.pk).update(
        created=now - timedelta(days=2))
    mojosec.replay_handler_outbox()
    swept.refresh_from_db()
    th.assert_eq(swept.publish_state, MojoSecReceipt.PUBLISH_DEAD,
                 "the cron must terminalize pruned-event receipts that never re-POST")

    survivor = MojoSecReceipt.objects.create(
        api_key=key, sensor_id=SENSOR_ID, wire_event_id="9d" * 32,
        payload_digest="9e" * 32, event=None, replay_features={"event": {}})
    mojosec.prune_receipts(now=now + timedelta(days=52))
    th.assert_true(not MojoSecReceipt.objects.filter(pk=orphan.pk).exists(),
                   "aged-out dead receipts must be pruned")
    th.assert_true(not MojoSecReceipt.objects.filter(pk=swept.pk).exists(),
                   "cron-swept dead receipts must be pruned once aged")
    th.assert_true(MojoSecReceipt.objects.filter(pk=survivor.pk).exists(),
                   "live pending receipts must survive the dead-row prune")
