"""One-way shared-validation admission and trusted local operator cutover."""
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from django.db import connections, transaction
from objict import objict

from mojo import errors as me
from mojo.helpers import advisory_locks, dates
from mojo.apps.dnsman import acme_hub_feature as feature

_NAMESPACE = advisory_locks.ACME_HUB_ADMISSION
_SESSION = ContextVar("acme_hub_admission", default=None)
_TOP = {"installation_uuid", "activation_uuid", "nonce", "deployment_uuid", "app_sha",
        "artifact", "roster_digest", "collector_identity", "classification", "observed_at",
        "expires_at", "nodes", "unmanaged_writer_statement", "launch_exclusion_statement",
        "provider_outcome_statement"}
_NODE = {"node_id", "instance_id", "boot_id", "helper_pid", "helper_start", "helper_session",
         "nonce", "activation_uuid", "floor_generation", "artifact", "old_processes_absent",
         "request_held", "engine_held", "scheduler_held", "cron_held", "supervisor_held",
         "launch_held", "no_root_launchers", "transaction_idle", "helper_confirmed_at",
         "installed_identity", "updater_identity", "lifecycle_identity", "processes"}
_HOLDS = {"old_processes_absent", "request_held", "engine_held", "scheduler_held", "cron_held",
          "supervisor_held", "launch_held", "no_root_launchers", "transaction_idle"}


def refuse(reason="state_unavailable"):
    raise me.ValueException(reason, code=503, status=503)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _uuid(value):
    try:
        result = str(uuid.UUID(str(value)))
        if result != value:
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError):
        refuse("evidence_unavailable")


def read_state():
    from mojo.apps.dnsman.models import AcmeHubInstallation
    try:
        rows = list(AcmeHubInstallation.objects.using("default").all()[:2])
        if len(rows) != 1 or rows[0].pk != 1 or rows[0].feature_epoch != feature.EPOCH:
            refuse()
        row = rows[0]
        if row.mode not in ("compatibility", "enabled"):
            refuse()
        if row.mode == "enabled":
            _uuid(str(row.activation_uuid))
            _uuid(str(row.deployment_uuid))
            feature.validate_artifact(artifact_for(row))
            if (not feature.HEX40.fullmatch(row.app_sha) or row.activated_at is None
                    or not feature.HEX64.fullmatch(row.roster_sha256)
                    or not feature.HEX64.fullmatch(row.attestation_sha256)):
                refuse()
        elif any((row.activation_uuid, row.deployment_uuid, row.activated_at, row.app_sha,
                  row.source_sha, row.minimum_release, row.wheel_sha256, row.manifest_sha256,
                  row.roster_sha256, row.attestation_sha256)):
            refuse()
        return row
    except me.MojoException:
        raise
    except Exception:
        refuse()


def artifact_for(row):
    return dict(epoch=row.feature_epoch, source_sha=row.source_sha,
                manifest_sha256=row.manifest_sha256, release=row.minimum_release,
                wheel_sha256=row.wheel_sha256)


@contextmanager
def admission(*, exclusive=False, state_reader=None):
    current = _SESSION.get()
    if current is not None:
        if exclusive and not current.exclusive:
            refuse("admission_conflict")
        yield current.state
        return
    db = connections["default"]
    if db.vendor != "postgresql":
        refuse("admission_unavailable")
    lock = "pg_advisory_lock" if exclusive else "pg_advisory_lock_shared"
    unlock = "pg_advisory_unlock" if exclusive else "pg_advisory_unlock_shared"
    with db.cursor() as cursor:
        cursor.execute(f"SELECT {lock}(%s, %s)", [_NAMESPACE, 1])
        cursor.execute("SELECT pg_backend_pid()")
        pid = cursor.fetchone()[0]
    token = None
    try:
        state = (state_reader or read_state)()
        token = _SESSION.set(objict(state=state, exclusive=exclusive, backend_pid=pid))
        yield state
    finally:
        if token is not None:
            _SESSION.reset(token)
        # Never silently reconnect and claim that it releases the lost lock.
        if db.connection is not None and not db.connection.closed:
            with db.cursor() as cursor:
                cursor.execute(f"SELECT {unlock}(%s, %s)", [_NAMESPACE, 1])


def check_session():
    session = _SESSION.get()
    if session is None:
        refuse("admission_unavailable")
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            if cursor.fetchone()[0] != session.backend_pid:
                refuse("admission_unavailable")
    except Exception:
        refuse("admission_unavailable")


def admitted(function=None, *, shared=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            reader = kwargs.pop("state_reader", None)
            with admission(state_reader=reader) as state:
                if shared and state.mode != "enabled":
                    refuse("acme_hub_unavailable")
                if not shared and state.mode == "compatibility":
                    from mojo.apps.dnsman.models import AcmeHubParticipant
                    if AcmeHubParticipant.objects.using("default").exists():
                        refuse("legacy_state_requires_qualification")
                return fn(*args, **kwargs)
        return wrapped
    return decorate(function) if function is not None else decorate


def enabled():
    session = _SESSION.get()
    return (session.state if session else read_state()).mode == "enabled"


def status():
    row = read_state()
    return dict(installation_uuid=str(row.installation_uuid), mode=row.mode,
                feature_epoch=row.feature_epoch,
                activation_uuid=str(row.activation_uuid) if row.activation_uuid else None,
                evidence="operator_attested" if row.mode == "enabled" else "evidence_unavailable")


def plan(artifact, roster, deployment_uuid, app_sha, *, activation_uuid=None, previous_floor=None):
    row = read_state()
    feature.validate_artifact(artifact)
    if (not isinstance(roster, list) or not 1 <= len(roster) <= 32
            or roster != sorted(set(roster)) or any(not isinstance(n, str) or not 1 <= len(n) <= 128 for n in roster)
            or not isinstance(app_sha, str) or not feature.HEX40.fullmatch(app_sha)):
        refuse("evidence_unavailable")
    if previous_floor is not None:
        expected = (str(row.installation_uuid), artifact, digest(roster))
        actual = (previous_floor.get("installation_uuid"), previous_floor.get("artifact"), previous_floor.get("roster_digest"))
        if actual != expected or (activation_uuid and activation_uuid != previous_floor.get("activation_uuid")):
            refuse("activation_conflict")
        activation_uuid = previous_floor["activation_uuid"]
    if row.mode == "enabled":
        activation_uuid = str(row.activation_uuid)
    return dict(installation_uuid=str(row.installation_uuid), activation_uuid=_uuid(activation_uuid) if activation_uuid else str(uuid.uuid4()),
                nonce=uuid.uuid4().hex, deployment_uuid=_uuid(deployment_uuid), app_sha=app_sha,
                artifact=dict(artifact), roster_digest=digest(roster), roster=list(roster),
                started_at=time.time())


def validate_attestation(value, operation, *, now=None):
    now = time.time() if now is None else now
    try:
        if (not isinstance(value, dict) or set(value) != _TOP or len(canonical(value)) > 65536
                or value["classification"] != "operator_attested"
                or not isinstance(value["collector_identity"], str)
                or not 1 <= len(value["collector_identity"]) <= 128):
            refuse("evidence_unavailable")
        for key in ("installation_uuid", "activation_uuid", "nonce", "deployment_uuid", "app_sha", "artifact", "roster_digest"):
            if value[key] != operation[key]:
                refuse("activation_conflict")
        feature.validate_artifact(value["artifact"])
        if (type(value["observed_at"]) not in (int, float) or type(value["expires_at"]) not in (int, float)
                or not operation["started_at"] <= value["observed_at"] <= now
                or not now < value["expires_at"] <= value["observed_at"] + 300):
            refuse("evidence_unavailable")
        if any(value[key] is not True for key in ("unmanaged_writer_statement", "launch_exclusion_statement", "provider_outcome_statement")):
            refuse("evidence_unavailable")
        nodes = value["nodes"]
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= 32:
            refuse("evidence_unavailable")
        if [n.get("node_id") for n in nodes] != operation["roster"]:
            refuse("evidence_unavailable")
        for node in nodes:
            if (set(node) != _NODE or any(node[k] is not True for k in _HOLDS)
                    or node["artifact"] != operation["artifact"]
                    or node["nonce"] != operation["nonce"]
                    or node["activation_uuid"] != operation["activation_uuid"]
                    or type(node["floor_generation"]) is not int or node["floor_generation"] != 1
                    or type(node["helper_pid"]) is not int or node["helper_pid"] <= 1
                    or type(node["helper_start"]) is not int or node["helper_start"] <= 0
                    or not operation["started_at"] <= node["helper_confirmed_at"] <= now
                    or now - node["helper_confirmed_at"] > 30):
                refuse("evidence_unavailable")
            if (node["installed_identity"] != operation["artifact"]
                    or any(not isinstance(node[k], str) or not feature.HEX64.fullmatch(node[k])
                           for k in ("updater_identity", "lifecycle_identity"))
                    or not isinstance(node["processes"], list) or len(node["processes"]) > 64):
                refuse("evidence_unavailable")
            seen = set()
            for process in node["processes"]:
                if (not isinstance(process, dict) or set(process) != {
                        "role", "pid", "start_ticks", "loaded_epoch", "loaded_source_sha", "loaded_manifest_sha256", "stopped"}
                        or process["role"] not in ("http", "worker", "scheduler", "cron", "deployer")
                        or type(process["pid"]) is not int or process["pid"] <= 1
                        or type(process["start_ticks"]) is not int or process["start_ticks"] <= 0
                        or process["stopped"] is not True
                        or type(process["loaded_epoch"]) is not int or process["loaded_epoch"] != feature.EPOCH
                        or process["loaded_source_sha"] != operation["artifact"]["source_sha"]
                        or process["loaded_manifest_sha256"] != operation["artifact"]["manifest_sha256"]
                        or (process["pid"], process["start_ticks"]) in seen):
                    refuse("evidence_unavailable")
                seen.add((process["pid"], process["start_ticks"]))
            for key in ("instance_id", "boot_id", "helper_session"):
                if not isinstance(node[key], str) or not 1 <= len(node[key]) <= 128:
                    refuse("evidence_unavailable")
            _uuid(node["boot_id"])
            if len(node["helper_session"]) != 32 or uuid.UUID(node["helper_session"]).hex != node["helper_session"]:
                refuse("evidence_unavailable")
        return digest(value)
    except (KeyError, TypeError, ValueError, OverflowError):
        refuse("evidence_unavailable")


def _bind_owners():
    from mojo.apps.dnsman.models import AcmeHubDelegation, AcmeHubParticipant, AcmeHubChallengeLease
    from mojo.apps.dnsman.services.acme_hub import _allocation_lock
    for allocation in AcmeHubDelegation.objects.using("default").select_related("group").order_by("pk").iterator(chunk_size=200):
        with _allocation_lock(allocation.pk):
            if allocation.group_id and str(allocation.group.uuid) != allocation.owner_project_uuid:
                refuse("owner_conflict")
            owner, unused = AcmeHubParticipant.objects.using("default").get_or_create(
                consumer_project_uuid=allocation.owner_project_uuid, client_ref=allocation.client_ref,
                defaults=dict(allocation=allocation, group_id=allocation.group_id, role="owner"))
            if (owner.allocation_id != allocation.pk or owner.group_id != allocation.group_id
                    or owner.role != "owner" or owner.revoked_at is not None):
                refuse("owner_conflict")
            for participant in AcmeHubParticipant.objects.using("default").filter(
                    allocation=allocation).select_related("group").iterator(chunk_size=200):
                if (participant.role not in ("owner", "trusted")
                        or (participant.role == "owner" and participant.pk != owner.pk)
                        or (participant.group_id and str(participant.group.uuid) != participant.consumer_project_uuid)):
                    refuse("owner_conflict")
            leases = AcmeHubChallengeLease.objects.using("default").filter(allocation=allocation)
            if leases.exclude(participant_id=None).exclude(participant__allocation=allocation).exists():
                refuse("owner_conflict")
            leases.filter(participant_id=None).update(participant_id=owner.pk)
    if AcmeHubChallengeLease.objects.using("default").filter(participant_id=None).exists():
        refuse("owner_conflict")


def activate(operation, attestation=None, confirmation=None, *, identity_reader=None, operator_uid=None):
    # Only the actual root operator is the production assertion authority.
    # Internal uid/identity seams are for local tests, never CLI options.
    if (os.geteuid() if operator_uid is None else operator_uid) != 0:
        refuse("evidence_unavailable")
    if attestation is None:
        refuse("evidence_unavailable")
    with admission(exclusive=True) as state:
        if operation.get("installation_uuid") != str(state.installation_uuid):
            refuse("activation_conflict")
        if state.mode == "enabled":
            if (str(state.activation_uuid) != operation["activation_uuid"] or artifact_for(state) != operation["artifact"]
                    or state.roster_sha256 != operation["roster_digest"]
                    or str(state.deployment_uuid) != operation["deployment_uuid"] or state.app_sha != operation["app_sha"]):
                refuse("activation_conflict")
            return status()
        # The attestation must be observed under THIS exclusive session.
        operation = dict(operation, started_at=time.time())
        value = attestation(operation) if callable(attestation) else attestation
        receipt = validate_attestation(value, operation)
        if receipt != confirmation and not callable(confirmation):
            refuse("evidence_unavailable")
        if callable(confirmation) and confirmation(receipt) != receipt:
            refuse("evidence_unavailable")
        loaded = (identity_reader or feature.loaded_identity)()
        expected = {k: v for k, v in operation["artifact"].items() if k != "wheel_sha256"}
        if loaded != expected:
            refuse("artifact_unavailable")
        from mojo.apps.edge.models import PlatformDeployment
        from mojo.apps.dnsman.models import AcmeHubInstallation
        record = PlatformDeployment.objects.using("default").filter(pk=operation["deployment_uuid"]).first()
        if (record is None or record.status != "converged" or record.sha != operation["app_sha"]
                or record.framework_version != operation["artifact"]["release"]
                or sorted(set(record.frozen_roster or [])) != operation["roster"]
                or PlatformDeployment.objects.using("default").filter(status__in=PlatformDeployment.ACTIVE_STATUSES).exists()):
            refuse("evidence_unavailable")
        check_session()
        validate_attestation(value, operation)
        with transaction.atomic(using="default"):
            AcmeHubInstallation.objects.using("default").select_for_update().get(pk=1)
            _bind_owners()
            artifact = operation["artifact"]
            changed = AcmeHubInstallation.objects.using("default").filter(pk=1, mode="compatibility").update(
                mode="enabled", activation_uuid=operation["activation_uuid"], deployment_uuid=record.pk,
                app_sha=record.sha, minimum_release=artifact["release"], source_sha=artifact["source_sha"],
                wheel_sha256=artifact["wheel_sha256"], manifest_sha256=artifact["manifest_sha256"],
                roster_sha256=operation["roster_digest"], attestation_sha256=receipt, activated_at=dates.utcnow())
            if changed != 1:
                refuse("activation_conflict")
        return status()
