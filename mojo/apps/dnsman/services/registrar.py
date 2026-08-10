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

import concurrent.futures
import hashlib
import hmac
import json
import re
import secrets as pysecrets
from decimal import Decimal, InvalidOperation

from django.db import transaction, IntegrityError, close_old_connections

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

# The DB/settings key holding the contact. Group-scopable: a Setting row for a
# group overrides its parent chain, which overrides the global row, which
# overrides the deployment's conf file.
REGISTRANT_CONTACT_KEY = "DNSMAN_REGISTRANT_CONTACT"

# Every member of the AWS `ContactDetail` shape, in full. An unknown key is not
# ignored by botocore — it raises ParamValidationError before the call leaves
# the process, which for a hand-written setting means a typo'd field name
# detonates at purchase time, AFTER durable intent. So the allow-list is
# enforced at write time instead. ExtraParams is included because ccTLD
# registries need it; its contents are AWS's to validate, not ours.
CONTACT_FIELDS = {
    "FirstName", "LastName", "ContactType", "OrganizationName",
    "AddressLine1", "AddressLine2", "City", "State", "CountryCode",
    "ZipCode", "PhoneNumber", "Email", "Fax", "ExtraParams",
}

# The AWS enum, verified against the installed botocore model for
# route53domains/2014-05-15. Anything else is a 400 from AWS mid-purchase.
CONTACT_TYPES = {"PERSON", "COMPANY", "ASSOCIATION", "PUBLIC_BODY", "RESELLER"}

# ICANN phone format: +<country code>.<number>
PHONE_RE = re.compile(r"^\+\d{1,3}\.\d{4,15}$")

COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

# Where a scope's own contact came from. `settings_file` is reachable only on
# the global scope — there is no per-group conf file — and tells the operator
# that saving will create a DB row shadowing the deployment's file.
SOURCE_DATABASE = "database"
SOURCE_SETTINGS_FILE = "settings_file"
SOURCE_NONE = "none"

# How long a `submitted` purchase with no operation id may go unresolved before
# we declare that the registration never reached AWS. Generous on purpose: AWS
# operations are visible in list_operations within seconds, so half an hour of
# silence means the call really did not happen.
RECONCILE_TIMEOUT_MINUTES = 30

MAX_YEARS = 10

# Batch-search fan-out. route53domains throttles around 5 TPS steady-state,
# and CheckDomainAvailability is the call that answers PENDING/DONT_KNOW
# under load — a modest pool is deliberate, not shy.
SEARCH_POOL_WORKERS = 4

# AWS caps GetDomainSuggestions at 50; 25 bounds the worst-case cold-cache
# price fill (one list_prices per distinct TLD in the suggestion list).
MAX_SUGGESTION_COUNT = 25


# ---------------------------------------------------------------------------
# settings + token helpers
# ---------------------------------------------------------------------------

def _purchase_enabled():
    return bool(settings.get("DNSMAN_PURCHASE_ENABLED", False, kind="bool"))


def _require_purchase_enabled():
    """The global kill switch. Checked before ANY registrar call is made."""
    if not _purchase_enabled():
        raise me.ValueException(PURCHASE_DISABLED)


def _coerce_contact(value):
    """A contact as a plain dict, whatever shape it arrived in. Never raises."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    if not isinstance(value, dict):
        return {}
    return dict(value)


def validate_contact(contact):
    """
    Return a list of problems with `contact` — empty means AWS will accept it.

    Every problem string NAMES A FIELD and never quotes a value: these strings
    reach REST responses and logs, and the values are PII.

    Shape, not just presence. The three ways a present-but-wrong contact still
    fails are all after durable intent: a `ContactType` outside the AWS enum, a
    `PhoneNumber` that is not ICANN `+<cc>.<number>`, and an unknown key —
    which botocore refuses with ParamValidationError before the call even goes
    out. Catching them at write time turns each into a readable 400.
    """
    contact = _coerce_contact(contact)
    if not contact:
        return ["A registrant contact is required"]

    problems = []

    for field in REQUIRED_CONTACT_FIELDS:
        if not str(contact.get(field) or "").strip():
            problems.append(f"{field} is required")

    # Type, not just truthiness. `str(value).strip()` accepts an int ZipCode or
    # a list Email happily, and botocore then raises ParamValidationError from
    # inside register_domain — after durable intent, which is the exact failure
    # this validator exists to move forward. Worse, that exception embeds the
    # offending VALUE and ends up on the purchase row. ExtraParams is the one
    # non-scalar member; AWS validates its contents.
    for field, value in contact.items():
        if value is None or field == "ExtraParams":
            continue
        if field in CONTACT_FIELDS and not isinstance(value, str):
            problems.append(f"{field} must be text")

    country = str(contact.get("CountryCode") or "").strip().upper()
    if country in STATE_REQUIRED_COUNTRIES and not str(contact.get("State") or "").strip():
        # Names the rule, not the value: a problem string reaches REST bodies
        # and logs, and echoing the country back would describe a contact the
        # reader may not be allowed to see.
        problems.append("State is required for US and CA registrant contacts")

    unknown = sorted(set(contact.keys()) - CONTACT_FIELDS)
    for field in unknown:
        problems.append(f"{field} is not a registrant contact field")

    contact_type = str(contact.get("ContactType") or "").strip()
    if contact_type and contact_type not in CONTACT_TYPES:
        problems.append(
            f"ContactType must be one of: {', '.join(sorted(CONTACT_TYPES))}")

    phone = str(contact.get("PhoneNumber") or "").strip()
    if phone and not PHONE_RE.match(phone):
        problems.append(
            "PhoneNumber must be in ICANN format +<country code>.<number>, "
            "for example +1.5035551212")

    # Shape only, never the 251-entry ISO enum: botocore does not enforce enums
    # client-side and AWS rejects a bogus code anyway. What is being guarded
    # here is malformed data, not an invented country.
    if country and not COUNTRY_RE.match(country):
        problems.append("CountryCode must be a two-letter ISO country code")

    return problems


def _resolve_contact(group=None):
    """
    The contact IN EFFECT for this scope, valid or not. Never raises.

    Resolution is the settings chain and nothing custom: the group's own row,
    then its parent chain, then the global row, then the deployment conf file.
    """
    contact = settings.get(REGISTRANT_CONTACT_KEY, {}, group=group, kind="dict")
    return _coerce_contact(contact)


def _registrant_contact(group=None):
    """
    Return the ICANN contact in effect for `group`, or refuse.

    Refusing here — before availability, before any row — means a misconfigured
    scope can never reach `register_domain` with a contact AWS will bounce. The
    values themselves are never echoed back to the caller; only field names
    appear in the refusal.

    A group with no contact of its own inherits its parent's, then the house
    contact. `group=None` is the house contact itself.
    """
    contact = _resolve_contact(group)
    if not contact:
        raise me.ValueException(
            "No registrant contact is configured (DNSMAN_REGISTRANT_CONTACT); "
            "domain purchasing is unavailable until one is set.")
    problems = validate_contact(contact)
    if problems:
        raise me.ValueException(
            "The configured registrant contact (DNSMAN_REGISTRANT_CONTACT) is "
            f"not usable: {'; '.join(problems)}")
    return dict(contact)


def contact_configured(group=None):
    """Whether `quote()` would accept this scope's contact right now."""
    try:
        _registrant_contact(group)
        return True
    except me.ValueException:
        return False


def read_contact(group=None):
    """
    Return (contact, source) for EXACTLY this scope — no inheritance.

    `settings.get()` deliberately cannot express "this scope only": it walks
    group -> parent chain -> global -> conf file, which is right for resolution
    and wrong for an editor, where a group must see its own row or nothing. A
    tenant learning the house contact by reading its own form is the whole
    thing this separation exists to prevent.

    `contact` is None only when this scope has nothing of its own; an existing
    row with an unusable value returns that value with source `database`, so
    the caller can report what is actually there rather than silently falling
    back to a scope it is not allowed to see.
    """
    from mojo.apps.account.models.setting import Setting

    row = Setting.objects.filter(key=REGISTRANT_CONTACT_KEY, group=group).first()
    if row is not None:
        return _coerce_contact(row.get_value()), SOURCE_DATABASE

    # No per-group conf file exists, so the file is a global-scope answer only.
    if group is None:
        conf = _coerce_contact(
            settings.get_static(REGISTRANT_CONTACT_KEY, {}, kind="dict"))
        if conf:
            return conf, SOURCE_SETTINGS_FILE

    return None, SOURCE_NONE


def save_contact(contact, group=None):
    """
    Validate and persist a contact for one scope. Returns the stored dict.

    `is_secret=True` is load-bearing, not caution. A plaintext Setting row is
    serialized as `display_value` by the generic settings REST, which any
    `manage_settings` holder can read — the operator's home address included.
    A secret row lands in `mojo_secrets`, which the serializer strips
    unconditionally, on every graph including the unknown-graph fallback.
    """
    from mojo.apps.account.models.setting import Setting

    contact = _coerce_contact(contact)
    problems = validate_contact(contact)
    if problems:
        raise me.ValueException(
            f"The registrant contact is not usable: {'; '.join(problems)}")

    Setting.set(REGISTRANT_CONTACT_KEY, contact, is_secret=True, group=group)
    logger.info(
        f"dnsman: registrant contact saved for {_scope_label(group)}")
    return contact


def clear_contact(group=None):
    """
    Drop this scope's own contact row. Returns True when a row was removed.

    Goes through `Setting.remove()` on purpose: a queryset `.delete()` never
    runs `Model.delete()`, so `remove_from_cache()` would not fire and a stale
    Redis entry would keep resolving after the row was gone.
    """
    from mojo.apps.account.models.setting import Setting

    removed = Setting.remove(REGISTRANT_CONTACT_KEY, group=group)
    if removed:
        logger.info(f"dnsman: registrant contact cleared for {_scope_label(group)}")
    return removed


def _scope_label(group):
    """A log-safe name for a contact scope. Never carries contact values."""
    return f"group {group.pk}" if group is not None else "the house account"


def _contact_fingerprint(contact):
    """
    A stable, non-reversible marker for WHICH contact was filed on a purchase.

    Salted with the deployment SECRET_KEY, so holding the ledger alone does not
    let anyone confirm a guessed contact by recomputing the digest. Recorded
    instead of the contact itself: the ledger must be able to answer "whose
    contact went on this domain" without becoming a second PII store.
    """
    canonical = json.dumps(
        _coerce_contact(contact), sort_keys=True, separators=(",", ":"), default=str)
    salt = str(settings.SECRET_KEY or "")
    return hmac.new(
        salt.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _max_price():
    try:
        return Decimal(str(settings.get("DNSMAN_MAX_DOMAIN_PRICE", 50.00)))
    except (InvalidOperation, TypeError, ValueError):
        logger.error("dnsman: DNSMAN_MAX_DOMAIN_PRICE is not a number; using 50.00")
        return Decimal("50.00")


def _quote_ttl_minutes():
    return int(settings.get("DNSMAN_QUOTE_TTL_MINUTES", 15, kind="int"))


def _search_batch_limit():
    return int(settings.get("DNSMAN_SEARCH_BATCH_LIMIT", 10, kind="int"))


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

def _reason_for(available, tld_supported, tld):
    """The one reason vocabulary shared by search, batch rows, and suggest rows."""
    if available is None:
        return ("The registry did not answer for this name yet — "
                "try the search again in a moment.")
    if not tld_supported:
        return f".{tld} cannot be registered through this system."
    if available is False:
        return "This domain is already registered."
    return None


def _search_row(result):
    """One result row. Single search, batch rows, and suggest rows all share
    this shape so a UI can render one grid for any of them."""
    return objict(
        name=result.name,
        available=result.available,
        status=result.status,
        price=result.price,
        currency=result.currency,
        tld=result.tld,
        tld_supported=result.tld_supported,
        privacy_supported=result.privacy_supported,
        reason=_reason_for(result.available, result.tld_supported, result.tld))


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
    return _search_row(result)


def _failed_row(name, reason):
    """A per-name failure inside a batch: available stays None (never False —
    'could not check' must not read as 'taken') and `reason` says why."""
    tld = None
    try:
        tld = naming.split_tld(name)
    except me.MojoException:
        pass
    return objict(
        name=name,
        available=None,
        status=None,
        price=None,
        currency=None,
        tld=tld,
        tld_supported=None,
        privacy_supported=None,
        reason=reason)


def _search_one(name):
    """search() with per-name failure isolation, for use inside a batch."""
    cleaned = str(name or "").strip().lower().rstrip(".")
    try:
        return search(cleaned)
    except me.MojoException as err:
        return _failed_row(cleaned, err.reason)
    except Exception as err:
        logger.error(f"dnsman: availability check failed for '{cleaned}': {err}")
        return _failed_row(
            cleaned,
            "The availability check failed for this name — try again in a moment.")
    finally:
        # Pool threads may touch the ORM through settings resolution; their
        # thread-local connections are Django's to nobody. Same discipline as
        # the job engine's executor workers.
        close_old_connections()


def _expand_tlds(domain, tlds):
    """
    Build '<base>.<tld>' candidates for the domain+tlds shape.

    The base may arrive with or without its own TLD — 'nativemojo.com' and
    'nativemojo' produce the same grid, since a wizard passes whatever the
    user typed. A dotted base that is not a valid domain fails the whole
    batch here: no per-name answer is meaningful when the base is garbage.
    """
    if not domain or not isinstance(domain, str):
        raise me.ValueException("'tlds' requires a base 'domain' name")
    if not isinstance(tlds, (list, tuple)):
        raise me.ValueException("'tlds' must be a list of TLDs")

    base = domain.strip().lower().rstrip(".")
    if "." in base:
        normalized = naming.normalize_domain(base)
        tld = naming.split_tld(normalized)
        base = normalized[:-(len(tld) + 1)]
    if not base:
        raise me.ValueException("'tlds' requires a base 'domain' name")

    cleaned = []
    seen = set()
    for tld in tlds:
        value = str(tld or "").strip().lower().lstrip(".").rstrip(".")
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    if not cleaned:
        raise me.ValueException("At least one TLD is required")
    return [f"{base}.{tld}" for tld in cleaned]


def search_batch(domain=None, domains=None, tlds=None):
    """
    Batch availability. Returns objict(results=[...]) — every row the same
    shape as search(), in request order.

    Two input shapes, never mixed: domain+tlds (one base against a TLD grid)
    or domains (full names). A name that fails validation or blows up becomes
    its own row with available=None and a reason — one bad name never fails
    its siblings, and the batch itself returns normally. The deduped list
    length is capped by DNSMAN_SEARCH_BATCH_LIMIT.
    """
    if tlds is not None and domains is not None:
        raise me.ValueException("Send either 'domain' + 'tlds' or 'domains', not both")
    if tlds is not None:
        names = _expand_tlds(domain, tlds)
    elif domains is not None:
        if domain:
            raise me.ValueException("Send either 'domain' + 'tlds' or 'domains', not both")
        if not isinstance(domains, (list, tuple)):
            raise me.ValueException("'domains' must be a list of domain names")
        names = list(domains)
    else:
        raise me.ValueException("A domain name is required")

    deduped = []
    seen = set()
    for name in names:
        key = str(name or "").strip().lower().rstrip(".")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(name)
    if not deduped:
        raise me.ValueException("At least one domain name is required")

    limit = _search_batch_limit()
    if len(deduped) > limit:
        raise me.ValueException(
            f"A batch search is limited to {limit} names; {len(deduped)} were given")

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(deduped), SEARCH_POOL_WORKERS)) as pool:
        results = list(pool.map(_search_one, deduped))
    return objict(results=results)


def suggest(name, count=10, only_available=True):
    """
    Alternate-name suggestions. Returns objict(results=[...]) — every row the
    same shape as search(), so a UI renders one grid for both.

    AWS returns availability but NO price on suggestions; prices come from
    the per-TLD price lookup (cached in the route53 helper), warmed
    concurrently so a cold cache costs ceil(n/POOL) round-trips instead of n
    sequential ones. A TLD the registrar does not sell keeps its row with
    tld_supported=False rather than being dropped.
    """
    name = naming.normalize_domain(name)
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise me.ValueException("Suggestion count must be a whole number")
    count = max(1, min(count, MAX_SUGGESTION_COUNT))

    try:
        suggestions = route53.get_domain_suggestions(
            name, count=count, only_available=bool(only_available))
    except me.MojoException:
        raise
    except Exception as err:
        # Raw botocore text carries the AWS account id and IAM principal
        # (AccessDenied on a missing GetDomainSuggestions grant is the likely
        # first-deploy failure) — log it, never echo it.
        logger.error(f"dnsman: suggestions failed for '{name}': {err}")
        raise me.ValueException(
            "Suggestions are unavailable right now — try again in a moment.")

    tagged = []
    for suggestion in suggestions:
        try:
            tld = naming.split_tld(suggestion.name)
        except me.MojoException:
            tld = None
        tagged.append((suggestion, tld))

    unique_tlds = []
    seen = set()
    for _, tld in tagged:
        if tld and tld not in seen:
            seen.add(tld)
            unique_tlds.append(tld)

    def _fetch_prices(tld):
        try:
            return route53.list_prices(tld)
        finally:
            close_old_connections()

    prices_by_tld = {}
    if unique_tlds:
        # list_prices never raises, so the map cannot blow up the request.
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(unique_tlds), SEARCH_POOL_WORKERS)) as pool:
            for tld, prices in zip(unique_tlds, pool.map(_fetch_prices, unique_tlds)):
                prices_by_tld[tld] = prices

    results = []
    for suggestion, tld in tagged:
        prices = prices_by_tld.get(tld) or objict(
            supported=False, registration_price=None, currency=None)
        results.append(objict(
            name=suggestion.name,
            available=suggestion.available,
            status=suggestion.status,
            price=prices.registration_price,
            currency=prices.currency,
            tld=tld,
            tld_supported=prices.supported,
            privacy_supported=route53.supports_privacy(tld) if tld else None,
            reason=_reason_for(suggestion.available, prices.supported, tld)))
    return objict(results=results)


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
    # Scoped to the buying group: a tenant with its own contact is validated
    # against THAT contact, not the house one, so the preflight answers the
    # same question `purchase()` will ask at redemption.
    _registrant_contact(group)

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

    # use_cache=False: this price is checked against the cap and written to
    # the ledger — no money decision may ride a cached answer.
    result = route53.check_availability(name, use_cache=False)

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

def purchase(group, user, purchase_id, token, confirm_domain, confirm_price):
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
            try:
                typed_domain = naming.normalize_domain(confirm_domain)
                typed_price = Decimal(str(confirm_price)).quantize(Decimal("0.01"))
            except (InvalidOperation, TypeError, ValueError):
                raise _refused()
            if typed_domain != row.domain_name or typed_price != row.price:
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
            # row.group, NEVER the `group` argument. The argument is optional
            # attribution — the check above accepts None, and request.group is
            # None for a group that went inactive between the quote and this
            # confirmation. Resolving from it would file the HOUSE contact on a
            # tenant's domain at the one irreversible, real-money step.
            # row.group is the authority; it is what the Domain row is created
            # with a few lines below.
            contact = _registrant_contact(row.group)
            registrant_scope = row.group_id if row.group_id else "house"

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
    # A quote does not pin the contact — the one read under the lock above is
    # what got filed. Now that the contact is tenant-editable, the ledger has
    # to be able to answer WHICH one, so record the scope it was resolved for
    # and a salted digest of the values. `metadata` is in neither REST graph,
    # so this adds no PII surface.
    metadata["registrant_scope"] = registrant_scope
    metadata["registrant_fingerprint"] = _contact_fingerprint(contact)
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
    # Same validator as the stored registrant contact: this dict reaches the
    # same AWS ContactDetail shape, so an unknown key or a malformed phone
    # number fails here identically rather than at the API boundary.
    problems = validate_contact(contact)
    if problems:
        raise me.ValueException(
            f"These contact fields are required: {'; '.join(problems)}")

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
