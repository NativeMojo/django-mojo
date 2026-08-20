"""Canonical same-origin authentication routes for Edge-hosted WebApps."""

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q

from mojo import errors as me
from mojo.helpers.settings import settings


# These are the root auth pages and auth-support APIs supplied by MojoAuth.
# Application APIs remain on their declared API origin.
AUTH_SUPPORT_API_PREFIXES = (
    "/api/auth",
    "/api/account",
    "/api/login",
    "/api/refresh_token",
)

# These decoys must be exact nginx locations. Prefix locations would capture
# legitimate SPA paths such as /signin/login and /signin/callback.
HONEYPOT_PATHS = ("/login", "/signin", "/signup")
CONTRACT_VERSION = 1


def _bouncer_path(name, default):
    from mojo.apps.edge import validators

    raw = str(settings.get_static(name, default) or default).strip().strip("/")
    path = f"/{raw}"
    return validators.validate_route_prefix(path)


def auth_route_prefixes():
    """Resolve file-backed bouncer pages plus the stable support APIs."""
    roots = (
        _bouncer_path("BOUNCER_LOGIN_PATH", "auth"),
        _bouncer_path("BOUNCER_REGISTER_PATH", "register"),
        _bouncer_path("BOUNCER_PASSKEY_PATH", "passkey"),
    )
    return tuple(dict.fromkeys(roots + AUTH_SUPPORT_API_PREFIXES))


def owning_app(vhost):
    """The WebApp this vhost serves, or None.

    A vhost belongs to an app one of two ways: it is the app's PRIMARY address
    (the `web_app` reverse of `WebApp.vhost`) or one of its ALIAS addresses
    (`Vhost.alias_of`). Both must render the identical auth contract — an alias
    without it would serve the SPA with no `/auth` route and no honeypots, so
    logging in on the custom domain would 404 while the platform address worked.
    """
    try:
        return vhost.web_app
    except ObjectDoesNotExist:
        pass
    return vhost.alias_of if vhost.alias_of_id else None


def _accessible_upstreams(vhost):
    from mojo.apps.edge.models import Upstream

    return Upstream.objects.filter(
        Q(group__isnull=True) | Q(group_id=vhost.domain.group_id),
        is_enabled=True,
    )


def _declared_upstream(vhost, value):
    from mojo.apps.edge.models import Upstream

    if isinstance(value, Upstream):
        upstream = value
    else:
        queryset = _accessible_upstreams(vhost)
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            upstream = queryset.filter(pk=int(raw)).first()
        else:
            matches = list(queryset.filter(name=raw)[:2])
            upstream = matches[0] if len(matches) == 1 else None
    if (upstream is None or not upstream.is_enabled or
            upstream.group_id not in (None, vhost.domain.group_id)):
        raise me.ValueException(
            "the WebApp auth upstream must be an enabled shared upstream or "
            "belong to the vhost's group")
    return upstream


def resolve_upstream(vhost, upstream=None):
    """Resolve the Django upstream without guessing across ambiguous APIs."""
    if upstream is not None:
        return _declared_upstream(vhost, upstream)

    login_path = _bouncer_path("BOUNCER_LOGIN_PATH", "auth")
    existing = vhost.routes.filter(path_prefix=login_path).select_related(
        "upstream").first()
    if existing is not None:
        return _declared_upstream(vhost, existing.upstream)

    configured = settings.get("EDGE_WEBAPP_AUTH_UPSTREAM", "")
    if configured not in (None, ""):
        return _declared_upstream(vhost, configured)

    from mojo.apps.edge.models import Vhost

    candidates = list(
        Vhost.objects.filter(
            kind="api", pool=vhost.pool, is_enabled=True,
            upstream__is_enabled=True,
        ).filter(
            Q(upstream__group__isnull=True) |
            Q(upstream__group_id=vhost.domain.group_id)
        ).select_related("upstream").order_by("upstream_id")
    )
    upstreams = {row.upstream_id: row.upstream for row in candidates}
    if len(upstreams) == 1:
        return next(iter(upstreams.values()))
    raise me.ValueException(
        "WebApp auth upstream is not unambiguous; pass --auth-upstream or "
        "set EDGE_WEBAPP_AUTH_UPSTREAM")


@transaction.atomic
def reconcile(vhost, upstream=None):
    """Idempotently install the hosted-auth prefix routes on one WebApp vhost."""
    from mojo.apps.edge.models import Vhost, VhostRoute

    vhost = Vhost.objects.select_for_update().select_related("domain").get(
        pk=vhost.pk)
    if vhost.kind not in ("site", "site_api"):
        raise me.ValueException(
            "hosted WebApp auth requires a site or site_api vhost")
    auth_upstream = resolve_upstream(vhost, upstream)

    if vhost.kind == "site":
        vhost.kind = "site_api"
        vhost.save(update_fields=["kind", "modified"])

    created = []
    for prefix in auth_route_prefixes():
        route = VhostRoute.objects.filter(
            vhost=vhost, path_prefix=prefix).select_related("upstream").first()
        if route is not None:
            if route.upstream_id != auth_upstream.pk:
                raise me.ValueException(
                    f"WebApp auth route {prefix} already uses another upstream")
            continue
        VhostRoute.objects.create(
            vhost=vhost, path_prefix=prefix, upstream=auth_upstream)
        created.append(prefix)
    return vhost, auth_upstream, created


def reconcile_all(publisher=None):
    """Heal every existing hosted WebApp during Edge runner startup.

    One ambiguous legacy app must not prevent other apps or pools from
    converging. New onboarding is strict and synchronous; this upgrade bridge
    records per-app failures and continues so every resolvable row heals.
    """
    from mojo.helpers import logit
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import convergence

    # Every serving address of every app — primaries AND aliases. An alias
    # healed to a different contract than its primary is exactly the split-brain
    # `owning_app` exists to prevent.
    vhosts = list(Vhost.objects.filter(
        Q(web_app__isnull=False) | Q(alias_of__isnull=False),
        is_enabled=True).select_related(
            "domain", "alias_of", "web_app").order_by("pk"))
    repaired = 0
    failures = []
    for vhost in vhosts:
        original_kind = vhost.kind
        web_app = owning_app(vhost)
        try:
            if publisher is None:
                _, _, created = reconcile(vhost)
            else:
                with convergence.publisher_scope(publisher):
                    _, _, created = reconcile(vhost)
            if created or original_kind != "site_api":
                repaired += 1
        except Exception as exc:
            failures.append(web_app.pk if web_app is not None else None)
            logit.error(
                f"edge: WebApp vhost {vhost.pk} hosted-auth reconciliation "
                f"failed ({type(exc).__name__}: {exc})")
    return {
        "checked": len(vhosts),
        "repaired": repaired,
        "failed": failures,
    }


def rendered_contract(vhost, routes=None):
    """Return renderer-owned exact routes for a WebApp auth vhost, if any."""
    if vhost.kind != "site_api":
        return None
    if owning_app(vhost) is None:
        return None
    rows = list(routes) if routes is not None else list(vhost.routes.all())
    login_path = _bouncer_path("BOUNCER_LOGIN_PATH", "auth")
    anchor = next((row for row in rows if row.path_prefix == login_path), None)
    if anchor is None:
        return None
    return {
        "version": CONTRACT_VERSION,
        "upstream": anchor.upstream,
        "honeypots": HONEYPOT_PATHS,
    }
