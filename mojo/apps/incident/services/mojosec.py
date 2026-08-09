"""Bounded, idempotent ingestion for the MojoSec host sensor."""

import hashlib
import json
import zlib

from django.db import IntegrityError, transaction
from django.db.models import F

from mojo.helpers import dates, logit
from mojo.helpers.settings import settings
from mojo.mojosec import protocol


logger = logit.get_logger(__name__, "incident.log")

MAX_COMPRESSED_BYTES = protocol.MAX_BATCH_BYTES
MAX_ERROR = 256
SOURCE_IP_KINDS = frozenset({
    "auth.ssh_login", "auth.ssh_failure", "web.probe", "web.error", "web.denied",
})
SEVERITY_LEVELS = {
    "info": 2,
    "warning": 5,
    "high": 8,
    "critical": 12,
}


class MojoSecIngestError(ValueError):
    def __init__(self, reason, status=400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _reject_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field: {key}")
        value[key] = child
    return value


def _decode_gzip(raw):
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        decoded = decoder.decompress(raw, protocol.MAX_BATCH_BYTES + 1)
        if decoder.unconsumed_tail or len(decoded) > protocol.MAX_BATCH_BYTES:
            raise MojoSecIngestError("decompressed MojoSec batch is too large", 413)
        decoded += decoder.flush(protocol.MAX_BATCH_BYTES + 1 - len(decoded))
    except zlib.error as err:
        raise MojoSecIngestError("invalid gzip-compressed MojoSec batch") from err
    if len(decoded) > protocol.MAX_BATCH_BYTES:
        raise MojoSecIngestError("decompressed MojoSec batch is too large", 413)
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise MojoSecIngestError("gzip payload is incomplete or contains trailing data")
    return decoded


def parse_request_batch(request):
    content_type = (request.META.get("CONTENT_TYPE") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise MojoSecIngestError("Content-Type must be application/json", 415)

    raw = request.body or b""
    if not raw:
        raise MojoSecIngestError("MojoSec batch body is empty")
    if len(raw) > MAX_COMPRESSED_BYTES:
        raise MojoSecIngestError("compressed MojoSec batch is too large", 413)

    encoding = (request.META.get("HTTP_CONTENT_ENCODING") or "").strip().lower()
    if encoding == "gzip":
        raw = _decode_gzip(raw)
    elif encoding not in ("", "identity"):
        raise MojoSecIngestError("unsupported Content-Encoding", 415)

    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_strict_object,
            parse_constant=_reject_constant)
        protocol.validate_batch(payload, encoded_size=len(raw))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, protocol.ProtocolError) as err:
        raise MojoSecIngestError("invalid MojoSec batch") from err
    return payload


def sensor_profile(api_key, batch):
    metadata = api_key.metadata if isinstance(api_key.metadata, dict) else {}
    profile = metadata.get("mojosec")
    if not isinstance(profile, dict) or profile.get("enabled") is not True:
        raise MojoSecIngestError("API key is not enrolled as a MojoSec sensor", 403)

    sensor_id = profile.get("sensor_id")
    versions = profile.get("allowed_versions")
    if sensor_id != batch["sensor_id"]:
        raise MojoSecIngestError("sensor identity does not match API key", 403)
    if (not isinstance(versions, list) or not versions or
            any(not isinstance(item, int) or isinstance(item, bool) for item in versions) or
            batch["version"] not in versions):
        raise MojoSecIngestError("protocol version is not allowed for this sensor", 403)
    return {
        "sensor_id": sensor_id,
        "allowed_versions": tuple(versions),
    }


def _payload_digest(event):
    return hashlib.sha256(protocol.canonical_json(event).encode("utf-8")).hexdigest()


def _event_projection(batch, sensor_event):
    from mojo.apps.incident.models import Event

    kind = sensor_event["kind"]
    attributes = sensor_event["attributes"]
    source_ip = attributes.get("source_ip") if kind in SOURCE_IP_KINDS else None
    event = Event(
        category=f"mojosec.{kind}",
        scope="mojosec",
        level=SEVERITY_LEVELS[sensor_event["severity"]],
        source_ip=source_ip,
        title=f"MojoSec detected {kind}"[:256],
        details=(
            f"MojoSec sensor {batch['sensor_id']} reported {sensor_event['count']} "
            f"occurrence(s) of {kind}. Host recommendation: "
            f"{sensor_event['recommendation']} (advisory only)."
        ),
        model_name="mojosec_sensor",
        metadata={
            "mojosec": {
                "sensor_id": batch["sensor_id"],
                "event_id": sensor_event["id"],
                "protocol_version": batch["version"],
                "sensor_policy_revision": batch["policy_revision"],
                "kind": kind,
                "severity": sensor_event["severity"],
                "recommendation": sensor_event["recommendation"],
                "count": sensor_event["count"],
                "first_seen": sensor_event["first_seen"],
                "last_seen": sensor_event["last_seen"],
            },
        },
    )
    event.sync_metadata()
    event.save()
    return event


def _create_receipt(api_key, batch, sensor_event, digest):
    from mojo.apps.incident.models import MojoSecReceipt

    try:
        with transaction.atomic():
            event = _event_projection(batch, sensor_event)
            receipt = MojoSecReceipt.objects.create(
                api_key=api_key,
                event=event,
                sensor_id=batch["sensor_id"],
                wire_event_id=sensor_event["id"],
                payload_digest=digest,
                protocol_version=batch["version"],
                sensor_policy_revision=batch["policy_revision"],
                replay_features={
                    "schema": batch["schema"],
                    "version": batch["version"],
                    "sensor_id": batch["sensor_id"],
                    "policy_revision": batch["policy_revision"],
                    "event": sensor_event,
                },
            )
        return receipt, True
    except IntegrityError:
        receipt = MojoSecReceipt.objects.filter(
            sensor_id=batch["sensor_id"], wire_event_id=sensor_event["id"]).first()
        if receipt is None:
            raise
        return receipt, False


def _publish_receipt(receipt):
    from mojo.apps.incident.models import MojoSecReceipt

    try:
        with transaction.atomic():
            locked = MojoSecReceipt.objects.select_for_update().select_related("event").get(
                pk=receipt.pk)
            if locked.publish_state == MojoSecReceipt.PUBLISH_PUBLISHED:
                return "duplicate", ""
            if locked.event_id is None:
                return "retry", "event projection is unavailable"
            locked.event.publish(
                use_catchall=False,
                allow_default_llm=False,
                exact_category=True,
            )
            locked.publish_state = MojoSecReceipt.PUBLISH_PUBLISHED
            locked.published_at = dates.utcnow()
            locked.publish_attempts += 1
            locked.last_error = ""
            locked.save(update_fields=[
                "publish_state", "published_at", "publish_attempts", "last_error", "modified"])
    except Exception as err:
        logger.exception("MojoSec event publication failed for receipt %s", receipt.pk)
        MojoSecReceipt.objects.filter(pk=receipt.pk).update(
            publish_attempts=F("publish_attempts") + 1,
            last_error=str(err)[:MAX_ERROR],
            modified=dates.utcnow(),
        )
        return "retry", "central publication failed"
    return "accepted", ""


def ingest_batch(api_key, batch):
    results = []
    for sensor_event in batch["events"]:
        digest = _payload_digest(sensor_event)
        try:
            receipt, created = _create_receipt(api_key, batch, sensor_event, digest)
            if receipt.payload_digest != digest:
                results.append({
                    "id": sensor_event["id"], "status": "rejected",
                    "reason": "event id was already used for different evidence",
                })
                continue
            if not created and receipt.publish_state == receipt.PUBLISH_PUBLISHED:
                results.append({"id": sensor_event["id"], "status": "duplicate"})
                continue
            status, reason = _publish_receipt(receipt)
            result = {"id": sensor_event["id"], "status": status}
            if reason:
                result["reason"] = reason
            results.append(result)
        except Exception:
            logger.exception("MojoSec receipt processing failed for %s", sensor_event["id"])
            results.append({
                "id": sensor_event["id"], "status": "retry",
                "reason": "receiver failed to persist event",
            })
    return {
        "schema": protocol.ACK_SCHEMA,
        "version": protocol.PROTOCOL_VERSION,
        "results": results,
    }


def prune_receipts(job=None, now=None):
    from mojo.apps.incident.models import MojoSecReceipt

    retention_days = settings.get_static(
        "MOJOSEC_RECEIPT_RETENTION_DAYS", 45, kind="int")
    if retention_days < 7:
        retention_days = 7
    cutoff = (now or dates.utcnow()) - dates.timedelta(days=retention_days)
    deleted, _ = MojoSecReceipt.objects.filter(
        publish_state=MojoSecReceipt.PUBLISH_PUBLISHED,
        published_at__lt=cutoff,
    ).delete()
    if job is not None and hasattr(job, "add_log"):
        job.add_log(f"Pruned {deleted} expired MojoSec receipt rows")
    return deleted
