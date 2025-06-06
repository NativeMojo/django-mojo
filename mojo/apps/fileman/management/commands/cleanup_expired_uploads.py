from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from datetime import datetime, timedelta
from django.db import transaction
import logging

from mojo.apps.fileman.models import File, FileManager
from mojo.apps.fileman.backends import get_backend


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Clean up expired file uploads and temporary files'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=1,
            help='Clean up uploads older than this many days (default: 1)'
        )
        
        parser.add_argument(
            '--hours',
            type=int,
            help='Clean up uploads older than this many hours (overrides --days)'
        )
        
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be cleaned up without actually doing it'
        )
        
        parser.add_argument(
            '--backend-cleanup',
            action='store_true',
            default=True,
            help='Also run backend-specific cleanup (default: True)'
        )
        
        parser.add_argument(
            '--no-backend-cleanup',
            action='store_false',
            dest='backend_cleanup',
            help='Skip backend-specific cleanup'
        )
        
        parser.add_argument(
            '--status',
            choices=['pending', 'uploading', 'failed', 'expired', 'all'],
            default='all',
            help='Which upload statuses to clean up (default: all non-completed)'
        )
        
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Verbose output'
        )

    def handle(self, *args, **options):
        self.dry_run = options['dry_run']
        self.verbose = options['verbose']
        
        # Calculate cutoff date
        if options['hours']:
            cutoff_date = timezone.now() - timedelta(hours=options['hours'])
            cutoff_desc = f"{options['hours']} hours"
        else:
            cutoff_date = timezone.now() - timedelta(days=options['days'])
            cutoff_desc = f"{options['days']} days"
        
        if self.dry_run:
            self.stdout.write(
                self.style.WARNING(f"DRY RUN: Showing what would be cleaned up")
            )
        
        self.stdout.write(
            f"Cleaning up uploads older than {cutoff_desc} (before {cutoff_date})"
        )
        
        # Clean up File records
        files_cleaned = self.cleanup_files(cutoff_date, options['status'])
        
        # Clean up backend temporary files
        if options['backend_cleanup']:
            backends_cleaned = self.cleanup_backends(cutoff_date)
        else:
            backends_cleaned = 0
        
        # Summary
        if self.dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"DRY RUN COMPLETE: Would clean up {files_cleaned} file records "
                    f"and {backends_cleaned} backend temporary files"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"CLEANUP COMPLETE: Cleaned up {files_cleaned} file records "
                    f"and {backends_cleaned} backend temporary files"
                )
            )

    def cleanup_files(self, cutoff_date, status_filter):
        """Clean up File model records"""
        
        # Build query based on status filter
        query = File.objects.filter(created__lt=cutoff_date)
        
        if status_filter == 'pending':
            query = query.filter(upload_status=File.PENDING)
        elif status_filter == 'uploading':
            query = query.filter(upload_status=File.UPLOADING)
        elif status_filter == 'failed':
            query = query.filter(upload_status=File.FAILED)
        elif status_filter == 'expired':
            query = query.filter(upload_status=File.EXPIRED)
        elif status_filter == 'all':
            # Clean up all non-completed uploads
            query = query.exclude(upload_status=File.COMPLETED)
        
        # Also include files with expired upload URLs
        expired_url_query = File.objects.filter(
            upload_expires_at__lt=timezone.now(),
            upload_status__in=[File.PENDING, File.UPLOADING]
        )
        
        # Combine queries
        files_to_cleanup = query.union(expired_url_query).distinct()
        
        if self.verbose:
            self.stdout.write(f"Found {files_to_cleanup.count()} file records to clean up")
        
        cleaned_count = 0
        
        for file_obj in files_to_cleanup:
            if self.verbose:
                self.stdout.write(
                    f"  File: {file_obj.original_filename} "
                    f"(status: {file_obj.upload_status}, "
                    f"created: {file_obj.created})"
                )
            
            if not self.dry_run:
                try:
                    with transaction.atomic():
                        # Try to delete the actual file from storage if it exists
                        if file_obj.file_path and file_obj.upload_status == File.COMPLETED:
                            try:
                                backend = get_backend(file_obj.file_manager)
                                if backend.exists(file_obj.file_path):
                                    backend.delete(file_obj.file_path)
                                    if self.verbose:
                                        self.stdout.write(f"    Deleted file from storage: {file_obj.file_path}")
                            except Exception as e:
                                logger.warning(f"Failed to delete file from storage: {e}")
                                if self.verbose:
                                    self.stdout.write(
                                        self.style.WARNING(f"    Warning: Could not delete from storage: {e}")
                                    )
                        
                        # Mark as expired or delete the record
                        if file_obj.upload_status in [File.PENDING, File.UPLOADING]:
                            file_obj.mark_as_expired()
                            if self.verbose:
                                self.stdout.write(f"    Marked as expired")
                        else:
                            file_obj.delete()
                            if self.verbose:
                                self.stdout.write(f"    Deleted record")
                        
                        cleaned_count += 1
                        
                except Exception as e:
                    logger.error(f"Error cleaning up file {file_obj.id}: {e}")
                    if self.verbose:
                        self.stdout.write(
                            self.style.ERROR(f"    Error: {e}")
                        )
            else:
                cleaned_count += 1
        
        return cleaned_count

    def cleanup_backends(self, cutoff_date):
        """Run backend-specific cleanup"""
        cleaned_count = 0
        
        # Get all active file managers
        file_managers = FileManager.objects.filter(is_active=True)
        
        if self.verbose:
            self.stdout.write(f"Running backend cleanup for {file_managers.count()} file managers")
        
        for file_manager in file_managers:
            try:
                backend = get_backend(file_manager)
                
                if self.verbose:
                    self.stdout.write(
                        f"  Cleaning up {file_manager.name} ({file_manager.backend_type})"
                    )
                
                if not self.dry_run:
                    backend.cleanup_expired_uploads(cutoff_date)
                
                cleaned_count += 1
                
            except Exception as e:
                logger.error(f"Error running backend cleanup for {file_manager.name}: {e}")
                if self.verbose:
                    self.stdout.write(
                        self.style.ERROR(f"    Error: {e}")
                    )
        
        return cleaned_count