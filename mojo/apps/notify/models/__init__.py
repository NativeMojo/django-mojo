from .account import Account
from .bounce import Bounce
from .complaint import Complaint
from .inbox import Inbox
from .inbox_message import InboxMessage
from .message import Message, Attachment
from .outbox import Outbox
from .outbox_message import OutboxMessage
from .template import NotifyTemplate

# Backward compatibility alias
MailTemplate = NotifyTemplate
