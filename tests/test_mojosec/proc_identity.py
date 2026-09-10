import json
import os
import socket
import tempfile
import threading

from testit import helpers as th


def _proc_fixture(root, pid=42, ticks=210):
    os.makedirs(os.path.join(root, "sys", "kernel", "random"))
    with open(os.path.join(root, "sys", "kernel", "random", "boot_id"), "w") as handle:
        handle.write("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n")
    directory = os.path.join(root, str(pid))
    os.makedirs(os.path.join(directory, "attr"))
    fields = [str(pid), "(python)", "S", "1"] + ["0"] * 17 + [str(ticks)]
    with open(os.path.join(directory, "stat"), "w") as handle:
        handle.write(" ".join(fields))
    with open(os.path.join(directory, "cmdline"), "wb") as handle:
        handle.write(b"/usr/bin/python\0/opt/api/bin/jobs.py\0engine\0foreground\0")
    with open(os.path.join(directory, "cgroup"), "w") as handle:
        handle.write("0::/user.slice/session.scope\n")
    with open(os.path.join(directory, "attr", "current"), "w") as handle:
        handle.write("unconfined\n")
    os.symlink("/usr/bin/python3.12", os.path.join(directory, "exe"))
    return directory


@th.unit_test("the unprivileged resolver proves one unchanged PID generation")
def test_lookup_executable(opts):
    from mojo.mojosec.proc_identity import ProcessIdentityError, lookup_executable

    with tempfile.TemporaryDirectory() as root:
        _proc_fixture(root)
        th.assert_eq(lookup_executable(42, 210, proc_root=root), "/usr/bin/python3.12",
                     "the same-UID helper must return only the proven executable")
        with th.assert_raises(ProcessIdentityError):
            lookup_executable(42, 211, proc_root=root)

        stat_path = os.path.join(root, "42", "stat")
        with open(stat_path) as handle:
            payload = handle.read()
        with open(stat_path, "w") as handle:
            handle.write(payload.replace("(python)", "(python worker)"))
        th.assert_eq(lookup_executable(42, 210, proc_root=root), "/usr/bin/python3.12",
                     "spaces in the kernel comm field must not shift start ticks")


@th.unit_test("the helper protocol is root-only bounded and exact")
def test_helper_protocol(opts):
    from mojo.mojosec.proc_identity import handle_connection

    with tempfile.TemporaryDirectory() as root:
        _proc_fixture(root)
        server, client = socket.socketpair()
        worker = threading.Thread(
            target=handle_connection, args=(server,),
            kwargs={"proc_root": root, "peer_uid": 0})
        worker.start()
        client.sendall(b'{"pid":42,"start_ticks":210}')
        client.shutdown(socket.SHUT_WR)
        response = json.loads(client.recv(1024))
        worker.join(timeout=2)
        server.close()
        client.close()
        th.assert_eq(response, {
            "ok": True, "pid": 42, "start_ticks": 210,
            "exe": "/usr/bin/python3.12",
        }, "the helper response must expose only the fixed process identity")

        server, client = socket.socketpair()
        worker = threading.Thread(
            target=handle_connection, args=(server,),
            kwargs={"proc_root": root, "peer_uid": 1000})
        worker.start()
        client.sendall(b'{"pid":42,"start_ticks":210}')
        client.shutdown(socket.SHUT_WR)
        refused = json.loads(client.recv(1024))
        worker.join(timeout=2)
        server.close()
        client.close()
        th.assert_eq(refused, {"ok": False},
                     "an application-user client must receive no process identity")


@th.unit_test("the sensor accepts only a generation-bound helper executable")
def test_enrich_process_resolver_seam(opts):
    from mojo.mojosec.lineage import enrich_process

    with tempfile.TemporaryDirectory() as root:
        directory = _proc_fixture(root)
        os.unlink(os.path.join(directory, "exe"))
        calls = []

        def resolver(pid, ticks):
            calls.append((pid, ticks))
            return "/usr/bin/python3.12"

        found = enrich_process(42, proc_root=root, exe_resolver=resolver)
        th.assert_eq(calls, [(42, 210)],
                     "the resolver must receive the first live PID generation")
        th.assert_eq((found["start_ticks"], found["exe"], found["cmdline"][-2:]),
                     (210, "/usr/bin/python3.12", ["engine", "foreground"]),
                     "helper executable proof must remain bracketed by live stat reads")
