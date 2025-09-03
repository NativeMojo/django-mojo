"""
Django management command for running the Job Scheduler.

Usage:
    python manage.py jobs_scheduler [options]
"""
from django.core.management.base import BaseCommand
from django.conf import settings

from mojo.apps.jobs.scheduler import Scheduler
from mojo.apps.jobs.daemon import DaemonRunner
from mojo.helpers import logit


class Command(BaseCommand):
    help = 'Run the Django-MOJO Job Scheduler to process delayed jobs'

    def add_arguments(self, parser):
        """Add command arguments."""
        parser.add_argument(
            '--channels',
            type=str,
            default=None,
            help='Comma-separated list of channels to schedule (default: all configured)'
        )
        parser.add_argument(
            '--scheduler-id',
            type=str,
            default=None,
            help='Explicit scheduler ID (auto-generated if not provided)'
        )
        parser.add_argument(
            '--daemon',
            action='store_true',
            help='Run as background daemon'
        )
        parser.add_argument(
            '--pidfile',
            type=str,
            default=None,
            help='PID file path (auto-generated if daemon mode and not specified)'
        )
        parser.add_argument(
            '--logfile',
            type=str,
            default=None,
            help='Log file path for daemon mode'
        )
        parser.add_argument(
            '--action',
            type=str,
            choices=['start', 'stop', 'restart', 'status'],
            default='start',
            help='Daemon control action (only with --daemon)'
        )

    def handle(self, *args, **options):
        """Execute the command."""
        # Parse channels if provided
        channels = None
        if options['channels']:
            channels = [c.strip() for c in options['channels'].split(',')]

        # Create scheduler
        scheduler = Scheduler(
            channels=channels,
            scheduler_id=options['scheduler_id']
        )

        # Auto-generate pidfile if daemon mode and not specified
        pidfile = options['pidfile']
        if options['daemon'] and not pidfile:
            scheduler_id = scheduler.scheduler_id
            pidfile = f"/tmp/job-scheduler-{scheduler_id}.pid"

        # Setup daemon runner
        runner = DaemonRunner(
            name="Scheduler",
            run_func=scheduler.start,
            stop_func=scheduler.stop,
            pidfile=pidfile,
            logfile=options['logfile'],
            daemon=options['daemon']
        )

        # Handle daemon actions
        if options['daemon'] and options['action'] != 'start':
            if options['action'] == 'stop':
                if runner.stop():
                    self.stdout.write(
                        self.style.SUCCESS('Scheduler stopped successfully')
                    )
                else:
                    self.stderr.write(
                        self.style.ERROR('Failed to stop Scheduler')
                    )
                    return

            elif options['action'] == 'restart':
                runner.restart()
                self.stdout.write(
                    self.style.SUCCESS('Scheduler restarted successfully')
                )
                return

            elif options['action'] == 'status':
                if runner.status():
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'Scheduler is running (PID file: {pidfile})'
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING('Scheduler is not running')
                    )
                return

        else:
            # Start the scheduler (foreground or background)
            try:
                if options['daemon']:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'Starting Scheduler as daemon (PID file: {pidfile})'
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'Starting Scheduler in foreground mode\n'
                            f'  Channels: {scheduler.channels}\n'
                            f'  Scheduler ID: {scheduler.scheduler_id}\n'
                            f'  Lock TTL: {scheduler.lock_ttl_ms}ms\n'
                            f'  Press Ctrl+C to stop\n'
                        )
                    )
                    self.stdout.write(
                        self.style.WARNING(
                            '\nNote: Only one scheduler should be active cluster-wide.\n'
                            'This instance will attempt to acquire leadership lock.\n'
                        )
                    )

                runner.start()

            except KeyboardInterrupt:
                self.stdout.write(
                    self.style.WARNING('\nScheduler interrupted by user')
                )
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(f'Scheduler failed: {e}')
                )
                raise
