"""Resolve where a managed-domain WebApp address points — the one authority.

A managed domain (route53/godaddy) has the platform write the app's CNAME, so
onboarding needs the single thing the browser cannot supply: the destination
that record points at. Historically that was the file-only
``EDGE_WEBAPP_CNAME_TARGET``, required before any managed onboarding could
proceed. Here that setting becomes an explicit override, and the destination is
otherwise derived from the platform's own public address (``BASE_URL``) — so an
ordinary controlled domain needs zero destination configuration.

The contract is small and used by precheck, create and advance alike:
``resolve(hostname=None)`` returns an objict ``{type, value, provenance}`` or
raises. ``DestinationUnavailable`` means the installation cannot serve app
addresses yet — a configuration-required state the operator fixes in System
Setup. A plain ``ValueException`` means the *requested* hostname is unusable
(it is the platform's own address) while the installation itself is fine.

The destination is always a CNAME to a hostname. A single-node EIP install and
a load-balanced fleet both reach their serving nginx through the platform's
public hostname, so a plain CNAME to it works at every DNS host without
assuming Route53 aliases. Split topologies — where web apps are served by a
tier the platform hostname does not front — keep the override.
"""

from urllib.parse import urlsplit

from objict import objict

from mojo import errors as me
from mojo.helpers.settings import settings

from mojo.apps.edge import validators


OVERRIDE_SETTING = "EDGE_WEBAPP_CNAME_TARGET"


class DestinationUnavailable(me.ValueException):
    """The installation has no usable WebApp serving destination yet.

    Distinct from a bad *request*: this says the platform itself is not ready
    to serve app addresses, and the message steers the operator to System Setup
    rather than blaming the address they typed.
    """


def _is_ip_literal(host):
    import ipaddress

    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _override():
    """The explicit override, stripped exactly as the legacy target was."""
    return str(settings.get_static(OVERRIDE_SETTING, "") or "").strip().rstrip(".")


def _base_url():
    """The platform's public origin (``BASE_URL``), read through one seam.

    Isolated so tests patch this instead of writing the shared ``Setting`` row:
    the suite runs against a single database and a stray ``BASE_URL`` write
    bleeds across modules (the reason commit 4db6c340 unhooked the Setup badge
    test from that row).
    """
    from mojo.apps.account.services import system_settings

    return str(system_settings.get_value(system_settings.BASE_URL) or "").strip()


def resolve(hostname=None):
    """Return the serving destination for a managed WebApp address.

    Precedence: the explicit ``EDGE_WEBAPP_CNAME_TARGET`` override, then the
    platform's own public hostname. Raises ``DestinationUnavailable`` when
    neither yields a usable hostname, and a plain ``ValueException`` when
    ``hostname`` is itself the resolved destination.
    """
    override = _override()
    if override:
        try:
            validators.validate_server_name(override)
        except me.MojoException:
            raise DestinationUnavailable(
                "The web-app address destination this platform was given is "
                "not a usable hostname. Update it in System Setup.")
        return _finish(override, "override", hostname)

    host = (urlsplit(_base_url()).hostname or "").strip().rstrip(".")
    if not host:
        raise DestinationUnavailable(
            "This platform's public web address isn't set up yet, so app "
            "addresses can't be created. Finish System Setup first.")
    if _is_ip_literal(host):
        raise DestinationUnavailable(
            "This platform is reached at a numeric address, which a web-app "
            "address can't point to. Finish System Setup with a domain name.")
    try:
        validators.validate_server_name(host)
    except me.MojoException:
        raise DestinationUnavailable(
            "This platform's public web address isn't a usable hostname yet. "
            "Finish System Setup first.")
    return _finish(host, "platform_base_url", hostname)


def _finish(value, provenance, hostname):
    if hostname and str(hostname).strip().rstrip(".").lower() == value.lower():
        raise me.ValueException(
            "That address is where this platform itself runs. Choose a "
            "different address for your app.")
    return objict(type="CNAME", value=value, provenance=provenance)
