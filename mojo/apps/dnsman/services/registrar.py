"""
Registrar service — search, quote, purchase, poll, WHOIS contacts and privacy.

This is the money path. Everything here is Route53 Domains only: a
`provider="godaddy"` Domain is management-only and every registrar entry point
refuses it with an explicit message rather than a generic denial.

PERMISSIONS ARE NOT CHECKED HERE. The REST layer gates every entry point before
calling in. `group` and `user` are taken as ATTRIBUTION data — who the purchase
belongs to and who confirmed it — never as authorization. A caller that reaches
these functions has already been allowed to.

The ordering in `purchase()` is load-bearing and must not be rearranged:

  1. Lock the purchase row, verify the token / TTL, and flip `quoted` ->
     `submitted` as a compare-and-swap UNDER the lock. A concurrent second
     confirmation sees a non-quoted status and gets a uniform refusal that says
     nothing about which check failed.
  2. Create the `Domain` row INSIDE that same transaction, so the unique-name
     collision fires BEFORE any money can move.
  3. Only AFTER the commit call Route53. A crash between the commit and the AWS
     call leaves a `submitted` purchase with NO operation id — a state
     `poll_pending()` reconciles from `list_operations`. There is deliberately
     no window in which money may have moved with nothing durable recorded, and
     no single-call purchase path exists anywhere.

Failure deletes the Domain row (the no-failed-rows invariant): the
DomainPurchase ledger is the audit trail, so the unique `name` constraint only
ever guards live registrations.
"""

import hashlib
import hmac
import secrets as pysecrets
from decimal import Decimal, InvalidOperation

from django.db import transaction, IntegrityError

from objict import objict

from mojo import errors as me
from mojo.helpers import dates
from mojo.helpers import logit
from mojo.helpers.aws import route53
from mojo.helpers.settings import settings

from mojo.apps.dnsman.models import Domain, DomainPurchase
from mojo.apps.dnsman.models.domain import (
    PROVIDER_ROUTE53, PROVIDER_GODADDY,
    STATUS_ACTIVE as DOMAIN_ACTIVE,
    STATUS_PENDING as DOMAIN_PENDING,
    STATUS_REGISTERING as DOMAIN_REGISTERING,
)
from mojo.apps.dnsman.models.domain_purchase import (
    KIND_REGISTER,
    STATUS_QUOTED, STATUS_SUBMITTED, STATUS_COMPLETED,
    STATUS_FAILED, STATUS_EXPIRED,
)
from mojo.apps.dnsman.services import naming

logger = logit.get_logger("dnsman", "dnsman.log")


# A confirm token is 36 random bytes -> exactly 48 urlsafe base64 characters.
CONFIRM_TOKEN_BYTES = 36

# Every purchase refusal uses the SAME message. Which check failed (unknown id,
# wrong token, expired quote, already-confirmed quote) is exactly the thing an
# attacker probing confirm tokens wants to learn.
PURCHASE_REFUSED = (
    "That purchase confirmation is not valid. Request a new quote and try again.")

PURCHASE_DISABLED = (
    "Domain purchasing is disabled on this deployment "
    "(DNSMAN_PURCHASE_ENABLED is off).")

# ICANN requires all of these on a registrant contact. AWS rejects the
# registration otherwise, and it rejects it AFTER we have already committed our
# durable intent — so the check happens up front, at quote time.
REQUIRED_CONTACT_FIELDS = [
    "FirstName", "LastName", "ContactType", "AddressLine1",
    "City", "CountryCode", "ZipCode", "PhoneNumber", "Email",
]

# Registries in these countries additionally require a state/province.
STATE_REQUIRED_COUNTRIES = {"US", "CA"}

# How long a `submitted` purchase with no operation id may go unresolved before
# we declare that the registration never reached AWS. Generous on purpose: AWS
# operations are visible in list_operations within seconds, so half an hour of
# silence means the call really did not happen.
RECONCILE_TIMEOUT_MINUTES = 30

MAX_YEARS = 10


# ---------------------------------------------------------------------------
# settings + token helpers
# ---------------------------------------------------------------------------

def _purchase_enabled():
    return bool(settings.get("DNSMAN_PURCHASE_ENABLED", False, kind="bool"))


def _require_purchase_enabled():
    """The global kill switch. Checked before ANY registrar call is made."""
    if not _purchase_enabled():
        raise me.ValueException(PURCHASE_DISABLED)


def _registrant_contact():
    """
    Return the configured ICANN contact, or refuse.

    Refusing here — before availability, before any row — means an unconfigured
    deployment can never reach `register_domain` with a contact AWS will bounce.
    The values themselves are never echoed back to the caller.
    """
    contact = settings.get("DNSMAN_REGISTRANT_CONTACT", {}, kind="dict") or {}
    if not contact:
        raise me.ValueException(
            "No registrant contact is configured (DNSMAN_REGISTRANT_CONTACT); "
            "domain purchasing is unavailable until one is set.")
    missing = [
        field for field in REQUIRED_CONTACT_FIELDS
        if not str(contact.get(field) or "").strip()
    ]
    country = str(contact.get("CountryCode") or "").strip().upper()
    if country in STATE_REQUIRED_COUNTRIES and not str(contact.get("State") or "").strip():
        missing.append("State")
    if missing:
        raise me.ValueException(
            "The configured registrant contact (DNSMAN_REGISTRANT_CONTACT) is "
            f"incomplete; missing: {', '.join(missing)}")
    return dict(contact)


def _max_price():
    try:
        return Decimal(str(settings.get("DNSMAN_MAX_DOMAIN_PRICE", 50.00)))
    except (InvalidOperation, TypeError, ValueError):
        logger.error("dnsman: DNSMAN_MAX_DOMAIN_PRICE is not a number; using 50.00")
        return Decimal("50.00")


def _quote_ttl_minutes():
    return int(settings.get("DNSMAN_QUOTE_TTL_MINUTES", 15, kind="int"))


def _new_token():
    return pysecrets.token_urlsafe(CONFIRM_TOKEN_BYTES)


def _hash_token(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _token_matches(token, stored_hash):
    """Constant-time compare of the SHA-256 hash. The raw token is never stored."""
    if not token or not stored_hash:
        return False
    return hmac.compare_digest(_hash_token(token), str(stored_hash))


def _refused():
    return me.ValueException(PURCHASE_REFUSED)


def _pk(value):
    return getattr(value, "pk", None) if value is not None else None


# ---------------------------------------------------------------------------
# provider guard
# ---------------------------------------------------------------------------

def _require_route53(domain):
    """
    Registrar operations exist only for domains we registered through Route53.

    A GoDaddy-backed domain gets a specific, actionable message — it is not a
    permission problem and must not read like one.
    """
    if domain.provider == PROVIDER_ROUTE53:
        return
    if domain.provider == PROVIDER_GODADDY:
        raise me.ValueException(
            f"GoDaddy domains are management-only here: '{domain.name}' can have "
            "its DNS records managed through this system, but WHOIS contacts, "
            "privacy and registration are controlled by the GoDaddy account that "
            "holds it.")
    raise me.ValueException(
        f"'{domain.name}' is not registered through Route53, so registrar "
        "operations are not available for it.")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def search(name):
    """
    Look up one domain's availability and price. Creates nothing.

    Returns objict(name, available, status, price, currency, tld, tld_supported,
    privacy_supported, reason).

    `available` is TRI-STATE and passed through from the helper untouched: a
    `None` means the registry did not answer, and `reason` says to retry. A
    caller must never read None as "yes".
    """
    name = naming.normalize_domain(name)
    result = route53.check_availability(name)

    reason = None
    if result.available is None:
        reason = ("The registry did not answer for this name yet — "
                  "try the search again in a moment.")
    elif not result.tld_supported:
        reason = f".{result.tld} cannot be registered through this system."
    elif result.available is False:
        reason = "This domain is already registered."

    return objict(
        name=result.name,
        available=result.available,
        status=result.status,
        price=result.price,
        currency=result.currency,
        tld=result.tld,
        tld_supported=result.tld_supported,
        privacy_supported=result.privacy_supported,
        reason=reason)


# ---------------------------------------------------------------------------
# quote
# ---------------------------------------------------------------------------

def quote(group, user, name, years=1):
    """
    Price a registration and hand back a single-use confirmation token.

    `group`/`user` are attribution only — the caller has already been
    authorized. Returns objict(purchase, name, price, currency, years, token,
    expires, privacy_supported). The raw token is returned EXACTLY ONCE; only
    its SHA-256 hash is stored.

    Checks run in a deliberate order: the two that cost nothing and can refuse
    a misconfigured deployment (kill switch, registrant contact) happen before
    any AWS call, and the duplicate-name check happens before it too. An
    indeterminate availability answer creates NO row at all.
    """
    _require_purchase_enabled()
    _registrant_contact()

    name = naming.normalize_domain(name)
    tld = naming.split_tld(name)

    try:
        years = int(years or 1)
    except (TypeError, ValueError):
        raise me.ValueException("Registration length must be a whole number of years")
    if years < 1 or years > MAX_YEARS:
        raise me.ValueException(
            f"Registration length must be between 1 and {MAX_YEARS} years")

    # Every Domain row is live (no-failed-rows invariant), so the existence of
    # a row is a complete answer: this name is already held here.
    if Domain.objects.filter(name=name).exists():
        raise me.ValueException(f"'{name}' is already managed by this system")

    result = route53.check_availability(name)

    if not result.tld_supported:
        raise me.ValueException(f".{tld} cannot be registered through this system")
    if result.available is None:
        raise me.ValueException(
            "The registry did not answer for this name — no quote was created. "
            "Try again in a moment.")
    if result.available is not True:
        raise me.ValueException(f"'{name}' is not available for registration")

    if result.price is None:
        raise me.ValueException(
            f"No registration price is available for .{tld}; cannot quote this domain")

    price = Decimal(str(result.price))
    cap = _max_price()
    if price > cap:
        raise me.ValueException(
            f"'{name}' costs {price} {result.currency or 'USD'}, above the "
            f"{cap} limit for automated purchases")

    token = _new_token()
    purchase = DomainPurchase.objects.create(
        group=group,
        user=user,
        domain_name=name,
        kind=KIND_REGISTER,
        status=STATUS_QUOTED,
        price=price,
        cost=price,
        currency=result.currency or "USD",
        years=years,
        confirm_token=_hash_token(token),
        quote_expires=dates.add(minutes=_quote_ttl_minutes()),
        metadata={
            "tld": tld,
            "availability_status": result.status,
            "privacy_supported": bool(result.privacy_supported),
        })

    return objict(
        purchase=purchase.id,
        name=name,
        price=price,
        currency=purchase.currency,
        years=years,
        token=token,
        expires=purchase.quote_expires,
        privacy_supported=bool(result.privacy_supported))


# ---------------------------------------------------------------------------
# purchase
# ---------------------------------------------------------------------------

def purchase(group, user, purchase_id, token):
    """
    Confirm a quote and register the domain. The one irreversible mutation here.

    See the module docstring for why the ordering may not be rearranged.
    Returns objict(purchase, domain, name, status, operation_id, privacy,
    privacy_downgraded).
    """
    _require_purchase_enabled()

    # Marking a stale quote `expired` cannot happen inside the transaction: the
    # refusal that follows rolls the transaction back and would take the mark
    # with it. The pk is carried out and written after the rollback instead.
    expired_pk = None
    try:
        with transaction.atomic():
            row = DomainPurchase.objects.select_for_update().filter(pk=purchase_id).first()
            if row is None:
                raise _refused()

            # COMPARE-AND-SWAP: the status check and the flip both happen under
            # this row lock, so exactly one concurrent confirmation can win.
            # Every failure below raises the same uniform refusal.
            if row.status != STATUS_QUOTED or row.kind != KIND_REGISTER:
                raise _refused()
            if not _token_matches(token, row.confirm_token):
                raise _refused()
            if row.quote_expires is None or row.quote_expires <= dates.utcnow():
                expired_pk = row.pk
                raise _refused()
            # A quote belongs to the group it was created for. Passing a
            # different group is a scoping error; passing none adopts the
            # quote's own group.
            if group is not None and _pk(group) != _pk(row.group):
                raise _refused()

            # Re-check under the lock: the switch may have been thrown between
            # the quote and this confirmation, and nothing durable exists yet.
            _require_purchase_enabled()
            contact = _registrant_contact()

            privacy_wanted = bool((row.metadata or {}).get("privacy_supported", True))

            # DURABLE INTENT BEFORE MONEY. The nested atomic is a savepoint so
            # an IntegrityError on the unique name does not poison the outer
            # transaction — and it fires here, before register_domain is called.
            try:
                with transaction.atomic():
                    domain = Domain.objects.create(
                        group=row.group,
                        user=user or row.user,
                        name=row.domain_name,
                        provider=PROVIDER_ROUTE53,
                        status=DOMAIN_REGISTERING,
                        auto_renew=True,
                        privacy=privacy_wanted,
                        verified=True,
                        metadata={"purchase": row.id})
            except IntegrityError:
                raise me.ValueException(
                    f"'{row.domain_name}' is already managed by this system")

            row.status = STATUS_SUBMITTED
            # Single use: the hash is dropped the moment the quote is consumed.
            row.confirm_token = None
            row.save(update_fields=["status", "confirm_token", "modified"])

            years = row.years or 1
            domain_name = row.domain_name
    except me.MojoException:
        if expired_pk is not None:
            DomainPurchase.objects.filter(pk=expired_pk, status=STATUS_QUOTED).update(
                status=STATUS_EXPIRED, confirm_token=None, modified=dates.utcnow())
        raise

    # --- committed. Only now may money move. ---
    try:
        result = route53.register(
            domain_name, contact,
            years=years, auto_renew=True, privacy=privacy_wanted)
    except Exception as err:
        _fail_purchase(row, str(err), domain)
        raise me.ValueException(f"Registration of '{domain_name}' failed: {err}")

    row.operation_id = result.operation_id
    metadata = dict(row.metadata or {})
    metadata["privacy_applied"] = bool(result.privacy)
    metadata["privacy_downgraded"] = bool(result.privacy_downgraded)
    row.metadata = metadata
    row.save(update_fields=["operation_id", "metadata", "modified"])

    # The row must never claim privacy it does not have: `register` downgrades
    # when the TLD refuses privacy, and result.privacy is what was applied.
    domain.privacy = bool(result.privacy)
    if result.privacy_downgraded:
        domain_metadata = dict(domain.metadata or {})
        domain_metadata["privacy_downgraded"] = True
        domain.metadata = domain_metadata
        domain.save(update_fields=["privacy", "metadata", "modified"])
        logger.info(
            f"dnsman: .{naming.split_tld(domain_name)} refused WHOIS privacy for "
            f"{domain_name}; registered without it")
    else:
        domain.save(update_fields=["privacy", "modified"])

    return objict(
        purchase=row.id,
        domain=domain.id,
        name=domain_name,
        status=domain.status,
        operation_id=row.operation_id,
        privacy=domain.privacy,
        privacy_downgraded=bool(result.privacy_downgraded))


def _fail_purchase(row, error, domain=None):
    """
    Record a failed purchase and drop its Domain row.

    The no-failed-rows invariant: the ledger keeps the audit trail, the Domain
    table keeps only live registrations, and the unique name constraint stays
    unpoisoned so the caller can retry.
    """
    DomainPurchase.objects.filter(pk=row.pk).update(
        status=STATUS_FAILED, error=str(error)[:4000], modified=dates.utcnow())
    row.status = STATUS_FAILED
    row.error = str(error)[:4000]
    _delete_domain_row(domain if domain is not None else row.domain_name)


def _delete_domain_row(domain_or_name):
    """Delete a not-yet-live Domain row. An active domain is never touched."""
    try:
        if isinstance(domain_or_name, str):
            Domain.objects.filter(
                name=domain_or_name,
                status__in=[DOMAIN_PENDING, DOMAIN_REGISTERING]).delete()
        elif domain_or_name is not None:
            Domain.objects.filter(
                pk=domain_or_name.pk,
                status__in=[DOMAIN_PENDING, DOMAIN_REGISTERING]).delete()
    except Exception as err:
        logger.error(f"dnsman: could not clean up domain row for {domain_or_name}: {err}")


# ---------------------------------------------------------------------------
# polling / reconciliation
# ---------------------------------------------------------------------------

def poll_pending():
    """
    Advance every open purchase. Idempotent — safe to run on a short cron.

    Three duties:
      - `submitted` WITH an operation id: ask AWS and settle the row.
      - `submitted` WITHOUT one: the crash window. Probe `list_operations` for
        the real operation, or fail the row after RECONCILE_TIMEOUT_MINUTES.
      - `quoted` rows past their TTL: expire them.

    Returns objict(completed, failed, adopted, expired, pending, errors).
    """
    result = objict(completed=0, failed=0, adopted=0, expired=0, pending=0, errors=0)

    _expire_quotes(result)

    rows = DomainPurchase.objects.filter(
        status=STATUS_SUBMITTED, kind=KIND_REGISTER).order_by("created")
    for row in rows:
        try:
            _poll_one(row, result)
        except Exception as err:
            result.errors += 1
            logger.error(
                f"dnsman: poll failed for purchase {row.id} ({row.domain_name}): {err}")
    return result


def _expire_quotes(result):
    now = dates.utcnow()
    stale = DomainPurchase.objects.filter(
        status=STATUS_QUOTED, quote_expires__lte=now)
    result.expired += stale.update(
        status=STATUS_EXPIRED, confirm_token=None, modified=now)


def _poll_one(row, result):
    if not row.operation_id:
        if not _reconcile(row, result):
            return
    detail = route53.operation_detail(row.operation_id)
    status = (detail.status or "").upper()
    if status == "SUCCESSFUL":
        _complete(row, result)
    elif status in ("ERROR", "FAILED"):
        _settle_failure(row, detail.message or f"Registration {status.lower()}", result)
    else:
        result.pending += 1


def _reconcile(row, result):
    """
    The crash window: committed `submitted`, but the operation id never landed.

    Either the register call never happened, or it happened and we died before
    storing its id. `list_operations` is the only way to tell the two apart, and
    guessing wrong either abandons a real registration or fails a domain we own.
    Returns True when an operation was adopted.
    """
    operations = route53.list_operations(submitted_since=row.created)
    for operation in operations or []:
        if (operation.type or "").upper() != "REGISTER_DOMAIN":
            continue
        if (operation.domain_name or "").strip().lower().rstrip(".") != row.domain_name:
            continue
        if not operation.operation_id:
            continue
        row.operation_id = operation.operation_id
        row.save(update_fields=["operation_id", "modified"])
        result.adopted += 1
        logger.info(
            f"dnsman: adopted registrar operation {operation.operation_id} for "
            f"purchase {row.id} ({row.domain_name})")
        return True

    cutoff = dates.subtract(minutes=RECONCILE_TIMEOUT_MINUTES)
    if row.created <= cutoff:
        message = (
            "Registration never reached AWS: no REGISTER_DOMAIN operation appeared "
            f"within {RECONCILE_TIMEOUT_MINUTES} minutes of the confirmed purchase.")
        logger.error(
            f"dnsman: purchase {row.id} ({row.domain_name}) failed reconciliation — "
            f"{message} Review the AWS account before re-quoting.")
        _settle_failure(row, message, result)
    else:
        result.pending += 1
    return False


def _complete(row, result):
    """
    Settle a successful registration.

    The purchase flip is a guarded UPDATE so two pollers observing the same
    operation cannot both count it. Activating the Domain is idempotent and runs
    outside that guard on purpose: if a previous poller died between the flip
    and the activation, the next pass still finishes the job.
    """
    flipped = DomainPurchase.objects.filter(
        pk=row.pk, status=STATUS_SUBMITTED).update(
            status=STATUS_COMPLETED, error=None, modified=dates.utcnow())

    domain = Domain.objects.filter(name=row.domain_name).first()
    if domain is not None and domain.status != DOMAIN_ACTIVE:
        _activate_domain(domain)

    if not flipped:
        # Another observer already settled this row — do not count it twice.
        return
    row.status = STATUS_COMPLETED
    result.completed += 1


def _activate_domain(domain):
    """Fill in what only the registrar knows, then mark the domain live."""
    zone_id = None
    detail = None
    try:
        zone_id = route53.find_zone_id(domain.name)
    except Exception as err:
        logger.error(f"dnsman: hosted zone lookup failed for {domain.name}: {err}")
    try:
        detail = route53.get_domain_detail(domain.name)
    except Exception as err:
        logger.error(f"dnsman: registrar detail lookup failed for {domain.name}: {err}")

    domain.status = DOMAIN_ACTIVE
    domain.verified = True
    domain.last_error = None
    if zone_id:
        domain.hosted_zone_id = zone_id
    if detail is not None:
        if detail.registered_on:
            domain.registered_on = detail.registered_on
        if detail.expires:
            domain.expires = detail.expires
        if detail.auto_renew is not None:
            domain.auto_renew = bool(detail.auto_renew)
        if detail.privacy is not None:
            domain.privacy = bool(detail.privacy)
    domain.save()


def _settle_failure(row, message, result):
    """Guarded failure flip + Domain cleanup (no-failed-rows invariant)."""
    flipped = DomainPurchase.objects.filter(
        pk=row.pk, status=STATUS_SUBMITTED).update(
            status=STATUS_FAILED, error=str(message)[:4000], modified=dates.utcnow())
    _delete_domain_row(row.domain_name)
    if not flipped:
        return
    row.status = STATUS_FAILED
    row.error = str(message)[:4000]
    result.failed += 1


# ---------------------------------------------------------------------------
# WHOIS contacts + privacy
# ---------------------------------------------------------------------------

def get_contacts(domain):
    """
    Return the registrar's view of a domain: contacts, privacy, expiry, NS.

    Route53-only; a GoDaddy domain gets the management-only message.
    """
    _require_route53(domain)
    detail = route53.get_domain_detail(domain.name)
    return objict(
        name=domain.name,
        registrant=detail.registrant_contact,
        admin=detail.admin_contact,
        tech=detail.tech_contact,
        privacy=detail.privacy,
        admin_privacy=detail.admin_privacy,
        registrant_privacy=detail.registrant_privacy,
        tech_privacy=detail.tech_privacy,
        auto_renew=detail.auto_renew,
        nameservers=detail.nameservers,
        registrar=detail.registrar,
        registered_on=detail.registered_on,
        expires=detail.expires,
        status_list=detail.status_list,
        privacy_supported=route53.supports_privacy(naming.split_tld(domain.name)))


def update_contacts(domain, contact):
    """
    Replace the Admin/Registrant/Tech WHOIS contacts. Returns objict(name,
    operation_id).

    A changed registrant email starts ICANN's 15-day verification clock — see
    the registrar runbook. Route53-only.
    """
    _require_route53(domain)
    if not contact or not isinstance(contact, dict):
        raise me.ValueException("Contact details are required")
    missing = [
        field for field in REQUIRED_CONTACT_FIELDS
        if not str(contact.get(field) or "").strip()
    ]
    country = str(contact.get("CountryCode") or "").strip().upper()
    if country in STATE_REQUIRED_COUNTRIES and not str(contact.get("State") or "").strip():
        missing.append("State")
    if missing:
        raise me.ValueException(
            f"These contact fields are required: {', '.join(missing)}")

    operation_id = route53.update_contacts(domain.name, dict(contact))
    return objict(name=domain.name, operation_id=operation_id)


def set_privacy(domain, enabled):
    """
    Toggle WHOIS privacy. Returns objict(name, privacy, operation_id).

    Enabling is refused up front for a TLD that cannot have privacy — AWS would
    reject the call anyway, and a registry-level "InvalidInput" tells the user
    nothing they can act on.
    """
    _require_route53(domain)
    enabled = bool(enabled)
    tld = naming.split_tld(domain.name)
    # Only ENABLING is capability-gated: turning privacy off is always legal.
    if enabled and not route53.supports_privacy(tld):
        raise me.ValueException(
            f"The .{tld} registry does not offer WHOIS privacy, so it cannot be "
            f"enabled for '{domain.name}'. Registrant details for this TLD are "
            "public by registry policy.")

    operation_id = route53.update_privacy(domain.name, enabled)
    domain.privacy = enabled
    domain.save(update_fields=["privacy", "modified"])
    return objict(name=domain.name, privacy=enabled, operation_id=operation_id)
