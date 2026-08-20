"""Durable truth and bounded evidence for platform deployment attempts."""

import hashlib
import json
import re
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from mojo.apps.edge.models import PlatformDeployment


MAX_ROSTER = 128
MAX_TRANSITIONS = 64
MAX_EVIDENCE = 128
# Per KIND ("failure" and "outcome" budget separately), and the FIRST entries
# are the story — a flood of late reports can never evict them.
MAX_DIAGNOSIS = 16
MAX_TEXT = 320
EVIDENCE_TTL = 600
FLEET_PROOF_GRACE = 120
# Evidence states that end an attempt on a node. These are copied into the
# immutable diagnosis journal, because node_evidence keeps only the LATEST
# observation per runner and a later probe (verify's `unavailable`) replaces
# them (item 2225).
TERMINAL_EVIDENCE_STATES = {"failed", "identity_mismatch"}
# Bounds for the lease-independent rollback-outcome sweep: only recently
# failed attempts are worth asking the node about, and only a handful.
ROLLBACK_OUTCOME_WINDOW = 48 * 3600
ROLLBACK_OUTCOME_SCAN = 5
# One node deploy job per runner, and the roster is capped. The bound exists so
# a malformed payload can never turn a callback into an unbounded scan.
MAX_HANDOFF_JOBS = 8
# Phase timings: a deploy pass emits about fourteen and a rollback pass adds
# ten more, so the cap is a bound on hostile input, not a working limit.
MAX_PHASES = 32
PHASE_NAME_PATTERN = re.compile(r"^[a-z_]{1,32}$")


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


def _diagnosis_key(detail):
    detail = detail if isinstance(detail, dict) else {}
    # identity_mismatch details carry `reason`, not `phase`.
    return detail.get("phase") or detail.get("reason")


def _append_diagnosis(row, runner, detail=None, proof=None, kind="failure"):
    """Append one entry to an already-locked row's diagnosis, or refuse.

    Returns the rebuilt list, or None when the entry is refused: the runner is
    not in the frozen roster, this kind's budget is already spent (the first
    entries ARE the story and are never evicted), or an entry with the same
    (kind, runner, phase-or-reason) already exists — idempotence against
    repeated callbacks and repeated sweeps. The append rules live here once;
    `record_diagnosis` and `evidence` both route through it.
    """
    runner = _text(runner, 64)
    if runner not in set(row.frozen_roster or []):
        return None
    kind = _text(kind, 16)
    detail = _safe(detail or {})
    entries = [item for item in list(row.diagnosis or []) if isinstance(item, dict)]
    if len([item for item in entries if item.get("kind") == kind]) >= MAX_DIAGNOSIS:
        return None
    key = _diagnosis_key(detail)
    for item in entries:
        if (item.get("kind") == kind and item.get("runner") == runner
                and _diagnosis_key(item.get("detail")) == key):
            return None
    entries.append({
        "runner": runner, "at": _at(), "kind": kind,
        "detail": detail, "proof": _safe(proof or {}),
    })
    return entries


def record_diagnosis(value, runner, detail=None, proof=None, kind="failure"):
    """Append one immutable diagnosis entry under the same row-lock pattern
    as `evidence()`. Returns whether the entry landed."""
    with transaction.atomic():
        row = get(value, for_update=True)
        if row is None:
            return False
        entries = _append_diagnosis(
            row, runner, detail=detail, proof=proof, kind=kind)
        if entries is None:
            return False
        # Assign the rebuilt list rather than mutating the fetched one, the
        # same way evidence() assigns row.node_evidence.
        row.diagnosis = entries
        row.save(update_fields=["diagnosis", "modified"])
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
        update_fields = ["node_evidence", "modified"]
        if entry["state"] in TERMINAL_EVIDENCE_STATES:
            # A terminal observation is the attempt's failure story; copy it
            # into the append-only journal before a later probe replaces this
            # runner's node_evidence entry.
            entries = _append_diagnosis(
                row, runner, detail=detail, proof=proof, kind="failure")
            if entries is not None:
                row.diagnosis = entries
                update_fields.append("diagnosis")
        row.save(update_fields=update_fields)
        return True


def parse_phases(text):
    """Phase timings as the node scripts append them, as bounded entries.

        <pass> <name> <value> <unit>        deploy post_deploy 41230 ms

    This is data written by a shell script on a box, read by the platform, so
    it is parsed as hostile input: fixed shape, a name charset, digits only,
    a known unit, and a hard entry cap. Anything that does not fit is dropped
    silently — a malformed line is a missing timing, never an error and never
    a reason to fail a deploy callback.

    Seconds normalise to milliseconds and are marked `approx`: `date +%s%3N`
    is a GNU extension, and a node whose date does not support it still has
    something worth reporting.
    """
    entries = []
    for line in (text or "").splitlines():
        if len(entries) >= MAX_PHASES:
            break
        parts = line.split()
        if len(parts) != 4:
            continue
        pass_name, name, value, unit = parts
        if pass_name not in ("deploy", "rollback"):
            continue
        if not PHASE_NAME_PATTERN.match(name):
            continue
        if not value.isdigit() or len(value) > 12:
            continue
        if unit not in ("ms", "s"):
            continue
        entry = {"phase": name, "pass": pass_name,
                 "ms": int(value) * (1000 if unit == "s" else 1)}
        if unit == "s":
            entry["approx"] = True
        entries.append(entry)
    return entries


def record_engine_restart(value):
    """Close the phase table with the one gap the node cannot time itself.

    update.sh's last act is to stop the engine that is running the deploy job,
    so nothing on the node is alive to time what happens next: the engine
    coming back on the new code and proving the release. That interval is
    measured HERE instead — from the `verified` transition the dying node
    wrote to the moment its replacement converges the row — and appended as a
    normal phase entry, so the table reads end to end.

    Idempotent, and silent about every failure: a missing timing is a missing
    timing.
    """
    from django.utils.dateparse import parse_datetime
    from mojo.helpers import logit

    try:
        with transaction.atomic():
            row = get(value, for_update=True)
            if row is None:
                return False
            at = None
            for entry in reversed(list(row.transitions or [])):
                if (isinstance(entry, dict)
                        and entry.get("status") == PlatformDeployment.STATUS_VERIFIED):
                    at = entry.get("at")
                    break
            started = parse_datetime(at) if at else None
            if started is None:
                return False
            elapsed = (timezone.now() - started).total_seconds()
            if elapsed < 0:
                return False
            detail = dict(row.detail or {})
            phases = [item for item in (detail.get("phases") or [])
                      if isinstance(item, dict)]
            if any(item.get("phase") == "engine_restart" for item in phases):
                return False
            phases.append({"phase": "engine_restart", "pass": "deploy",
                           "ms": int(elapsed * 1000)})
            detail["phases"] = phases[:MAX_PHASES]
            row.detail = detail
            row.save(update_fields=["detail", "modified"])
            return True
    except Exception:
        logit.exception(
            f"edge deploy {value}: failed to record the engine restart phase")
        return False


def record_phases(value, entries):
    """Store a node's phase timings on the durable row's `detail`."""
    from mojo.helpers import logit

    try:
        if not entries:
            return False
        with transaction.atomic():
            row = get(value, for_update=True)
            if row is None:
                return False
            detail = dict(row.detail or {})
            detail["phases"] = _safe(list(entries)[:MAX_PHASES])
            row.detail = detail
            row.save(update_fields=["detail", "modified"])
            return True
    except Exception:
        logit.exception(f"edge deploy {value}: failed to record phase timings")
        return False


def close_handoff_job(value, state="completed"):
    """Close this deployment's node job before its engine is killed.

    update.sh stops the engine that is running `deploy_node` for this exact
    deployment, so that job never ends on its own: the row stays `running`
    with a live in-flight lease, and the story of the deploy is told by a
    later reaper timing the lease out. The node knows the real outcome at
    callback time — it is the thing reporting it — so it writes the terminal
    status here, and drops the in-flight entry the dying engine will not.

    SCOPED TO THIS NODE, WITH A FALLBACK. A fleet deploy publishes ONE
    deployment UUID to EVERY node (`asyncjobs._publish_deploy_node`), and every
    node reaches this call, so the deployment alone identifies up to a whole
    fleet of sibling rows. Closing those would record peers `completed` before
    they have finished and delete the in-flight leases whose expiry is the only
    thing that ever detects an abandoned node deploy — and on the failure path
    it would rewrite healthy peers to `failed`. So the first pass matches this
    box: `runner_id` (stamped by the engine that claimed the row) or `channel`
    (what the orchestrator published to), against `deploy.local_runner_id()`.

    The fallback is what keeps the ORIGINAL bug closed. `JobEngine` accepts an
    explicit `--runner-id` (see mojo.apps.jobs.cli), and a node started that
    way has a runner id — and therefore a channel — that `local_runner_id()`
    cannot predict, so the scoped pass matches nothing there. Only when it
    matches NOTHING do we fall back to the deployment-only match: on such a
    node it is the one row that can be meant, and on a normal node the scoped
    pass has already answered. Each matched row's OWN channel is released.

    Every failure is logged and swallowed. This runs on the deploy callback
    and on the rollback report; neither may be blocked by a jobs-plane
    problem, the same rule `_report_failure_incident` follows.

    Returns the number of rows closed.
    """
    from django.db.models import Q
    from mojo.helpers import logit

    try:
        if state not in ("completed", "failed"):
            raise ValueError(f"invalid handoff job state: {state}")
        owner = deployment_id(value)
        if not owner:
            return 0
        from mojo.apps.jobs.manager import get_manager
        from mojo.apps.jobs.models import Job, JobEvent
        from mojo.apps.edge.services import deploy

        candidates = Job.objects.filter(
            func=deploy.DEPLOY_NODE_JOB, status="running",
            payload__deployment=owner)
        local = deploy.local_runner_id()
        rows = []
        if local:
            rows = list(candidates.filter(
                Q(runner_id=local) | Q(channel=local))[:MAX_HANDOFF_JOBS])
        if not rows:
            rows = list(candidates[:MAX_HANDOFF_JOBS])
        closed = 0
        for job in rows:
            metadata = dict(job.metadata or {})
            metadata["handoff"] = True
            # Compare-and-set on `running`: a job the engine somehow DID
            # finish keeps the outcome it reported for itself.
            if not Job.objects.filter(pk=job.pk, status="running").update(
                    status=state, finished_at=timezone.now(),
                    metadata=metadata, modified=timezone.now()):
                continue
            JobEvent.objects.create(
                job=job, channel=job.channel, event=state,
                runner_id=job.runner_id, attempt=job.attempt,
                details={"reason": "deploy_engine_handoff"})
            get_manager().release_inflight(job.channel, job.pk)
            closed += 1
        return closed
    except Exception:
        logit.exception(
            f"edge deploy {value}: failed to close the handoff job")
        return 0


_DESIRED_UNSET = object()


def strip_stderr_tail(entries):
    """Copy evidence-shaped entries with any `detail.stderr_tail` removed.

    The tail is the last lines of update-script stderr. It is redacted per
    line, but the redactor has gaps a credential can survive, so only
    view_platform_security / manage_platform / admin may read it.

    Copies rather than pops in place: `serialize` is called on rows other code
    paths go on to save, and mutating `row.node_evidence` here would persist
    the stripped copy and destroy the durable evidence.
    """
    result = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            result.append(entry)
            continue
        detail = entry.get("detail")
        if isinstance(detail, dict) and "stderr_tail" in detail:
            entry = dict(entry)
            detail = dict(detail)
            detail.pop("stderr_tail", None)
            entry["detail"] = detail
        result.append(entry)
    return result


def _node_summary(row):
    """Bucket counts a UI can render without re-deriving evidence semantics.

    The buckets partition the OBSERVED entries — they always sum to
    len(node_evidence) — while `expected` is the frozen roster size.
    """
    summary = {"expected": len(row.frozen_roster or []), "proven": 0,
               "reported": 0, "failed": 0, "dispatched": 0, "other": 0}
    for item in row.node_evidence or []:
        state = item.get("state") if isinstance(item, dict) else None
        if state == "proven":
            summary["proven"] += 1
        elif state in ("deploying", "identity_pending"):
            summary["reported"] += 1
        elif state in ("failed", "identity_mismatch", "publish_failed"):
            summary["failed"] += 1
        elif state == "dispatched":
            summary["dispatched"] += 1
        else:
            # unavailable, rolled_back, and anything unrecognized.
            summary["other"] += 1
    return summary


def serialize(row, desired_commit=_DESIRED_UNSET, include_stderr=False):
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
        "transitions": (list(row.transitions or []) if include_stderr
                        else strip_stderr_tail(row.transitions)),
        "node_evidence": (list(row.node_evidence or []) if include_stderr
                          else strip_stderr_tail(row.node_evidence)),
        "diagnosis": (list(row.diagnosis or []) if include_stderr
                      else strip_stderr_tail(row.diagnosis)),
        "node_summary": _node_summary(row),
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


def last_converged_deployment():
    """The newest CONVERGED deploy ROW, or None. See last_converged_framework."""
    return PlatformDeployment.objects.filter(
        status=PlatformDeployment.STATUS_CONVERGED).order_by("-created").first()


def same_sha_retry(row, actor, created_by=None, idempotency_key=None):
    deployment, _replayed = create(
        row.sha, actor=actor, source="admin_retry", created_by=created_by,
        idempotency_key=idempotency_key, retry_of=row)
    return deployment


def proof_matches(row, proof):
    """True only for this attempt's exact UUID and a full live commit SHA."""
    if not isinstance(proof, dict):
        return False
    from mojo.apps.edge.services import deploy
    sha = proof.get("platform_sha")
    return bool(
        proof.get("platform_deployment") == str(row.pk)
        and isinstance(sha, str) and len(sha) == 40
        and deploy.is_valid_sha(sha) and sha.startswith(row.sha))


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
        good = proof_matches(row, proof)
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


def _local_evidence(row, runner_id):
    for entry in reversed(list(row.node_evidence or [])):
        if isinstance(entry, dict) and entry.get("runner") == runner_id:
            return entry
    return None


def finalize_post_restart():
    """Finalize one exact single-runner terminal lease after self-restart.

    Callback-time code records intent but never clears the lease: update.sh
    can kill that engine immediately afterward. The replacement engine proves
    the installed identity, closes the durable row, clears only its UUID, then
    gives one queued successor a chance to claim the now-empty lease.
    """
    from mojo.apps.edge.services import deploy, readiness

    current = deploy.get_status() or {}
    if current.get("state") not in deploy.TERMINAL_STATES:
        return None
    owner = deployment_id(current.get("deployment"))
    if not owner:
        # Empty-UUID v1 leases have no safe durable owner and remain TTL-bound.
        return None
    row = get(owner)
    if row is None:
        if deploy.clear_status(owner):
            deploy.resume_stranded_target()
            return owner
        return None

    runner_id = deploy.local_runner_id()
    roster = list(row.frozen_roster or [])
    if len(roster) != 1 or runner_id not in roster:
        return None
    observed = _local_evidence(row, runner_id)
    if not observed:
        return None

    if current.get("state") == deploy.STATUS_FAILED:
        if observed.get("state") not in {"failed", "identity_mismatch"}:
            return None
        if row.status != PlatformDeployment.STATUS_FAILED:
            if not transition(
                    row.pk, PlatformDeployment.STATUS_FAILED,
                    {"reason": "post_restart_failed"}):
                return None
    else:
        if observed.get("state") not in {
                deploy.STATUS_DEPLOYING, "identity_pending", "proven"}:
            return None
        try:
            proof = readiness.local_node_proof()
        except Exception:
            return None
        if not proof_matches(row, proof):
            return None
        if not evidence(row.pk, runner_id, "proven", proof=proof, detail={}):
            return None
        if row.status != PlatformDeployment.STATUS_CONVERGED:
            if not transition(
                    row.pk, PlatformDeployment.STATUS_CONVERGED,
                    {"source": "post_restart", "proven": 1, "expected": 1}):
                return None
        # The node timed everything up to its own death; this is the rest.
        record_engine_restart(row.pk)

    if not deploy.clear_status(row.pk):
        return None
    deploy.resume_stranded_target()
    return str(row.pk)


def record_rollback_outcomes():
    """Close the loop on a failed single-runner deploy: what runs there NOW?

    Lease-independent and idempotent, unlike `finalize_post_restart` — a
    failed lease may be long cleared (or expired) by the time this node's
    replacement engine runs, yet the durable row still says only "failed",
    never whether the rollback took. The node's own identity answers that: a
    valid FOREIGN identity (another attempt's UUID with a full live SHA)
    proves the node serves something other than the failed attempt, so a
    `rolled_back` observation plus an outcome-kind diagnosis entry is
    recorded. The node's OWN identity, an empty one, or an invalidated one
    proves nothing and records nothing. Single-runner rosters only — on a
    multi-node fleet this node's identity says nothing about the canary that
    failed. Returns the number of attempts recorded.
    """
    from mojo.apps.edge.services import deploy, readiness

    runner = deploy.local_runner_id()
    cutoff = timezone.now() - timedelta(seconds=ROLLBACK_OUTCOME_WINDOW)
    rows = PlatformDeployment.objects.filter(
        status=PlatformDeployment.STATUS_FAILED,
        modified__gte=cutoff).order_by("-modified")[:ROLLBACK_OUTCOME_SCAN]
    candidates = []
    for row in rows:
        if list(row.frozen_roster or []) != [runner]:
            continue
        kinds = {item.get("kind") for item in (row.diagnosis or [])
                 if isinstance(item, dict)}
        if "failure" in kinds and "outcome" not in kinds:
            candidates.append(row)
    if not candidates:
        return 0
    try:
        proof = readiness.local_node_proof()
    except Exception:
        return 0
    proof = proof if isinstance(proof, dict) else {}
    sha = proof.get("platform_sha")
    owner = proof.get("platform_deployment")
    if not (isinstance(sha, str) and len(sha) == 40 and deploy.is_valid_sha(sha)):
        return 0
    if not owner:
        return 0
    recorded = 0
    for row in candidates:
        if str(owner) == str(row.pk):
            # The node still proves THIS attempt; its own identity is not
            # evidence of a rollback (the SAME_RELEASE failure case).
            continue
        evidence(row.pk, runner, "rolled_back", proof=proof,
                 detail={"reason": "post_restart_observation"})
        if record_diagnosis(row.pk, runner, detail={"phase": "rolled_back"},
                            proof=proof, kind="outcome"):
            recorded += 1
    return recorded


def reconcile_stale():
    """Verify released fleets, then close abandoned coordination attempts."""
    from datetime import timedelta
    from mojo.apps.edge.services import deploy

    finalize_post_restart()
    record_rollback_outcomes()
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
