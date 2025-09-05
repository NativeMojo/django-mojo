"""
AWS services package

Convenience re-exports for AWS services so callers can do:
    from mojo.apps.aws.services import send_email, send_template_email
    from mojo.apps.aws.services import onboard_email_domain, audit_email_domain
"""

from .email import send_email, send_template_email, send_with_template
from .email_ops import (
    onboard_email_domain,
    audit_email_domain,
    reconcile_email_domain,
    generate_audit_recommendations,
    EmailDomainNotFound,
    InvalidConfiguration,
)

__all__ = [
    # Email sending
    "send_email",
    "send_template_email",
    "send_with_template",
    # Domain management
    "onboard_email_domain",
    "audit_email_domain",
    "reconcile_email_domain",
    "generate_audit_recommendations",
    # Exceptions
    "EmailDomainNotFound",
    "InvalidConfiguration",
]
