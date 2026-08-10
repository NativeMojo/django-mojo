"""Idempotent post-commit publication of pool desired-state convergence."""

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
        job_ids = jobs.publish(
            func=INSTALL_JOB,
            payload={"pool": pool, "generation": generation},
            channel=EDGE_CHANNEL, broadcast=True,
            idempotency_key=f"edge-converge:{pool}:{generation}")
    except Exception as error:
        logit.error(
            f"edge: convergence publication pending for pool {pool}: {error}")
        return objict(
            status="pending", pool=pool, generation=generation,
            error="Convergence publication failed; the periodic sweep will retry")
    return objict(
        status="published", pool=pool, generation=generation, jobs=job_ids)


def publish_after_commit(*pools):
    """Schedule publication after persistence; duplicates converge by hash."""
    clean = sorted({str(pool or "default") for pool in pools if pool is not None})
    for pool in clean:
        transaction.on_commit(lambda pool=pool: publish_pool(pool))
    return clean
