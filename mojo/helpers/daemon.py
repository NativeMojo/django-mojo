import os
import sys
import signal

class Daemon:
    """
    A simple base class for creating a Linux daemon process without using third-party daemon libraries.
    """
    def __init__(self, name, var_path=None):
        self.name = name
        self.var_path = var_path
        if var_path is not None:
            self.pid_file = os.path.join(var_path, name, f"{name}.pid")
            self.log_file = os.path.join(var_path, name, f"{name}.log")
            self.error_file = os.path.join(var_path, name, "errors.log")
        else:
            self.pid_file = f"{name}.pid"
            self.log_file = f"{name}.log"
            self.error_file = "errors.log"
        self.running = True

    def daemonize(self):
        """Daemonizes the process using the double-fork technique."""
        if os.fork() > 0:
            sys.exit(0)  # Exit parent
        os.chdir("/")
        os.setsid()  # Become session leader
        os.umask(0)
        if os.fork() > 0:
            sys.exit(0)  # Exit second parent
        # Redirect standard file descriptors
        sys.stdout.flush()
        sys.stderr.flush()
        with open(self.log_file, "a", buffering=1) as log, open(self.error_file, "a", buffering=1) as err:
            os.dup2(log.fileno(), sys.stdout.fileno())
            os.dup2(err.fileno(), sys.stderr.fileno())
        self.write_pid()
        self.register_signal_handlers()

    def write_pid(self):
        """Writes the process ID to a PID file."""
        with open(self.pid_file, "w") as f:
            f.write(str(os.getpid()) + "\n")

    def remove_pid(self):
        """Removes the PID file upon shutdown."""
        if os.path.exists(self.pid_file):
            os.remove(self.pid_file)

    def register_signal_handlers(self):
        """Registers signal handlers for graceful shutdown."""
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)

    def handle_signal(self, signum, frame):
        """Handles termination signals to shut down cleanly."""
        self.running = False
        if signum == signal.SIGTERM:
            self.cleanup()
            sys.exit(0)

    def cleanup(self):
        """Override this method for custom cleanup actions before shutdown."""
        self.remove_pid()

    def start(self):
        """Starts the daemon process."""
        if os.path.exists(self.pid_file):
            sys.exit(1)
        self.daemonize()
        self.run()

    def get_pid(self):
        """Returns the process ID of the running daemon."""
        try:
            with open(self.pid_file, "r") as f:
                return int(f.read().strip())
        except FileNotFoundError:
            return None

    def stop(self, force=False):
        """Stops the running daemon by sending SIGTERM or SIGINT based on the force option."""
        try:
            pid = self.get_pid()
            os.kill(pid, signal.SIGINT if force else signal.SIGTERM)
            self.remove_pid()
        except FileNotFoundError:
            pass
        except ProcessLookupError:
            self.remove_pid()

    def run(self):
        """Override this method in subclasses to define the daemon's main loop."""
        raise NotImplementedError("You must override the run() method!")
