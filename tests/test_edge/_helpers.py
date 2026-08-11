"""Shared fixtures for the edge tests.

Filename starts with `_` so testit skips it during discovery.
"""

import uuid as _uuid


EDGE_PERMS = ["view_dns", "manage_dns"]

# The hostname a vhost used to be refused outright — it stood in for the
# deployment's own name while EDGE_RESERVED_SERVER_NAMES existed. Kept so the
# removal (#1646) has a positive proof that reads as "the exact name that used
# to be refused". Note it does NOT start with `edge-`, so `cleanup()` does not
# sweep rows built on it — a test that uses it cleans up after itself.
API_HOSTNAME = "api.edge-tests.internal"


def declare_edge_runner(runner_id="edge-test-engine"):
    """Publish one fresh, short-lived edge runner for deploy receiver tests."""
    import json
    import time

    from mojo.apps.jobs.keys import JobKeys
    from mojo.helpers import dates
    from mojo.helpers.redis import get_client

    client = get_client()
    keys = JobKeys()
    client.zadd(keys.runner_registry("edge"), {runner_id: time.time()})
    client.expire(keys.runner_registry("edge"), 30)
    client.set(keys.runner_hb(runner_id), json.dumps({
        "runner_id": runner_id,
        "hostname": "test-host",
        "channels": ["edge"],
        "jobs_processed": 0,
        "jobs_failed": 0,
        "started": dates.utcnow().isoformat(),
        "last_heartbeat": dates.utcnow().isoformat(),
    }), ex=15)
    return runner_id


def unique_email(prefix):
    return f"{prefix}_{_uuid.uuid4().hex[:8]}@edge.test"


def make_user(perms=None, is_superuser=False):
    """A verified, MFA-free login user with optional GLOBAL permissions."""
    from mojo.apps.account.models import User

    email = unique_email("edge")
    password = "Ed##edge99xx"
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    if is_superuser:
        user.is_superuser = True
    user.save()
    if perms:
        user.add_permission(list(perms))
        user.save()
    return user, email, password


def make_group(prefix="edge"):
    from mojo.apps.account.models import Group

    return Group.objects.create(
        name=f"{prefix}_{_uuid.uuid4().hex[:8]}", kind="organization")


def make_group_member(perms, group=None):
    """A user holding `perms` ONLY at the GroupMember level.

    This is the fixture behind the authorization regression test in
    `5_desired_state.py`: a member-scoped grant plus `?group=<own group>` is
    exactly the escalation `requires_global_perms` exists to refuse.
    """
    from mojo.apps.account.models import User, GroupMember

    email = unique_email("edgemember")
    password = "Ed##member99"
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    if group is None:
        group = make_group()
    member, _ = GroupMember.objects.get_or_create(user=user, group=group)
    member.permissions = {p: True for p in perms}
    member.save()
    return user, email, password, group


def login(opts, email, password):
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(email, password)
    assert ok, f"login failed for {email}: {opts.client.last_response.body}"


def make_domain(name=None, group=None, provider="godaddy", status="active"):
    """A Domain safe to point an edge test at — no provider call is reachable."""
    from mojo.apps.dnsman.models import Domain

    if name is None:
        name = f"edge-{_uuid.uuid4().hex[:10]}.com"
    Domain.objects.filter(name=name).delete()
    return Domain.objects.create(
        name=name, group=group, provider=provider, status=status, verified=True)


def make_certificate(domain, status="active", common_name=None, sans=None):
    """A Certificate row with NO key material.

    Deliberate, same as dnsman's fixture: setting secrets would invoke
    KSMSecrets, which needs a KMS key the test environment does not have.
    """
    from mojo.apps.dnsman.models import Certificate

    if common_name is None:
        common_name = domain.name
    if sans is None:
        sans = [domain.name, f"*.{domain.name}"]
    return Certificate.objects.create(
        domain=domain, common_name=common_name, sans=sans, status=status)


def make_upstream(name=None, group=None, kind="http", host="127.0.0.1",
                  port=8000, socket_path=None):
    from mojo.apps.edge.models import Upstream

    if name is None:
        name = f"up-{_uuid.uuid4().hex[:8]}"
    if kind == "unix":
        host, port = None, None
    return Upstream.objects.create(
        group=group, name=name, kind=kind, host=host, port=port,
        socket_path=socket_path)


def make_vhost(domain, certificate=None, label="", kind="site",
               upstream=None, pool="default", is_enabled=True, **extra):
    """`extra` passes the per-kind knobs straight through: spa, body_size_mb,
    quiet_paths, serve_static, redirect_to."""
    from mojo.apps.edge.models import Vhost

    if certificate is None:
        certificate = make_certificate(domain)
    return Vhost.objects.create(
        domain=domain, certificate=certificate, label=label, kind=kind,
        upstream=upstream, pool=pool, is_enabled=is_enabled, **extra)


def make_route(vhost, path_prefix, upstream):
    from mojo.apps.edge.models import VhostRoute

    return VhostRoute.objects.create(
        vhost=vhost, path_prefix=path_prefix, upstream=upstream)


RELEASE_BUCKET = "edge-test-releases"

# Pools these tests use. `validate_pool` restricts a vhost to the deployment's
# DECLARED pools (a tenant must not be able to invent one and land their
# certificate on an isolated node), so every pool a test uses has to be here.
TEST_POOLS = [
    "default", "staging", "nginxreal", "reltest", "wwwsync",
    "itesthappy", "itestexclude", "itesthouse",
    "itestgraph", "itestretired", "itesthouseup",
]


def declare_pools(pools=None):
    from mojo.apps.account.models.setting import Setting

    Setting.set("EDGE_POOLS", list(pools or TEST_POOLS), group=None)


def declare_release_buckets(buckets=None):
    """Declare the release buckets. Registering a site fails closed without it."""
    from mojo.apps.account.models.setting import Setting

    Setting.set("EDGE_RELEASE_BUCKETS", list(buckets or [RELEASE_BUCKET]),
                group=None)


def make_webapp(group, slug=None, vhost=None, bucket=None):
    from mojo.apps.edge.models import WebApp

    if slug is None:
        slug = f"app{_uuid.uuid4().hex[:8]}"
    web_app = WebApp(
        group=group, slug=slug, vhost=vhost,
        bucket=bucket or RELEASE_BUCKET, prefix="pending")
    web_app.save()
    # `storage_prefix` needs a pk, so the derived value lands on the second
    # save — the same two-step the REST create path takes via on_rest_created.
    web_app.prefix = web_app.storage_prefix()
    web_app.save()
    return web_app


def make_manifest(paths=("index.html",)):
    import hashlib

    return [
        dict(path=path,
             sha256=hashlib.sha256(path.encode()).hexdigest(),
             size=len(path))
        for path in paths
    ]


def make_release(web_app, version, status="pending", manifest=None):
    from mojo.apps.edge.models import WebAppRelease

    return WebAppRelease.objects.create(
        webapp=web_app, version=version, status=status,
        manifest=manifest if manifest is not None else make_manifest())


def cleanup():
    """Drop rows a previous run left behind. Long-lived DB — see testing rules.

    **Order matters and the scope cannot be narrowed.** `Vhost.upstream` is
    `on_delete=PROTECT`, so every test vhost must go before any upstream does —
    including vhosts belonging to a different test module in the same run.
    An earlier version took a prefix argument and narrowed only the vhost
    delete, which raised ProtectedError and silently broke the calling module's
    setup. Every test fixture in this app uses the `edge-`/`up-` prefixes
    precisely so this can be unconditional.
    """
    from mojo.apps.dnsman.models import Certificate, Domain
    from mojo.apps.edge.models import (
        BlocklistEntry, Upstream, Vhost, VhostRoute, WebApp, WebAppRelease,
    )

    # Test-created blocklist rows only — the migration's seed rows are real
    # content other tests assert on.
    BlocklistEntry.objects.filter(note__startswith="bltest").delete()

    # WebApp -> current_release is SET_NULL and WebAppRelease -> webapp is
    # CASCADE, so the pointer has to be cleared before the rows go, or the
    # delete order decides whether it works.
    WebApp.objects.filter(slug__startswith="app").update(current_release=None)
    WebAppRelease.objects.filter(webapp__group__name__startswith="edge").delete()
    WebApp.objects.filter(group__name__startswith="edge").delete()
    # Routes PROTECT their upstream, so they go before any Upstream does —
    # deleting the vhosts would cascade them anyway, but being explicit keeps
    # the ordering requirement visible.
    VhostRoute.objects.filter(vhost__domain__name__startswith="edge-").delete()
    Vhost.objects.filter(domain__name__startswith="edge-").delete()
    Upstream.objects.filter(name__startswith="up-").delete()
    Certificate.objects.filter(domain__name__startswith="edge-").delete()
    Domain.objects.filter(name__startswith="edge-").delete()


def ensure_blocklist_seed():
    """(Re)apply migration 0004's blocklist seed, and return its note marker.

    The test workflow flushes tables between runs but keeps the migration
    records, so RunPython-seeded reference data vanishes while the migration
    reports applied. Re-running the migration's OWN function keeps the tests
    asserting the real seed logic rather than a copy of it. (importlib,
    because a module named `0004_...` cannot be a plain import.)
    """
    import importlib

    from django.apps import apps as django_apps

    from mojo.apps.edge.models import BlocklistEntry

    module = importlib.import_module(
        "mojo.apps.edge.migrations.0004_blocklist_seed")
    BlocklistEntry.objects.filter(note=module.SEED_NOTE).delete()
    module.seed_blocklist(django_apps, None)
    return module.SEED_NOTE


def raises(func, *args, **kwargs):
    """Run `func`, return the exception it raised, or None.

    testit has no assertRaises; every call site asserts on the returned value
    with its own message, which keeps the failure output specific.
    """
    try:
        func(*args, **kwargs)
    except Exception as err:
        return err
    return None


_SENTINEL = object()


def with_setting(name, value, func):
    """Run `func` with a Django settings attribute overridden, then restore.

    For `settings.get_static` reads (file-only posture flags like
    EDGE_CONVERGE_ENABLED): the DM-015 pattern — a direct setattr on
    `django.conf.settings` with a try/finally restore, because
    `th.server_settings` would reload the wrong process for in-process
    service tests.
    """
    from django.conf import settings as dj_settings

    saved = getattr(dj_settings, name, _SENTINEL)
    setattr(dj_settings, name, value)
    try:
        return func()
    finally:
        if saved is _SENTINEL:
            delattr(dj_settings, name)
        else:
            setattr(dj_settings, name, saved)
