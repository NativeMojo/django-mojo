"""
AWS services package

Convenience re-exports for the email sending service so callers can do:
    from mojo.apps.aws.services import send_email, send_template_email
"""

from .email import send_email, send_template_email, send_with_template

__all__ = [
    "send_email",
    "send_template_email",
    "send_with_template",
]
