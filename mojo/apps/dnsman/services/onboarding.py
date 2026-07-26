"""
Bringing domains and provider credentials into dnsman.

Three ways in, none of which spends money:

  - `link_credential` — store a provider API key/secret, but only after the
    provider itself has confirmed the pair works.
  - `register_existing` — self-serve BYO: prove, per name, that the linked
    account actually holds the domain, then start managing it.
  - `adopt_route53` — take over an existing hosted zone in the house AWS
    account (superuser-only, enforced by the REST layer).

Blast-radius rule that shapes all of this: a provider API key is ACCOUNT-WIDE.
Nothing here ever returns, stores or logs the list of domains in the linked
account — only a count, and only per-name confirmations for names the caller
already supplied. There is deliberately no "show me everything in this account"
surface.

Like `services/dns.py`, this module is ungated mechanism: permissions are the
REST layer's job.
"""

from django.utils import timezone

from mojo import errors as me
from mojo.helpers import logit

from mojo.apps.dnsman.models.dns_credential import DnsCredential
from mojo.apps.dnsman.models.domain import (
    PROVIDER_GODADDY, PROVIDER_ROUTE53, STATUS_ACTIVE, Domain)
from mojo.apps.dnsman.services import naming
from mojo.apps.dnsman.services.providers import godaddy_provider


logger = logit.get_logger("dnsman", "dnsman.log")

# Providers a tenant can bring their own credential for. Route53 rides the
# house AWS credentials and is never linked this way.
LINKABLE_PROVIDERS = (PROVIDER_GODADDY,)


def _provider_error_message(err):
    """A short, user-facing reason out of a provider exception."""
    response = getattr(err, "response", None)
    if response is not None:
        body = None
        try:
            body = response.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            message = body.get("message") or body.get("code")
            if message:
                return str(message)
        status = getattr(response, "status_code", None)
        if status:
            return f"HTTP {status}"
    return str(err) or err.__class__.__name__


def _refuse_if_tracked(name):
    if Domain.objects.filter(name=name).exists():
        raise me.ValueException(f"'{name}' is already tracked by this system")


def _require_usable(credential):
    if credential is None:
        raise me.ValueException("A verified provider credential is required")
    if not credential.is_usable:
        raise me.ValueException(
            f"The credential '{credential.name}' is not usable — it must be active, "
            f"verified, and hold an API key and secret")


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------

def link_credential(group, provider, api_key, api_secret, name=None, credential=None):
    """
    Link (or rotate) a provider API credential after verifying it works.

    Ordering is the whole point: the key/secret are proven against the provider
    BEFORE anything is persisted.

      - First link (`credential=None`): a failed verification persists NOTHING —
        no half-created row, no stored secret that was never known to work.
      - Rotation (`credential=<row>`): the new pair is verified first, and the
        stored secrets are overwritten ONLY on success, so a bad rotation leaves
        the working credential exactly as it was (it records `last_error` and
        raises).

    `get_domains` is called purely as proof of life; only the COUNT is stored
    (`domain_count`) — never the list.
    """
    provider = (provider or PROVIDER_GODADDY).strip().lower()
    if provider not in LINKABLE_PROVIDERS:
        raise me.ValueException(
            f"'{provider}' credentials cannot be linked "
            f"(supported: {', '.join(LINKABLE_PROVIDERS)})")
    if not api_key or not api_secret:
        raise me.ValueException("An API key and secret are required")
    if credential is not None and credential.provider != provider:
        raise me.ValueException(
            f"Credential '{credential.name}' is a {credential.provider} credential, "
            f"not {provider}")

    manager = godaddy_provider.build_manager_from_pair(api_key, api_secret)
    try:
        domain_count = godaddy_provider.account_domain_count(manager)
    except Exception as err:
        message = _provider_error_message(err)
        logger.warning(f"dnsman: {provider} credential verification failed: {message}")
        if credential is not None and credential.pk:
            # Rotation failure — the previously stored secrets are untouched.
            credential.verified = False
            credential.last_error = message
            credential.save()
        raise me.ValueException(
            f"The {provider} credential could not be verified: {message}")

    if credential is None:
        credential = DnsCredential(
            group=group,
            provider=provider,
            name=name or f"{provider} credential")
    elif name:
        credential.name = name

    credential.set_credentials(api_key, api_secret)
    credential.is_active = True
    credential.verified = True
    credential.verified_at = timezone.now()
    credential.domain_count = domain_count
    credential.last_error = None
    credential.save()
    return credential


# ---------------------------------------------------------------------------
# domains
# ---------------------------------------------------------------------------

def register_existing(group, user, name, credential):
    """
    Self-serve BYO onboarding: start managing a domain the caller already owns.

    The working credential IS the proof of control, but only for names the
    caller explicitly supplies: the probe is per-name (`get_domain_info`), so a
    member can confirm a domain they already named and nothing else. The
    provider must report the domain as ACTIVE on that account.

    Nothing is created when the probe fails.
    """
    name = naming.normalize_domain(name)
    _require_usable(credential)
    if credential.provider != PROVIDER_GODADDY:
        raise me.ValueException(
            f"'{credential.provider}' domains cannot be registered this way")
    if credential.group_id is not None and group is not None and credential.group_id != group.id:
        raise me.ValueException(
            f"Credential '{credential.name}' belongs to another group")
    _refuse_if_tracked(name)

    manager = godaddy_provider.build_manager(credential)
    try:
        info = godaddy_provider.domain_info(manager, name)
    except Exception as err:
        message = _provider_error_message(err)
        raise me.ValueException(
            f"'{name}' could not be confirmed on this {credential.provider} account: {message}")

    reported = info.get("domain") if hasattr(info, "get") else None
    if reported and str(reported).strip().lower().rstrip(".") != name:
        raise me.ValueException(
            f"The {credential.provider} account returned '{reported}' when asked about "
            f"'{name}' — refusing to register a domain that was not confirmed")

    status = str((info.get("status") if hasattr(info, "get") else "") or "").upper()
    if status != "ACTIVE":
        raise me.ValueException(
            f"'{name}' is not ACTIVE on this {credential.provider} account "
            f"(status '{status or 'unknown'}')")

    domain = Domain(
        group=group,
        user=user,
        name=name,
        provider=PROVIDER_GODADDY,
        credential=credential,
        status=STATUS_ACTIVE,
        verified=True)
    domain.save()
    return domain


def adopt_route53(group, user, name, create_zone=False):
    """
    Take over an existing Route53 hosted zone in the house AWS account.

    No purchase, no money — this only starts managing a zone that already
    exists (or, with `create_zone=True`, creates an empty one).

    PERMISSIONS: the REST layer enforces `manage_dns` AND superuser. It is not
    checked here because this module is ungated mechanism, but the superuser
    requirement is not optional: adoption hands a group control of a hosted zone
    in the HOUSE AWS account, so exposing it to any `manage_dns` holder would be
    a cross-tenant zone-claim primitive.
    """
    name = naming.normalize_domain(name)
    _refuse_if_tracked(name)

    from mojo.helpers.aws import route53

    zone_id = route53.find_zone_id(name)
    if not zone_id:
        if not create_zone:
            raise me.ValueException(
                f"No Route53 hosted zone exists for '{name}'. "
                f"Pass create_zone to create one.")
        zone_id = route53.create_hosted_zone(name).zone_id

    domain = Domain(
        group=group,
        user=user,
        name=name,
        provider=PROVIDER_ROUTE53,
        status=STATUS_ACTIVE,
        verified=True,
        hosted_zone_id=zone_id)
    domain.save()
    return domain
