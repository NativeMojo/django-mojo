"""The advisory-lock namespace registry (#9048).

1.39.0 shipped the deploy migration lock and shared-ACME admission on the same
key, ``(1423, 1)``, and every guarded deploy then refused its own migration.
These tests fail when two fixed namespaces collide, when a known call site
stops taking its namespace from the registry, or when a new file starts taking
Postgres advisory locks without registering one.
"""
from pathlib import Path
import re

from testit import helpers as th

TESTIT_TIER = "framework"
ROOT = Path(__file__).resolve().parents[2]

# Every file under mojo/ that calls a pg_*advisory* function. A new one fails
# test_every_call_site_is_known until its namespace is in the registry and it
# is listed here.
CALL_SITES = {
    "mojo/apps/dnsman/services/certs.py",
    "mojo/apps/dnsman/services/acme_hub.py",
    "mojo/apps/dnsman/services/acme_hub_upgrade.py",
    "mojo/apps/dnsman/services/connection_config.py",
    "mojo/apps/edge/management/commands/migrate_locked.py",
    # Hash-derived keys, deliberately unregistered (see the registry docstring).
    "mojo/apps/account/services/health_snapshot.py",
    "mojo/helpers/advisory_locks.py",
}


@th.unit_test("no two registered advisory-lock namespaces share a number")
def test_namespaces_unique(opts):
    from mojo.helpers import advisory_locks

    values = list(advisory_locks.NAMESPACES.values())
    th.assert_eq(len(values), len(set(values)),
                 f"duplicate advisory-lock namespace: {advisory_locks.NAMESPACES}")


@th.unit_test("migrate_locked and shared-ACME admission use different keys")
def test_migrate_not_admission(opts):
    from mojo.helpers import advisory_locks
    from mojo.apps.dnsman.services import acme_hub_upgrade
    from mojo.apps.edge.management.commands import migrate_locked

    th.assert_eq(migrate_locked.LOCK_NAMESPACE, advisory_locks.MIGRATE,
                 "migrate_locked must take its namespace from the registry")
    th.assert_eq(acme_hub_upgrade._NAMESPACE, advisory_locks.ACME_HUB_ADMISSION,
                 "admission must take its namespace from the registry")
    th.assert_true(migrate_locked.LOCK_NAMESPACE != acme_hub_upgrade._NAMESPACE,
                   "a deploy holds admission for its whole run, so migrate_locked "
                   "on the same key refuses every deploy's own migration")


@th.django_unit_test("the dnsman lock sites take their namespaces from the registry")
def test_dnsman_sites_registered(opts):
    from mojo.helpers import advisory_locks
    from mojo.apps.dnsman.services import acme_hub, certs

    th.assert_eq(certs._ISSUE_LOCK_NAMESPACE, advisory_locks.CERT_ISSUE,
                 "certificate issuance must take its namespace from the registry")
    th.assert_eq(acme_hub._LOCK_NAMESPACE, advisory_locks.ACME_HUB_ALLOCATION,
                 "ACME allocation must take its namespace from the registry")
    source = (ROOT / "mojo/apps/dnsman/services/connection_config.py").read_text()
    th.assert_true("advisory_locks.DNSMAN_CONNECTION_CONFIG" in source,
                   "connection_config must take its namespace from the registry")


@th.unit_test("every file taking a Postgres advisory lock is a known call site")
def test_every_call_site_is_known(opts):
    pattern = re.compile(r"pg_\w*advisory\w*")
    found = {
        str(path.relative_to(ROOT))
        for path in (ROOT / "mojo").rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore"))}
    unknown = sorted(found - CALL_SITES)
    th.assert_eq(unknown, [],
                 "new advisory-lock call site(s): register a namespace in "
                 f"mojo/helpers/advisory_locks.py and list the file here: {unknown}")
