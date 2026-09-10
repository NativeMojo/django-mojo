"""Least-privilege cross-UID process executable resolver for MojoSec."""

import ctypes
import json
import os
import socket
import stat
import struct


SOCKET_PATH = "/run/mojosec-proc-identity.sock"
MAX_WIRE_BYTES = 1024
MAX_EXE_BYTES = 512
_REQUEST_KEYS = {"pid", "start_ticks"}


class ProcessIdentityError(RuntimeError):
    pass


def _positive_integer(value, maximum=2 ** 63 - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ProcessIdentityError("invalid process identity request")
    return value


def _start_ticks(proc_root, pid):
    path = os.path.join(proc_root, str(pid), "stat")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ProcessIdentityError("process stat is not regular")
        payload = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    if len(payload) > 4096:
        raise ProcessIdentityError("process stat is too large")
    # The parenthesized comm field may contain spaces or closing parentheses,
    # so fields after it must be located from its final ``) `` delimiter.
    end = payload.rfind(b") ")
    fields = payload[end + 2:].decode("ascii", errors="strict").split()
    if end < 1 or len(fields) < 20:
        raise ProcessIdentityError("process stat is incomplete")
    return _positive_integer(int(fields[19]))


def lookup_executable(pid, start_ticks, proc_root="/proc"):
    """Resolve one same-UID executable while proving the requested PID generation."""
    pid = _positive_integer(pid, 2 ** 31 - 1)
    start_ticks = _positive_integer(start_ticks)
    if _start_ticks(proc_root, pid) != start_ticks:
        raise ProcessIdentityError("process generation changed")
    value = os.readlink(os.path.join(proc_root, str(pid), "exe"))
    if (not isinstance(value, str) or not value.startswith("/") or "\0" in value or
            len(value.encode("utf-8", errors="strict")) > MAX_EXE_BYTES):
        raise ProcessIdentityError("process executable is invalid")
    if _start_ticks(proc_root, pid) != start_ticks:
        raise ProcessIdentityError("process generation changed")
    return value


def _unique_object(payload, message):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ProcessIdentityError(message)
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeError, ValueError, json.JSONDecodeError) as err:
        raise ProcessIdentityError(message) from err
    if not isinstance(value, dict):
        raise ProcessIdentityError(message)
    return value


def _strict_json(payload):
    value = _unique_object(payload, "invalid process identity request")
    if set(value) != _REQUEST_KEYS:
        raise ProcessIdentityError("invalid process identity request")
    return value


def _receive_bounded(connection):
    chunks = []
    size = 0
    while True:
        chunk = connection.recv(min(256, MAX_WIRE_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_WIRE_BYTES:
            raise ProcessIdentityError("process identity request is too large")
    return b"".join(chunks)


def _peer_uid(connection):
    if not hasattr(socket, "SO_PEERCRED"):
        raise ProcessIdentityError("peer credentials are unavailable")
    payload = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    if len(payload) != 12:
        raise ProcessIdentityError("peer credentials are invalid")
    return struct.unpack("3i", payload)[1]


def handle_connection(connection, proc_root="/proc", peer_uid=None):
    """Serve exactly one root-authenticated, bounded lookup."""
    response = {"ok": False}
    try:
        if (peer_uid if peer_uid is not None else _peer_uid(connection)) != 0:
            raise ProcessIdentityError("caller is not root")
        request = _strict_json(_receive_bounded(connection))
        pid = _positive_integer(request["pid"], 2 ** 31 - 1)
        ticks = _positive_integer(request["start_ticks"])
        response = {
            "ok": True, "pid": pid, "start_ticks": ticks,
            "exe": lookup_executable(pid, ticks, proc_root=proc_root),
        }
    except (OSError, ProcessIdentityError, UnicodeError, ValueError):
        response = {"ok": False}
    payload = json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8")
    connection.sendall(payload)


def _secure_socket(path, expected_owner, expected_group):
    info = os.lstat(path)
    return bool(stat.S_ISSOCK(info.st_mode) and info.st_uid == expected_owner and
                info.st_gid == expected_group and stat.S_IMODE(info.st_mode) == 0o600)


def resolve_executable(pid, start_ticks, path=SOCKET_PATH, expected_owner=0,
                       expected_group=0, timeout=1.0):
    """Ask the unprivileged helper for one executable identity, or fail closed."""
    pid = _positive_integer(pid, 2 ** 31 - 1)
    start_ticks = _positive_integer(start_ticks)
    if not _secure_socket(path, expected_owner, expected_group):
        raise ProcessIdentityError("process identity socket is unsafe")
    request = json.dumps(
        {"pid": pid, "start_ticks": start_ticks},
        sort_keys=True, separators=(",", ":")).encode("ascii")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(max(0.1, min(2.0, float(timeout))))
        client.connect(path)
        client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        response = _strict_response(_receive_bounded(client), pid, start_ticks)
    return response["exe"]


def _strict_response(payload, pid, start_ticks):
    value = _unique_object(payload, "invalid process identity response")
    if (not isinstance(value, dict) or set(value) != {"ok", "pid", "start_ticks", "exe"} or
            value.get("ok") is not True or value.get("pid") != pid or
            value.get("start_ticks") != start_ticks):
        raise ProcessIdentityError("invalid process identity response")
    exe = value.get("exe")
    if (not isinstance(exe, str) or not exe.startswith("/") or "\0" in exe or
            len(exe.encode("utf-8", errors="strict")) > MAX_EXE_BYTES):
        raise ProcessIdentityError("invalid process identity response")
    return value


def _disable_core_dumps():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise ProcessIdentityError("cannot protect process identity helper")


def serve():
    """Serve the one systemd-activated root-only Unix socket."""
    if os.geteuid() == 0:
        raise ProcessIdentityError("process identity helper must be unprivileged")
    if (os.environ.get("LISTEN_PID") != str(os.getpid()) or
            os.environ.get("LISTEN_FDS") != "1"):
        raise ProcessIdentityError("one systemd socket is required")
    _disable_core_dumps()
    listener = socket.socket(fileno=3)
    if (listener.family != socket.AF_UNIX or listener.getsockopt(
            socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1):
        raise ProcessIdentityError("invalid process identity listener")
    while True:
        connection, _ = listener.accept()
        try:
            with connection:
                connection.settimeout(2.0)
                handle_connection(connection)
        except (OSError, ProcessIdentityError, UnicodeError, ValueError):
            # A timed-out or disconnected client is request-local. Keep the
            # systemd listener alive for the next lineage lookup.
            continue


def main():
    try:
        serve()
    except (OSError, ProcessIdentityError, ValueError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
