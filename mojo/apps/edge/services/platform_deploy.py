"""Durable truth and bounded evidence for platform deployment attempts."""

import hashlib
import json
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from mojo.apps.edge.models import PlatformDeployment


MAX_ROSTER = 128
MAX_TRANSITIONS = 64
MAX_EVIDENCE = 128
MAX_TEXT = 320
EVIDENCE_TTL = 600
FLEET_PROOF_GRACE = 120


def _text(value, limit=MAX_TEXT):
    value = str(value or "").replace("\r", " ").replace("\n", " ")
    return value[:limit]


def _safe(value, depth=0):
    if depth > 4:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        # Redact credential-shaped content independent of the surrounding key.
        # Runner/provider values are evidence, not trusted display strings.
        # bytes route through the SAME path rather than falling through to
        # _text's str() — subprocess failures carry bytes even under text=True
        # (POSIX TimeoutExpired), and b"password=..." stringified is a
        # credential that never met the redactor.
        from mojo.apps.account.services.setup_safety import sanitize
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", "replace")
        return _text(sanitize(value, max_bytes=1024))
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in value[:32]]
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:32]:
            name = _text(key, 64)
            lowered = name.lower()
            if any(token in lowered for token in (
                    "secret", "token", "password", "credential", "private_key")):
                result[name] = "[redacted]"
            else:
                result[name] = _safe(item, depth + 1)
        return result
    return _text(value)


def _at():
    return timezone.now().isoformat()


def deployment_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def edge_roster():
    """Freeze only live runners that explicitly consume the edge channel."""
    from mojo.apps import jobs
    rows = jobs.get_runners_bounded(
        channel="edge", limit=MAX_ROSTER, max_scan_pages=16, timeout=1.0) or []
    roster = sorted({
        _text(row.get("runner_id"), 64) for row in rows
        if row.get("alive") and row.get("runner_id")
    })
    if not roster:
        raise RuntimeError("runner_roster_empty")
    if len(roster) > MAX_ROSTER:
        raise RuntimeError("runner_roster_overflow")
    return roster


def request_key(source, sha, supplied=None):
    if supplied:
        supplied = _text(supplied, 128)
        if supplied:
            # Idempotency keys are opaque caller credentials in some clients;
            # equality is enough, so never persist or expose the raw value.
            raw = f"{source}:{supplied}"
            return hashlib.sha256(raw.encode()).hexdigest()
    raw = f"{source}:{sha}:{uuid.uuid4()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def create(sha, actor="", source="external", created_by=None,
           idempotency_key=None, source_delivery="", retry_of=None, links=None):
    key = request_key(source, sha, idempotency_key)
    now = _at()
    try:
        roster = edge_roster()
        roster_available = True
    except Exception:
        # Persist the attempt before failing closed. Provider exception text is
        # deliberately discarded; a missing roster must never silently become
        # a single-runner deployment.
        roster = []
        roster_available = False
    initial_status = (PlatformDeployment.STATUS_REQUESTED if roster_available
                      else PlatformDeployment.STATUS_FAILED)
    initial_detail = {} if roster_available else {"reason": "roster_unavailable"}
    try:
        with transaction.atomic():
            row = PlatformDeployment.objects.create(
                sha=sha, actor=_safe(actor), source=_text(source, 32),
                created_by=created_by, request_key=key,
                source_delivery=_text(source_delivery, 128), retry_of=retry_of,
                links=_safe(links or {}),
                frozen_roster=roster, started=timezone.now(),
                finished=None if roster_available else timezone.now(),
                status=initial_status, detail=initial_detail,
                transitions=[{"status": initial_status,
                              "at": now, "detail": initial_detail}])
            return row, False
    except IntegrityError:
        existing = PlatformDeployment.objects.filter(request_key=key).first()
        if existing is None and source == "github" and source_delivery:
            existing = PlatformDeployment.objects.filter(
                source="github", source_delivery=source_delivery)
            existing = existing.first()
        if existing is None:
            raise
        return existing, True


def get(value, for_update=False):
    value = deployment_id(value)
    if not value:
        return None
    qs = PlatformDeployment.objects
    if for_update:
        qs = qs.select_for_update()
    return qs.filter(pk=value).first()


def transition(value, status, detail=None, expected=None, finished=None):
    allowed = {
        PlatformDeployment.STATUS_REQUESTED,
        PlatformDeployment.STATUS_CANARY,
        PlatformDeployment.STATUS_FLEET,
        *PlatformDeployment.TERMINAL_STATUSES,
    }
    if status not in allowed:
        raise ValueError(f"invalid platform deployment status: {status}")
    with transaction.atomic():
        row = get(value, for_update=True)
        if row is None:
            return False
        if expected is not None and row.status not in set(expected):
            return False
        immutable = {
            PlatformDeployment.STATUS_FAILED,
            PlatformDeployment.STATUS_SUPERSEDED,
            PlatformDeployment.STATUS_CONVERGED,
        }
        if row.status in immutable and row.status != status:
            return False
        entries = list(row.transitions or [])[-(MAX_TRANSITIONS - 1):]
        entries.append({"status": status, "at": _at(), "detail": _safe(detail or {})})
        row.status = status
        row.transitions = entries
        if status in PlatformDeployment.TERMINAL_STATUSES or finished:
            row.finished = timezone.now()
        row.save(update_fields=["status", "transitions", "finished", "modified"])
        return True


def set_framework(value, framework):
    PlatformDeployment.objects.filter(pk=value).update(
        framework_version=_text(framework, 64), modified=timezone.now())


def add_link(value, kind, target):
    with transaction.atomic():
        row = get(value, for_update=True)
        if row is None:
            return False
        links = dict(row.links or {})
        kind = _text(kind, 48)
        if kind == "incident_events":
            values = list(links.get(kind) or [])
            cleaned = _text(target, 160)
            if cleaned not in values:
                values.append(cleaned)
            links[kind] = values[-16:]
        else:
            links[kind] = _text(target, 320)
        row.links = links
        row.save(update_fields=["links", "modified"])
        return True


def evidence(value, runner, state, proof=None, detail=None):
    """Append one sanitized, bounded node observation under a row lock."""
    with transaction.atomic():
        row = get(value, for_update=True)
        if row is None or _text(runner, 64) not in set(row.frozen_roster or []):
            return False
        observed = timezone.now()
        entry = {
            "runner": _text(runner, 64), "state": _text(state, 32),
            "observed_at": observed.isoformat(),
            "stale_after": (observed + timedelta(seconds=EVIDENCE_TTL)).isoformat(),
            "proof": _safe(proof or {}), "detail": _safe(detail or {}),
        }
        # Latest-per-runner truth: repeated callbacks replace their runner's
        # observation and cannot evict proof for another frozen-roster member.
        by_runner = {
            item.get("runner"): item for item in list(row.node_evidence or [])
            if isinstance(item, dict) and item.get("runner")
        }
        by_runner[entry["runner"]] = entry
        row.node_evidence = [
            by_runner[name] for name in sorted(by_runner)
            if name in set(row.frozen_roster or [])
        ][:MAX_EVIDENCE]
        row.save(update_fields=["node_evidence", "modified"])
        return True


_DESIRED_UNSET = object()


def serialize(row, desired_commit=_DESIRED_UNSET):
    duration = None
    if row.started:
        duration = ((row.finished or timezone.now()) - row.started).total_seconds()
    desired = desired_commit
    if desired_commit is _DESIRED_UNSET:
        desired = None
        try:
            from mojo.apps.edge.services import deploy
            target = deploy.get_target()
            desired = (target or {}).get("sha")
        except Exception:
            pass
    current = sorted({
        str((item.get("proof") or {}).get("platform_sha"))
        for item in (row.node_evidence or []) if isinstance(item, dict)
        and (item.get("proof") or {}).get("platform_sha")
    })
    return {
        "id": str(row.pk), "created": row.created.isoformat(), "sha": row.sha,
        "framework_version": row.framework_version, "status": row.status,
        "source": row.source, "actor": row.actor,
        "retry_of": str(row.retry_of_id) if row.retry_of_id else None,
        "frozen_roster": list(row.frozen_roster or []),
        "transitions": list(row.transitions or []),
        "node_evidence": list(row.node_evidence or []),
        "links": dict(row.links or {}), "detail": dict(row.detail or {}),
        "started": row.started.isoformat() if row.started else None,
        "finished": row.finished.isoformat() if row.finished else None,
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "current_commits": current, "desired_commit": desired,
    }


def last_converged_framework():
    """The framework version of the newest CONVERGED deploy, or None.

    Converged and not merely released: that status is the reconciler's proof
    that every frozen runner answered with this deployment's UUID and SHA,
    which is the only evidence the version actually RUNS on this fleet. A
    `fleet` row is a dispatch, not a proof, and holding at one would freeze the
    fleet onto a version that may never have booted.
    """
    return PlatformDeployment.objects.filter(
        status=PlatformDeployment.STATUS_CONVERGED).exclude(
            framework_version="").order_by("-created").values_list(
                "framework_version", flat=True).first() or None


def same_sha_retry(row, actor, created_by=None, idempotency_key=None):
    return create(
        row.sha, actor=actor, source="admin_retry", created_by=created_by,
        idempotency_key=idempotency_key, retry_of=row)


def verify(value, timeout=2.0):
    """Collect bounded UUID/SHA proof from the frozen edge-channel roster."""
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
    from mojo.apps.jobs.manager import get_manager

    row = get(value)
    if row is None:
        return None
    manager = get_manager()
    roster = list(row.frozen_roster or [])[:MAX_ROSTER]

    def collect(runner):
        return manager.execute_on_runner(
            runner, "mojo.apps.edge.services.readiness.local_node_proof",
            {"deployment": str(row.pk), "sha": row.sha}, timeout=timeout)

    results = {}
    pool = ThreadPoolExecutor(max_workers=min(8, max(1, len(roster))))
    futures = {pool.submit(collect, runner): runner for runner in roster}
    try:
        for future in as_completed(futures, timeout=max(timeout + 1.0, 2.0)):
            runner = futures[future]
            try:
                results[runner] = future.result()
            except Exception:
                results[runner] = None
    except TimeoutError:
        pass
    finally:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

    proven = 0
    for runner in roster:
        response = results.get(runner)
        proof = response.get("result") if (
            isinstance(response, dict) and response.get("status") == "success") else None
        good = bool(
            isinstance(proof, dict)
            and proof.get("platform_deployment") == str(row.pk)
            and str(proof.get("platform_sha") or "").startswith(row.sha))
        if good:
            proven += 1
        evidence(
            row.pk, runner, "proven" if good else "unavailable",
            proof=proof or {}, detail={} if good else {"reason": "proof_unavailable"})
    if roster and proven == len(roster):
        status = PlatformDeployment.STATUS_CONVERGED
    elif proven:
        status = PlatformDeployment.STATUS_PARTIAL
    else:
        status = PlatformDeployment.STATUS_UNKNOWN
    transition(row.pk, status, {"proven": proven, "expected": len(roster)})
    return get(row.pk)


def converge(value):
    """Publish existing desired hosting state, then return current UUID proof."""
    from mojo.apps.edge import cronjobs
    from mojo.apps.edge.services import convergence

    row = get(value)
    if row is None:
        return None
    receipts = []
    for pool in cronjobs.converge_pools()[:32]:
        result = convergence.publish_pool(pool)
        receipts.append({
            "pool": pool, "status": _text(getattr(result, "status", "unknown"), 32),
        })
    transition(row.pk, row.status, {"convergence": receipts})
    return verify(row.pk)


def reconcile_stale():
    """Verify released fleets, then close abandoned coordination attempts."""
    from datetime import timedelta
    from mojo.apps.edge.services import deploy

    now = timezone.now()
    cutoff = now - timedelta(seconds=max(60, deploy.status_ttl()))
    proof_cutoff = now - timedelta(seconds=FLEET_PROOF_GRACE)
    active = list(PlatformDeployment.objects.filter(
        status__in=PlatformDeployment.ACTIVE_STATUSES).order_by("modified")[:100])
    changed = 0
    current = deploy.get_status()
    target = deploy.get_target() or {}
    for row in active:
        if current and current.get("deployment") == str(row.pk):
            if current.get("state") != deploy.STATUS_FAILED:
                # A live lease means the deploy is still running: hands off.
                continue
            # A terminal-failed lease is the node's own report that this
            # attempt is over. Nothing else closes it — the orchestrator that
            # would have is exactly the thing that died — so the row sat
            # `canary` forever and the lease held the plane shut for its TTL.
            changed += int(transition(
                row.pk, PlatformDeployment.STATUS_FAILED,
                {"reason": "node_reported_failed"}))
            deploy.clear_status(row.pk)
            continue
        if (row.status == PlatformDeployment.STATUS_FLEET
                and row.modified < proof_cutoff):
            try:
                result = verify(row.pk)
            except Exception:
                # A transient jobs/Redis failure leaves the row active for the
                # next bounded sweep; it must not become a false terminal.
                continue
            changed += int(result is not None and result.status in {
                PlatformDeployment.STATUS_CONVERGED,
                PlatformDeployment.STATUS_PARTIAL,
                PlatformDeployment.STATUS_UNKNOWN,
            })
        elif row.modified < cutoff:
            if (row.status == PlatformDeployment.STATUS_REQUESTED
                    and target.get("deployment") == str(row.pk)):
                # The live target names this row: it is the fleet's desired
                # state, not an abandoned attempt. resume_stranded_target owns
                # it — closing it `unknown` here would erase the only record
                # of what the fleet is supposed to converge onto.
                continue
            changed += int(transition(
                row.pk, PlatformDeployment.STATUS_UNKNOWN,
                {"reason": "coordination_lease_expired"}))
    return changed
