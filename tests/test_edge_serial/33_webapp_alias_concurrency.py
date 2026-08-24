"""Two `attach()` calls racing for one hostname (item #2724).

`attach()` decides who owns `(domain, label)` BEFORE it does provider work,
then commits the alias in a later transaction. Two overlapping calls both read
an empty name, both finish their provider prerequisites, and both arrive at the
commit — historically one of them hit the enabled-name unique index and the raw
`IntegrityError` surfaced as a 500 instead of the documented outcome: an
idempotent `attached` for the same app, or the plain "already serving something
else here" refusal for a different one.

The race is made deterministic without a single process-global patch. Both
calls are held at a two-party `threading.Barrier` inside the **call-local**
`resolve_cname` seam, which `attach()` reaches only after its own occupancy
check found the name free — so releasing the barrier reproduces exactly the
stale read the bug lives in. Everything else is uuid-scoped fixtures on an
external (`provider="mojo"`) domain, where the platform holds no zone
credential, so the whole path is database work with no provider seam to mock.

`select_for_update()` is a no-op on SQLite. This proves PostgreSQL behaviour,
which is what the test project and every deployment run.

No `cleanup()` here on purpose: every fixture is uuid-named and lives in a
group created by this setup, so there is nothing from a previous run to collide
with — and the shared `cleanup()` sweeps rows belonging to the other edge
modules.
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from objict import objict

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools, declare_release_buckets, make_certificate, make_domain,
    make_group, make_group_member, make_upstream, make_vhost, make_webapp,
    with_setting,
)


TARGET = "edge-alias-race-target.example.net"

# Long enough that a loaded machine never trips it, short enough that a worker
# which dies before the seam fails the test instead of hanging the suite.
BARRIER_TIMEOUT = 60


@th.django_unit_setup()
def setup_webapp_alias_concurrency(opts):
    from mojo.apps.account.models.setting import Setting

    declare_pools()
    declare_release_buckets()
    opts.group = make_group("edge-alias-race")
    opts.actor, _, _, _ = make_group_member(
        ["manage_webapp", "manage_dns"], group=opts.group)
    opts.auth_upstream = make_upstream()
    Setting.set("EDGE_WEBAPP_AUTH_UPSTREAM", str(opts.auth_upstream.pk),
                group=None)


def _app_with_primary(opts):
    """A WebApp already live on its own address — attach()'s precondition."""
    domain = make_domain(group=opts.group, provider="route53")
    certificate = make_certificate(domain)
    vhost = make_vhost(domain, certificate, label="primary", kind="site_api",
                       pool="default")
    return make_webapp(opts.group, slug=f"rc{uuid.uuid4().hex[:8]}",
                       vhost=vhost)


def _external_domain(opts):
    """An external (`provider="mojo"`) domain with a live apex+wildcard cert.

    External means the platform writes no DNS: `attach()` only PROBES the name
    through the injected `resolve_cname`, and the active wildcard certificate
    means it requests nothing either. That leaves the alias claim itself as the
    only thing the two calls can contend on.
    """
    domain = make_domain(group=opts.group, provider="mojo")
    make_certificate(domain)
    return domain


def _attach_call(web_app_pk, actor_pk, hostname):
    """One racing `attach()`, built to run on its own thread.

    Every ORM instance it touches is re-read inside the worker, so the two
    calls share nothing but the database rows they are fighting over.
    """
    def call(barrier):
        from mojo.apps.account.models import User
        from mojo.apps.edge.models import WebApp
        from mojo.apps.edge.services import webapp_alias

        def resolve_cname(name, *args, **kwargs):
            # attach() reaches this seam only after its own occupancy check
            # found the name free. Both callers are therefore past their
            # preflight when the barrier releases — the stale-read window.
            barrier.wait()
            return objict(targets=[TARGET])

        web_app = WebApp.objects.get(pk=web_app_pk)
        actor = User.objects.get(pk=actor_pk)
        return webapp_alias.attach(web_app, hostname, actor,
                                   resolve_cname=resolve_cname)
    return call


def _run_race(calls):
    """Run the calls in parallel; return `[(result, error), ...]` in order.

    Each worker opens and closes its own database connection so the threads do
    not share one (Django connections are not thread-safe, and the whole point
    here is two independent transactions).
    """
    from django.db import close_old_connections

    barrier = threading.Barrier(len(calls), timeout=BARRIER_TIMEOUT)

    def worker(call):
        close_old_connections()
        try:
            return call(barrier), None
        except Exception as err:
            # A worker that died before the seam must not leave its partner
            # blocked for the whole timeout.
            barrier.abort()
            return None, err
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(worker, call) for call in calls]
        return [future.result() for future in futures]


def _race_attaches(opts, calls):
    return with_setting("EDGE_WEBAPP_CNAME_TARGET", TARGET,
                        lambda: _run_race(calls))


@th.django_unit_test("two overlapping attaches for ONE app converge on one alias")
def test_attach_same_webapp_race_converges(opts):
    from django.db import IntegrityError
    from mojo.apps.edge.models import Vhost

    web_app = _app_with_primary(opts)
    external = _external_domain(opts)
    hostname = f"www.{external.name}"

    outcomes = _race_attaches(opts, [
        _attach_call(web_app.pk, opts.actor.pk, hostname),
        _attach_call(web_app.pk, opts.actor.pk, hostname),
    ])

    errors = [err for _, err in outcomes if err is not None]
    assert not [err for err in errors if isinstance(err, IntegrityError)], \
        f"a raw IntegrityError escaped a same-app attach race: {errors}"
    assert not errors, \
        f"a same-app attach race raised instead of converging: {errors}"

    results = [result for result, _ in outcomes]
    statuses = [result.status for result in results]
    assert statuses == ["attached", "attached"], \
        f"both same-app attaches should report attached, got {statuses}"
    created = [bool(result.created) for result in results]
    assert created.count(True) == 1, \
        f"exactly one racing call should report created=True, got {created}"
    assert results[0].vhost == results[1].vhost, \
        (f"the racing calls named different alias vhosts: "
         f"{results[0].vhost} vs {results[1].vhost}")

    rows = list(Vhost.objects.filter(domain=external, label="www"))
    assert len(rows) == 1, \
        f"the race left {len(rows)} vhosts at {hostname}, not exactly one"
    assert rows[0].alias_of_id == web_app.pk, \
        (f"the surviving alias belongs to app {rows[0].alias_of_id}, "
         f"not {web_app.pk}")
    assert rows[0].pk == results[0].vhost, \
        (f"the surviving alias {rows[0].pk} is not the vhost the calls "
         f"reported ({results[0].vhost})")


@th.django_unit_test("two DIFFERENT apps racing one hostname: one wins, one is refused")
def test_attach_different_webapps_race_refuses_loser(opts):
    from django.db import IntegrityError

    from mojo import errors as me
    from mojo.apps.edge.models import Vhost

    apps = [_app_with_primary(opts), _app_with_primary(opts)]
    external = _external_domain(opts)
    hostname = f"www.{external.name}"

    outcomes = _race_attaches(opts, [
        _attach_call(apps[0].pk, opts.actor.pk, hostname),
        _attach_call(apps[1].pk, opts.actor.pk, hostname),
    ])

    errors = [err for _, err in outcomes if err is not None]
    assert not [err for err in errors if isinstance(err, IntegrityError)], \
        f"a raw IntegrityError escaped a two-app attach race: {errors}"

    winners = [index for index, (result, _) in enumerate(outcomes)
               if result is not None and result.status == "attached"]
    assert len(winners) == 1, \
        (f"exactly one app should have won {hostname}, "
         f"got {len(winners)}: {[result for result, _ in outcomes]}")
    assert len(errors) == 1, \
        f"exactly one racing call should have been refused, got {errors}"

    refusal = errors[0]
    assert isinstance(refusal, me.ValueException), \
        (f"the losing app got {type(refusal).__name__} rather than the "
         f"documented ValueException: {refusal}")
    assert "already serving" in str(refusal), \
        f"the refusal did not carry the documented wording: {refusal}"

    rows = list(Vhost.objects.filter(domain=external, label="www"))
    assert len(rows) == 1, \
        f"the race left {len(rows)} vhosts at {hostname}, not exactly one"
    assert rows[0].alias_of_id == apps[winners[0]].pk, \
        (f"the surviving alias belongs to app {rows[0].alias_of_id}, not the "
         f"winning app {apps[winners[0]].pk}")
