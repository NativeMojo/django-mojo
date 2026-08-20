"""The workspace's "apps domain" — where new WebApps can go live instantly.

A group (or one of its ancestors) that owns a writable, verified domain can
serve every new app under it with ZERO per-app DNS work: one ``*.{domain}``
CNAME at the serving destination plus one apex+wildcard certificate cover any
subdomain the wizard picks. This module answers two questions:

- ``resolve(group)`` — which domain is that, for this group? Ancestor-aware
  (a team's child group inherits the organization's domain; siblings never
  see each other's), and preferring the domain the platform's own address
  sits under when several qualify.
- ``converge(domain)`` — make the wildcard record and certificate exist,
  idempotently. Called lazily from the onboarding address step and available
  to Setup, so the first app pays the one-time cost and every later app is
  covered before it is even named.

``owned_by_group_or_ancestor`` is also the shared ownership rule the
onboarding pipeline (precheck suffix match, address-choice validation) uses so
a child group may onboard under an ancestor's domain — ancestors only, never
siblings or another branch's descendants, mirroring
``validators._group_at_or_below``.
"""

from urllib.parse import urlsplit

from objict import objict

from mojo.helpers import logit


NO_DOMAIN_REASON = "This workspace has no domain managed here yet."
AMBIGUOUS_REASON = (
    "This workspace has more than one domain; choose the app's address "
    "under one of them.")


def _base_url():
    """The platform's public origin (``BASE_URL``), read through one seam.

    Isolated so tests patch this instead of writing the shared ``Setting``
    row — same reasoning as ``webapp_destination._base_url``.
    """
    from mojo.apps.account.services import system_settings

    return str(system_settings.get_value(system_settings.BASE_URL) or "").strip()


def _ancestor_ids(group):
    """The group's own pk plus its ancestors', depth-capped.

    Walks the chain like ``validators._group_at_or_below`` (no
    ``Group.get_parents()`` — that helper has no cycle guard).
    """
    ids = []
    current = group
    depth = 0
    while current is not None and depth <= 8:
        ids.append(current.pk)
        current = current.parent
        depth += 1
    return ids


def owned_by_group_or_ancestor(group):
    """Domains owned by ``group`` itself or any of its ancestors.

    Ancestors ONLY: never siblings, never another branch's descendants. No
    status filter — callers add their own, so a choice naming an inactive own
    domain still resolves to "wait", not "outside this group".
    """
    from mojo.apps.dnsman.models import Domain

    if group is None:
        return Domain.objects.none()
    return Domain.objects.filter(group_id__in=_ancestor_ids(group))


def _writable(queryset):
    from mojo.apps.dnsman.models.domain import PROVIDER_MOJO

    return queryset.filter(status="active", verified=True).exclude(
        provider=PROVIDER_MOJO)


def _pick(candidates):
    """One domain from ``candidates``: the BASE_URL-suffix match, else the
    single candidate, else None."""
    candidates = list(candidates)
    if not candidates:
        return None
    base_host = (urlsplit(_base_url()).hostname or "").strip().rstrip(".").lower()
    if base_host:
        suffixed = [domain for domain in candidates
                    if base_host == domain.name
                    or base_host.endswith(f".{domain.name}")]
        if suffixed:
            # Longest suffix wins, matching precheck's longest-match rule.
            return max(suffixed, key=lambda domain: len(domain.name))
    if len(candidates) == 1:
        return candidates[0]
    return None


def resolve(group):
    """The writable domain new apps in ``group`` go live under, or None."""
    return _pick(_writable(owned_by_group_or_ancestor(group)))


def no_domain_reason(group):
    """Plain-language reason ``resolve(group)`` returned None."""
    if group is not None and _writable(owned_by_group_or_ancestor(group)).exists():
        return AMBIGUOUS_REASON
    return NO_DOMAIN_REASON


def installation_domain():
    """The installation-level flavor of ``resolve``: across ALL groups, the
    writable domain the platform's own address sits under, else the single
    writable domain, else None. Used by readiness, which has no group."""
    from mojo.apps.dnsman.models import Domain

    return _pick(_writable(Domain.objects.all()))


def _record_values(record):
    return sorted(str(value).rstrip(".") for value in (
        getattr(record, "record_values", None) or
        (record.get("record_values") if isinstance(record, dict) else []) or []))


def _covering_certificate(domain, hostname):
    """An active (not renewal-due) or in-flight certificate covering
    ``hostname`` — the same scan shape as the address step's."""
    from django.utils import timezone

    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.models.certificate import STATUS_ACTIVE as CERT_ACTIVE
    from mojo.apps.edge import validators

    now = timezone.now()
    for candidate in Certificate.objects.filter(
            domain=domain, status__in=("pending", "issuing", CERT_ACTIVE)):
        if not validators.certificate_covers(candidate, hostname):
            continue
        if (candidate.status == CERT_ACTIVE and
                candidate.renew_after is not None and
                candidate.renew_after <= now):
            continue
        return candidate
    return None


def status(domain):
    """What converge would find: ``objict(record, certificate, destination)``.

    ``record`` — a ``*.{domain}`` CNAME already points at the serving
    destination. ``certificate`` — something active or in flight covers a
    one-label subdomain (probed as ``x.{domain}``). Raises what
    ``webapp_destination.resolve`` or the DNS provider raise.
    """
    from mojo.apps.dnsman.services import dns
    from mojo.apps.edge.services import webapp_destination

    destination = webapp_destination.resolve()
    target = destination.value
    wildcard_name = f"*.{domain.name}"
    record = any(
        str(getattr(row, "name", row.get("name", ""))).rstrip(".") == wildcard_name
        and str(getattr(row, "type", row.get("type", ""))).upper() == "CNAME"
        and _record_values(row) == [target]
        for row in dns.list_records(domain))
    certificate = _covering_certificate(domain, f"x.{domain.name}")
    return objict(record=record, certificate=certificate is not None,
                  destination=target)


def converge(domain):
    """Make the domain's wildcard coverage exist. Idempotent: a second call
    finds everything in place and writes nothing.

    Authorization note: converge has no actor context of its own. The
    onboarding path (``webapp_onboarding._advance_address``) enforces the
    actor gate before calling it — writes to an ancestor-owned domain require
    ``webapp_authority.can_manage_group_webapps(actor, domain.group)`` in the
    DOMAIN-OWNING group. The remaining direct callers (readiness/Setup) carry
    no actor and sit behind the platform-admin readiness surface.
    """
    from mojo.apps.dnsman.services import certs, dns

    state = status(domain)
    record_written = False
    if not state.record:
        dns.upsert_record(domain, "CNAME", f"*.{domain.name}",
                          [state.destination], ttl=300)
        record_written = True
        logit.info(f"apps-domain wildcard CNAME written for {domain.name}")
    certificate_requested = False
    certificate_id = None
    if not state.certificate:
        certificate = certs.request_certificate(domain)
        certificate_requested = True
        certificate_id = certificate.pk
    return objict(record_written=record_written,
                  certificate_requested=certificate_requested,
                  certificate=certificate_id,
                  destination=state.destination)
