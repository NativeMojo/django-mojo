"""Idempotent post-commit publication of pool desired-state convergence."""

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import transaction

from objict import objict

from mojo.helpers import logit


INSTALL_JOB = "mojo.apps.edge.asyncjobs.install_generation"
EDGE_CHANNEL = "edge"
_PUBLISHER = ContextVar("edge_convergence_publisher", default=None)


def desired_generation(pool):
    from mojo.apps.edge.rest.node import enabled_vhosts
    from mojo.apps.edge.services import releases, render

    vhosts = enabled_vhosts(pool)
    return render.desired_state(
        vhosts, webapps=releases.desired_webapps(vhosts))["generation"]


def publish_pool(pool, jobs_module=None, generation=None):
    """Publish one generation once; a retry reuses the durable job receipt."""
    if jobs_module is None:
        from mojo.apps import jobs as jobs_module

    generation = generation or desired_generation(pool)
    try:
        # Same roster policy as jobs.publish(broadcast=True): this function
        # hand-rolls that fan-out and inherited the same defect (item #2729).
        # A swallowed get_runners error looked like an empty fleet, and the
        # `or [EDGE_CHANNEL]` below then queued one job for a single runner to
        # win. broadcast_roster reports what it could not prove; the empty
        # fallback stays, but is no longer reachable from a hidden failure.
        rows, _roster_exact = jobs_module.broadcast_roster(
            EDGE_CHANNEL, INSTALL_JOB)
        runners = [
            row.get("runner_id") for row in rows
            if row.get("alive") and row.get("runner_id")]
        targets = sorted(set(runners)) or [EDGE_CHANNEL]
        job_ids = []
        for target in targets:
            # Job.idempotency_key is varchar(64). Hash pool/generation/target
            # together rather than appending a runner id to a long base key.
            # This also avoids broadcast's fan-out suffix exceeding that DB
            # bound for a long but valid runner id.
            identity = f"edge-converge:{pool}:{generation}:{target}"
            key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            job_ids.append(jobs_module.publish(
                func=INSTALL_JOB,
                payload={"pool": pool, "generation": generation},
                channel=target, idempotency_key=key))
    except Exception as error:
        from mojo.apps.account.services.setup_safety import failure_metadata
        failure = failure_metadata(error, "edge.publish")
        logit.error(
            f"edge: convergence publication pending for pool {pool} "
            f"(action={failure['action']}, error={failure['exception_class']})")
        return objict(
            status="pending", pool=pool, generation=generation,
            error="Convergence publication failed; the periodic sweep will retry")
    return objict(status="published", pool=pool, generation=generation,
                  jobs=job_ids, targets=targets)


@contextmanager
def publisher_scope(publisher):
    """Temporarily inject a callback publisher in this execution context.

    Context-local scope keeps in-process tests and callers isolated across
    parallel threads/tasks. Registered callbacks retain the selected publisher
    after the scope exits, matching Django's deferred on-commit behavior.
    """
    if not callable(publisher):
        raise ValueError("convergence publisher must be callable")
    token = _PUBLISHER.set(publisher)
    try:
        yield
    finally:
        _PUBLISHER.reset(token)


def publish_after_commit(*pools, publisher=None):
    """Schedule publication after persistence; duplicates converge by hash."""
    publisher = publisher or _PUBLISHER.get() or publish_pool
    clean = sorted({str(pool or "default") for pool in pools if pool is not None})
    for pool in clean:
        transaction.on_commit(
            lambda pool=pool, publisher=publisher: publisher(pool))
    return clean
