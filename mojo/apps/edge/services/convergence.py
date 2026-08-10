"""Idempotent post-commit publication of pool desired-state convergence."""

import hashlib

from django.db import transaction

from objict import objict

from mojo.helpers import logit


INSTALL_JOB = "mojo.apps.edge.asyncjobs.install_generation"
EDGE_CHANNEL = "edge"


def desired_generation(pool):
    from mojo.apps.edge.rest.node import enabled_vhosts
    from mojo.apps.edge.services import releases, render

    vhosts = enabled_vhosts(pool)
    return render.desired_state(
        vhosts, webapps=releases.desired_webapps(vhosts))["generation"]


def publish_pool(pool):
    """Publish one generation once; a retry reuses the durable job receipt."""
    from mojo.apps import jobs

    generation = desired_generation(pool)
    try:
        runners = [
            row.get("runner_id") for row in jobs.get_runners(channel=EDGE_CHANNEL)
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
            job_ids.append(jobs.publish(
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


def publish_after_commit(*pools):
    """Schedule publication after persistence; duplicates converge by hash."""
    clean = sorted({str(pool or "default") for pool in pools if pool is not None})
    for pool in clean:
        transaction.on_commit(lambda pool=pool: publish_pool(pool))
    return clean
