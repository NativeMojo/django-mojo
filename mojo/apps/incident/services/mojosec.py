"""Bounded, idempotent ingestion for the MojoSec host sensor."""

import hashlib
import json
import re
import zlib
import datetime

from django.db import DataError, IntegrityError, transaction
from django.db.models import F, Q

from mojo.helpers import dates, logit
from mojo.helpers.settings import settings
from mojo.mojosec import protocol
from mojo.mojosec.disposition import is_local_only
from . import mojosec_evidence
from . import mojosec_correlation


logger = logit.get_logger(__name__, "incident.log")

MAX_COMPRESSED_BYTES = protocol.MAX_BATCH_BYTES
MAX_ERROR = 256
LOCAL_ONLY_REPLAY_SCHEMA = "local_only_receipt_v1"
# Case-routed receipts keep the full sensor event for replay, learning
# exemplars and terminal conversion — deliberately NOT the local-only schema,
# which mojosec_learning refuses as a feedback subject.
CASE_ROUTED_REPLAY_SCHEMA = "case_routed_receipt_v1"
KIND_POLICY = {
    "auth.ssh_login": {"level": 8},
    "auth.ssh_failure": {"level": 5},
    "auth.sudo_command": {"level": 8},
    "auth.sudo_failure": {"level": 8},
    "auth.session_open": {"level": 2},
    "system.oom": {"level": 12},
    "system.service_error": {"level": 8},
    "web.probe": {"level": 8},
    "web.error": {"level": 5},
    "web.denied": {"level": 5},
    "fim.change": {"level": 8},
    "fim.overflow": {"level": 12},
    "fim.expected_change_error": {"level": 8},
}
UNKNOWN_KIND_POLICY = {"category": "mojosec.unrecognized", "level": 2}
_DEPLOYMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


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

    content_length = request.META.get("CONTENT_LENGTH")
    declared_length = None
    if content_length not in (None, ""):
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError) as err:
            raise MojoSecIngestError("invalid Content-Length") from err
        if declared_length < 0:
            raise MojoSecIngestError("invalid Content-Length")
        if declared_length > MAX_COMPRESSED_BYTES:
            raise MojoSecIngestError("compressed MojoSec batch is too large", 413)
    try:
        raw = request.read(MAX_COMPRESSED_BYTES + 1) or b""
    except Exception as err:
        raise MojoSecIngestError("unable to read MojoSec batch") from err
    if not raw:
        raise MojoSecIngestError("MojoSec batch body is empty")
    if len(raw) > MAX_COMPRESSED_BYTES:
        raise MojoSecIngestError("compressed MojoSec batch is too large", 413)
    if declared_length is not None and len(raw) != declared_length:
        raise MojoSecIngestError("Content-Length does not match MojoSec batch body")

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
    protected = metadata.get("protected")
    profile = protected.get("mojosec") if isinstance(protected, dict) else None
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


def _expected_change_projection(kind, attributes, last_seen=None):
    """Project only the exact safe annotation; never raw FIM attributes.

    Applies the same expiry/TTL trust bounds as the correlator's
    `_safe_expected`, so Event metadata never carries an annotation the
    case routing would reject.
    """
    if kind != "fim.change" or not isinstance(attributes, dict):
        return None
    value = attributes.get("expected_change")
    v1_fields = {"deployment_id", "expires_at"}
    v2_fields = v1_fields | {"operation_id", "operation_kind", "completed_at"}
    if not isinstance(value, dict) or set(value) not in (v1_fields, v2_fields):
        return None
    deployment_id = value.get("deployment_id")
    expires_at = value.get("expires_at")
    if (not isinstance(deployment_id, str) or not _DEPLOYMENT_ID.fullmatch(deployment_id) or
            not isinstance(expires_at, str) or len(expires_at) > 40):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    observed = mojosec_correlation._observed(last_seen)
    ttl_bound = datetime.timedelta(
        seconds=mojosec_correlation.MAX_EXPECTED_TTL_SECONDS)
    if observed is not None and parsed < observed:
        return None
    result = {"deployment_id": deployment_id, "expires_at": expires_at}
    if set(value) == v2_fields:
        for field in ("operation_id", "operation_kind"):
            if (not isinstance(value[field], str) or
                    not _DEPLOYMENT_ID.fullmatch(value[field])):
                return None
        completed_at = value["completed_at"]
        if not isinstance(completed_at, str) or len(completed_at) > 40:
            return None
        try:
            completed = datetime.datetime.fromisoformat(
                completed_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if completed.tzinfo is None or completed > parsed:
            return None
        if parsed > completed + ttl_bound:
            return None
        result.update({
            "operation_id": value["operation_id"],
            "operation_kind": value["operation_kind"],
            "completed_at": completed_at,
        })
    elif observed is not None and parsed > observed + ttl_bound:
        return None
    return result


def _event_projection(batch, sensor_event):
    from mojo.apps.incident.models import Event

    kind = sensor_event["kind"]
    attributes = sensor_event["attributes"]
    policy = KIND_POLICY.get(kind, UNKNOWN_KIND_POLICY)
    category = policy.get("category", f"mojosec.{kind}")
    projection = mojosec_evidence.project(
        kind, attributes, sensor_event["count"], sensor_event["last_seen"])
    source_ip = projection["source_ip"]
    expected_change = _expected_change_projection(
        kind, attributes, sensor_event["last_seen"])
    projected_metadata = {
        "sensor_id": batch["sensor_id"],
        "installation_key_id": batch["installation_key_id"],
        "event_id": sensor_event["id"],
        "protocol_version": batch["version"],
        "sensor_policy_revision_sha256": hashlib.sha256(
            batch["policy_revision"].encode("utf-8")).hexdigest(),
        "kind": kind,
        "sensor_severity": sensor_event["severity"],
        "effective_level": policy["level"],
        "recommendation": sensor_event["recommendation"],
        "count": sensor_event["count"],
        "first_seen": sensor_event["first_seen"],
        "last_seen": sensor_event["last_seen"],
    }
    if expected_change is not None:
        projected_metadata["expected_change"] = expected_change
    if projection["evidence"]:
        projected_metadata["evidence"] = projection["evidence"]
    title = (
        "MojoSec detected local PAM service session"
        if kind == "auth.session_open" else f"MojoSec detected {kind}"
    )
    event = Event(
        category=category,
        scope="mojosec",
        level=policy["level"],
        source_ip=source_ip,
        title=title[:256],
        details=(
            f"MojoSec sensor {batch['sensor_id']} reported {sensor_event['count']} "
            f"occurrence(s) of {kind}. Host recommendation: "
            f"{sensor_event['recommendation']} (advisory only)."
        ),
        model_name="mojosec_sensor",
        metadata={
            "mojosec": projected_metadata,
        },
    )
    # Do not call Event.sync_metadata() here: it performs GeoIP enrichment for
    # source-bearing events, which would turn one bounded batch into hundreds
    # of synchronous external lookups. The receiver needs only this fixed,
    # server-derived projection; background/incident policy may enrich later.
    event.metadata.update({
        "level": event.level,
        "scope": event.scope,
        "category": event.category,
        "source_ip": event.source_ip,
        "title": event.title,
        "details": event.details,
        "model_name": event.model_name,
        "model_id": event.model_id,
    })
    event.save()
    return event


def _create_receipt(api_key, batch, sensor_event, digest):
    from mojo.apps.incident.models import MojoSecReceipt

    try:
        with transaction.atomic():
            effective_kind = sensor_event["kind"]
            effective_count = sensor_event["count"]
            effective_level = KIND_POLICY.get(
                effective_kind, UNKNOWN_KIND_POLICY)["level"]
            if effective_level >= 12:
                effective_severity = "critical"
            elif effective_level >= 8:
                effective_severity = "high"
            elif effective_level >= 5:
                effective_severity = "warning"
            else:
                effective_severity = "info"
            projection_batch = dict(batch)
            projection_batch["installation_key_id"] = api_key.pk
            event = _event_projection(projection_batch, sensor_event)
            receipt = MojoSecReceipt.objects.create(
                api_key=api_key,
                event=event,
                sensor_id=batch["sensor_id"],
                wire_event_id=sensor_event["id"],
                payload_digest=digest,
                protocol_version=batch["version"],
                sensor_policy_revision=batch["policy_revision"],
                replay_features={
                    "feature_schema": "replay_features_v1",
                    "schema": batch["schema"],
                    "version": batch["version"],
                    "sensor_id": batch["sensor_id"],
                    "policy_revision": batch["policy_revision"],
                    "effective": {
                        "kind": effective_kind,
                        "count": effective_count,
                        "level": effective_level,
                        "severity": effective_severity,
                    },
                    "event": sensor_event,
                },
            )
        return receipt, True
    except IntegrityError:
        receipt = MojoSecReceipt.objects.filter(
            api_key=api_key, wire_event_id=sensor_event["id"]).first()
        if receipt is None:
            raise
        return receipt, False


def _local_only_replay(batch, sensor_event):
    return {
        "feature_schema": LOCAL_ONLY_REPLAY_SCHEMA,
        "schema": batch["schema"],
        "version": batch["version"],
        "sensor_id": batch["sensor_id"],
        "policy_revision": batch["policy_revision"],
        "event_id": sensor_event["id"],
        "kind": sensor_event["kind"],
        "disposition": "local_only",
        "event": sensor_event,
    }


def _accept_local_only(api_key, batch, sensor_event, digest):
    """Create or terminalize an eventless receipt under the publication lock."""
    from mojo.apps.incident.models import MojoSecReceipt

    replay = _local_only_replay(batch, sensor_event)
    try:
        with transaction.atomic():
            receipt = MojoSecReceipt.objects.create(
                api_key=api_key,
                event=None,
                sensor_id=batch["sensor_id"],
                wire_event_id=sensor_event["id"],
                payload_digest=digest,
                protocol_version=batch["version"],
                sensor_policy_revision=batch["policy_revision"],
                publish_state=MojoSecReceipt.PUBLISH_PUBLISHED,
                published_at=dates.utcnow(),
                handler_state=MojoSecReceipt.HANDLER_NONE,
                replay_features=replay,
            )
        return receipt, True
    except IntegrityError:
        pass
    with transaction.atomic():
        receipt = MojoSecReceipt.objects.select_for_update().get(
            api_key=api_key, wire_event_id=sensor_event["id"])
        if receipt.payload_digest != digest:
            return receipt, False
        if receipt.publish_state == MojoSecReceipt.PUBLISH_PUBLISHED:
            return receipt, False
        if not isinstance(receipt.replay_features, dict):
            raise ValueError("pending receipt replay evidence is not an object")
        replay = dict(receipt.replay_features)
        replay["feature_schema"] = LOCAL_ONLY_REPLAY_SCHEMA
        replay["disposition"] = "local_only"
        receipt.publish_state = MojoSecReceipt.PUBLISH_PUBLISHED
        receipt.published_at = dates.utcnow()
        receipt.handler_state = MojoSecReceipt.HANDLER_NONE
        receipt.handler_job_id = ""
        receipt.handler_last_error = ""
        receipt.last_error = ""
        receipt.replay_features = replay
        receipt.save(update_fields=[
            "publish_state", "published_at", "handler_state", "handler_job_id",
            "handler_last_error", "last_error", "replay_features", "modified",
        ])
        return receipt, True


def _case_route_attempt_cap():
    return settings.get_static("MOJOSEC_CASE_ROUTE_MAX_ATTEMPTS", 100, kind="int")


def _routable_digest_tier(binding, sensor_event):
    """True when this evidence is digest tier and may skip Event projection.

    Bound web observations normalize to info/warning by construction. FIM
    routes only for a valid, unexpired, TTL-bounded trusted expected-change
    annotation; unannotated changes, overflow and annotation errors stay
    immediate per-receipt Events at their existing severity.
    """
    if binding["kind"] == "web":
        return True
    attributes = sensor_event.get("attributes")
    observed = mojosec_correlation._observed(sensor_event.get("last_seen"))
    return (sensor_event.get("kind") == "fim.change" and
            isinstance(attributes, dict) and observed is not None and
            mojosec_correlation._safe_expected(attributes, observed) is not None)


def _case_routed_replay(batch, sensor_event):
    return {
        "feature_schema": CASE_ROUTED_REPLAY_SCHEMA,
        "schema": batch["schema"],
        "version": batch["version"],
        "sensor_id": batch["sensor_id"],
        "policy_revision": batch["policy_revision"],
        "disposition": "case_routed",
        "event": sensor_event,
    }


def _accept_case_routed(api_key, batch, sensor_event, digest):
    """Create the sticky eventless receipt that case contribution will own."""
    from mojo.apps.incident.models import MojoSecReceipt

    try:
        with transaction.atomic():
            receipt = MojoSecReceipt.objects.create(
                api_key=api_key,
                event=None,
                sensor_id=batch["sensor_id"],
                wire_event_id=sensor_event["id"],
                payload_digest=digest,
                protocol_version=batch["version"],
                sensor_policy_revision=batch["policy_revision"],
                publish_state=MojoSecReceipt.PUBLISH_PENDING,
                handler_state=MojoSecReceipt.HANDLER_NONE,
                case_routed=True,
                replay_features=_case_routed_replay(batch, sensor_event),
            )
        return receipt, True
    except IntegrityError:
        receipt = MojoSecReceipt.objects.filter(
            api_key=api_key, wire_event_id=sensor_event["id"]).first()
        if receipt is None:
            raise
        return receipt, False


def _mark_case_routed_published(receipt):
    from mojo.apps.incident.models import MojoSecReceipt

    MojoSecReceipt.objects.filter(pk=receipt.pk).exclude(
        publish_state=MojoSecReceipt.PUBLISH_PUBLISHED).update(
        publish_state=MojoSecReceipt.PUBLISH_PUBLISHED,
        published_at=dates.utcnow(),
        handler_state=MojoSecReceipt.HANDLER_NONE,
        publish_attempts=F("publish_attempts") + 1,
        last_error="",
        modified=dates.utcnow())


def _convert_case_routed(receipt, sensor_event=None):
    """Terminal fallback: surface orphaned evidence as an ordinary Event.

    Used when the enrollment binding is permanently unresolvable (policy
    version bump, vhost disabled, target list gone) or transient failures
    exhausted the attempt budget. Visible noise, never silent loss.
    """
    from mojo.apps.incident.models import MojoSecReceipt

    with transaction.atomic():
        locked = MojoSecReceipt.objects.select_for_update().get(pk=receipt.pk)
        wire_id = locked.wire_event_id
        if not locked.case_routed or locked.publish_state != \
                MojoSecReceipt.PUBLISH_PENDING:
            return {"id": wire_id, "status": "duplicate"}
        replay = locked.replay_features if isinstance(
            locked.replay_features, dict) else {}
        payload = sensor_event or replay.get("event")
        if not isinstance(payload, dict):
            # No retained evidence to convert; terminal and honest.
            locked.publish_state = MojoSecReceipt.PUBLISH_DEAD
            locked.last_error = "case-routed receipt lost its replay evidence"
            locked.publish_attempts += 1
            locked.save(update_fields=[
                "publish_state", "last_error", "publish_attempts", "modified"])
            return {"id": wire_id, "status": "rejected",
                    "reason": "case-routed evidence was lost before conversion"}
        projection_batch = {
            "sensor_id": locked.sensor_id,
            "installation_key_id": locked.api_key_id,
            "version": locked.protocol_version,
            "policy_revision": locked.sensor_policy_revision,
        }
        event = _event_projection(projection_batch, payload)
        locked.event = event
        locked.case_routed = False
        locked.save(update_fields=["event", "case_routed", "modified"])
    mojosec_correlation._record_metric("route_conversions")
    published, _did_publish = _publish_receipt(locked)
    if published is None:
        return {"id": wire_id, "status": "retry",
                "reason": "central publication failed"}
    published.refresh_from_db()
    if not _queue_handler(published):
        return {"id": wire_id, "status": "retry",
                "reason": "handler dispatch was not durably queued"}
    return {"id": wire_id, "status": "accepted"}


def _finish_case_routed(receipt, sensor_event=None):
    """Contribution IS publication for a case-routed receipt.

    Returns the ack result for this delivery. The sensor redelivers on
    `retry`; `retry_case_routed` covers a sensor that never comes back.
    """
    from mojo.apps.incident.models import MojoSecReceipt

    replay = receipt.replay_features if isinstance(
        receipt.replay_features, dict) else {}
    payload = sensor_event or replay.get("event")
    wire_id = receipt.wire_event_id
    if receipt.publish_state == MojoSecReceipt.PUBLISH_PUBLISHED:
        return {"id": wire_id, "status": "duplicate"}
    if not isinstance(payload, dict):
        return _convert_case_routed(receipt, sensor_event)
    try:
        case, contributed = mojosec_correlation.contribute(receipt, payload)
    except Exception as err:
        logger.exception(
            "MojoSec case contribution failed for receipt %s", receipt.pk)
        MojoSecReceipt.objects.filter(pk=receipt.pk).update(
            publish_attempts=F("publish_attempts") + 1,
            last_error=str(err)[:MAX_ERROR],
            modified=dates.utcnow())
        mojosec_correlation._record_metric("ack_retries")
        if receipt.publish_attempts + 1 >= _case_route_attempt_cap():
            return _convert_case_routed(receipt, sensor_event)
        return {"id": wire_id, "status": "retry",
                "reason": "case contribution failed"}
    if case is None and not contributed:
        # Binding or normalization no longer resolves — permanent for this
        # receipt, so convert rather than strand it.
        return _convert_case_routed(receipt, sensor_event)
    _mark_case_routed_published(receipt)
    if contributed:
        mojosec_correlation._record_metric("events_suppressed")
    return {"id": wire_id, "status": "accepted" if contributed else "duplicate"}


def retry_case_routed(now=None, limit=100):
    """Re-drive stranded case-routed receipts from their replay evidence."""
    from mojo.apps.incident.models import MojoSecReceipt

    now = now or dates.utcnow()
    stranded = MojoSecReceipt.objects.filter(
        case_routed=True,
        publish_state=MojoSecReceipt.PUBLISH_PENDING,
        modified__lt=now - dates.timedelta(seconds=60),
    ).order_by("modified")[:limit]
    retried = 0
    for receipt in stranded:
        try:
            _finish_case_routed(receipt)
            retried += 1
        except Exception:
            logger.exception(
                "MojoSec case-routed retry failed for receipt %s", receipt.pk)
    return retried


def _publish_receipt(receipt):
    from mojo.apps.incident.models import MojoSecReceipt

    try:
        with transaction.atomic():
            # Lock only the receipt row. ``event`` is nullable for retained
            # audit receipts, and PostgreSQL refuses FOR UPDATE on that outer
            # joined side when select_related() is used here.
            locked = MojoSecReceipt.objects.select_for_update().get(pk=receipt.pk)
            if locked.publish_state == MojoSecReceipt.PUBLISH_PUBLISHED:
                return locked, False
            if locked.case_routed:
                # Case contribution owns this receipt. The event_id-is-None
                # branch below would dead-letter it and reject the ack, which
                # deletes the sensor's spool copy — never allowed here.
                logger.error(
                    "case-routed MojoSec receipt %s reached _publish_receipt",
                    locked.pk)
                return locked, False
            if locked.event_id is None:
                # The Event projection was pruned by retention; publication can
                # never succeed. Terminal, idempotent, and never a traceback.
                if locked.publish_state != MojoSecReceipt.PUBLISH_DEAD:
                    locked.publish_state = MojoSecReceipt.PUBLISH_DEAD
                    locked.last_error = "event was pruned before publication"
                    locked.publish_attempts += 1
                    locked.save(update_fields=[
                        "publish_state", "last_error", "publish_attempts", "modified"])
                return locked, False
            publication = locked.event.publish(
                use_catchall=False,
                dispatch_handlers=False,
                allow_default_llm=False,
                exact_category=True,
            )
            locked.rule_set = publication["rule_set"]
            locked.incident = publication["incident"]
            locked.handler_state = (
                MojoSecReceipt.HANDLER_PENDING
                if publication["should_dispatch"] else MojoSecReceipt.HANDLER_NONE)
            locked.publish_state = MojoSecReceipt.PUBLISH_PUBLISHED
            locked.published_at = dates.utcnow()
            locked.publish_attempts += 1
            locked.last_error = ""
            locked.save(update_fields=[
                "rule_set", "incident", "handler_state", "publish_state", "published_at",
                "publish_attempts", "last_error", "modified"])
    except Exception as err:
        logger.exception("MojoSec event publication failed for receipt %s", receipt.pk)
        MojoSecReceipt.objects.filter(pk=receipt.pk).update(
            publish_attempts=F("publish_attempts") + 1,
            last_error=str(err)[:MAX_ERROR],
            modified=dates.utcnow(),
        )
        return None, False
    return locked, True


def _handler_attempt_cap():
    return settings.get_static("MOJOSEC_HANDLER_MAX_ATTEMPTS", 100, kind="int")


def _handler_queued_stale_seconds():
    return settings.get_static("MOJOSEC_HANDLER_QUEUED_STALE_SECONDS", 1800, kind="int")


def _handler_job_key(receipt):
    return f"mojosec-handler:{receipt.pk}"


def _handler_prefix(receipt):
    identity = f"{receipt.api_key_id}:{receipt.wire_event_id}".encode("utf-8")
    return f"mojosec:{receipt.pk}:{hashlib.sha256(identity).hexdigest()[:16]}"


def _queue_handler(receipt):
    from mojo.apps import jobs
    from mojo.apps.incident.models import MojoSecReceipt

    if receipt.handler_state in (
            MojoSecReceipt.HANDLER_NONE, MojoSecReceipt.HANDLER_DISPATCHED,
            MojoSecReceipt.HANDLER_DEAD):
        return True
    if receipt.handler_state == MojoSecReceipt.HANDLER_FAILED and receipt.handler_job_id:
        if receipt.handler_attempts >= _handler_attempt_cap():
            # Dead-lettered by attempt budget: the replay cron sweeps the state
            # to dead; re-POSTs must not dispatch or fail the ack.
            return True
        try:
            return _dispatch_receipt_handlers(receipt.pk)
        except Exception:
            return False
    try:
        job_id = jobs.publish(
            "mojo.apps.incident.services.mojosec.dispatch_mojosec_receipt",
            {"receipt_id": receipt.pk}, channel="incident_handlers",
            max_retries=20, expires_in=604800,
            idempotency_key=_handler_job_key(receipt),
        )
    except Exception as err:
        MojoSecReceipt.objects.filter(pk=receipt.pk).update(
            handler_state=MojoSecReceipt.HANDLER_FAILED,
            handler_last_error=str(err)[:MAX_ERROR], modified=dates.utcnow())
        return False
    MojoSecReceipt.objects.filter(pk=receipt.pk).exclude(
        handler_state=MojoSecReceipt.HANDLER_DISPATCHED).update(
            handler_state=MojoSecReceipt.HANDLER_QUEUED,
            handler_job_id=job_id, handler_last_error="", modified=dates.utcnow())
    return True


def _dispatch_receipt_handlers(receipt_id):
    from mojo.apps.incident.models import MojoSecReceipt

    receipt = MojoSecReceipt.objects.select_related(
        "event", "rule_set", "incident").get(pk=receipt_id)
    if receipt.handler_state in (
            MojoSecReceipt.HANDLER_DISPATCHED, MojoSecReceipt.HANDLER_DEAD):
        return True
    if receipt.rule_set_id is None or not receipt.rule_set.handler:
        MojoSecReceipt.objects.filter(pk=receipt.pk).update(
            handler_state=MojoSecReceipt.HANDLER_NONE,
            handler_last_error="", modified=dates.utcnow())
        return True
    try:
        dispatched = receipt.rule_set.run_handler(
            receipt.event, receipt.incident,
            idempotency_prefix=_handler_prefix(receipt), strict=True)
        if not dispatched:
            raise RuntimeError("selected RuleSet did not dispatch a handler")
    except Exception as err:
        MojoSecReceipt.objects.filter(pk=receipt.pk).update(
            handler_state=MojoSecReceipt.HANDLER_FAILED,
            handler_attempts=F("handler_attempts") + 1,
            handler_last_error=str(err)[:MAX_ERROR], modified=dates.utcnow())
        raise
    MojoSecReceipt.objects.filter(pk=receipt.pk).update(
        handler_state=MojoSecReceipt.HANDLER_DISPATCHED,
        handler_attempts=F("handler_attempts") + 1,
        handler_last_error="", handler_dispatched_at=dates.utcnow(), modified=dates.utcnow())
    return True


def dispatch_mojosec_receipt(job):
    return _dispatch_receipt_handlers(job.payload["receipt_id"])


def ingest_batch(api_key, batch):
    if protocol.has_unstorable_text(batch["policy_revision"]):
        # Every event in the batch would fail on this one field; one line,
        # zero database work, and the sensor frees its spool slots.
        logger.warning(
            "MojoSec batch from sensor %s carried an unstorable policy_revision; "
            "rejecting %s event(s)", batch["sensor_id"], len(batch["events"]))
        return {
            "schema": protocol.ACK_SCHEMA,
            "version": protocol.PROTOCOL_VERSION,
            "results": [
                {"id": sensor_event["id"], "status": "rejected",
                 "reason": "policy_revision contains unstorable text"}
                for sensor_event in batch["events"]
            ],
        }
    results = []
    for sensor_event in batch["events"]:
        try:
            if protocol.has_unstorable_text(sensor_event):
                logger.warning(
                    "MojoSec event %s contains unstorable text; rejected",
                    sensor_event["id"])
                results.append({
                    "id": sensor_event["id"], "status": "rejected",
                    "reason": "event contains unstorable text",
                })
                continue
            digest = _payload_digest(sensor_event)
            if is_local_only(sensor_event, wire=True):
                receipt, accepted = _accept_local_only(
                    api_key, batch, sensor_event, digest)
                if receipt.payload_digest != digest:
                    results.append({
                        "id": sensor_event["id"], "status": "rejected",
                        "reason": "event id was already used for different evidence",
                    })
                else:
                    results.append({
                        "id": sensor_event["id"],
                        "status": "accepted" if accepted else "duplicate",
                    })
                continue
            binding = mojosec_correlation.binding_for(api_key.pk, sensor_event)
            route_new = (
                binding is not None and
                binding.get("mode") == "authoritative" and
                _routable_digest_tier(binding, sensor_event))
            if route_new:
                receipt, _created = _accept_case_routed(
                    api_key, batch, sensor_event, digest)
            else:
                receipt, _created = _create_receipt(
                    api_key, batch, sensor_event, digest)
            if receipt.payload_digest != digest:
                results.append({
                    "id": sensor_event["id"], "status": "rejected",
                    "reason": "event id was already used for different evidence",
                })
                continue
            if receipt.case_routed:
                # Sticky: once routed, contribution owns the receipt on every
                # later delivery — regardless of what the binding says now.
                results.append(_finish_case_routed(receipt, sensor_event))
                continue
            was_published = receipt.publish_state == receipt.PUBLISH_PUBLISHED
            receipt, did_publish = _publish_receipt(receipt)
            if receipt is None:
                results.append({
                    "id": sensor_event["id"], "status": "retry",
                    "reason": "central publication failed",
                })
                continue
            if receipt.publish_state == receipt.PUBLISH_DEAD:
                results.append({
                    "id": sensor_event["id"], "status": "rejected",
                    "reason": "event evidence was pruned before publication",
                })
                continue
            receipt.refresh_from_db()
            if not _queue_handler(receipt):
                results.append({
                    "id": sensor_event["id"], "status": "retry",
                    "reason": "handler dispatch was not durably queued",
                })
                continue
            try:
                mojosec_correlation.contribute(receipt, sensor_event)
            except Exception:
                # Shadow state never owns acknowledgements, Event publication,
                # RuleSet handlers, or replay semantics in this slice.
                logger.exception(
                    "MojoSec shadow contribution failed for receipt %s", receipt.pk)
                mojosec_correlation._record_metric("failures")
            status = "duplicate" if was_published or not did_publish else "accepted"
            result = {"id": sensor_event["id"], "status": status}
            results.append(result)
        except (DataError, UnicodeEncodeError) as err:
            # Value-domain storage failures can never succeed on retry.
            logger.warning("MojoSec event %s could not be stored: %s",
                           sensor_event["id"], str(err)[:MAX_ERROR])
            results.append({
                "id": sensor_event["id"], "status": "rejected",
                "reason": "event could not be stored",
            })
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
        handler_state__in=(
            MojoSecReceipt.HANDLER_NONE, MojoSecReceipt.HANDLER_DISPATCHED,
            MojoSecReceipt.HANDLER_DEAD),
        published_at__lt=cutoff,
    ).delete()
    dead_deleted, _ = MojoSecReceipt.objects.filter(
        publish_state=MojoSecReceipt.PUBLISH_DEAD,
        modified__lt=cutoff,
    ).delete()
    if job is not None and hasattr(job, "add_log"):
        job.add_log(f"Pruned {deleted} expired and {dead_deleted} dead "
                    "MojoSec receipt rows")
    return deleted + dead_deleted


def replay_handler_outbox(job=None, limit=100):
    from mojo.apps.incident.models import MojoSecReceipt

    cap = _handler_attempt_cap()
    stale = _handler_queued_stale_seconds()
    now = dates.utcnow()
    dead_handlers = MojoSecReceipt.objects.filter(
        publish_state=MojoSecReceipt.PUBLISH_PUBLISHED,
        handler_state__in=(
            MojoSecReceipt.HANDLER_PENDING, MojoSecReceipt.HANDLER_QUEUED,
            MojoSecReceipt.HANDLER_FAILED),
        handler_attempts__gte=cap,
    ).update(handler_state=MojoSecReceipt.HANDLER_DEAD, modified=now)
    dead_publishes = MojoSecReceipt.objects.filter(
        publish_state=MojoSecReceipt.PUBLISH_PENDING,
        event__isnull=True,
        case_routed=False,
        created__lt=now - dates.timedelta(days=1),
    ).update(
        publish_state=MojoSecReceipt.PUBLISH_DEAD,
        last_error="event was pruned before publication", modified=now)
    receipts = MojoSecReceipt.objects.filter(
        publish_state=MojoSecReceipt.PUBLISH_PUBLISHED,
        handler_attempts__lt=cap,
    ).filter(
        Q(handler_state__in=(
            MojoSecReceipt.HANDLER_PENDING, MojoSecReceipt.HANDLER_FAILED)) |
        Q(handler_state=MojoSecReceipt.HANDLER_QUEUED,
          modified__lt=now - dates.timedelta(seconds=stale)),
    ).order_by("modified")[:limit]
    replayed = 0
    for receipt in receipts:
        try:
            if receipt.handler_state in (
                    MojoSecReceipt.HANDLER_FAILED, MojoSecReceipt.HANDLER_QUEUED):
                _dispatch_receipt_handlers(receipt.pk)
                replayed += 1
            elif _queue_handler(receipt):
                replayed += 1
        except Exception:
            logger.exception("MojoSec handler replay failed for receipt %s", receipt.pk)
    if job is not None and hasattr(job, "add_log"):
        job.add_log(
            f"Replayed {replayed} MojoSec handler outbox rows; dead-lettered "
            f"{dead_handlers} capped handlers and {dead_publishes} unpublishable receipts")
    return replayed
