#!/usr/bin/env python3
"""
CLI interface for Django-MOJO Jobs System

Simple, clean interface for managing job engine and scheduler.

Usage:
    python -m mojo.apps.jobs.cli [options] [command]

Global Commands:
    status              Check status of all daemons
    stop                Stop all running daemons
    # start (deprecated): Use component commands instead

Component Commands:
    engine start        Start just the engine as daemon
    engine foreground   Start just the engine in foreground
    engine stop         Stop just the engine
    scheduler start     Start just the scheduler as daemon
    scheduler foreground Start just the scheduler in foreground
    scheduler stop      Stop just the scheduler

Examples:
    # Global control
    python -m mojo.apps.jobs.cli status
    python -m mojo.apps.jobs.cli stop

    # Component control
    python -m mojo.apps.jobs.cli engine start
    python -m mojo.apps.jobs.cli engine stop
    python -m mojo.apps.jobs.cli scheduler foreground
    python -m mojo.apps.jobs.cli scheduler stop

    # Verbose output
    python -m mojo.apps.jobs.cli -v status
"""
import os
import sys
import argparse
import signal
import time
import subprocess
from pathlib import Path
from typing import Optional, List

from mojo.helpers import logit


def is_engine_running():
    """Check if any job engine is currently running."""
    from mojo.apps.jobs.daemon import DaemonRunner

    for pid_file in Path('/tmp').glob('job-engine-*.pid'):
        runner = DaemonRunner("JobEngine", lambda: None, pidfile=str(pid_file))
        if runner.status():
            return True
    return False


def is_scheduler_running():
    """Check if any job scheduler is currently running."""
    from mojo.apps.jobs.daemon import DaemonRunner

    for pid_file in Path('/tmp').glob('job-scheduler-*.pid'):
        runner = DaemonRunner("Scheduler", lambda: None, pidfile=str(pid_file))
        if runner.status():
            return True
    return False


def validate_environment(verbose=False):
    """Validate that all required services are available."""
    errors = []

    # Check Redis connection
    try:
        from mojo.apps.jobs.adapters import get_adapter
        redis = get_adapter()
        redis.ping()
        if verbose:
            print("✓ Redis connection successful")
    except Exception as e:
        errors.append(f"Redis connection failed: {e}")

    # Check database connection
    try:
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        if verbose:
            print("✓ Database connection successful")
    except Exception as e:
        errors.append(f"Database connection failed: {e}")

    # Check job models
    try:
        from mojo.apps.jobs.models import Job
        Job.objects.count()  # Simple query to test model access
        if verbose:
            print("✓ Job models accessible")
    except Exception as e:
        errors.append(f"Job models not accessible: {e}")

    if errors:
        if verbose:
            print("\n❌ Environment validation failed:")
            for error in errors:
                print(f"  • {error}")
            print("\nPlease fix these issues before running the job system.")
        return False
    else:
        if verbose:
            print("✓ Environment validation passed")
        return True


def setup_signal_handlers(engine=None, scheduler=None):
    """Setup signal handlers for graceful shutdown."""
    def signal_handler(signum, frame):
        logit.info(f"Received signal {signum}, initiating graceful shutdown...")
        if engine:
            engine.stop()
        if scheduler:
            scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


def start_engine_daemon(verbose=False, logfile_override: Optional[str] = None):
    """Start engine as daemon process."""
    if is_engine_running():
        if verbose:
            print("✓ Engine already running, skipping")
        return True

    from mojo.apps.jobs.job_engine import JobEngine
    from mojo.apps.jobs.daemon import DaemonRunner
    from mojo.helpers import paths

    # Get channels from settings
    try:
        from django.conf import settings
        channels = getattr(settings, 'JOBS_CHANNELS', ['default'])
        if isinstance(channels, str):
            channels = [channels]
    except:
        channels = ['default']

    # Create engine
    engine = JobEngine(channels=channels)

    # Auto-generate pidfile
    pidfile = f"/tmp/job-engine-{engine.runner_id}.pid"

    # Get logfile from settings
    # logfile = logfile_override if logfile_override is not None else getattr(settings, 'JOBS_ENGINE_LOGFILE', None)
    logfile = paths.LOG_ROOT / 'job_engine.log'

    # Setup daemon runner
    runner = DaemonRunner(
        name="JobEngine",
        run_func=engine.start,
        stop_func=engine.stop,
        pidfile=pidfile,
        logfile=logfile,
        daemon=True
    )

    try:
        if verbose:
            print(f"🚀 Starting engine daemon (PID file: {pidfile})")
        runner.start()
        return True
    except Exception as e:
        if verbose:
            print(f"❌ Failed to start engine: {e}")
        return False


def start_scheduler_daemon(verbose=False):
    """Start scheduler as daemon process."""
    if is_scheduler_running():
        if verbose:
            print("✓ Scheduler already running, skipping")
        return True

    from mojo.apps.jobs.scheduler import Scheduler
    from mojo.apps.jobs.daemon import DaemonRunner
    from mojo.helpers import paths

    # Get channels from settings
    try:
        from django.conf import settings
        channels = getattr(settings, 'JOBS_CHANNELS', ['default'])
        if isinstance(channels, str):
            channels = [channels]
    except:
        channels = ['default']

    # Create scheduler
    scheduler = Scheduler(channels=channels)

    # Auto-generate pidfile
    pidfile = f"/tmp/job-scheduler-{scheduler.scheduler_id}.pid"

    # Get logfile from settings
    # logfile = getattr(settings, 'JOBS_SCHEDULER_LOGFILE', None)
    logfile = paths.LOG_ROOT / 'job_scheduler.log'

    # Setup daemon runner
    runner = DaemonRunner(
        name="Scheduler",
        run_func=scheduler.start,
        stop_func=scheduler.stop,
        pidfile=pidfile,
        logfile=logfile,
        daemon=True
    )

    try:
        if verbose:
            print(f"🚀 Starting scheduler daemon (PID file: {pidfile})")
        runner.start()
        return True
    except Exception as e:
        if verbose:
            print(f"❌ Failed to start scheduler: {e}")
        return False


def start_engine_foreground(verbose=False):
    """Start engine in foreground mode."""
    from mojo.apps.jobs.job_engine import JobEngine

    # Get channels from settings
    try:
        from django.conf import settings
        channels = getattr(settings, 'JOBS_CHANNELS', ['default'])
        if isinstance(channels, str):
            channels = [channels]
    except:
        channels = ['default']

    # Create engine
    engine = JobEngine(channels=channels)

    if verbose:
        print(f"🚀 Starting engine in foreground mode")
        print(f"   Channels: {channels}")
        print(f"   Runner ID: {engine.runner_id}")
        print(f"   Press Ctrl+C to stop")
        print()

    # Setup signal handlers
    setup_signal_handlers(engine)

    try:
        engine.start()
        return True
    except KeyboardInterrupt:
        if verbose:
            print("\n👋 Engine interrupted by user")
        engine.stop()
        return True
    except Exception as e:
        if verbose:
            print(f"❌ Engine failed: {e}")
        logit.error(f"Engine failed: {e}")
        engine.stop()
        return False


def start_scheduler_foreground(verbose=False):
    """Start scheduler in foreground mode."""
    from mojo.apps.jobs.scheduler import Scheduler

    # Get channels from settings
    try:
        from django.conf import settings
        channels = getattr(settings, 'JOBS_CHANNELS', ['default'])
        if isinstance(channels, str):
            channels = [channels]
    except:
        channels = ['default']

    # Create scheduler
    scheduler = Scheduler(channels=channels)

    if verbose:
        print(f"🚀 Starting scheduler in foreground mode")
        print(f"   Channels: {channels}")
        print(f"   Scheduler ID: {scheduler.scheduler_id}")
        print(f"   Press Ctrl+C to stop")
        print()
        print("⚠️  Note: Only one scheduler should be active cluster-wide.")
        print("   This instance will attempt to acquire leadership lock.")
        print()

    # Setup signal handlers
    setup_signal_handlers(scheduler=scheduler)

    try:
        scheduler.start()
        return True
    except KeyboardInterrupt:
        if verbose:
            print("\n👋 Scheduler interrupted by user")
        scheduler.stop()
        return True
    except Exception as e:
        if verbose:
            print(f"❌ Scheduler failed: {e}")
        logit.error(f"Scheduler failed: {e}")
        scheduler.stop()
        return False


def status_command(verbose=False):
    """Check status of all running daemons."""
    from mojo.apps.jobs.daemon import DaemonRunner

    results = []

    # Check for engine PIDs
    for pid_file in Path('/tmp').glob('job-engine-*.pid'):
        runner = DaemonRunner("JobEngine", lambda: None, pidfile=str(pid_file))
        if runner.status():
            results.append(f"✓ Engine running (PID file: {pid_file})")
        else:
            results.append(f"❌ Engine not running (stale PID file: {pid_file})")

    # Check for scheduler PIDs
    for pid_file in Path('/tmp').glob('job-scheduler-*.pid'):
        runner = DaemonRunner("Scheduler", lambda: None, pidfile=str(pid_file))
        if runner.status():
            results.append(f"✓ Scheduler running (PID file: {pid_file})")
        else:
            results.append(f"❌ Scheduler not running (stale PID file: {pid_file})")

    if results:
        for result in results:
            print(result)
    else:
        print("No job system daemons running")

    return len(results) > 0


def stop_command(verbose=False):
    """Stop all running daemons."""
    from mojo.apps.jobs.daemon import DaemonRunner

    stopped = 0
    failed = 0

    # Stop all engines
    for pid_file in Path('/tmp').glob('job-engine-*.pid'):
        runner = DaemonRunner("JobEngine", lambda: None, pidfile=str(pid_file))
        if runner.stop():
            if verbose:
                print(f"✓ Stopped engine (PID file: {pid_file})")
            stopped += 1
        else:
            if verbose:
                print(f"❌ Failed to stop engine (PID file: {pid_file})")
            failed += 1

    # Stop all schedulers
    for pid_file in Path('/tmp').glob('job-scheduler-*.pid'):
        runner = DaemonRunner("Scheduler", lambda: None, pidfile=str(pid_file))
        if runner.stop():
            if verbose:
                print(f"✓ Stopped scheduler (PID file: {pid_file})")
            stopped += 1
        else:
            if verbose:
                print(f"❌ Failed to stop scheduler (PID file: {pid_file})")
            failed += 1

    if verbose or (stopped > 0 or failed > 0):
        print(f"Stopped: {stopped}, Failed: {failed}")

    return failed == 0


def stop_engine_daemon(verbose=False):
    """Stop just the engine daemon."""
    from mojo.apps.jobs.daemon import DaemonRunner

    stopped = 0
    failed = 0

    # Stop all engine instances
    for pid_file in Path('/tmp').glob('job-engine-*.pid'):
        runner = DaemonRunner("JobEngine", lambda: None, pidfile=str(pid_file))
        if runner.stop():
            if verbose:
                print(f"✓ Stopped engine (PID file: {pid_file})")
            stopped += 1
        else:
            if verbose:
                print(f"❌ Failed to stop engine (PID file: {pid_file})")
            failed += 1

    if stopped == 0 and failed == 0:
        if verbose:
            print("No engine daemons found to stop")
        return True

    if verbose:
        print(f"Engine stop: {stopped} stopped, {failed} failed")

    return failed == 0


def stop_scheduler_daemon(verbose=False):
    """Stop just the scheduler daemon."""
    from mojo.apps.jobs.daemon import DaemonRunner

    stopped = 0
    failed = 0

    # Stop all scheduler instances
    for pid_file in Path('/tmp').glob('job-scheduler-*.pid'):
        runner = DaemonRunner("Scheduler", lambda: None, pidfile=str(pid_file))
        if runner.stop():
            if verbose:
                print(f"✓ Stopped scheduler (PID file: {pid_file})")
            stopped += 1
        else:
            if verbose:
                print(f"❌ Failed to stop scheduler (PID file: {pid_file})")
            failed += 1

    if stopped == 0 and failed == 0:
        if verbose:
            print("No scheduler daemons found to stop")
        return True

    if verbose:
        print(f"Scheduler stop: {stopped} stopped, {failed} failed")

    return failed == 0


def start_command(verbose=False):
    """Start both engine and scheduler as daemons (separate processes)."""
    if verbose:
        print("🚀 Starting both engine and scheduler as daemons...")

    args_common = ["-v"] if verbose else []
    python = sys.executable
    module = "mojo.apps.jobs.cli"

    # Launch engine in separate process
    engine_result = subprocess.run(
        [python, "-m", module, "engine", "start"] + args_common,
        stdout=None if verbose else subprocess.DEVNULL,
        stderr=None if verbose else subprocess.DEVNULL,
    )

    # Launch scheduler in separate process
    scheduler_result = subprocess.run(
        [python, "-m", module, "scheduler", "start"] + args_common,
        stdout=None if verbose else subprocess.DEVNULL,
        stderr=None if verbose else subprocess.DEVNULL,
    )

    engine_success = (engine_result.returncode == 0)
    scheduler_success = (scheduler_result.returncode == 0)

    if engine_success and scheduler_success:
        if verbose:
            print("✅ Both components started successfully")
        return True
    else:
        if verbose:
            print(f"❌ Failed to start one or more components "
                  f"(engine_rc={engine_result.returncode}, scheduler_rc={scheduler_result.returncode})")
        return False


def main(args=None):
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Django-MOJO Jobs System CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Global Commands:
  status              Check status of all daemons
  stop                Stop all running daemons
  start               Start both engine and scheduler as daemons

Component Commands:
  engine start        Start just the engine as daemon
  engine foreground   Start just the engine in foreground
  engine stop         Stop just the engine
  scheduler start     Start just the scheduler as daemon
  scheduler foreground Start just the scheduler in foreground
  scheduler stop      Stop just the scheduler

Examples:
  %(prog)s status                # Check what's running
  %(prog)s start                 # Start everything
  %(prog)s stop                  # Stop everything
  %(prog)s engine start          # Start just engine
  %(prog)s engine stop           # Stop just engine
  %(prog)s scheduler foreground  # Run scheduler in foreground
  %(prog)s scheduler stop        # Stop just scheduler
  %(prog)s -v status             # Verbose status
        """
    )

    # Global options
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose output (default is quiet mode)'
    )
    parser.add_argument(
        '--validate',
        action='store_true',
        help='Validate environment and exit'
    )

    # Positional arguments for commands
    parser.add_argument(
        'command',
        nargs='?',
        choices=['status', 'stop', 'start', 'engine', 'scheduler'],
        help='Command to execute'
    )
    # Engine-only options
    parser.add_argument(
        '--logfile',
        type=str,
        default=None,
        help='Log file path for engine daemon mode (overrides settings)'
    )
    parser.add_argument(
        'action',
        nargs='?',
        choices=['start', 'foreground', 'stop'],
        help='Action for component commands (engine/scheduler only)'
    )

    # Parse arguments
    parsed_args = parser.parse_args(args)
    verbose = parsed_args.verbose

    # Handle validation-only mode
    if parsed_args.validate:
        if validate_environment(verbose=True):
            print("✅ Environment is ready for job system.")
            return True
        else:
            return False

    # Validate environment
    if not validate_environment(verbose=verbose):
        return False

    # Handle commands
    command = parsed_args.command
    action = parsed_args.action

    if not command:
        parser.print_help()
        return False

    try:
        if command == 'status':
            return status_command(verbose)
        elif command == 'stop':
            return stop_command(verbose)
        elif command == 'start':
            # Deprecated global start
            if verbose:
                print("⚠️  'start' is deprecated. Use 'engine start' and 'scheduler start' instead.")
            return False
        elif command == 'engine':
            if action == 'start':
                return start_engine_daemon(verbose, logfile_override=parsed_args.logfile)
            elif action == 'foreground':
                return start_engine_foreground(verbose)
            elif action == 'stop':
                return stop_engine_daemon(verbose)
            else:
                print("Engine command requires 'start', 'foreground', or 'stop' action")
                return False
        elif command == 'scheduler':
            if action == 'start':
                return start_scheduler_daemon(verbose)
            elif action == 'foreground':
                return start_scheduler_foreground(verbose)
            elif action == 'stop':
                return stop_scheduler_daemon(verbose)
            else:
                print("Scheduler command requires 'start', 'foreground', or 'stop' action")
                return False
        else:
            parser.print_help()
            return False

    except Exception as e:
        if verbose:
            print(f"❌ Command failed: {e}")
        logit.error(f"CLI command failed: {e}")
        return False


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
