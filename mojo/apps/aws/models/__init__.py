"""
AWS models package exports
"""

from .email_domain import EmailDomain
from .mailbox import Mailbox
from .incoming_email import IncomingEmail
from .email_attachment import EmailAttachment
from .sent_message import SentMessage
from .email_template import EmailTemplate

__all__ = [
    "EmailDomain",
    "Mailbox",
    "IncomingEmail",
    "EmailAttachment",
    "SentMessage",
    "EmailTemplate",
]
