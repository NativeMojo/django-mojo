"""Maestro item 9315 — an Admin "add node" clone with no var/profile must not
start the app.

The clone's image is a copy of the source node's disk. settings/helper.py
boots ``settings.local`` when VAR_ROOT/profile is absent, so a markerless
source used to produce markerless clones that were restarted, joined the job
fleet and went behind the balancer. The first-boot script now stops the app
and the job engine and exits non-zero instead; with no engine the join leg's
runner wait fails and the clone is never registered.

The REAL generated script runs under bash. Every external command is a stub
on PATH that logs to a calls file, and the node root is a temp directory.
"""
import os
import subprocess
import tempfile
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "bug"

STUBS = ("curl", "hostnamectl", "systemctl", "python3")


def _run_user_data(with_profile):
    from mojo.apps.aws.services import capacity

    tmp = tempfile.mkdtemp(prefix="item9315_")
    root = os.path.join(tmp, "opt", "api")
    stub_dir = os.path.join(tmp, "stub")
    calls = os.path.join(tmp, "calls.log")
    os.makedirs(os.path.join(root, "var"))
    os.makedirs(os.path.join(root, "bin"))
    os.makedirs(stub_dir)
    if with_profile:
        with open(os.path.join(root, "var", "profile"), "w") as fh:
            fh.write("prod\n")
    for name in STUBS:
        path = os.path.join(stub_dir, name)
        with open(path, "w") as fh:
            fh.write(f'#!/bin/bash\necho "{name} $*" >> "{calls}"\n')
        os.chmod(path, 0o755)
    jobman = os.path.join(root, "bin", "jobman")
    with open(jobman, "w") as fh:
        fh.write(f'#!/bin/bash\necho "jobman $*" >> "{calls}"\n')
    os.chmod(jobman, 0o755)

    script = os.path.join(tmp, "user_data.sh")
    with open(script, "w") as fh:
        fh.write(capacity.node_user_data("mojo-api-a", root=root))
    env = {"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": tmp}
    proc = subprocess.run(["/bin/bash", script], env=env, capture_output=True,
                          text=True, timeout=30)
    log = open(calls).read() if os.path.exists(calls) else ""
    return proc, log


@th.django_unit_test("a clone with no var/profile stops the app and exits non-zero (THE regression)")
def test_markerless_clone_refuses(opts):
    proc, log = _run_user_data(with_profile=False)
    assert_true(proc.returncode != 0,
                f"a markerless clone must fail its first boot, got rc={proc.returncode}")
    assert_true("systemctl stop mojo-asgi.service" in log,
                f"the app must be stopped, calls were:\n{log}")
    assert_true("jobman stop" in log, f"the job engine must be stopped, calls were:\n{log}")
    assert_true("systemctl restart mojo-asgi.service" not in log,
                f"the app must not be restarted, calls were:\n{log}")
    assert_true("jobman restart" not in log,
                f"the job engine must not be restarted, calls were:\n{log}")
    assert_true("config-sync" not in log and "node_setup" not in log,
                f"nothing after the guard may run, calls were:\n{log}")
    assert_true("var/profile is missing" in proc.stderr,
                f"the refusal must say why, stderr was: {proc.stderr!r}")


@th.django_unit_test("a clone with var/profile boots exactly as before")
def test_profiled_clone_boots(opts):
    proc, log = _run_user_data(with_profile=True)
    assert_eq(proc.returncode, 0, f"stderr: {proc.stderr!r}")
    order = [line.split(" ", 1)[0] + " " + line.split(" ", 2)[1]
             for line in log.strip().splitlines() if line.startswith(("systemctl", "python3", "jobman"))]
    assert_eq(order, ["systemctl start", "python3 -m", "systemctl restart", "jobman restart"],
              f"config-sync, node_setup, app restart, engine restart, in order; calls were:\n{log}")
    assert_true("systemctl stop" not in log, f"nothing is stopped, calls were:\n{log}")
