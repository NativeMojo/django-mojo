"""Shared fixtures for the edge tests.

Filename starts with `_` so testit skips it during discovery.

**The reserved-names fixture is load-bearing, not boilerplate.** The test
project ships `ALLOWED_HOSTS = ["*"]`, so `validators.reserved_server_names()`
returns None and every attempt to enable a vhost fails closed — which is the
designed behaviour, not a bug. `declare_reserved_names()` writes a DB-backed
`Setting`, which both this process and the live server read through
`settings.get`, so a test can model a correctly-configured deployment without a
server reload. `0_validators.py` deliberately clears it to prove the
fail-closed path.
"""

import uuid as _uuid


EDGE_PERMS = ["view_dns", "manage_dns"]

RESERVED_KEY = "EDGE_RESERVED_SERVER_NAMES"

# The hostname a tenant vhost must never be able to claim.
API_HOSTNAME = "api.edge-tests.internal"


def declare_reserved_names(names=None):
    """Give this deployment a name of its own, so vhosts may be enabled."""
    from mojo.apps.account.models.setting import Setting

    Setting.set(RESERVED_KEY, list(names or [API_HOSTNAME]), group=None)


def clear_reserved_names():
    from mojo.apps.account.models.setting import Setting

    Setting.remove(RESERVED_KEY, group=None)


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
    quiet_paths, serve_static, redirect_to, claims_reserved."""
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
    "default", "staging", "nginxreal", "reltest",
    "itesthappy", "itestexclude", "itesthouse",
]


def declare_pools(pools=None):
    from mojo.apps.account.models.setting import Setting

    Setting.set("EDGE_POOLS", list(pools or TEST_POOLS), group=None)


def declare_release_buckets(buckets=None):
    """Declare the release buckets. Registering a site fails closed without it."""
    from mojo.apps.account.models.setting import Setting

    Setting.set("EDGE_RELEASE_BUCKETS", list(buckets or [RELEASE_BUCKET]),
                group=None)


def make_webapp(group, slug=None, vhost=None, bucket=None, auto_promote=False):
    from mojo.apps.edge.models import WebApp

    if slug is None:
        slug = f"app{_uuid.uuid4().hex[:8]}"
    web_app = WebApp(
        group=group, slug=slug, vhost=vhost,
        bucket=bucket or RELEASE_BUCKET, prefix="pending",
        auto_promote=auto_promote)
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
        Upstream, Vhost, VhostRoute, WebApp, WebAppRelease,
    )

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
