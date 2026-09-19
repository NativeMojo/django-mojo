"""Bounded subprocess execution for file renditions.

The parent starts a tiny supervisor in a new process group.  The converter
inherits that group, so a timeout kills the converter and any descendants in
one operation.  On POSIX, a private pipe also lets the supervisor notice that
its job-engine parent disappeared and tear the group down before it can become
orphaned.

This file is executed directly for supervisor mode.  Keep module imports
stdlib-only and import Django settings inside the parent-only helper.
"""

import os
import select
import signal
import subprocess
import sys


DEFAULT_RENDER_TIMEOUT = 1500
MAX_ERROR_LENGTH = 200
_SUPERVISOR_FLAG = "--fileman-render-supervisor"


class RendererProcessError(RuntimeError):
    """A converter could not be started or did not complete successfully."""


class RendererProcessTimeout(RendererProcessError):
    """A converter exceeded its configured wall-clock deadline."""


class RendererProcessFailed(RendererProcessError):
    """A converter exited unsuccessfully or could not be executed."""


def _configured_timeout():
    from mojo.helpers.settings import settings

    try:
        timeout = int(settings.get("FILEMAN_RENDER_TIMEOUT", DEFAULT_RENDER_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_RENDER_TIMEOUT
    return max(1, timeout)


def _tool_name(command):
    if not command:
        return "renderer"
    return os.path.basename(str(command[0]))[:80] or "renderer"


def _stop_process_tree(proc):
    if os.name == "posix":
        # The supervisor handles SIGTERM by killing the converter group.  It
        # remains responsible even after the converter leader has exited.
        if proc.poll() is None:
            proc.terminate()
    elif os.name == "nt" and proc.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    elif proc.poll() is None:
        proc.kill()


def run(command, timeout=None, check=False, input=None, capture_output=False, **kwargs):
    """Run a renderer command with a deadline and process-tree cleanup.

    The return value matches ``subprocess.run``.  Failure messages deliberately
    exclude command arguments and stderr because media paths and embedded
    metadata can be sensitive.
    """
    command = [str(value) for value in command]
    tool = _tool_name(command)
    timeout = _configured_timeout() if timeout is None else max(0.01, float(timeout))

    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE

    heartbeat_read = None
    heartbeat_write = None
    popen_command = command
    if os.name == "posix":
        heartbeat_read, heartbeat_write = os.pipe()
        popen_command = [
            sys.executable,
            os.path.abspath(__file__),
            _SUPERVISOR_FLAG,
            str(heartbeat_read),
        ] + command
        kwargs["pass_fds"] = tuple(set(kwargs.get("pass_fds", ())) | {heartbeat_read})
        kwargs["start_new_session"] = True
    elif os.name == "nt":
        kwargs["creationflags"] = (
            kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
        )

    try:
        proc = subprocess.Popen(popen_command, **kwargs)
    except (OSError, ValueError) as exc:
        if heartbeat_read is not None:
            os.close(heartbeat_read)
            os.close(heartbeat_write)
        raise RendererProcessFailed("%s could not start: %s" % (tool, type(exc).__name__))

    if heartbeat_read is not None:
        os.close(heartbeat_read)

    try:
        try:
            stdout, stderr = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_process_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                stdout, stderr = None, None
            raise RendererProcessTimeout(
                "%s exceeded %ss and its process group was killed"
                % (tool, "%g" % timeout)
            )
    finally:
        if heartbeat_write is not None:
            os.close(heartbeat_write)

    result = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
    if proc.returncode != 0 and check:
        reason = "is unavailable" if proc.returncode == 127 else "failed with exit status %s" % proc.returncode
        raise RendererProcessFailed("%s %s" % (tool, reason))
    return result


def _supervise(heartbeat_fd, command):
    """Run the converter until it exits or the engine-side pipe closes."""
    stopping = [False]

    def stop(_signum, _frame):
        stopping[0] = True

    signal.signal(signal.SIGTERM, stop)
    try:
        child = subprocess.Popen(command, start_new_session=True)
    except OSError:
        return 127

    while child.poll() is None:
        if stopping[0]:
            break
        readable, _, _ = select.select([heartbeat_fd], [], [], 0.1)
        if readable and os.read(heartbeat_fd, 1) == b"":
            stopping[0] = True
            break

    returncode = child.poll()
    # A converter can fork and let its direct child exit. Always clear the
    # converter's group before the supervisor returns so those descendants
    # cannot retain pipes or outlive the engine.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if stopping[0]:
        return 124
    return returncode


if __name__ == "__main__" and len(sys.argv) >= 4 and sys.argv[1] == _SUPERVISOR_FLAG:
    raise SystemExit(_supervise(int(sys.argv[2]), sys.argv[3:]))
