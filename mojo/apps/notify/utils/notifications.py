from django.utils import timezone
from django.db import transaction
from django.core.exceptions import ValidationError
from typing import Optional, Dict, Any, List, Union
import logging

from ..models import Account, Inbox, InboxMessage, Outbox, OutboxMessage


logger = logging.getLogger(__name__)


class NotificationError(Exception):
    """Base exception for notification errors"""
    pass


class MessageSender:
    """
    High-level interface for sending messages through various notification channels
    """
    
    @staticmethod
    def send_email(
        to_address: str,
        subject: str,
        message: str,
        from_address: Optional[str] = None,
        group=None,
        user=None,
        metadata: Optional[Dict[str, Any]] = None,
        scheduled_at: Optional[timezone.datetime] = None
    ) -> OutboxMessage:
        """Send an email message"""
        return MessageSender._send_message(
            kind=Account.EMAIL,
            to_address=to_address,
            subject=subject,
            message=message,
            from_address=from_address,
            group=group,
            user=user,
            metadata=metadata or {},
            scheduled_at=scheduled_at
        )
    
    @staticmethod
    def send_sms(
        to_address: str,
        message: str,
        from_address: Optional[str] = None,
        group=None,
        user=None,
        metadata: Optional[Dict[str, Any]] = None,
        scheduled_at: Optional[timezone.datetime] = None
    ) -> OutboxMessage:
        """Send an SMS message"""
        return MessageSender._send_message(
            kind=Account.SMS,
            to_address=to_address,
            subject=None,
            message=message,
            from_address=from_address,
            group=group,
            user=user,
            metadata=metadata or {},
            scheduled_at=scheduled_at
        )
    
    @staticmethod
    def send_whatsapp(
        to_address: str,
        message: str,
        from_address: Optional[str] = None,
        group=None,
        user=None,
        metadata: Optional[Dict[str, Any]] = None,
        scheduled_at: Optional[timezone.datetime] = None
    ) -> OutboxMessage:
        """Send a WhatsApp message"""
        return MessageSender._send_message(
            kind=Account.WHATSAPP,
            to_address=to_address,
            subject=None,
            message=message,
            from_address=from_address,
            group=group,
            user=user,
            metadata=metadata or {},
            scheduled_at=scheduled_at
        )
    
    @staticmethod
    def send_push(
        to_address: str,
        message: str,
        title: Optional[str] = None,
        from_address: Optional[str] = None,
        group=None,
        user=None,
        metadata: Optional[Dict[str, Any]] = None,
        scheduled_at: Optional[timezone.datetime] = None
    ) -> OutboxMessage:
        """Send a push notification"""
        metadata = metadata or {}
        if title:
            metadata['title'] = title
            
        return MessageSender._send_message(
            kind=Account.PUSH,
            to_address=to_address,
            subject=title,
            message=message,
            from_address=from_address,
            group=group,
            user=user,
            metadata=metadata,
            scheduled_at=scheduled_at
        )
    
    @staticmethod
    def _send_message(
        kind: str,
        to_address: str,
        message: str,
        subject: Optional[str] = None,
        from_address: Optional[str] = None,
        group=None,
        user=None,
        metadata: Optional[Dict[str, Any]] = None,
        scheduled_at: Optional[timezone.datetime] = None
    ) -> OutboxMessage:
        """Internal method to send messages of any kind"""
        
        # Find appropriate outbox
        outbox = OutboxFinder.find_outbox(
            kind=kind,
            from_address=from_address,
            group=group
        )
        
        if not outbox:
            raise NotificationError(f"No active outbox found for {kind} messages")
        
        if not outbox.can_send_messages():
            raise NotificationError(f"Outbox {outbox} cannot send messages")
            
        if not outbox.check_rate_limit():
            raise NotificationError(f"Outbox {outbox} has exceeded rate limit")
        
        # Use outbox address if no from_address specified
        if not from_address:
            from_address = outbox.address
        
        # Create the outbox message
        with transaction.atomic():
            outbox_message = OutboxMessage.objects.create(
                outbox=outbox,
                user=user,
                group=group or outbox.group,
                to_address=to_address,
                from_address=from_address,
                subject=subject,
                message=message,
                metadata=metadata or {},
                scheduled_at=scheduled_at
            )
        
        logger.info(f"Queued {kind} message from {from_address} to {to_address}")
        return outbox_message


class OutboxFinder:
    """
    Utility for finding appropriate outboxes for sending messages
    """
    
    @staticmethod
    def find_outbox(
        kind: str,
        from_address: Optional[str] = None,
        group=None
    ) -> Optional[Outbox]:
        """Find the best outbox for sending a message"""
        
        query = Outbox.objects.filter(
            account__kind=kind,
            account__is_active=True,
            is_active=True
        ).select_related('account', 'group')
        
        # Prefer outboxes with matching group
        if group:
            group_matches = query.filter(group=group)
            if group_matches.exists():
                query = group_matches
        
        # Prefer outboxes with matching address
        if from_address:
            address_matches = query.filter(address=from_address)
            if address_matches.exists():
                return address_matches.first()
        
        # Return any available outbox
        return query.first()
    
    @staticmethod
    def get_outboxes_for_account(account: Account) -> List[Outbox]:
        """Get all active outboxes for an account"""
        return list(
            Outbox.objects.filter(
                account=account,
                is_active=True
            ).select_related('group')
        )


class MessageProcessor:
    """
    Utility for processing received messages
    """
    
    @staticmethod
    def process_inbox_message(inbox_message: InboxMessage) -> bool:
        """Process a received inbox message"""
        if inbox_message.processed:
            return True
            
        inbox = inbox_message.inbox
        handler_path = inbox.get_handler()
        
        if not handler_path:
            logger.warning(f"No handler configured for inbox {inbox}")
            return False
        
        try:
            # Import and call the handler
            handler_func = _import_handler(handler_path)
            if handler_func:
                result = handler_func(inbox_message)
                inbox_message.mark_processed()
                logger.info(f"Processed message {inbox_message.id} with handler {handler_path}")
                return True
        except Exception as e:
            logger.error(f"Error processing message {inbox_message.id}: {e}")
            return False
        
        return False
    
    @staticmethod
    def bulk_process_inbox_messages(inbox: Inbox, limit: int = 100) -> int:
        """Process multiple unprocessed messages for an inbox"""
        messages = InboxMessage.objects.filter(
            inbox=inbox,
            processed=False
        ).order_by('created')[:limit]
        
        processed_count = 0
        for message in messages:
            if MessageProcessor.process_inbox_message(message):
                processed_count += 1
        
        return processed_count


class BulkNotifier:
    """
    Utility for sending bulk notifications
    """
    
    @staticmethod
    def send_bulk_email(
        recipients: List[str],
        subject: str,
        message: str,
        from_address: Optional[str] = None,
        group=None,
        metadata: Optional[Dict[str, Any]] = None,
        scheduled_at: Optional[timezone.datetime] = None,
        batch_size: int = 100
    ) -> List[OutboxMessage]:
        """Send bulk email messages"""
        
        messages = []
        for i in range(0, len(recipients), batch_size):
            batch = recipients[i:i + batch_size]
            
            with transaction.atomic():
                for recipient in batch:
                    try:
                        msg = MessageSender.send_email(
                            to_address=recipient,
                            subject=subject,
                            message=message,
                            from_address=from_address,
                            group=group,
                            metadata=metadata,
                            scheduled_at=scheduled_at
                        )
                        messages.append(msg)
                    except Exception as e:
                        logger.error(f"Failed to queue email to {recipient}: {e}")
        
        return messages
    
    @staticmethod
    def send_bulk_sms(
        recipients: List[str],
        message: str,
        from_address: Optional[str] = None,
        group=None,
        metadata: Optional[Dict[str, Any]] = None,
        scheduled_at: Optional[timezone.datetime] = None,
        batch_size: int = 100
    ) -> List[OutboxMessage]:
        """Send bulk SMS messages"""
        
        messages = []
        for i in range(0, len(recipients), batch_size):
            batch = recipients[i:i + batch_size]
            
            with transaction.atomic():
                for recipient in batch:
                    try:
                        msg = MessageSender.send_sms(
                            to_address=recipient,
                            message=message,
                            from_address=from_address,
                            group=group,
                            metadata=metadata,
                            scheduled_at=scheduled_at
                        )
                        messages.append(msg)
                    except Exception as e:
                        logger.error(f"Failed to queue SMS to {recipient}: {e}")
        
        return messages


class MessageStats:
    """
    Utility for getting message statistics
    """
    
    @staticmethod
    def get_outbox_stats(outbox: Outbox) -> Dict[str, Any]:
        """Get statistics for an outbox"""
        messages = outbox.messages.all()
        
        return {
            'total_messages': messages.count(),
            'pending_messages': messages.filter(status=OutboxMessage.PENDING).count(),
            'sent_messages': messages.filter(status=OutboxMessage.SENT).count(),
            'failed_messages': messages.filter(status=OutboxMessage.FAILED).count(),
            'recent_messages': messages.filter(
                created__gte=timezone.now() - timezone.timedelta(hours=24)
            ).count(),
        }
    
    @staticmethod
    def get_inbox_stats(inbox: Inbox) -> Dict[str, Any]:
        """Get statistics for an inbox"""
        messages = inbox.messages.all()
        
        return {
            'total_messages': messages.count(),
            'processed_messages': messages.filter(processed=True).count(),
            'unprocessed_messages': messages.filter(processed=False).count(),
            'recent_messages': messages.filter(
                created__gte=timezone.now() - timezone.timedelta(hours=24)
            ).count(),
        }


def _import_handler(handler_path: str):
    """Import a handler function from a module path"""
    try:
        module_path, function_name = handler_path.rsplit('.', 1)
        module = __import__(module_path, fromlist=[function_name])
        return getattr(module, function_name)
    except (ImportError, AttributeError, ValueError) as e:
        logger.error(f"Failed to import handler {handler_path}: {e}")
        return None


# Convenience functions for common operations
def send_email(to_address: str, subject: str, message: str, **kwargs) -> OutboxMessage:
    """Convenience function to send an email"""
    return MessageSender.send_email(to_address, subject, message, **kwargs)


def send_sms(to_address: str, message: str, **kwargs) -> OutboxMessage:
    """Convenience function to send an SMS"""
    return MessageSender.send_sms(to_address, message, **kwargs)


def send_whatsapp(to_address: str, message: str, **kwargs) -> OutboxMessage:
    """Convenience function to send a WhatsApp message"""
    return MessageSender.send_whatsapp(to_address, message, **kwargs)


def send_push(to_address: str, message: str, title: Optional[str] = None, **kwargs) -> OutboxMessage:
    """Convenience function to send a push notification"""
    return MessageSender.send_push(to_address, message, title, **kwargs)