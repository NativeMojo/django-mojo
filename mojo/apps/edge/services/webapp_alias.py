"""Extra custom-domain addresses ("aliases") for one WebApp.

A WebApp keeps exactly one PRIMARY address — `WebApp.vhost`, the one onboarding
mints and the one every existing surface (deploy, health, summary, take-offline)
already reasons about. An alias is one more `Vhost` for the same app, marked by
`Vhost.alias_of`, so `www.customer.com` and the app's platform address serve the
identical release from the identical release bytes.

`attach()` is the whole flow and it is **re-enterable**: the UI's Check button
calls it again, and every call makes at most one provider write. It never raises
for a state the user can fix — those come back as a status plus a plain-language
`reason` (and, when the user has records to publish, a complete `records` list).
A refusal — an address that cannot ever work here, or one that belongs to
someone else — is an exception, because retrying it will never help.

Statuses:

- ``needs_domain``          no Domain here covers that hostname; connect it first
- ``records_needed``        external DNS; the user must publish these records
- ``certificate_pending``   issuance is in flight; check again shortly
- ``certificate_failed``    issuance failed; repair the records, then retry
- ``attached``              the alias is live (or already was — idempotent)

`preview()` is the same question asked without doing anything: it runs
`_resolve_target` — attach's own pre-write gates — and reports `ready` (with
who publishes the DNS), `needs_domain`, or `unusable`. No provider read, no
probe, no certificate, no write, so the dialog can call it on every keystroke.

**Why `needs_domain` never delegates.** There is no public-suffix list in this
repo, so "the registrable parent of `shop.customer.co.uk`" is a guess, and
`delegation.initiate` on a guessed name delegates ACME control of the wrong
zone. Connecting a domain is its own deliberate flow on the Domains page.
"""

from django.db import transaction
from django.utils import timezone

from objict import objict

from mojo import errors as me
from mojo.apps.account.services import webapp_authority


def _domain_for(web_app, hostname):
    """Longest-suffix match over the domains this app's group may serve under.

    Own group and ancestors only — the same rule as onboarding's precheck and
    address step, so an alias can never be hung off a sibling's or the house's
    domain.
    """
    from mojo.apps.edge.services import webapp_apps_domain

    matched = None
    for candidate in webapp_apps_domain.owned_by_group_or_ancestor(
            web_app.group).filter(status="active", verified=True):
        base = candidate.name
        if hostname == base or hostname.endswith(f".{base}"):
            if matched is None or len(base) > len(matched.name):
                matched = candidate
    return matched


def _needs_domain_reason(hostname):
    """The one sentence a hostname no Domain here covers gets told.

    Written once so `attach()` and `preview()` cannot drift into telling the
    same person two different things about the same address.
    """
    return (f"{hostname} isn't connected here yet. Connect it on the "
            f"Domains page, then add this address.")


def _resolve_target(web_app, hostname, actor):
    """Every gate `attach()` clears BEFORE it touches a provider or the db.

    Extracted verbatim, in its original order, so the pre-submit `preview()`
    below reaches its verdict through the identical code rather than a second
    implementation that agrees today and drifts tomorrow. Raises exactly what
    attach raised: `ValueException` for an address that can never work here,
    `PermissionDeniedException` for one that belongs to a workspace the actor
    has no authority in.

    Returns `objict(domain, label, hostname)` with the hostname normalized.
    `domain` is None when no Domain here covers the name — the caller turns
    that into the needs_domain steer.
    """
    from mojo.apps.dnsman.services import naming
    from mojo.apps.edge import validators

    primary = web_app.vhost if web_app.vhost_id else None
    if primary is None:
        raise me.ValueException(
            "Give this app its own address first, then add a custom one.")

    raw = str(hostname or "").strip().lower().rstrip(".")
    if raw.startswith("*"):
        raise me.ValueException(
            "A wildcard address can't be added; use one exact address.")
    hostname = naming.normalize_domain(raw)

    domain = _domain_for(web_app, hostname)
    if domain is None:
        return objict(domain=None, label=None, hostname=hostname)

    if hostname == domain.name:
        raise me.ValueException(
            f"The bare domain can't point here. Use www.{domain.name} instead.")
    label = hostname[: -(len(domain.name) + 1)]
    if "." in label:
        # One label keeps every alias inside the apex+wildcard certificate the
        # domain already has. A deeper name would need its own certificate.
        raise me.ValueException(
            f"Use one label under {domain.name}, like "
            f"{label.split('.')[-1]}.{domain.name}.")
    validators.validate_label(label)

    if domain.group_id != web_app.group_id:
        # Reading an ancestor's domain is the inheritance contract; WRITING to
        # it (this CNAME, a certificate request) needs authority in the group
        # that OWNS it. Checked before any provider read or write.
        if not webapp_authority.can_manage_group_webapps(actor, domain.group):
            raise me.PermissionDeniedException(
                f"Managing DNS for {domain.name} requires access in the "
                "workspace that owns it")

    return objict(domain=domain, label=label, hostname=hostname)


def preview(web_app, hostname, actor):
    """What adding this address WOULD do, decided without doing any of it.

    The add-an-address dialog calls this while someone is still typing, so it
    must be free: no provider read, no DNS probe, no certificate lookup, no
    write. Everything it reports comes from `_resolve_target` — the same gates
    the write runs, so the answer cannot disagree with what Add then does.

    Occupancy is deliberately NOT reported. "That address is already serving
    something else" is a fact about another tenant's vhost, and a keystroke-fast
    endpoint that answered it would be a probe for which names are taken.

    Only `ValueException` is caught. An authorization denial is the same denial
    the write raises — it propagates, so the preview 403s exactly like Add and
    the security incident still fires.
    """
    try:
        resolved = _resolve_target(web_app, hostname, actor)
    except me.ValueException as err:
        return objict(
            status="unusable",
            hostname=str(hostname or "").strip().lower().rstrip("."),
            reason=str(err))
    if resolved.domain is None:
        return objict(status="needs_domain", hostname=resolved.hostname,
                      reason=_needs_domain_reason(resolved.hostname))
    return objict(
        status="ready", hostname=resolved.hostname,
        dns=_dns_mode(resolved.domain),
        domain={"id": resolved.domain.pk, "name": resolved.domain.name})


def _covering_certificate(domain, hostname):
    """An ACTIVE certificate covering `hostname` and not due for renewal."""
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.models.certificate import STATUS_ACTIVE
    from mojo.apps.edge import validators

    now = timezone.now()
    for candidate in Certificate.objects.filter(
            domain=domain, status=STATUS_ACTIVE):
        if not validators.certificate_covers(candidate, hostname):
            continue
        if candidate.renew_after is not None and candidate.renew_after <= now:
            continue
        return candidate
    return None


def _in_flight_certificate(domain, hostname):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.edge import validators

    for candidate in Certificate.objects.filter(
            domain=domain, status__in=("pending", "issuing")):
        if validators.certificate_covers(candidate, hostname):
            return candidate
    return None


def _latest_profile_certificate(domain):
    """The newest certificate for the domain's apex+wildcard profile.

    `certs.request_certificate` reuses a row only when it is pending, issuing or
    active — a FAILED row matches nothing, so an auto-retrying caller would mint
    a brand-new ACME order on every click. This is what the caller checks before
    deciding to request again.
    """
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import certs

    names = sorted(certs.normalize_names(domain, None))
    for candidate in Certificate.objects.filter(domain=domain).order_by("-pk"):
        if sorted(candidate.sans or []) == names:
            return candidate
    return None


def _repair_records(hostname, target, domain):
    """The records a user republishes when delegated issuance failed."""
    from mojo.apps.edge.services import webapp_onboarding

    return webapp_onboarding._external_records(
        hostname, target, domain, include_challenge=True)


def _needs_challenge_record(domain):
    """Whether the ACME delegation is missing or broken for this domain."""
    from mojo.apps.dnsman.models.acme_delegation import STATE_BROKEN
    from mojo.apps.dnsman.services import delegation

    row = delegation.for_domain(domain)
    return row is None or row.state == STATE_BROKEN


def _dns_mode(domain):
    """Who publishes this name's DNS: the PLATFORM, or the customer.

    Every attach result carries it so the UI can say "we handle the records and
    HTTPS for you" from server evidence instead of guessing from the hostname.
    """
    return "external" if domain.provider == "mojo" else "managed"


def _record_values(record):
    return sorted(str(value).rstrip(".") for value in (
        getattr(record, "record_values", None) or
        (record.get("record_values") if isinstance(record, dict) else []) or []))


def _record_name(record):
    return str(getattr(record, "name", record.get("name", ""))).rstrip(".")


def _record_type(record):
    return str(getattr(record, "type", record.get("type", ""))).upper()


def _write_managed_cname(domain, hostname, target):
    """Mirror the onboarding address step's managed branch, exactly.

    Returns True when a record was written. A `*.{domain}` CNAME already at the
    destination routes this name too, so writing a per-host record would be a
    no-op in resolution and clutter in the zone.
    """
    from mojo.apps.dnsman.services import dns

    records = dns.list_records(domain)
    same_name = [row for row in records if _record_name(row) == hostname]
    cnames = [row for row in same_name if _record_type(row) == "CNAME"]
    conflicts = [row for row in same_name if row not in cnames]
    if conflicts or len(cnames) > 1:
        raise me.ValueException(
            f"{hostname} already has other DNS records. Remove them and try "
            f"again.")
    if cnames:
        if _record_values(cnames[0]) != [target]:
            raise me.ValueException(
                f"{hostname} already points somewhere else. Point it here (or "
                f"remove the record) and try again.")
        return False
    wildcard_covers = any(
        _record_name(row) == f"*.{domain.name}" and
        _record_type(row) == "CNAME" and _record_values(row) == [target]
        for row in records)
    if wildcard_covers:
        return False
    dns.upsert_record(domain, "CNAME", hostname, [target], ttl=300)
    return True


def _reconcile_routes(web_app, alias):
    """Make an alias serve the primary's complete route contract.

    Hosted-auth routes are first proven on the primary, and that proven
    upstream is passed explicitly to the alias. Application routes then copy
    from primary rows rather than being re-resolved from configuration. A
    missing row is repaired; any extra route, duplicate logical identity, or
    different destination is refused without rewriting it.
    """
    from mojo.apps.edge.models import Vhost, VhostRoute
    from mojo.apps.edge.services import webapp_auth_routes, webapp_serving

    with transaction.atomic():
        primary = Vhost.objects.select_for_update().select_related(
            "domain").get(pk=web_app.vhost_id)
        alias = Vhost.objects.select_for_update().select_related(
            "domain").get(pk=alias.pk, alias_of=web_app)

        # Heal a lone trailing-slash spelling before the auth reconciler looks
        # for exact canonical prefixes. Duplicate logical rows stay a refusal.
        webapp_serving._canonical_route_contract(primary, lock=True)
        primary, auth_upstream, _ = webapp_auth_routes.reconcile(primary)
        source = webapp_serving._canonical_route_contract(primary, lock=True)
        target = webapp_serving._canonical_route_contract(alias, lock=True)

        extras = sorted(set(target) - set(source))
        if extras:
            raise me.ValueException(
                f"The alias has a route the primary does not: {extras[0]}")
        for prefix, route in target.items():
            if route.upstream_id != source[prefix].upstream_id:
                raise me.ValueException(
                    f"Alias route {prefix} already uses another upstream")

        alias, _, _ = webapp_auth_routes.reconcile(
            alias, upstream=auth_upstream)
        target = webapp_serving._canonical_route_contract(alias, lock=True)
        for prefix, source_route in source.items():
            if prefix in target:
                continue
            VhostRoute.objects.create(
                vhost=alias, path_prefix=prefix,
                upstream=source_route.upstream)
        return alias


def attach(web_app, hostname, actor, retry_certificate=False):
    """Point one more address at this app. Safe to call again at any point."""
    from mojo.apps.dnsman.models.domain import PROVIDER_MOJO
    from mojo.apps.dnsman.services import certs
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import webapp_destination, webapp_onboarding

    # Every pre-write gate, in its original order, shared with preview().
    resolved = _resolve_target(web_app, hostname, actor)
    hostname = resolved.hostname
    domain = resolved.domain
    if domain is None:
        return objict(status="needs_domain", hostname=hostname,
                      reason=_needs_domain_reason(hostname))
    label = resolved.label
    # `_resolve_target` already refused an app with no address of its own, so
    # the FK is set — and its own read left it cached on the instance.
    primary = web_app.vhost

    # Every row at this name, ENABLED OR NOT. Scanning only enabled rows
    # matched the partial unique constraint but missed a PARKED one — another
    # app's disabled address, or one an admin took down. Attach would then mint
    # a second (enabled) row for the same name, and the parked row's owner
    # could never re-enable it: that write hits the constraint as an
    # IntegrityError with nothing left to do about it.
    rows = list(Vhost.objects.filter(domain=domain, label=label)
                .select_related("certificate")
                .order_by("-is_enabled", "pk"))
    ours = [row for row in rows if row.alias_of_id == web_app.pk]
    if len(ours) != len(rows):
        # Anything here that is not already this app's own alias — another
        # app's primary, another app's alias, an admin-created vhost of any
        # kind, enabled or parked — is the same plain refusal, so the
        # enabled-name uniqueness constraint is never reached as an
        # IntegrityError.
        raise me.ValueException(
            f"{hostname} is already serving something else here.")
    existing = ours[0] if ours else None
    if existing is not None and existing.is_enabled:
        # Already ours and live: report success without touching DNS again.
        # This is what makes the Check button free to press. Reconcile anyway —
        # it is idempotent, and it is what REPAIRS an alias whose routes never
        # landed (see the create+reconcile transaction below). Without it a
        # half-configured alias would report `attached` forever while serving
        # the SPA with no /auth routes and no honeypots.
        _reconcile_routes(web_app, existing)
        return objict(status="attached", hostname=hostname,
                      vhost=existing.pk, domain=domain.pk,
                      dns=_dns_mode(domain), created=False)
    # `existing` is now either None, or this app's own PARKED alias row. A
    # parked one is revived below rather than duplicated — but only after the
    # DNS and certificate gates below have passed, exactly as a fresh one is.

    destination = webapp_destination.resolve(hostname)
    target = destination.value

    if domain.provider == PROVIDER_MOJO:
        # External DNS: the platform holds no credential for this zone, so the
        # user publishes the record and we confirm it authoritatively.
        if not webapp_onboarding._verify_external_cname(hostname, target):
            return objict(
                status="records_needed", hostname=hostname, domain=domain.pk,
                dns=_dns_mode(domain),
                reason="Add this record at your DNS host, then check again.",
                records=webapp_onboarding._external_records(
                    hostname, target, domain,
                    include_challenge=_needs_challenge_record(domain)))
    else:
        _write_managed_cname(domain, hostname, target)

    certificate = _covering_certificate(domain, hostname)
    if certificate is None:
        pending = _in_flight_certificate(domain, hostname)
        if pending is not None:
            return objict(
                status="certificate_pending", hostname=hostname,
                domain=domain.pk, certificate=pending.pk,
                dns=_dns_mode(domain),
                reason="The security certificate is still being issued. "
                       "Check again in a few minutes.")
        latest = _latest_profile_certificate(domain)
        if latest is not None and latest.status == "failed" and not retry_certificate:
            # No automatic re-request. request_certificate's reuse scan does
            # not match a failed row, so an auto-retrying Check would mint a
            # fresh ACME order every click — rate limits and all.
            return objict(
                status="certificate_failed", hostname=hostname,
                domain=domain.pk, certificate=latest.pk,
                dns=_dns_mode(domain),
                reason="The security certificate could not be issued. Check "
                       "these records, then try again.",
                records=_repair_records(hostname, target, domain))
        requested = certs.request_certificate(domain)
        return objict(
            status="certificate_pending", hostname=hostname,
            domain=domain.pk, certificate=requested.pk,
            dns=_dns_mode(domain),
            reason="The security certificate has been requested. Check again "
                   "in a few minutes.")

    # ONE transaction. The alias row and its auth routes are a single unit: a
    # committed row whose reconcile raised would converge to the fleet as a
    # custom domain serving the SPA with no /auth routes and no honeypots, and
    # the next Check would take the "already ours" path above and report
    # `attached` over the top of it.
    with transaction.atomic():
        if existing is not None:
            # This app's own parked address, revived — never a duplicate row.
            existing.certificate = certificate
            existing.pool = primary.pool
            existing.spa = True
            existing.is_enabled = True
            existing.save()
            vhost = existing
        else:
            vhost = Vhost.objects.create(
                domain=domain, label=label, kind="site_api",
                certificate=certificate, pool=primary.pool, spa=True,
                alias_of=web_app)
        vhost = _reconcile_routes(web_app, vhost)
    return objict(status="attached", hostname=hostname, vhost=vhost.pk,
                  domain=domain.pk, certificate=certificate.pk,
                  dns=_dns_mode(domain), created=existing is None)


def detach(web_app, vhost):
    """Remove one alias address. The certificate and the app are untouched."""
    if web_app.vhost_id == vhost.pk:
        raise me.ValueException(
            "That is this app's own address; take the site offline instead.")
    if vhost.alias_of_id != web_app.pk:
        raise me.ValueException("That address does not belong to this app.")
    hostname = vhost.server_name
    # Vhost.delete() publishes fleet convergence on commit, so nodes drop the
    # server block without waiting for the sweep.
    vhost.delete()
    return objict(status="detached", hostname=hostname)


def _address_row(vhost, role):
    certificate = vhost.certificate if vhost.certificate_id else None
    return {
        "role": role,
        "vhost": vhost.pk,
        "hostname": vhost.server_name,
        "domain": {"id": vhost.domain_id, "name": vhost.domain.name,
                   "provider": vhost.domain.provider},
        # Whether the PLATFORM writes this name's DNS, or the customer does.
        # A live per-name lookup would be a provider round trip per row, which
        # a status list must not spend.
        "dns": _dns_mode(vhost.domain),
        "enabled": vhost.is_enabled,
        "certificate": ({
            "status": certificate.status,
            "not_after": (certificate.not_after.isoformat()
                          if certificate.not_after else None),
        } if certificate else None),
    }


def status_rows(web_app):
    """Every address this app answers on: the primary first, then aliases."""
    from mojo.apps.edge.models import Vhost

    rows = []
    if web_app.vhost_id:
        primary = Vhost.objects.select_related(
            "domain", "certificate").filter(pk=web_app.vhost_id).first()
        if primary is not None:
            rows.append(_address_row(primary, "primary"))
    for alias in Vhost.objects.filter(alias_of=web_app).select_related(
            "domain", "certificate").order_by("pk"):
        rows.append(_address_row(alias, "alias"))
    return rows
