"""How one WebApp is served: its address, its certificate, its shape, its paths.

The app page's Serving tab is one read (`serving_for`) and four writes. Every
one of them is expressed in terms of the APP, never of a vhost — a WebApp
answers on its primary address (`WebApp.vhost`) and on any number of ALIAS
addresses (`Vhost.alias_of`), and a serving change that landed on one but not
the others is exactly the split-brain `webapp_auth_routes.owning_app` exists to
prevent. So `apply`, `add_route` and `remove_route` each touch the primary AND
every alias, in one transaction.

Two derived facts carry the whole design:

- **Managed routes are DERIVED, never stored.** A route is "managed for you"
  when its prefix is in `managed_prefixes()` — the resolved hosted-auth
  contract. Storing a flag on `VhostRoute` would let the two disagree the
  moment a bouncer path setting changes, and the stored answer would win.
- **Fleet inventory is a WRITER's fact.** `pools` and `upstreams` are the
  deployment's own topology (which node pools exist, which upstreams an
  ancestor org declared). A viewer gets `null` for both; only a caller who
  could actually save them is told what the choices are.
"""

from django.db import transaction
from django.utils import timezone

from mojo import errors as me
from mojo.apps.account.services import webapp_authority


# The certificate profile delegated ACME v1 is limited to. A domain issuing
# through delegation (or one whose DNS lives elsewhere entirely) can only ever
# hold the apex+wildcard certificate, so "a certificate just for this app" is
# not a thing that can be requested there — see certs._require_delegated_profile.
SHARED_ONLY_REASON = (
    "HTTPS for this domain is issued as one certificate covering the whole "
    "domain, so a separate one just for this app isn’t available here.")

SPA_POLICY_REFUSAL = (
    "This app has a security policy tied to its current mode — update the "
    "policy first, then change this.")


def managed_prefixes():
    """The paths the platform serves for this app and nobody edits by hand."""
    from mojo.apps.edge.services import webapp_auth_routes

    return tuple(webapp_auth_routes.auth_route_prefixes())


def _flag(value):
    """Strict truthiness — `bool("false")` is True and the browser sends strings."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _primary(web_app):
    from mojo.apps.edge.models import Vhost

    if not web_app.vhost_id:
        return None
    return Vhost.objects.select_related("domain", "certificate").filter(
        pk=web_app.vhost_id).first()


def _aliases(web_app):
    from mojo.apps.edge.models import Vhost

    return list(Vhost.objects.filter(alias_of=web_app).select_related(
        "domain", "certificate").order_by("pk"))


def _require_primary(web_app):
    primary = _primary(web_app)
    if primary is None:
        raise me.ValueException(
            "Give this app an address first, then change how it’s served.")
    return primary


def dedicated_support(domain):
    """Whether this app can hold a certificate of its own, and why not.

    Delegated ACME and platform-external DNS both route through
    `certs._require_delegated_profile`, which allows exactly the apex plus
    wildcard profile — an exact-name request there is refused by the issuer,
    so offering the button would be offering a dead end.
    """
    from mojo.apps.dnsman.models.domain import PROVIDER_MOJO
    from mojo.apps.dnsman.services import delegation

    if domain is None:
        return False, SHARED_ONLY_REASON
    if domain.provider == PROVIDER_MOJO:
        return False, SHARED_ONLY_REASON
    if delegation.for_domain(domain) is not None:
        return False, SHARED_ONLY_REASON
    return True, None


def _exact_name_certificate(domain, hostname):
    """The newest certificate issued for THIS app's name alone, or None."""
    from mojo.apps.dnsman.models import Certificate

    if domain is None or not hostname:
        return None
    for candidate in Certificate.objects.filter(
            domain=domain).order_by("-pk"):
        if sorted(candidate.sans or []) == [hostname]:
            return candidate
    return None


def _certificate_payload(certificate, hostname, domain):
    from mojo.apps.edge import validators

    if certificate is None:
        supported, reason = dedicated_support(domain)
        return {
            "id": None, "common_name": None, "sans": [], "status": None,
            "not_after": None, "renew_after": None, "days_remaining": None,
            "wildcard": False, "dedicated": None,
            "dedicated_supported": supported, "dedicated_reason": reason,
        }
    names = list(certificate.sans or [])
    wildcard = any(str(name).startswith("*.") for name in names) or \
        str(certificate.common_name or "").startswith("*.")
    days = None
    if certificate.not_after is not None:
        days = (certificate.not_after - timezone.now()).days
    dedicated = _exact_name_certificate(domain, hostname)
    supported, reason = dedicated_support(domain)
    return {
        "id": certificate.pk,
        "common_name": certificate.common_name,
        "sans": names,
        "status": certificate.status,
        "not_after": (certificate.not_after.isoformat()
                      if certificate.not_after else None),
        "renew_after": (certificate.renew_after.isoformat()
                        if certificate.renew_after else None),
        "days_remaining": days,
        "wildcard": wildcard,
        "dedicated": ({
            "id": dedicated.pk,
            "status": dedicated.status,
            # Ready means "switching to it would be accepted": active, and it
            # really covers the name. Anything else is still in flight.
            "ready": (dedicated.status == "active" and
                      validators.certificate_covers(dedicated, hostname)),
        } if dedicated is not None else None),
        "dedicated_supported": supported,
        "dedicated_reason": reason,
    }


def _route_rows(vhost):
    managed = set(managed_prefixes())
    if vhost is None:
        return []
    rows = []
    for route in vhost.routes.select_related("upstream").order_by("path_prefix"):
        rows.append({
            "id": route.pk,
            "path_prefix": route.path_prefix,
            "upstream": ({"id": route.upstream_id,
                          "name": route.upstream.name}
                         if route.upstream_id else None),
            "managed": route.path_prefix in managed,
        })
    return rows


def serving_for(web_app, include_editables=False):
    """Everything the Serving tab shows, from server evidence only.

    `include_editables` is the caller's WRITE authority, decided by the REST
    layer. It gates `serving.pools` and `upstreams` — the deployment's node
    pool inventory and the upstream names an ancestor org declared are
    operator topology, not something a read-only viewer of one app is owed.
    """
    from mojo.apps.edge import validators
    from mojo.apps.edge.models.vhost import KIND_SITE_API
    from mojo.apps.edge.services import webapp_alias, webapp_auth_routes

    primary = _primary(web_app)
    domain = primary.domain if primary is not None else None
    hostname = primary.server_name if primary is not None else None
    certificate = (primary.certificate
                   if primary is not None and primary.certificate_id else None)

    wildcard_name = f"*.{domain.name}" if domain is not None else None
    wildcard_covered = bool(
        certificate is not None and wildcard_name and
        validators.certificate_covers(certificate, wildcard_name))

    pools = None
    upstreams = None
    if include_editables:
        pools = list(validators.declared_pools())
        upstreams = []
        if primary is not None:
            upstreams = [
                {"id": row.pk, "name": row.name}
                for row in webapp_auth_routes._accessible_upstreams(
                    primary).order_by("name")
            ]

    return {
        "schema_version": 1,
        "webapp": {"id": web_app.pk, "slug": web_app.slug,
                   "display_name": web_app.display_name},
        "address": {
            "hostname": hostname,
            "https_origin": f"https://{hostname}" if hostname else None,
            "domain": ({"id": domain.pk, "name": domain.name,
                        "provider": domain.provider} if domain else None),
            "dns": webapp_alias._dns_mode(domain) if domain else None,
            "wildcard": {"covered": wildcard_covered, "name": wildcard_name},
        },
        "certificate": _certificate_payload(certificate, hostname, domain),
        "serving": {
            "kind": primary.kind if primary is not None else None,
            "pool": primary.pool if primary is not None else None,
            "pools": pools,
            "spa": bool(primary.spa) if primary is not None else None,
            "routes_supported": (primary is not None and
                                 primary.kind == KIND_SITE_API),
        },
        "routes": _route_rows(primary),
        "upstreams": upstreams,
        "aliases": [{"vhost": alias.pk, "hostname": alias.server_name}
                    for alias in _aliases(web_app)],
    }


def _resolve_certificate(primary, value):
    """A certificate this app may actually serve: its domain's, active, covering."""
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.edge import validators

    certificate = Certificate.objects.filter(
        pk=value, domain_id=primary.domain_id).first()
    if certificate is None:
        raise me.ValueException(
            "That certificate isn’t one of this address’s certificates.")
    if certificate.status != "active":
        raise me.ValueException(
            "That certificate isn’t ready yet. Wait for it to finish, then "
            "switch to it.")
    validators.validate_certificate_covers(
        certificate, primary.domain_id, primary.server_name)
    return certificate


def apply(web_app, data):
    """Change how this app is served, on every address it answers on.

    Only `pool`, `spa` and `certificate` are read. `kind` is deliberately NOT
    settable: the shape a vhost has decides which knobs the renderer has a
    branch for, and changing it under a live app is a delete-and-recreate, not
    a toggle.
    """
    from mojo.apps.edge import validators
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.models.vhost import KIND_SITE

    primary = _require_primary(web_app)

    pool = data.get("pool")
    pool = str(pool).strip() if pool not in (None, "") else None
    if pool is not None:
        # Refused BEFORE the transaction so an undeclared pool never reaches a
        # save and never publishes convergence for a pool nobody serves.
        validators.validate_pool(pool)

    raw_spa = data.get("spa")
    spa = None if raw_spa is None else _flag(raw_spa)

    certificate = None
    if data.get("certificate") not in (None, ""):
        certificate = _resolve_certificate(primary, data.get("certificate"))

    if pool is None and spa is None and certificate is None:
        return serving_for(web_app, include_editables=True)

    with transaction.atomic():
        locked = Vhost.objects.select_for_update().get(pk=primary.pk)
        aliases = list(Vhost.objects.select_for_update().filter(
            alias_of=web_app).order_by("pk"))

        if spa is not None:
            for vhost in [locked] + aliases:
                # A `site` vhost's mojosec policy pins its expected response
                # class to spa_fallback OR static_site, so flipping spa makes
                # validate_vhost raise deep inside save(). Say the plain thing
                # instead of surfacing a renderer contract error.
                if (vhost.kind == KIND_SITE and vhost.mojosec_policy and
                        bool(vhost.spa) != spa):
                    raise me.ValueException(SPA_POLICY_REFUSAL)

        # PRIMARY FIRST, always. `validate_vhost_alias` re-reads the primary's
        # pool on every alias save and refuses an alias whose pool differs, so
        # saving an alias into the new pool before the primary moved would be
        # refused by the app's own invariant.
        if pool is not None:
            locked.pool = pool
        if spa is not None:
            locked.spa = spa
        if certificate is not None:
            locked.certificate = certificate
        locked.save()

        for alias in aliases:
            changed = False
            if pool is not None and alias.pool != pool:
                alias.pool = pool
                changed = True
            if spa is not None and bool(alias.spa) != spa:
                alias.spa = spa
                changed = True
            # `certificate` is primary-only: an alias serves a different name
            # and holds whatever certificate covers IT.
            if changed:
                alias.save()

    return serving_for(web_app, include_editables=True)


def request_dedicated_certificate(web_app, actor):
    """Ask for a certificate covering this app's address alone.

    Two-phase on purpose: this only REQUESTS. Switching the app onto the new
    certificate is a separate `apply(certificate=...)` once it is active and
    really covers the name — a vhost pointed at a pending row would serve
    nothing.
    """
    from mojo.apps.dnsman.services import certs

    primary = _require_primary(web_app)
    domain = primary.domain
    hostname = primary.server_name

    if domain.group_id != web_app.group_id:
        # Reading an ancestor's domain is the inheritance contract; WRITING to
        # it — and an ACME order IS a write against that zone — needs authority
        # in the group that OWNS it. Checked before any provider action, the
        # same gate `webapp_alias.attach` applies.
        if not webapp_authority.can_manage_group_webapps(actor, domain.group):
            raise me.PermissionDeniedException(
                f"Managing DNS for {domain.name} requires access in the "
                "workspace that owns it")

    supported, reason = dedicated_support(domain)
    if not supported:
        raise me.ValueException(reason)

    # Press-twice safety. `certs.request_certificate` RAISES on an active row
    # that is not due for renewal, so a second click on a finished request
    # would report an error over a perfectly good certificate.
    existing = _exact_name_certificate(domain, hostname)
    if existing is not None and existing.status in ("pending", "issuing", "active"):
        return existing
    return certs.request_certificate(domain, names=[hostname])


def _require_routes(primary):
    from mojo.apps.edge.models.vhost import KIND_SITE_API

    if primary.kind != KIND_SITE_API:
        raise me.ValueException(
            "Every path on this app is served straight from your build, so "
            "there are no paths to send elsewhere.")
    return primary


def _resolve_upstream(primary, value):
    from mojo.apps.edge.services import webapp_auth_routes

    raw = str(value or "").strip()
    if not raw:
        raise me.ValueException("Choose where this path should go.")
    queryset = webapp_auth_routes._accessible_upstreams(primary)
    if raw.isdigit():
        upstream = queryset.filter(pk=int(raw)).first()
    else:
        matches = list(queryset.filter(name=raw)[:2])
        upstream = matches[0] if len(matches) == 1 else None
    if upstream is None:
        raise me.ValueException(
            "That destination isn’t one this app can send traffic to.")
    return upstream


def _clean_prefix(path_prefix):
    from mojo.apps.edge import validators

    prefix = str(path_prefix or "").strip()
    if not prefix.startswith("/"):
        prefix = f"/{prefix}"
    prefix = prefix.rstrip("/") or "/"
    validators.validate_route_prefix(prefix)
    if prefix in managed_prefixes():
        raise me.ValueException(
            f"{prefix} is handled for you — sign-in and account paths can’t "
            f"be changed here.")
    return prefix


def add_route(web_app, path_prefix, upstream):
    """Send one path to a declared destination, on every address this app has."""
    from mojo.apps.edge.models import Vhost, VhostRoute

    primary = _require_routes(_require_primary(web_app))
    prefix = _clean_prefix(path_prefix)
    destination = _resolve_upstream(primary, upstream)

    with transaction.atomic():
        locked = Vhost.objects.select_for_update().get(pk=primary.pk)
        targets = [locked] + list(Vhost.objects.select_for_update().filter(
            alias_of=web_app).order_by("pk"))
        for vhost in targets:
            existing = VhostRoute.objects.filter(
                vhost=vhost, path_prefix=prefix).first()
            if existing is not None:
                if existing.upstream_id != destination.pk:
                    raise me.ValueException(
                        f"{prefix} already goes somewhere else. Remove it "
                        f"first, then add it again.")
                continue
            VhostRoute.objects.create(
                vhost=vhost, path_prefix=prefix, upstream=destination)
    return serving_for(web_app, include_editables=True)


def remove_route(web_app, path_prefix):
    """Stop sending one path elsewhere, on every address this app has."""
    from mojo.apps.edge.models import Vhost, VhostRoute

    primary = _require_routes(_require_primary(web_app))
    prefix = _clean_prefix(path_prefix)

    with transaction.atomic():
        locked = Vhost.objects.select_for_update().get(pk=primary.pk)
        targets = [locked] + list(Vhost.objects.select_for_update().filter(
            alias_of=web_app).order_by("pk"))
        removed = 0
        for vhost in targets:
            # One at a time, never a queryset delete: VhostRoute.delete()
            # publishes fleet convergence for the pool it belonged to.
            for route in VhostRoute.objects.filter(
                    vhost=vhost, path_prefix=prefix):
                route.delete()
                removed += 1
        if not removed:
            raise me.ValueException(f"{prefix} isn’t set up on this app.")
    return serving_for(web_app, include_editables=True)
