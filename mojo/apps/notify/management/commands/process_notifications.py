from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.db import transaction, models
from django.conf import settings
import time
import logging
import signal
import sys
from datetime import timedelta
from typing import Optional

from mojo.apps.notify.models import (
    Account, Inbox, InboxMessage, Outbox, OutboxMessage
)
from mojo.apps.notify.utils.notifications import MessageProcessor, _import_handler

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Process notification messages (inbox and outbox)'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.should_stop = False
        
    def add_arguments(self, parser):
        parser.add_argument(
            '--daemon',
            action='store_true',
            help='Run as daemon (continuous processing)',
        )
        
        parser.add_argument(
            '--interval',
            type=int,
            default=30,
            help='Processing interval in seconds (default: 30)',
        )
        
        parser.add_argument(
            '--inbox-only',
            action='store_true',
            help='Process only inbox messages',
        )
        
        parser.add_argument(
            '--outbox-only',
            action='store_true',
            help='Process only outbox messages',
        )
        
        parser.add_argument(
            '--kind',
            type=str,
            choices=['email', 'sms', 'whatsapp', 'signal', 'ws', 'push'],
            help='Process only messages of specific kind',
        )
        
        parser.add_argument(
            '--limit',
            type=int,
            default=100,
            help='Maximum messages to process per batch (default: 100)',
        )
        
        parser.add_argument(
            '--retry-failed',
            action='store_true',
            help='Process failed messages for retry',
        )
        
        parser.add_argument(
            '--max-age-hours',
            type=int,
            default=24,
            help='Maximum age of messages to retry in hours (default: 24)',
        )

    def handle(self, *args, **options):
        self.setup_signal_handlers()
        
        if options['daemon']:
            self.run_daemon(options)
        else:
            self.run_once(options)

    def setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown"""
        def signal_handler(signum, frame):
            self.stdout.write(
                self.style.WARNING(f'Received signal {signum}, shutting down gracefully...')
            )
            self.should_stop = True
            
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    def run_daemon(self, options):
        """Run as daemon with continuous processing"""
        interval = options['interval']
        
        self.stdout.write(
            self.style.SUCCESS(f'Starting notification processor daemon (interval: {interval}s)')
        )
        
        while not self.should_stop:
            try:
                stats = self.run_once(options)
                
                if stats['total_processed'] > 0:
                    self.stdout.write(
                        f'Processed {stats["total_processed"]} messages '
                        f'(inbox: {stats["inbox_processed"]}, outbox: {stats["outbox_processed"]})'
                    )
                
                # Sleep with interruption check
                for _ in range(interval):
                    if self.should_stop:
                        break
                    time.sleep(1)
                    
            except Exception as e:
                logger.error(f'Error in daemon loop: {e}')
                self.stderr.write(
                    self.style.ERROR(f'Error in processing: {e}')
                )
                time.sleep(interval)
        
        self.stdout.write(
            self.style.SUCCESS('Notification processor daemon stopped')
        )

    def run_once(self, options):
        """Run processing once"""
        stats = {
            'inbox_processed': 0,
            'outbox_processed': 0,
            'total_processed': 0,
            'inbox_failed': 0,
            'outbox_failed': 0,
        }
        
        try:
            if not options['outbox_only']:
                inbox_stats = self.process_inbox_messages(options)
                stats['inbox_processed'] = inbox_stats['processed']
                stats['inbox_failed'] = inbox_stats['failed']
            
            if not options['inbox_only']:
                outbox_stats = self.process_outbox_messages(options)
                stats['outbox_processed'] = outbox_stats['processed'] 
                stats['outbox_failed'] = outbox_stats['failed']
            
            if options['retry_failed']:
                retry_stats = self.process_failed_messages(options)
                stats['outbox_processed'] += retry_stats['processed']
                stats['outbox_failed'] += retry_stats['failed']
            
            stats['total_processed'] = stats['inbox_processed'] + stats['outbox_processed']
            
        except Exception as e:
            logger.error(f'Error in processing cycle: {e}')
            raise CommandError(f'Processing failed: {e}')
        
        return stats

    def process_inbox_messages(self, options):
        """Process unprocessed inbox messages"""
        stats = {'processed': 0, 'failed': 0}
        limit = options['limit']
        kind = options.get('kind')
        
        # Build query
        query = InboxMessage.objects.filter(processed=False).select_related(
            'inbox', 'inbox__account'
        ).order_by('created')
        
        if kind:
            query = query.filter(inbox__account__kind=kind)
        
        messages = query[:limit]
        
        self.stdout.write(f'Processing {len(messages)} inbox messages...')
        
        for message in messages:
            if self.should_stop:
                break
                
            try:
                with transaction.atomic():
                    success = MessageProcessor.process_inbox_message(message)
                    if success:
                        stats['processed'] += 1
                        self.stdout.write(
                            f'  ✓ Processed inbox message {message.id} from {message.from_address}'
                        )
                    else:
                        stats['failed'] += 1
                        self.stdout.write(
                            self.style.WARNING(
                                f'  ✗ Failed to process inbox message {message.id}'
                            )
                        )
                        
            except Exception as e:
                stats['failed'] += 1
                logger.error(f'Error processing inbox message {message.id}: {e}')
                self.stderr.write(
                    self.style.ERROR(f'  ✗ Error processing inbox message {message.id}: {e}')
                )
        
        return stats

    def process_outbox_messages(self, options):
        """Process pending outbox messages"""
        stats = {'processed': 0, 'failed': 0}
        limit = options['limit']
        kind = options.get('kind')
        
        # Build query for ready-to-send messages
        query = OutboxMessage.objects.filter(
            status=OutboxMessage.PENDING
        ).select_related('outbox', 'outbox__account').order_by('created')
        
        if kind:
            query = query.filter(outbox__account__kind=kind)
        
        # Filter messages that are ready to send (scheduled_at <= now or null)
        now = timezone.now()
        query = query.filter(
            models.Q(scheduled_at__isnull=True) | models.Q(scheduled_at__lte=now)
        )
        
        messages = query[:limit]
        
        self.stdout.write(f'Processing {len(messages)} outbox messages...')
        
        for message in messages:
            if self.should_stop:
                break
                
            try:
                with transaction.atomic():
                    success = self.send_outbox_message(message)
                    if success:
                        stats['processed'] += 1
                        self.stdout.write(
                            f'  ✓ Sent {message.outbox.account.kind} message to {message.to_address}'
                        )
                    else:
                        stats['failed'] += 1
                        
            except Exception as e:
                stats['failed'] += 1
                logger.error(f'Error sending outbox message {message.id}: {e}')
                self.stderr.write(
                    self.style.ERROR(f'  ✗ Error sending message {message.id}: {e}')
                )
        
        return stats

    def process_failed_messages(self, options):
        """Process failed messages for retry"""
        stats = {'processed': 0, 'failed': 0}
        limit = options['limit']
        max_age_hours = options['max_age_hours']
        kind = options.get('kind')
        
        # Find failed messages that can be retried
        cutoff_time = timezone.now() - timedelta(hours=max_age_hours)
        
        query = OutboxMessage.objects.filter(
            status=OutboxMessage.FAILED,
            failed_at__gte=cutoff_time
        ).select_related('outbox', 'outbox__account').order_by('failed_at')
        
        if kind:
            query = query.filter(outbox__account__kind=kind)
        
        messages = [msg for msg in query[:limit] if msg.can_retry]
        
        self.stdout.write(f'Retrying {len(messages)} failed messages...')
        
        for message in messages:
            if self.should_stop:
                break
                
            try:
                with transaction.atomic():
                    # Reset for retry
                    message.reset_for_retry()
                    
                    # Try to send again
                    success = self.send_outbox_message(message)
                    if success:
                        stats['processed'] += 1
                        self.stdout.write(
                            f'  ✓ Retry successful for message {message.id}'
                        )
                    else:
                        stats['failed'] += 1
                        
            except Exception as e:
                stats['failed'] += 1
                logger.error(f'Error retrying message {message.id}: {e}')
                self.stderr.write(
                    self.style.ERROR(f'  ✗ Error retrying message {message.id}: {e}')
                )
        
        return stats

    def send_outbox_message(self, message: OutboxMessage) -> bool:
        """Send an outbox message using its handler"""
        try:
            outbox = message.outbox
            
            # Check if outbox can send messages
            if not outbox.can_send_messages():
                message.mark_failed("Outbox is not active or account is disabled")
                return False
            
            # Check rate limits
            if not outbox.check_rate_limit():
                # Don't mark as failed, just skip for now
                logger.warning(f'Rate limit exceeded for outbox {outbox.id}')
                return False
            
            # Get handler
            handler_path = outbox.handler
            if not handler_path:
                message.mark_failed("No handler configured for outbox")
                return False
            
            # Import and call handler
            handler_func = _import_handler(handler_path)
            if not handler_func:
                message.mark_failed(f"Could not import handler: {handler_path}")
                return False
            
            # Call the handler
            return handler_func(message)
            
        except Exception as e:
            message.mark_failed(str(e))
            logger.error(f'Error sending message {message.id}: {e}')
            return False

    def get_processing_stats(self):
        """Get current processing statistics"""
        stats = {}
        
        # Inbox stats
        stats['inbox_unprocessed'] = InboxMessage.objects.filter(processed=False).count()
        
        # Outbox stats
        stats['outbox_pending'] = OutboxMessage.objects.filter(
            status=OutboxMessage.PENDING
        ).count()
        
        stats['outbox_failed'] = OutboxMessage.objects.filter(
            status=OutboxMessage.FAILED
        ).count()
        
        stats['outbox_ready'] = OutboxMessage.objects.filter(
            status=OutboxMessage.PENDING,
            scheduled_at__lte=timezone.now()
        ).count()
        
        return stats