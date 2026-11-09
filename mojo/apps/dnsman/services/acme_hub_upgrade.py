"""Shared-validation admission and enablement on the existing deployment plane."""
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from django.db import connections, transaction
from objict import objict
from packaging.version import Version, InvalidVersion

from mojo import errors as me
from mojo.helpers import advisory_locks, dates

_NAMESPACE = advisory_locks.ACME_HUB_ADMISSION
_SESSION = ContextVar("acme_hub_admission", default=None)
# Published releases through 1.39.1 used the previous activation contract
# (1.39.1 is the 1.39.0 migration-lock hotfix, cut from the 1.39.0 tag).
LEGACY_RELEASE_CEILING = Version("1.39.1")


def refuse(reason="state_unavailable", status=503):
    raise me.ValueException(reason, code=status, status=status)


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        refuse("invalid_request", 400)


def _release(value):
    try:
        parsed = Version(value)
        if parsed.is_prerelease or parsed.is_devrelease or parsed.local is not None:
            raise ValueError()
        return parsed
    except (InvalidVersion, TypeError, ValueError):
        refuse("framework_upgrade_required")


def read_state():
    from mojo.apps.dnsman.models import AcmeHubInstallation
    try:
        rows = list(AcmeHubInstallation.objects.using("default").all()[:2])
        if len(rows) != 1 or rows[0].pk != 1 or rows[0].mode not in ("compatibility", "enabled"):
            refuse()
        row = rows[0]
        if row.mode == "enabled" and (not row.activation_uuid or not row.deployment_uuid
                or not row.activated_at or len(row.app_sha) != 40
                or not row.minimum_release):
            refuse()
        return row
    except me.MojoException:
        raise
    except Exception:
        refuse()


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
                return fn(*args, **kwargs)
        return wrapped
    return decorate(function) if function is not None else decorate


def enabled():
    session = _SESSION.get()
    return (session.state if session else read_state()).mode == "enabled"


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


def _configured():
    from mojo.helpers.settings import settings
    from mojo.apps.dnsman.services import acme_hub
    if not settings.get_static("DNSMAN_ACME_HUB_ZONE", None):
        return False, "hub_unconfigured"
    try:
        acme_hub._new_zone()
    except Exception:
        return False, "hub_unavailable"
    return True, None


def _candidate(*, lock=False):
    import mojo
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import platform_deploy, deploy

    rows = PlatformDeployment.objects.order_by("-created", "-pk")
    row = (rows.select_for_update() if lock else rows).first()
    if PlatformDeployment.objects.filter(status__in=PlatformDeployment.ACTIVE_STATUSES).exists():
        return row, "deployment_in_progress"
    if row is None:
        return None, "deployment_required"
    if row.status != PlatformDeployment.STATUS_CONVERGED:
        return row, "deployment_not_converged"
    try:
        current = _release(mojo.__version__)
        if current <= LEGACY_RELEASE_CEILING or _release(row.framework_version) != current:
            return row, "framework_upgrade_required"
    except me.MojoException:
        return row, "framework_upgrade_required"
    target = deploy.get_target() or {}
    if target and (target.get("deployment") != str(row.pk) or target.get("sha") != row.sha):
        return row, "deployment_not_converged"
    roster = row.frozen_roster
    evidence = row.node_evidence
    if (not isinstance(roster, list) or not roster or len(roster) > platform_deploy.MAX_ROSTER
            or any(not isinstance(node, str) or not node for node in roster)
            or len(set(roster)) != len(roster) or not isinstance(evidence, list)
            or len(evidence) != len(roster)):
        return row, "deployment_proof_unavailable"
    seen = set()
    for entry in evidence:
        if not isinstance(entry, dict):
            return row, "deployment_proof_unavailable"
        runner, proof = entry.get("runner"), entry.get("proof")
        try:
            fresh = dates.parse_datetime(entry["stale_after"]) > dates.utcnow()
        except (KeyError, TypeError, ValueError, AttributeError):
            fresh = False
        if (not fresh or runner not in roster or runner in seen or entry.get("state") != "proven"
                or not platform_deploy.proof_matches(row, proof)
                or proof.get("platform_sha") != row.sha
                or proof.get("django_mojo_version") != row.framework_version):
            return row, "deployment_proof_unavailable"
        seen.add(runner)
    return row, None


def status(actor):
    from mojo.apps.account.services import provider_setup
    provider_setup._superuser(actor)
    state = read_state()
    configured, reason = _configured()
    candidate, deployment_reason = _candidate()
    enabled = state.mode == "enabled"
    reason = None if enabled else reason or deployment_reason
    return dict(version=1, enabled=enabled, configured=configured,
                deployment_uuid=str(candidate.pk) if candidate else None,
                can_enable=not enabled and reason is None, reason=reason)


def enable(actor, deployment_uuid):
    from mojo.apps.account.services import provider_setup
    from mojo.apps.dnsman.models import AcmeHubInstallation
    deployment_uuid = _uuid(deployment_uuid)
    provider_setup._superuser(actor)
    with admission(exclusive=True) as state:
        with transaction.atomic(using="default"):
            actor = provider_setup._superuser(actor, lock=True)
            AcmeHubInstallation.objects.using("default").select_for_update().get(pk=1)
            # Ordinary proof refreshes also lock this deployment row. Read and
            # validate only after obtaining that same lock, through enablement.
            candidate, reason = _candidate(lock=True)
            if candidate is None or str(candidate.pk) != deployment_uuid:
                refuse("deployment_changed", 409)
            if state.mode == "enabled":
                return status(actor)
            if reason:
                refuse(reason)
            configured, reason = _configured()
            if not configured:
                refuse(reason)
            _bind_owners()
            check_session()
            AcmeHubInstallation.objects.using("default").filter(pk=1, mode="compatibility").update(
                mode="enabled", activation_uuid=uuid.uuid4(), deployment_uuid=candidate.pk,
                app_sha=candidate.sha, minimum_release=candidate.framework_version,
                activated_at=dates.utcnow())
        actor.log(f"Shared DNS validation enabled for deployment {candidate.pk}", "dnsman_acme:enabled")
    return status(actor)
