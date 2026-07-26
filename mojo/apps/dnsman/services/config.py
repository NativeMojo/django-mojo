"""
dnsman capability discovery.

The one place a caller can ask "what's turned on?" without probing a gated
action to find out. Reports shapes and booleans, never secrets:
`registrant_contact_configured` says whether purchasing has its prerequisite
met, not what the contact is — `registrar._registrant_contact()` already
documents that the contact itself is never echoed back to any caller.
"""

from mojo import errors as me
from mojo.helpers import acme
from mojo.apps.dnsman.models.domain import PROVIDER_ROUTE53, PROVIDER_GODADDY
from mojo.apps.dnsman.services import registrar
from mojo.apps.dnsman.services import dns as dns_service
from mojo.apps.dnsman.services import certs


def _registrant_contact_configured():
    try:
        registrar._registrant_contact()
        return True
    except me.ValueException:
        return False


def get_config():
    directory_url = certs.directory_url()
    return dict(
        purchase_enabled=registrar._purchase_enabled(),
        registrant_contact_configured=_registrant_contact_configured(),
        max_domain_price=str(registrar._max_price()),
        currency="USD",
        quote_ttl_minutes=registrar._quote_ttl_minutes(),
        allowed_record_types=sorted(dns_service.allowed_record_types()),
        providers=[
            dict(name=PROVIDER_ROUTE53, purchase=True, requires_credential=False),
            dict(name=PROVIDER_GODADDY, purchase=False, requires_credential=True),
        ],
        acme=dict(
            configured=bool(directory_url),
            # Never echo the directory URL itself -- a self-hosted ACME
            # server could embed a token in its path. staging is derived by
            # comparison only.
            staging=(directory_url == acme.LETSENCRYPT_STAGING),
        ),
        cert_renew_days=certs.renew_days(),
    )
