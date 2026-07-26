"""
Email (SES) integration for dnsman-held domains.

`mojo/helpers/aws/ses_domain.py` COMPUTES the DNS records SES needs
(verification TXT, DKIM CNAMEs, optional MAIL FROM MX + SPF); it deliberately
applies nothing. Applying them used to be the caller's problem, which is how
`EmailDomain.dns_mode="route53"` ended up being a stub that was never
implemented and how GoDaddy API secrets ended up travelling in request bodies.

This module closes that gap: one dispatch (`apply_records`) picks the provider
from the dnsman `Domain` row, and one call (`onboard_email_domain`) takes an
`EmailDomain` from "SES knows nothing about this domain" to "the records are
live in whichever zone actually hosts it". Credentials come from the linked
`DnsCredential`, never from the caller.
"""

from objict import objict

from mojo import errors as me
from mojo.helpers import logit
from mojo.helpers.settings import settings
from mojo.helpers.aws import ses_domain

from mojo.apps.dnsman.models.domain import PROVIDER_GODADDY, PROVIDER_ROUTE53
from mojo.apps.dnsman.services import naming


logger = logit.get_logger("dnsman", "dnsman.log")


def record_to_dict(record):
    """Flatten a `ses_domain.DnsRecord` for a response body or a log line."""
    return objict(
        type=record.type,
        name=record.name,
        value=record.value,
        ttl=record.ttl)


def find_domain(name):
    """
    Return the dnsman Domain for `name`, or None.

    The name is normalized first — Domain.name is unique on the normalized
    form, so a caller passing "Example.COM." must still find it.
    """
    from mojo.apps.dnsman.models import Domain
    return Domain.objects.filter(name=naming.normalize_domain(name)).first()


def require_domain(name):
    """
    Return the dnsman Domain for `name` or raise an actionable error.

    "Not held here" is a real, expected answer (an EmailDomain may predate
    dnsman, or its zone may live somewhere we have no credential for), so it
    gets a message that names the fix rather than a bare 404.
    """
    domain_obj = find_domain(name)
    if domain_obj is None:
        raise me.ValueException(
            f"'{name}' is not managed by dnsman — register or adopt the domain "
            f"first, or apply the DNS records manually",
            code=404, status=404)
    return domain_obj


def apply_records(domain_obj, records):
    """
    Apply a list of `ses_domain.DnsRecord` to whichever provider hosts
    `domain_obj`'s zone.

    This is the whole point of the dispatch: a caller hands over records and a
    Domain, and never chooses (or even knows) a provider. Route 53 rides the
    house AWS credentials; GoDaddy rides the Domain's linked DnsCredential and
    fails closed — no active, verified credential means no network call at all.

    Returns objict(provider, applied, records).
    """
    if domain_obj is None:
        raise me.ValueException("A dnsman domain is required to apply DNS records")

    records = list(records or [])
    result = objict(
        provider=domain_obj.provider,
        applied=len(records),
        records=[record_to_dict(r) for r in records])
    if not records:
        return result

    if domain_obj.provider == PROVIDER_ROUTE53:
        ses_domain.apply_dns_records_route53(
            domain=domain_obj.name,
            records=records,
            zone_id=domain_obj.hosted_zone_id)
    elif domain_obj.provider == PROVIDER_GODADDY:
        if not domain_obj.has_usable_credential:
            raise me.ValueException(
                f"No working DNS credential for '{domain_obj.name}' — link and "
                f"verify a {domain_obj.provider} credential before applying DNS records")
        credential = domain_obj.credential
        ses_domain.apply_dns_records_godaddy(
            domain=domain_obj.name,
            records=records,
            api_key=credential.api_key,
            api_secret=credential.api_secret)
    else:
        raise me.ValueException(
            f"dnsman cannot apply DNS records for provider '{domain_obj.provider}'")

    logger.info(
        f"dnsman applied {len(records)} DNS record(s) for {domain_obj.name} "
        f"via {domain_obj.provider}")
    return result


def _resolve_email_domain(email_domain):
    """Accept an EmailDomain instance or its pk."""
    from mojo.apps.aws.models import EmailDomain
    if isinstance(email_domain, EmailDomain):
        return email_domain
    obj = EmailDomain.objects.filter(pk=email_domain).first()
    if obj is None:
        raise me.ValueException(
            f"EmailDomain {email_domain} not found", code=404, status=404)
    return obj


def _endpoints(endpoints):
    endpoints = endpoints or {}
    return ses_domain.SnsEndpoints(
        bounce=endpoints.get("bounce"),
        complaint=endpoints.get("complaint"),
        delivery=endpoints.get("delivery"),
        inbound=endpoints.get("inbound"))


def onboard_email_domain(email_domain, region=None, receiving_enabled=None,
                         s3_bucket=None, s3_prefix=None, ensure_mail_from=False,
                         mail_from_subdomain="feedback", endpoints=None,
                         access_key=None, secret_key=None):
    """
    One-call SES onboarding for a domain dnsman already holds.

    SES computes the records, dnsman applies them through the provider
    dispatch. Nothing about the provider reaches the caller, and no provider
    API secret is ever passed in — that is what makes
    `EmailDomain.dns_mode="route53"` real for the first time.

    `email_domain` may be an EmailDomain instance or its pk.
    Returns the SES result plus what was applied.
    """
    email_domain = _resolve_email_domain(email_domain)
    domain_obj = require_domain(email_domain.name)

    if not domain_obj.is_active:
        raise me.ValueException(
            f"'{domain_obj.name}' is not active in dnsman (status "
            f"'{domain_obj.status}') — DNS records cannot be applied yet")

    region = region or email_domain.region or settings.get("AWS_REGION", "us-east-1")
    if receiving_enabled is None:
        receiving_enabled = email_domain.receiving_enabled
    s3_bucket = s3_bucket or email_domain.s3_inbound_bucket
    s3_prefix = s3_prefix if s3_prefix is not None else (email_domain.s3_inbound_prefix or "")
    if receiving_enabled and not s3_bucket:
        raise me.ValueException("s3_bucket is required when receiving_enabled is true")

    access_key = access_key or email_domain.aws_key or settings.get("AWS_KEY", None)
    secret_key = secret_key or email_domain.aws_secret or settings.get("AWS_SECRET", None)

    # dns_mode="manual" on purpose: onboard_domain never applies DNS itself, and
    # asking it for anything else would only be misleading. Applying is this
    # module's job, one line down.
    result = ses_domain.onboard_domain(
        domain=domain_obj.name,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
        receiving_enabled=bool(receiving_enabled),
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        dns_mode="manual",
        ensure_mail_from=bool(ensure_mail_from),
        mail_from_subdomain=mail_from_subdomain,
        endpoints=_endpoints(endpoints))

    applied = apply_records(domain_obj, result.dns_records)

    notes = list(result.notes or [])
    notes.append(f"Applied {applied.applied} DNS record(s) via dnsman ({domain_obj.provider})")

    # The model now describes reality: the records live in this provider's zone.
    updates = {}
    if email_domain.dns_mode != domain_obj.provider:
        updates["dns_mode"] = domain_obj.provider
    if email_domain.region != region:
        updates["region"] = region
    if updates:
        for key, value in updates.items():
            setattr(email_domain, key, value)
        email_domain.save(update_fields=list(updates.keys()) + ["modified"])

    return objict(
        domain=result.domain,
        region=result.region,
        provider=domain_obj.provider,
        dnsman_domain_id=domain_obj.id,
        verification_token=result.verification_token,
        dkim_tokens=list(result.dkim_tokens or []),
        dns_records=[record_to_dict(r) for r in result.dns_records],
        topic_arns=result.topic_arns,
        receipt_rule=result.receipt_rule,
        rule_set=result.rule_set,
        notes=notes,
        applied=applied)
