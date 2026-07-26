"""
Provider adapters for dnsman.

`services/dns.py` is the only module that should pick one — call
`dns.get_adapter(domain)` rather than importing an adapter directly, so the
fail-closed credential gate is never bypassed.
"""

from .base import DnsProvider
from .route53_provider import Route53Provider
from .godaddy_provider import GoDaddyProvider

__all__ = [
    "DnsProvider",
    "Route53Provider",
    "GoDaddyProvider",
]
