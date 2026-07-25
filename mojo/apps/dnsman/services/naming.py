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

    value = str(name).strip().lower().rstrip(".")
    domain_name = domain_name.strip().lower().rstrip(".")

    if value in ("", "@", domain_name):
        return domain_name
    if value.endswith("." + domain_name):
        return value
    return f"{value}.{domain_name}"


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
