#!/usr/bin/env python3
"""
Standalone Job Engine Runner

A standalone script to run the Django-MOJO Job Engine outside of
Django's management command system. This script sets up Django
environment and runs the job engine with all necessary configuration.

Usage:
    python run_jobs_engine.py [options]

Examples:
    # Run in foreground on default channel
    python run_jobs_engine.py

    # Run on multiple channels
    python run_jobs_engine.py --channels default,email,reports

    # Run as background daemon
    python run_jobs_engine.py --daemon --logfile /tmp/jobs.log

    # Stop daemon
    python run_jobs_engine.py --daemon --action stop

Requirements:
    - Django settings must be configured (DJANGO_SETTINGS_MODULE)
    - Redis must be available and configured
    - Database must be available and migrated
"""
import os
import sys
import argparse
import signal
import time
from pathlib import Path

# Add the project root to Python path if needed
project_root = Path(__file__).parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Configure Django settings
if 'DJANGO_SETTINGS_MODULE' not in os.environ:
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mojo.settings')

# Initialize Django
try:
    import django
    django.setup()
except Exception as e:
    print(f"Failed to initialize Django: {e}")
    print("Make sure DJANGO_SETTINGS_MODULE is set correctly")
    sys.exit(1)

# Import job engine components after Django setup
from mojo.apps.jobs.job_engine import JobEngine
from mojo.apps.jobs.daemon import DaemonRunner
from mojo.helpers import logit


def validate_environment():
    """Validate that all required services are available."""
    errors = []

    # Check Redis connection
    try:
        from mojo.apps.jobs.adapters import get_adapter
        redis = get_adapter()
        redis.ping()
        print("✓ Redis connection successful")
    except Exception as e:
        errors.append(f"Redis connection failed: {e}")

    # Check database connection
    try:
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        print("✓ Database connection successful")
    except Exception as e:
        errors.append(f"Database connection failed: {e}")

    # Check job models
    try:
        from mojo.apps.jobs.models import Job
        Job.objects.count()  # Simple query to test model access
        print("✓ Job models accessible")
    except Exception as e:
        errors.append(f"Job models not accessible: {e}")

    if errors:
        print("\n❌ Environment validation failed:")
        for error in errors:
            print(f"  • {error}")
        print("\nPlease fix these issues before running the job engine.")
        sys.exit(1)
    else:
        print("✓ Environment validation passed\n")


def setup_signal_handlers(engine):
    """Setup signal handlers for graceful shutdown."""
    def signal_handler(signum, frame):
        logit.info(f"Received signal {signum}, initiating graceful shutdown...")
        if engine:
            engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


def create_engine_with_scheduler(channels, runner_id, with_scheduler=False):
    """Create job engine and optionally start scheduler in same process."""
    engine = JobEngine(channels=channels, runner_id=runner_id)

    if with_scheduler:
        # Import scheduler after Django setup
        from mojo.apps.jobs.scheduler import Scheduler
        import threading

        scheduler = Scheduler(channels=channels)

        def start_scheduler():
            """Start scheduler in background thread."""
            try:
                scheduler.start()
            except Exception as e:
                logit.error(f"Scheduler thread crashed: {e}")

        scheduler_thread = threading.Thread(
            target=start_scheduler,
            name="SchedulerThread",
            daemon=True
        )
        scheduler_thread.start()

        logit.info(f"Started scheduler thread for channels: {channels}")

        # Store reference for cleanup
        engine._scheduler = scheduler
        engine._scheduler_thread = scheduler_thread

    return engine


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Django-MOJO Standalone Job Engine Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    # Run in foreground
  %(prog)s --channels default,email          # Multiple channels
  %(prog)s --daemon --logfile /tmp/jobs.log  # Background daemon
  %(prog)s --daemon --action stop            # Stop daemon
  %(prog)s --daemon --action status          # Check status
  %(prog)s --with-scheduler                  # Include scheduler
        """
    )

    # Engine options
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
        '--max-workers',
        type=int,
        default=None,
        help='Maximum number of worker threads (default from settings)'
    )

    # Scheduler option
    parser.add_argument(
        '--with-scheduler',
        action='store_true',
        help='Also run scheduler in the same process (for single-machine deployments)'
    )

    # Daemon options
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

    # Utility options
    parser.add_argument(
        '--validate',
        action='store_true',
        help='Validate environment and exit'
    )
    parser.add_argument(
        '--quiet',
        action='store_true',
        help='Suppress startup output'
    )

    args = parser.parse_args()

    # Handle validation-only mode
    if args.validate:
        validate_environment()
        print("Environment is ready for job engine.")
        return

    # Validate environment unless quiet mode
    if not args.quiet:
        print("Django-MOJO Standalone Job Engine")
        print("=" * 40)
        validate_environment()

    # Parse channels
    channels = [c.strip() for c in args.channels.split(',')]

    # Create engine (but don't start yet for daemon actions)
    engine = None
    if args.action == 'start' or not args.daemon:
        engine = create_engine_with_scheduler(
            channels=channels,
            runner_id=args.runner_id,
            with_scheduler=args.with_scheduler
        )

        # Update max_workers if specified
        if args.max_workers:
            engine.max_workers = args.max_workers
            # Recreate executor with new size
            engine.executor.shutdown(wait=False)
            import concurrent.futures
            engine.executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=engine.max_workers,
                thread_name_prefix=f"JobWorker-{engine.runner_id}"
            )

    # Auto-generate pidfile if daemon mode and not specified
    pidfile = args.pidfile
    if args.daemon and not pidfile:
        runner_id = engine.runner_id if engine else 'unknown'
        pidfile = f"/tmp/job-engine-{runner_id}.pid"

    # Setup daemon runner
    def start_engine():
        if engine:
            engine.start()

    def stop_engine():
        if engine:
            engine.stop()

    runner = DaemonRunner(
        name="JobEngine",
        run_func=start_engine,
        stop_func=stop_engine,
        pidfile=pidfile,
        logfile=args.logfile,
        daemon=args.daemon
    )

    # Handle daemon actions
    if args.daemon and args.action != 'start':
        if args.action == 'stop':
            if runner.stop():
                print("✓ JobEngine stopped successfully")
                sys.exit(0)
            else:
                print("❌ Failed to stop JobEngine")
                sys.exit(1)

        elif args.action == 'restart':
            print("🔄 Restarting JobEngine...")
            runner.restart()
            print("✓ JobEngine restarted successfully")
            sys.exit(0)

        elif args.action == 'status':
            if runner.status():
                print(f"✓ JobEngine is running (PID file: {pidfile})")
                sys.exit(0)
            else:
                print("❌ JobEngine is not running")
                sys.exit(1)

    else:
        # Start the engine (foreground or background)
        try:
            if not args.quiet:
                if args.daemon:
                    print(f"🚀 Starting JobEngine as daemon...")
                    print(f"   PID file: {pidfile}")
                    if args.logfile:
                        print(f"   Log file: {args.logfile}")
                else:
                    print(f"🚀 Starting JobEngine in foreground mode")
                    print(f"   Channels: {channels}")
                    print(f"   Runner ID: {engine.runner_id}")
                    print(f"   Max workers: {engine.max_workers}")
                    if args.with_scheduler:
                        print(f"   Scheduler: enabled")
                    print(f"   Press Ctrl+C to stop")
                    print()

            # Setup signal handlers for foreground mode
            if not args.daemon:
                setup_signal_handlers(engine)

            runner.start()

        except KeyboardInterrupt:
            if not args.quiet:
                print("\n👋 JobEngine interrupted by user")
        except Exception as e:
            print(f"❌ JobEngine failed: {e}")
            logit.error(f"JobEngine failed: {e}")
            sys.exit(1)


if __name__ == '__main__':
    main()
