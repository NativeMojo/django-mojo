"""Every fixed Postgres advisory-lock namespace django-mojo takes, in one place.

Advisory locks share one keyspace per database. Two features that pick the
same ``(namespace, id)`` silently serialise against each other: in 1.39.0 the
deploy's migration lock and shared-ACME admission both used ``(1423, 1)``, and
because a guarded deploy job holds admission for its whole run, every deploy
refused its own migration. A new lock takes a new number here, and
``tests/test_helpers/advisory_locks.py`` fails on a duplicate or on a call
site that does not use this registry.

Not registered: ``health_snapshot``'s per-source transaction lock derives both
keys from the source id hash, so it has no fixed namespace to reserve.
"""

CERT_ISSUE = 1421                       # dnsman certificate issuance; id = domain
ACME_HUB_ALLOCATION = 1422              # dnsman ACME delegation; id = allocation pk
ACME_HUB_ADMISSION = 1423               # dnsman shared-ACME admission; id = 1
MIGRATE = 1424                          # edge migrate_locked; id = 1
DNSMAN_CONNECTION_CONFIG = 0x444e534d   # "DNSM"; id = 8986, transaction lock

NAMESPACES = {
    "CERT_ISSUE": CERT_ISSUE,
    "ACME_HUB_ALLOCATION": ACME_HUB_ALLOCATION,
    "ACME_HUB_ADMISSION": ACME_HUB_ADMISSION,
    "MIGRATE": MIGRATE,
    "DNSMAN_CONNECTION_CONFIG": DNSMAN_CONNECTION_CONFIG,
}


def holders(cursor, namespace, lock_id):
    """Other sessions holding or waiting on ``(namespace, lock_id)``.

    For error messages only: a refusal that names the holder is diagnosable
    from the log line, where "another process holds the lock" is not. Two-key
    advisory locks appear in ``pg_locks`` as classid/objid with objsubid 2.
    """
    cursor.execute(
        "SELECT l.pid, l.mode, l.granted, a.application_name, a.backend_start"
        " FROM pg_locks l LEFT JOIN pg_stat_activity a ON a.pid = l.pid"
        " WHERE l.locktype = 'advisory' AND l.classid = %s AND l.objid = %s"
        " AND l.objsubid = 2 AND l.pid <> pg_backend_pid()"
        " ORDER BY l.pid",
        [namespace, lock_id])
    return [
        dict(pid=pid, mode=mode, granted=granted,
             application_name=app or "", backend_start=started)
        for pid, mode, granted, app, started in cursor.fetchall()]


def describe_holders(cursor, namespace, lock_id):
    """``holders`` as one log-friendly string; never raises."""
    try:
        rows = holders(cursor, namespace, lock_id)
    except Exception as err:
        return f"holder unknown ({err.__class__.__name__})"
    if not rows:
        return "holder unknown (released before it could be read)"
    return "; ".join(
        f"pid {row['pid']} {row['mode']}"
        f"{'' if row['granted'] else ' waiting'}"
        f" app={row['application_name'] or '?'}"
        f" since {row['backend_start'].isoformat() if row['backend_start'] else '?'}"
        for row in rows)
