"""
AWS Route53 Helper Module

Model-free primitives for AWS Route53 (hosted zones + records) and
AWS Route53 Domains (availability, pricing, registration, WHOIS contacts).

House style for `mojo/helpers/aws/*`:
  - module level functions, no classes, no Django imports, no model imports
  - explicit access_key / secret_key / region arguments falling back to settings
  - objict returns

Two client factories exist on purpose:
  - `_domains_client()` is HARDCODED to us-east-1. The `route53domains`
    endpoint only exists in us-east-1; using the configured region breaks
    every registrar call for deployments outside of it.
  - `_dns_client()` uses the configured region. Route53 itself is a global
    service, but boto3 still wants a region on the session.
"""

import re
import time
import uuid

import botocore.exceptions

from objict import objict

from .client import get_session
from mojo.helpers.settings import settings
from mojo.helpers import logit
from mojo import errors as me

logger = logit.get_logger(__name__, "aws.log")


# The Route53 Domains API lives only in us-east-1.
DOMAINS_REGION = "us-east-1"

# Route53 requires each TXT character-string to be at most 255 characters and
# wrapped in double quotes. Longer values are split across several quoted
# strings inside a single record value.
TXT_CHUNK_SIZE = 255

# Raw AWS availability enums, grouped for the tri-state answer.
AVAILABLE_STATUSES = {"AVAILABLE", "AVAILABLE_RESERVED", "AVAILABLE_PREORDER"}
UNAVAILABLE_STATUSES = {
    "UNAVAILABLE",
    "UNAVAILABLE_PREMIUM",
    "UNAVAILABLE_RESTRICTED",
    "RESERVED",
}
# "The registry did not answer" — not a no, not a yes. Callers must retry.
INDETERMINATE_STATUSES = {"PENDING", "DONT_KNOW"}

INVALID_NAME_STATUS = "INVALID_NAME_FOR_TLD"

# TLDs where WHOIS privacy is NOT available through Route53.
#
# AWS exposes NO API for this — there is no "does this TLD support privacy"
# call anywhere in route53domains. This list is therefore maintained by hand
# from the AWS "Domains that you can register with Amazon Route 53" table and
# is BEST EFFORT: it can go stale when AWS or a registry changes policy.
# Because of that, `register()` also tolerates AWS rejecting privacy for a TLD
# that is not listed here (it retries once without privacy and reports the
# downgrade) — never assume this set is complete.
TLDS_WITHOUT_PRIVACY = {
    "us",
    "ca",
    "eu",
    "in",
    "sg",
    "com.au",
    "net.au",
    "org.au",
    "co.nz",
    "net.nz",
    "org.nz",
    "com.br",
    "co.za",
    "es",
    "com.es",
    "nom.es",
    "org.es",
    "fr",
    "it",
    "de",
    "se",
    "nu",
    "fi",
    "co.uk",
    "me.uk",
    "org.uk",
    "uk",
    "com.sg",
    "co.in",
    "net.in",
    "org.in",
    "firm.in",
    "gen.in",
    "ind.in",
}

# Route53 escapes non-printable / special octets in names as \ooo (octal).
_OCTAL_ESCAPE = re.compile(r"\\(\d{3})")


# ---------------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------------

def _domains_client(access_key=None, secret_key=None):
    """
    Build a route53domains client.

    ALWAYS us-east-1 — the Route53 Domains endpoint exists nowhere else.
    """
    session = get_session(
        access_key or settings.get("AWS_KEY", None),
        secret_key or settings.get("AWS_SECRET", None),
        DOMAINS_REGION)
    return session.client("route53domains")


def _dns_client(access_key=None, secret_key=None, region=None):
    """Build a route53 (hosted zone / record) client on the configured region."""
    session = get_session(
        access_key or settings.get("AWS_KEY", None),
        secret_key or settings.get("AWS_SECRET", None),
        region or settings.get("AWS_REGION", DOMAINS_REGION))
    return session.client("route53")


# ---------------------------------------------------------------------------
# name helpers
# ---------------------------------------------------------------------------

def normalize_name(name):
    """
    Normalize a DNS name: lowercase, trimmed, no trailing dot.

    Works for both registrable domains ("example.com") and record names
    ("_acme-challenge.example.com", "*.example.com").
    """
    if not name or not isinstance(name, str):
        raise me.ValueException("A domain name is required")
    value = name.strip().lower().rstrip(".")
    if not value:
        raise me.ValueException("A domain name is required")
    if any(c.isspace() for c in value) or "/" in value:
        raise me.ValueException(f"'{name}' is not a valid domain name")
    return value


def get_tld(name):
    """
    Return everything after the first label of a registrable domain.

    "example.com" -> "com", "example.com.au" -> "com.au". Registrable domains
    are always <label>.<tld>, so this is exact for the inputs this module
    accepts (it is NOT a public-suffix parser for arbitrary hostnames).
    """
    parts = normalize_name(name).split(".")
    if len(parts) < 2:
        raise me.ValueException(f"'{name}' is not a valid domain name")
    return ".".join(parts[1:])


def _clean_tld(tld):
    return (tld or "").strip().lower().lstrip(".").rstrip(".")


def _decode_name(name):
    """Decode Route53's octal escapes (e.g. \\052 -> *) and drop the trailing dot."""
    if not name:
        return name
    decoded = _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), name)
    return decoded.rstrip(".").lower()


def _zone_id(zone_id):
    """Accept either 'Z123' or '/hostedzone/Z123' and return the bare id."""
    if not zone_id:
        raise me.ValueException("A hosted zone id is required")
    return str(zone_id).strip().split("/")[-1]


def _change_id(change_id):
    if not change_id:
        return None
    return str(change_id).strip().split("/")[-1]


def format_txt_value(value):
    """
    Format a TXT value the way Route53 requires it.

    Route53 stores TXT values as one or more quoted character-strings, each at
    most 255 characters. A DKIM key or a long SPF record therefore has to be
    chunked, and every chunk has to be quoted — getting this wrong silently
    breaks SES verification and ACME DNS-01 validation.

    Already-quoted input is passed through untouched so callers can hand back
    a value they read out of Route53.
    """
    if value is None:
        raise me.ValueException("A TXT value is required")
    text = value if isinstance(value, str) else str(value)
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        return text
    if not text:
        return '""'
    chunks = [text[i:i + TXT_CHUNK_SIZE] for i in range(0, len(text), TXT_CHUNK_SIZE)]
    quoted = []
    for chunk in chunks:
        escaped = chunk.replace("\\", "\\\\").replace('"', '\\"')
        quoted.append(f'"{escaped}"')
    return " ".join(quoted)


def parse_txt_value(value):
    """
    Reverse of `format_txt_value` — join the quoted chunks back into the raw
    value. Unquoted input is returned as-is.
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    if '"' not in text:
        return text
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    if not parts:
        return text
    joined = "".join(parts)
    return joined.replace('\\"', '"').replace("\\\\", "\\")


# ---------------------------------------------------------------------------
# Route53 Domains — availability, pricing, registration
# ---------------------------------------------------------------------------

def supports_privacy(tld):
    """
    Best-effort answer to "can this TLD have WHOIS privacy?".

    See TLDS_WITHOUT_PRIVACY — AWS has no API for this, so the list is curated
    and can be stale. Treat a True here as "probably", never as a guarantee.
    """
    return _clean_tld(tld) not in TLDS_WITHOUT_PRIVACY


def _tristate(status, name=None):
    """
    Map a raw AWS availability enum onto the tri-state answer.

    INVALID_NAME_FOR_TLD maps to None here; `check_availability` raises for it
    BEFORE calling this — a bad request must stay a validation error on the
    direct path, but a suggestion row must never blow up the whole list.
    """
    if status in AVAILABLE_STATUSES:
        return True
    if status in UNAVAILABLE_STATUSES:
        return False
    if status in INDETERMINATE_STATUSES or status == INVALID_NAME_STATUS:
        return None
    logger.warning(f"route53 returned unknown availability '{status}' for {name}")
    return None


# Per-TLD price cache: tld -> (fetched_at, result). Registry pricing is
# effectively static, and without this every availability check pays a second
# AWS round-trip just to re-fetch it. Only real AWS answers are stored —
# including "not sold here" — while the exception path below never caches, so
# a transient failure cannot pin a TLD unsupported for a whole TTL.
_price_cache = {}


def _price_cache_ttl():
    """Cache lifetime in seconds. ROUTE53_PRICE_CACHE_HOURS <= 0 disables."""
    return int(settings.get("ROUTE53_PRICE_CACHE_HOURS", 24, kind="int")) * 3600


def list_prices(tld, access_key=None, secret_key=None, use_cache=True):
    """
    Return objict(tld, supported, registration_price, renewal_price, currency).

    NEVER raises. An unsupported or unknown TLD comes back with
    supported=False and null prices — availability search must not blow up
    because someone typed a TLD Route53 does not sell.

    Real answers are cached per TLD for ROUTE53_PRICE_CACHE_HOURS (default 24,
    <= 0 disables); hits and stores are copies, so no caller can mutate the
    cached entry. use_cache=False skips the read but the fresh answer still
    refreshes the store — the quote path uses it so no money decision ever
    rides a stale price. Explicit credentials bypass the cache entirely: it is
    keyed on TLD alone and must only ever hold house-account answers.
    """
    tld = _clean_tld(tld)
    result = objict(
        tld=tld,
        supported=False,
        registration_price=None,
        renewal_price=None,
        currency=None)
    if not tld:
        return result

    cacheable = access_key is None and secret_key is None
    ttl = _price_cache_ttl() if cacheable else 0
    if ttl > 0 and use_cache:
        entry = _price_cache.get(tld)
        if entry is not None and (time.time() - entry[0]) < ttl:
            return objict(entry[1])

    try:
        client = _domains_client(access_key, secret_key)
        resp = client.list_prices(Tld=tld)
    except Exception as err:
        logger.warning("Route53 Domains call failed operation=route53domains.list_prices tld=%s", tld)
        return result

    entries = resp.get("Prices") or []
    match = None
    for entry in entries:
        if _clean_tld(entry.get("Name")) == tld:
            match = entry
            break
    if match is None and entries:
        match = entries[0]

    if match is not None:
        registration = match.get("RegistrationPrice") or {}
        renewal = match.get("RenewalPrice") or {}
        result.supported = True
        result.registration_price = registration.get("Price")
        result.renewal_price = renewal.get("Price")
        result.currency = registration.get("Currency") or renewal.get("Currency")

    if ttl > 0:
        _price_cache[tld] = (time.time(), objict(result))
    return result


def check_availability(name, access_key=None, secret_key=None, idn_lang_code=None,
                       use_cache=True):
    """
    Check whether a domain can be registered.

    Returns objict(name, status, available, price, currency, tld,
    tld_supported, privacy_supported).

    `status` is the RAW AWS enum. `available` is TRI-STATE:
      True  -> AVAILABLE / AVAILABLE_RESERVED / AVAILABLE_PREORDER
      False -> UNAVAILABLE* / RESERVED
      None  -> PENDING / DONT_KNOW — the registry did not answer, retry later.
               Callers must never treat None as "yes" and start a purchase.

    INVALID_NAME_FOR_TLD raises a clean validation error rather than being
    reported as unavailable — it is a bad request, not a taken name.

    use_cache is forwarded to list_prices — pass False when the price will
    feed a money decision.
    """
    name = normalize_name(name)
    tld = get_tld(name)
    client = _domains_client(access_key, secret_key)
    params = {"DomainName": name}
    if idn_lang_code:
        params["IdnLangCode"] = idn_lang_code
    resp = client.check_domain_availability(**params)
    status = resp.get("Availability")

    if status == INVALID_NAME_STATUS:
        raise me.ValueException(f"'{name}' is not a valid domain name for the .{tld} registry")

    available = _tristate(status, name)

    prices = list_prices(tld, access_key=access_key, secret_key=secret_key,
                         use_cache=use_cache)
    return objict(
        name=name,
        status=status,
        available=available,
        price=prices.registration_price,
        currency=prices.currency,
        tld=tld,
        tld_supported=prices.supported,
        privacy_supported=supports_privacy(tld))


def get_domain_suggestions(name, count=10, only_available=True,
                           access_key=None, secret_key=None):
    """
    Ask Route53 Domains for alternate-name suggestions.

    Returns a list of objict(name, status, available) — `available` is the
    same tri-state as `check_availability`. AWS returns NO price on
    suggestions, and none is filled here; the dnsman registrar service fills
    prices per TLD from `list_prices` (cached). An unusable suggestion row is
    skipped with a warning rather than failing the list.
    """
    name = normalize_name(name)
    client = _domains_client(access_key, secret_key)
    resp = client.get_domain_suggestions(
        DomainName=name,
        SuggestionCount=int(count),
        OnlyAvailable=bool(only_available))

    suggestions = []
    for entry in resp.get("SuggestionsList") or []:
        raw = entry.get("DomainName")
        try:
            suggestion_name = normalize_name(raw)
        except me.ValueException:
            logger.warning(f"route53 returned an unusable suggestion {raw!r} for {name}")
            continue
        status = entry.get("Availability")
        suggestions.append(objict(
            name=suggestion_name,
            status=status,
            available=_tristate(status, suggestion_name)))
    return suggestions


def _is_privacy_rejection(err):
    """True when AWS refused a register call specifically because of privacy."""
    if not isinstance(err, botocore.exceptions.ClientError):
        return False
    return "privacy" in str(err).lower()


def register(name, contact, years=1, auto_renew=True, privacy=True,
             access_key=None, secret_key=None):
    """
    Register a domain through Route53 Domains.

    The same contact block is used for the Admin, Registrant and Tech roles.
    Returns objict(name, operation_id, privacy, privacy_downgraded).

    `privacy` in the RESULT is what was actually applied, which is not always
    what was asked for: TLDS_WITHOUT_PRIVACY is a hand-maintained list, so when
    AWS rejects the call purely because the TLD forbids privacy we retry ONCE
    with privacy off and report privacy=False / privacy_downgraded=True. A
    registration must not fail because our list went stale, but the caller must
    never record privacy it does not actually have.
    """
    name = normalize_name(name)
    if not contact:
        raise me.ValueException("Registrant contact details are required to register a domain")
    client = _domains_client(access_key, secret_key)
    params = {
        "DomainName": name,
        "DurationInYears": int(years),
        "AutoRenew": bool(auto_renew),
        "AdminContact": dict(contact),
        "RegistrantContact": dict(contact),
        "TechContact": dict(contact),
        "PrivacyProtectAdminContact": bool(privacy),
        "PrivacyProtectRegistrantContact": bool(privacy),
        "PrivacyProtectTechContact": bool(privacy),
    }
    try:
        resp = client.register_domain(**params)
    except Exception as err:
        if not (privacy and _is_privacy_rejection(err)):
            raise
        logger.warning(
            f"route53 refused privacy for {name}; retrying registration without privacy: {err}")
        params["PrivacyProtectAdminContact"] = False
        params["PrivacyProtectRegistrantContact"] = False
        params["PrivacyProtectTechContact"] = False
        resp = client.register_domain(**params)
        return objict(
            name=name,
            operation_id=resp.get("OperationId"),
            privacy=False,
            privacy_downgraded=True)
    return objict(
        name=name,
        operation_id=resp.get("OperationId"),
        privacy=bool(privacy),
        privacy_downgraded=False)


def _operation(data):
    return objict(
        operation_id=data.get("OperationId"),
        status=data.get("Status"),
        domain_name=data.get("DomainName"),
        type=data.get("Type"),
        message=data.get("Message"),
        submitted_on=data.get("SubmittedDate"))


def operation_detail(operation_id, access_key=None, secret_key=None):
    """Return objict(operation_id, status, domain_name, type, message, submitted_on)."""
    if not operation_id:
        raise me.ValueException("An operation id is required")
    client = _domains_client(access_key, secret_key)
    return _operation(client.get_operation_detail(OperationId=operation_id))


def list_operations(submitted_since=None, access_key=None, secret_key=None, max_pages=20):
    """
    List registrar operations, newest first, following the page marker.

    This is the crash-window reconciliation probe: a purchase row that was
    committed but whose OperationId was never stored can be matched back to
    the real AWS operation from here.
    """
    client = _domains_client(access_key, secret_key)
    params = {"MaxItems": 100}
    if submitted_since is not None:
        params["SubmittedSince"] = submitted_since
    operations = []
    pages = 0
    while True:
        resp = client.list_operations(**params)
        for entry in resp.get("Operations") or []:
            operations.append(_operation(entry))
        marker = resp.get("NextPageMarker")
        pages += 1
        if not marker or pages >= max_pages:
            break
        params["Marker"] = marker
    return operations


def list_registered_domains(access_key=None, secret_key=None, max_pages=20):
    """
    Every domain REGISTERED in this AWS account.

    Returns objict(domains=[objict(name, auto_renew, transfer_lock, expires)],
    truncated=bool) — deliberately NOT a bare list like `list_operations` above.
    An operations poll that stops early self-heals on the next run; an
    *inventory* that stops early silently answers "that is everything", which is
    a wrong answer. `truncated` is True when `max_pages` was reached with a page
    marker still outstanding, so a caller can report the gap instead of hiding
    it.

    NOTE the API asymmetry with `list_hosted_zones` below: route53domains takes
    an INTEGER MaxItems; route53 takes a STRING.
    """
    client = _domains_client(access_key, secret_key)
    params = {"MaxItems": 100}
    domains = []
    pages = 0
    truncated = False
    while True:
        resp = client.list_domains(**params)
        for entry in resp.get("Domains") or []:
            name = entry.get("DomainName")
            if not name:
                continue
            try:
                name = normalize_name(name)
            except me.ValueException:
                # One unparseable registrar row must not blank the whole
                # inventory — this is a listing, not a lookup. Hand the raw
                # value back lowercased and let the caller decide; dnsman's
                # discovery flags anything that will not normalize as
                # un-adoptable rather than dropping or exploding on it.
                name = name.strip().lower().rstrip(".")
            domains.append(objict(
                name=name,
                auto_renew=entry.get("AutoRenew"),
                transfer_lock=entry.get("TransferLock"),
                expires=entry.get("Expiry")))
        pages += 1
        marker = resp.get("NextPageMarker")
        if not marker:
            break
        if pages >= max_pages:
            truncated = True
            break
        params["Marker"] = marker
    return objict(domains=domains, truncated=truncated)


def get_domain_detail(name, access_key=None, secret_key=None):
    """Return the registrar view of a domain: contacts, expiry, nameservers, flags."""
    name = normalize_name(name)
    client = _domains_client(access_key, secret_key)
    resp = client.get_domain_detail(DomainName=name)
    nameservers = [ns.get("Name") for ns in (resp.get("Nameservers") or []) if ns.get("Name")]
    return objict(
        name=name,
        status_list=resp.get("StatusList") or [],
        nameservers=nameservers,
        auto_renew=resp.get("AutoRenew"),
        privacy=bool(resp.get("RegistrantPrivacy")),
        admin_privacy=bool(resp.get("AdminPrivacy")),
        registrant_privacy=bool(resp.get("RegistrantPrivacy")),
        tech_privacy=bool(resp.get("TechPrivacy")),
        registrar=resp.get("RegistrarName"),
        registered_on=resp.get("CreationDate"),
        expires=resp.get("ExpirationDate"),
        abuse_contact_email=resp.get("AbuseContactEmail"),
        admin_contact=objict.from_dict(resp.get("AdminContact") or {}),
        registrant_contact=objict.from_dict(resp.get("RegistrantContact") or {}),
        tech_contact=objict.from_dict(resp.get("TechContact") or {}))


def update_contacts(name, contact, access_key=None, secret_key=None):
    """Update the Admin/Registrant/Tech WHOIS contacts. Returns the OperationId."""
    name = normalize_name(name)
    if not contact:
        raise me.ValueException("Contact details are required")
    client = _domains_client(access_key, secret_key)
    resp = client.update_domain_contact(
        DomainName=name,
        AdminContact=dict(contact),
        RegistrantContact=dict(contact),
        TechContact=dict(contact))
    return resp.get("OperationId")


def update_privacy(name, enabled, access_key=None, secret_key=None):
    """Toggle WHOIS privacy on all three contacts. Returns the OperationId."""
    name = normalize_name(name)
    enabled = bool(enabled)
    client = _domains_client(access_key, secret_key)
    resp = client.update_domain_contact_privacy(
        DomainName=name,
        AdminPrivacy=enabled,
        RegistrantPrivacy=enabled,
        TechPrivacy=enabled)
    return resp.get("OperationId")


def update_auto_renew(name, enabled, access_key=None, secret_key=None):
    """Enable or disable registrar auto-renew. Returns True."""
    name = normalize_name(name)
    client = _domains_client(access_key, secret_key)
    if enabled:
        client.enable_domain_auto_renew(DomainName=name)
    else:
        client.disable_domain_auto_renew(DomainName=name)
    return True


# ---------------------------------------------------------------------------
# Route53 — hosted zones
# ---------------------------------------------------------------------------

def list_hosted_zones(access_key=None, secret_key=None, region=None, max_pages=20):
    """
    Every hosted zone in this AWS account.

    Returns objict(zones=[objict(id, name, private, record_count)],
    truncated=bool) — same contract and the same reasoning as
    `list_registered_domains` above: a partial inventory that reports as
    complete is worse than one that admits the gap.

    Private zones are RETURNED, flagged `private=True`, not filtered. This
    helper stays a faithful mirror of AWS; the policy call belongs to the
    caller (dnsman's discovery service excludes them — see
    mojo/apps/dnsman/services/onboarding.py).

    NOTE: MaxItems is a STRING on this API, as in `list_records` below — but an
    INTEGER on route53domains. Passing the wrong type is a ParamValidationError.
    """
    client = _dns_client(access_key, secret_key, region)
    params = {"MaxItems": "100"}
    zones = []
    pages = 0
    truncated = False
    while True:
        resp = client.list_hosted_zones(**params)
        for zone in resp.get("HostedZones") or []:
            if not zone.get("Id"):
                continue
            config = zone.get("Config") or {}
            zones.append(objict(
                id=_zone_id(zone.get("Id")),
                name=_decode_name(zone.get("Name")),
                private=bool(config.get("PrivateZone")),
                record_count=zone.get("ResourceRecordSetCount")))
        pages += 1
        if not resp.get("IsTruncated"):
            break
        marker = resp.get("NextMarker")
        if not marker:
            break
        if pages >= max_pages:
            truncated = True
            break
        params["Marker"] = marker
    return objict(zones=zones, truncated=truncated)


def find_zone_id(name, access_key=None, secret_key=None, region=None):
    """
    Return the hosted zone id for `name`, or None.

    EXACT name match only. `list_hosted_zones_by_name` returns a lexically
    ordered page STARTING AT the requested name, so a prefix/startswith match
    would happily select a neighbouring zone ("example.com.au" when asked for
    "example.com") and every subsequent record write would land in the wrong
    zone. Because the page starts at our name, any zone we could match is on
    the first page — no pagination needed.

    When both a private and a public zone carry the name, the public one wins.
    """
    name = normalize_name(name)
    client = _dns_client(access_key, secret_key, region)
    resp = client.list_hosted_zones_by_name(DNSName=f"{name}.")
    private_match = None
    for zone in resp.get("HostedZones") or []:
        if _decode_name(zone.get("Name")) != name:
            continue
        config = zone.get("Config") or {}
        if not config.get("PrivateZone"):
            return _zone_id(zone.get("Id"))
        if private_match is None:
            private_match = _zone_id(zone.get("Id"))
    return private_match


def get_zone(zone_id, access_key=None, secret_key=None, region=None):
    """Return objict(id, name, private, record_count, name_servers, comment)."""
    zone_id = _zone_id(zone_id)
    client = _dns_client(access_key, secret_key, region)
    resp = client.get_hosted_zone(Id=zone_id)
    zone = resp.get("HostedZone") or {}
    config = zone.get("Config") or {}
    delegation = resp.get("DelegationSet") or {}
    return objict(
        id=_zone_id(zone.get("Id") or zone_id),
        name=_decode_name(zone.get("Name")),
        private=bool(config.get("PrivateZone")),
        comment=config.get("Comment"),
        record_count=zone.get("ResourceRecordSetCount"),
        name_servers=delegation.get("NameServers") or [])


def create_hosted_zone(name, comment=None, caller_reference=None,
                       access_key=None, secret_key=None, region=None):
    """
    Create a public hosted zone.

    Needed for adopt-with-no-zone, and for a purchased domain whose
    auto-created zone is missing.
    """
    name = normalize_name(name)
    client = _dns_client(access_key, secret_key, region)
    resp = client.create_hosted_zone(
        Name=name,
        CallerReference=caller_reference or f"mojo-{name}-{uuid.uuid4().hex}",
        HostedZoneConfig={"Comment": comment or "created by django-mojo dnsman"})
    zone = resp.get("HostedZone") or {}
    delegation = resp.get("DelegationSet") or {}
    change = resp.get("ChangeInfo") or {}
    return objict(
        zone_id=_zone_id(zone.get("Id")),
        name=_decode_name(zone.get("Name")) or name,
        name_servers=delegation.get("NameServers") or [],
        change_id=_change_id(change.get("Id")))


def get_change(change_id, access_key=None, secret_key=None, region=None):
    """Return objict(change_id, status, insync, submitted_on) for a change batch."""
    if not change_id:
        raise me.ValueException("A change id is required")
    client = _dns_client(access_key, secret_key, region)
    resp = client.get_change(Id=_change_id(change_id))
    info = resp.get("ChangeInfo") or {}
    status = info.get("Status")
    return objict(
        change_id=_change_id(info.get("Id")) or _change_id(change_id),
        status=status,
        insync=status == "INSYNC",
        submitted_on=info.get("SubmittedAt"))


# ---------------------------------------------------------------------------
# Route53 — records
# ---------------------------------------------------------------------------

def _record_from_rrset(rrset):
    # The value list is deliberately NOT called "values": objict subclasses
    # dict, so `record.values` resolves to the dict method and would silently
    # hand callers a bound method instead of the record's values.
    alias = rrset.get("AliasTarget") or None
    return objict(
        type=rrset.get("Type"),
        name=_decode_name(rrset.get("Name")),
        record_values=[rr.get("Value") for rr in (rrset.get("ResourceRecords") or [])],
        ttl=rrset.get("TTL"),
        set_identifier=rrset.get("SetIdentifier"),
        alias=objict(
            dns_name=_decode_name(alias.get("DNSName")),
            zone_id=alias.get("HostedZoneId"),
            evaluate_target_health=alias.get("EvaluateTargetHealth")) if alias else None)


def list_records(zone_id, access_key=None, secret_key=None, region=None, max_pages=200):
    """
    List every record in a hosted zone.

    Returns a list of objict(type, name, record_values, ttl, set_identifier,
    alias). The value list is `record_values`, NOT `values` — objict subclasses
    dict, so `.values` would resolve to the dict method.

    Follows `IsTruncated` using NextRecordName / NextRecordType (and
    NextRecordIdentifier for weighted/latency sets). A zone with more than one
    page of records is completely normal, so a single unpaginated call would
    silently return a partial view.
    """
    zone_id = _zone_id(zone_id)
    client = _dns_client(access_key, secret_key, region)
    params = {"HostedZoneId": zone_id, "MaxItems": "300"}
    records = []
    pages = 0
    while True:
        resp = client.list_resource_record_sets(**params)
        for rrset in resp.get("ResourceRecordSets") or []:
            records.append(_record_from_rrset(rrset))
        pages += 1
        if not resp.get("IsTruncated") or pages >= max_pages:
            break
        next_name = resp.get("NextRecordName")
        if not next_name:
            break
        params["StartRecordName"] = next_name
        if resp.get("NextRecordType"):
            params["StartRecordType"] = resp.get("NextRecordType")
        if resp.get("NextRecordIdentifier"):
            params["StartRecordIdentifier"] = resp.get("NextRecordIdentifier")
    return records


def _guard_record(zone_name, rtype, name):
    """
    Validate a record change against its zone.

    Refuses the two ways a record change can break a zone outright:
      - the apex NS / SOA record sets (deleting or rewriting them takes the
        whole domain off the internet)
      - any name that is not inside the zone (Route53 would reject it, but the
        clearer failure belongs here, before the network call)
    """
    zone_name = normalize_name(zone_name)
    name = normalize_name(name)
    rtype = (rtype or "").strip().upper()
    if not rtype:
        raise me.ValueException("A record type is required")
    if name != zone_name and not name.endswith(f".{zone_name}"):
        raise me.ValueException(
            f"'{name}' is not inside the '{zone_name}' hosted zone")
    if name == zone_name and rtype in ("NS", "SOA"):
        raise me.ValueException(
            f"Refusing to modify the apex {rtype} record set for '{zone_name}'")
    return rtype, name


def _resolve_zone_name(zone_id, zone_name, access_key, secret_key, region):
    if zone_name:
        return normalize_name(zone_name)
    return get_zone(zone_id, access_key=access_key, secret_key=secret_key, region=region).name


def _submit_change(client, zone_id, action, rrset, comment=None):
    resp = client.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Comment": comment or "django-mojo dnsman",
            "Changes": [{"Action": action, "ResourceRecordSet": rrset}],
        })
    return _change_id((resp.get("ChangeInfo") or {}).get("Id"))


def upsert_record(zone_id, rtype, name, values, ttl=300, zone_name=None, comment=None,
                  access_key=None, secret_key=None, region=None):
    """
    Create or replace a record set. Returns the ChangeId.

    `values` may be a single string or a list — the whole desired value list is
    always sent, because Route53 UPSERT replaces the record set wholesale.
    TXT/SPF values are quoted and 255-chunked automatically.
    """
    zone_id = _zone_id(zone_id)
    zone_name = _resolve_zone_name(zone_id, zone_name, access_key, secret_key, region)
    rtype, name = _guard_record(zone_name, rtype, name)
    if isinstance(values, str):
        values = [values]
    if not values:
        raise me.ValueException("At least one record value is required")
    if rtype in ("TXT", "SPF"):
        values = [format_txt_value(v) for v in values]
    client = _dns_client(access_key, secret_key, region)
    rrset = {
        "Name": name,
        "Type": rtype,
        "TTL": int(ttl),
        "ResourceRecords": [{"Value": v} for v in values],
    }
    return _submit_change(client, zone_id, "UPSERT", rrset, comment)


def delete_record(zone_id, rtype, name, values=None, ttl=None, zone_name=None, comment=None,
                  access_key=None, secret_key=None, region=None):
    """
    Delete a record set. Returns the ChangeId.

    Route53 requires the DELETE change to carry the record set exactly as it is
    stored, so when `values`/`ttl` are not supplied the current set is read
    back first. A missing record raises rather than silently no-opping.
    """
    zone_id = _zone_id(zone_id)
    zone_name = _resolve_zone_name(zone_id, zone_name, access_key, secret_key, region)
    rtype, name = _guard_record(zone_name, rtype, name)
    if isinstance(values, str):
        values = [values]
    if values and rtype in ("TXT", "SPF"):
        values = [format_txt_value(v) for v in values]

    if not values or ttl is None:
        existing = None
        for record in list_records(zone_id, access_key=access_key, secret_key=secret_key,
                                   region=region):
            if record.type == rtype and record.name == name:
                existing = record
                break
        if existing is None:
            raise me.ValueException(
                f"No {rtype} record named '{name}' exists in this hosted zone")
        if not values:
            values = existing.record_values
        if ttl is None:
            ttl = existing.ttl

    if not values:
        raise me.ValueException(
            f"The {rtype} record '{name}' has no values to delete")

    client = _dns_client(access_key, secret_key, region)
    rrset = {
        "Name": name,
        "Type": rtype,
        "TTL": int(ttl if ttl is not None else 300),
        "ResourceRecords": [{"Value": v} for v in values],
    }
    return _submit_change(client, zone_id, "DELETE", rrset, comment)
