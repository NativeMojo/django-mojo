"""
Domain-name normalization shared by every dnsman service.

One rule, applied at every edge: names are lowercased, stripped of a trailing
dot, and IDNA-encoded before they reach a provider API or the database. The
unique constraint on Domain.name is only meaningful if every write path agrees
on what "the same name" means.
"""

import re

from mojo import errors as me


# A conservative label check. Deliberately rejects underscores in the
# registrable name even though they are legal in some record names — an
# underscore here almost always means a record name leaked into a domain field.
LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# Record labels are looser than registrable-domain labels: leading underscores
# are legal and load-bearing (`_acme-challenge`, `_dmarc`, `_domainkey`), and a
# wildcard is legal as the leftmost label.
#
# This charset check is a SECURITY control, not tidiness. A record name is
# otherwise only validated by zone suffix, and some providers interpolate the
# relative label straight into a REST URL path — so a name like
# "a/../../../../victim.com/records/A/@.example.com" passes an endswith() check,
# and the HTTP client then normalizes the dot-segments into a write against a
# DIFFERENT domain in the same provider account. Rejecting the charset kills
# that class outright, at the one place every write path already passes through.
RECORD_LABEL_RE = re.compile(r"^(\*|[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)$")


def normalize_domain(name):
    """
    Normalize a registrable domain name.

    Raises ValueException when the input cannot be a domain at all, so a bad
    value fails at the edge rather than as a confusing provider error later.
    """
    if not name or not isinstance(name, str):
        raise me.ValueException("A domain name is required")

    value = name.strip().lower().rstrip(".")

    if not value:
        raise me.ValueException("A domain name is required")
    if " " in value or "/" in value or ":" in value:
        raise me.ValueException(f"'{name}' is not a valid domain name")

    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        raise me.ValueException(f"'{name}' is not a valid domain name")

    labels = value.split(".")
    if len(labels) < 2:
        raise me.ValueException(f"'{name}' is not a fully qualified domain name")
    for label in labels:
        if not LABEL_RE.match(label):
            raise me.ValueException(f"'{name}' is not a valid domain name")

    return value


def normalize_record_name(name, domain_name):
    """
    Normalize a DNS record name to an FQDN with no trailing dot.

    Services always speak FQDN; converting to a provider's preferred form
    (relative labels, '@' for the apex) belongs to that provider's adapter and
    nowhere else.
    """
    if name is None:
        raise me.ValueException("A record name is required")

    raw = str(name).strip().lower()
    value = raw.rstrip(".")
    domain_name = domain_name.strip().lower().rstrip(".")

    # Only these three forms mean "the apex". Checking `raw` rather than the
    # dot-stripped `value` matters: a nonsense name like ".." strips to empty
    # and would otherwise be silently treated as the apex, so a typo could
    # overwrite the zone's apex record.
    if raw in ("", "@") or value == domain_name:
        return domain_name
    if not value:
        raise me.ValueException(f"'{name}' is not a valid record name")
    if value.endswith("." + domain_name):
        validate_record_labels(value)
        return value
    validate_record_labels(f"{value}.{domain_name}")
    return f"{value}.{domain_name}"


def validate_record_labels(fqdn):
    """
    Reject any record name whose labels are not legal DNS labels.

    A zone-suffix check alone is not enough: it constrains the END of the name
    and says nothing about what precedes it. Providers that interpolate the
    relative portion into a REST URL path will happily accept slashes and
    dot-segments, which the HTTP client then normalizes into a request against
    a different domain entirely. Validating the charset here — the one place
    every write path already passes through — closes that off.
    """
    labels = fqdn.split(".")
    for index, label in enumerate(labels):
        if label == "*" and index != 0:
            raise me.ValueException(
                f"'{fqdn}' is not a valid record name (a wildcard may only be the leftmost label)")
        if not RECORD_LABEL_RE.match(label):
            raise me.ValueException(f"'{fqdn}' is not a valid record name")
    return fqdn


def is_in_zone(record_name, domain_name):
    """True when record_name sits inside domain_name's zone."""
    record_name = record_name.strip().lower().rstrip(".")
    domain_name = domain_name.strip().lower().rstrip(".")
    return record_name == domain_name or record_name.endswith("." + domain_name)


def split_tld(name):
    """
    Return the TLD portion used for pricing and privacy-capability lookups.

    Handles the common two-level public suffixes we care about (co.uk, com.au
    and friends) without dragging in a full public-suffix list — Route53's
    supported-TLD set is small enough that the long tail does not arise.
    """
    value = normalize_domain(name)
    parts = value.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org", "gov", "ac"):
        return ".".join(parts[-2:])
    return parts[-1]
