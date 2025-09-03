"""
Django management command for running the Job Engine.

Usage:
    python manage.py jobs_engine [options]
"""
from django.core.management.base import BaseCommand
from django.conf import settings

from mojo.apps.jobs.job_engine import JobEngine
from mojo.apps.jobs.daemon import DaemonRunner
from mojo.helpers import logit


class Command(BaseCommand):
    help = 'Run the Django-MOJO Job Engine to process background jobs'

    def add_arguments(self, parser):
        """Add command arguments."""
        parser.add_argument(
            '--channels',
            type=str,
            default='default',
            help='Comma-separated list of channels to serve (default: default)'
        )
        parser.add_argument(
            '--runner-id',
            type=str,
            default=None,
            help='Explicit runner ID (auto-generated if not provided)'
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
        parser.add_argument(
            '--with-scheduler',
            action='store_true',
            help='Also run scheduler in the same process (for development)'
        )

    def handle(self, *args, **options):
        """Execute the command."""
        # Parse channels
        channels = [c.strip() for c in options['channels'].split(',')]

        # Create engine
        engine = JobEngine(
            channels=channels,
            runner_id=options['runner_id']
        )

        # Auto-generate pidfile if daemon mode and not specified
        pidfile = options['pidfile']
        if options['daemon'] and not pidfile:
            runner_id = engine.runner_id
            pidfile = f"/tmp/job-engine-{runner_id}.pid"

        # Check if we should also run scheduler
        if options['with_scheduler']:
            # Import here to avoid circular dependency
            from mojo.apps.jobs.scheduler import Scheduler
            import threading

            # Create scheduler
            scheduler = Scheduler(channels=channels)

            # Start scheduler in background thread
            scheduler_thread = threading.Thread(
                target=scheduler.start,
                name="SchedulerThread",
                daemon=True
            )
            scheduler_thread.start()

            self.stdout.write(
                self.style.SUCCESS(
                    f"Started scheduler thread for channels: {channels}"
                )
            )

        # Setup daemon runner
        runner = DaemonRunner(
            name="JobEngine",
            run_func=engine.start,
            stop_func=engine.stop,
            pidfile=pidfile,
            logfile=options['logfile'],
            daemon=options['daemon']
        )

        # Handle daemon actions
        if options['daemon'] and options['action'] != 'start':
            if options['action'] == 'stop':
                if runner.stop():
                    self.stdout.write(
                        self.style.SUCCESS('JobEngine stopped successfully')
                    )
                else:
                    self.stderr.write(
                        self.style.ERROR('Failed to stop JobEngine')
                    )
                    return

            elif options['action'] == 'restart':
                runner.restart()
                self.stdout.write(
                    self.style.SUCCESS('JobEngine restarted successfully')
                )
                return

            elif options['action'] == 'status':
                if runner.status():
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'JobEngine is running (PID file: {pidfile})'
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING('JobEngine is not running')
                    )
                return

        else:
            # Start the engine (foreground or background)
            try:
                if options['daemon']:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'Starting JobEngine as daemon (PID file: {pidfile})'
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'Starting JobEngine in foreground mode\n'
                            f'  Channels: {channels}\n'
                            f'  Runner ID: {engine.runner_id}\n'
                            f'  Press Ctrl+C to stop\n'
                        )
                    )

                runner.start()

            except KeyboardInterrupt:
                self.stdout.write(
                    self.style.WARNING('\nJobEngine interrupted by user')
                )
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(f'JobEngine failed: {e}')
                )
                raise
