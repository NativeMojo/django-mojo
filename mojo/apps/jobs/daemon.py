"""
Daemon utility for running processes in foreground or background mode.

Provides daemonization support for job engine and scheduler processes,
with proper PID file management and signal handling.
"""
import os
import sys
import atexit
import signal
import time
import fcntl
from typing import Callable, Optional
from pathlib import Path

from mojo.helpers import logit
logger = logit.get_logger("jobs", "jobs.log")

class DaemonContext:
    """
    Context manager for daemonizing a process.

    Supports both foreground and background modes with proper
    signal handling and PID file management.
    """

    def __init__(
        self,
        pidfile: Optional[str] = None,
        stdin: str = '/dev/null',
        stdout: str = '/dev/null',
        stderr: str = '/dev/null',
        working_dir: str = '/',
        umask: int = 0o22,
        detach: bool = True
    ):
        """
        Initialize daemon context.

        Args:
            pidfile: Path to PID file (optional)
            stdin: Path for stdin redirection in daemon mode
            stdout: Path for stdout redirection in daemon mode
            stderr: Path for stderr redirection in daemon mode
            working_dir: Working directory for daemon
            umask: File creation mask
            detach: If True, detach from terminal (background mode)
        """
        self.pidfile = pidfile
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.working_dir = working_dir
        self.umask = umask
        self.detach = detach
        self._pidfile_handle = None

    def __enter__(self):
        """Enter daemon context."""
        if self.detach:
            self._daemonize()

        # Write PID file
        if self.pidfile:
            self._write_pidfile()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit daemon context and cleanup."""
        if self.pidfile and self._pidfile_handle:
            self._remove_pidfile()

    def _daemonize(self):
        """
        Detach from terminal and become a daemon.

        Uses double-fork technique to properly detach from terminal.
        """
        # First fork
        try:
            pid = os.fork()
            if pid > 0:
                # Parent process exits
                sys.exit(0)
        except OSError as e:
            logger.error(f"First fork failed: {e}")
            sys.exit(1)

        # Decouple from parent environment
        os.chdir(self.working_dir)
        os.setsid()
        os.umask(self.umask)

        # Second fork
        try:
            pid = os.fork()
            if pid > 0:
                # Parent process exits
                sys.exit(0)
        except OSError as e:
            logger.error(f"Second fork failed: {e}")
            sys.exit(1)

        # Redirect standard file descriptors
        sys.stdout.flush()
        sys.stderr.flush()

        # Open file descriptors
        si = open(self.stdin, 'r')
        so = open(self.stdout, 'a+')
        se = open(self.stderr, 'a+')

        # Duplicate file descriptors
        os.dup2(si.fileno(), sys.stdin.fileno())
        os.dup2(so.fileno(), sys.stdout.fileno())
        os.dup2(se.fileno(), sys.stderr.fileno())

        logger.info(f"Process daemonized with PID: {os.getpid()}")

    def _write_pidfile(self):
        """Write PID file with exclusive lock."""
        pid = str(os.getpid())

        try:
            # Create parent directories if needed
            Path(self.pidfile).parent.mkdir(parents=True, exist_ok=True)

            # Open with exclusive create
            self._pidfile_handle = open(self.pidfile, 'w')

            # Try to acquire exclusive lock
            fcntl.lockf(self._pidfile_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

            # Write PID
            self._pidfile_handle.write(f"{pid}\n")
            self._pidfile_handle.flush()

            # Register cleanup
            atexit.register(self._remove_pidfile)

            logger.info(f"PID file created: {self.pidfile} (PID: {pid})")

        except IOError as e:
            if e.errno == 11:  # Resource temporarily unavailable
                logger.error(f"Another instance is already running (PID file: {self.pidfile})")
            else:
                logger.error(f"Failed to write PID file: {e}")
            sys.exit(1)

    def _remove_pidfile(self):
        """Remove PID file on exit."""
        if self._pidfile_handle:
            try:
                # Release lock and close
                fcntl.lockf(self._pidfile_handle, fcntl.LOCK_UN)
                self._pidfile_handle.close()
                self._pidfile_handle = None
            except Exception:
                pass

        if self.pidfile and os.path.exists(self.pidfile):
            try:
                os.remove(self.pidfile)
                logger.info(f"PID file removed: {self.pidfile}")
            except Exception as e:
                logger.warn(f"Failed to remove PID file: {e}")


class DaemonRunner:
    """
    Runner for daemon processes with signal handling.

    Provides a standard interface for running processes as daemons
    with proper signal handling and lifecycle management.
    """

    def __init__(
        self,
        name: str,
        run_func: Callable,
        stop_func: Optional[Callable] = None,
        pidfile: Optional[str] = None,
        logfile: Optional[str] = None,
        daemon: bool = False
    ):
        """
        Initialize daemon runner.

        Args:
            name: Daemon name for logging
            run_func: Main function to run
            stop_func: Function to call on shutdown (optional)
            pidfile: Path to PID file
            logfile: Path to log file (for background mode)
            daemon: If True, run as background daemon
        """
        self.name = name
        self.run_func = run_func
        self.stop_func = stop_func
        self.pidfile = pidfile
        self.logfile = logfile
        self.daemon = daemon
        self._stop_requested = False

    def start(self):
        """Start the daemon."""
        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        # Determine output files for daemon mode
        stdout = self.logfile if self.daemon and self.logfile else '/dev/null'
        stderr = self.logfile if self.daemon and self.logfile else '/dev/null'

        # Darwin safety: allow fork without exec for Objective-C initialized runtimes
        if self.daemon and sys.platform == 'darwin' and 'OBJC_DISABLE_INITIALIZE_FORK_SAFETY' not in os.environ:
            os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
            logger.info("Set OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES for Darwin fork-safety")

        # Optional working directory from settings
        working_dir = '/'
        try:
            from mojo.helpers.settings import settings as mojo_settings
            working_dir = mojo_settings.get('JOBS_DAEMON_WORKDIR', '/')
        except Exception:
            working_dir = '/'

        # Create daemon context
        context = DaemonContext(
            pidfile=self.pidfile,
            stdout=stdout,
            stderr=stderr,
            working_dir=working_dir,
            detach=self.daemon
        )

        with context:
            if self.daemon:
                logger.info(f"{self.name} started as background daemon (PID: {os.getpid()})")
            else:
                logger.info(f"{self.name} started in foreground mode (PID: {os.getpid()})")

            try:
                # Run the main function
                self.run_func()
            except Exception:
                logger.exception(f"{self.name} crashed")
                sys.exit(1)
            finally:
                if self.stop_func and not self._stop_requested:
                    self.stop_func()

    def stop(self):
        """Stop a running daemon by PID file."""
        if not self.pidfile or not os.path.exists(self.pidfile):
            logger.error(f"PID file not found: {self.pidfile}")
            return False

        try:
            with open(self.pidfile, 'r') as f:
                pid = int(f.read().strip())

            # Send SIGTERM
            os.kill(pid, signal.SIGTERM)
            logger.info(f"Sent SIGTERM to {self.name} (PID: {pid})")

            # Wait for process to stop
            for i in range(10):
                try:
                    os.kill(pid, 0)  # Check if process exists
                    time.sleep(1)
                except ProcessLookupError:
                    logger.info(f"{self.name} stopped successfully")
                    return True

            # Force kill if still running
            try:
                os.kill(pid, signal.SIGKILL)
                logger.warn(f"Force killed {self.name} (PID: {pid})")
            except ProcessLookupError:
                pass

            return True

        except Exception as e:
            logger.error(f"Failed to stop {self.name}: {e}")
            return False

    def status(self) -> bool:
        """
        Check if daemon is running.

        Returns:
            True if daemon is running, False otherwise
        """
        if not self.pidfile or not os.path.exists(self.pidfile):
            return False

        try:
            with open(self.pidfile, 'r') as f:
                pid = int(f.read().strip())

            # Check if process exists
            os.kill(pid, 0)
            return True

        except (ProcessLookupError, ValueError):
            # Process doesn't exist or invalid PID
            return False
        except Exception:
            return False

    def restart(self):
        """Restart the daemon."""
        if self.status():
            logger.info(f"Stopping {self.name}...")
            self.stop()
            time.sleep(2)

        logger.info(f"Starting {self.name}...")
        self.start()

    def _handle_signal(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"{self.name} received signal {signum}, shutting down gracefully")
        self._stop_requested = True

        if self.stop_func:
            self.stop_func()

        sys.exit(0)


def run_daemon(
    name: str,
    main_func: Callable,
    args=None,
    kwargs=None,
    daemon: bool = False,
    pidfile: Optional[str] = None,
    logfile: Optional[str] = None
):
    """
    Convenience function to run a process as a daemon.

    Args:
        name: Process name
        main_func: Main function to run
        args: Positional arguments for main_func
        kwargs: Keyword arguments for main_func
        daemon: If True, run as background daemon
        pidfile: PID file path
        logfile: Log file path (for background mode)
    """
    args = args or ()
    kwargs = kwargs or {}

    def run():
        main_func(*args, **kwargs)

    runner = DaemonRunner(
        name=name,
        run_func=run,
        pidfile=pidfile,
        logfile=logfile,
        daemon=daemon
    )

    runner.start()
