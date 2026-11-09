"""Maestro item 9315 — an Admin "add node" clone with no var/profile must not
start the app, now or later.

The clone's image is a copy of the source node's disk. settings/helper.py
boots ``settings.local`` when VAR_ROOT/profile is absent, so a markerless
source used to produce markerless clones that were restarted, joined the job
fleet and went behind the balancer. Stopping the app once was not enough
(review 141472): the jobs cron starts the engine again every minute and
config-sync restarts the app. The first-boot script now checks the marker
before it takes the clone's own hostname (so an engine started mid-quarantine
can never answer as the runner the join leg admits, review 141607), moves the
project's cron files out of cron.d first, gates the units on a non-empty
marker with a drop-in, checks every stop, and powers the box off.

The REAL generated script runs under bash. Every external command is a stub
on PATH that logs to a calls file; the node root, systemd directory and cron
directory are temp directories.
"""
import os
import subprocess
import tempfile
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "bug"

STUBS = ("python3", "shutdown")
IID = "i-0abc1234def567890"
SOURCE_HOSTNAME = "mojo-api-a-source"
UNITS = ("mojo-asgi.service", "config-sync.service", "config-sync.timer")
DROPIN = "zz-mojo-clone-refused.conf"


def _stub(path, body):
    with open(path, "w") as fh:
        fh.write("#!/bin/bash\n" + body)
    os.chmod(path, 0o755)


def _run_user_data(with_profile, stop_fails=False, marker="prod\n"):
    """Run the generated script. ``stop_fails`` makes the engine stop fail and
    leaves the app reported active, i.e. a quarantine that did not take.

    The box starts with the SOURCE's hostname, as a clone of its disk does.
    Every ``systemctl stop`` and ``jobman`` call logs the identity an engine
    started at that instant would register under (the hostname, which is what
    job_engine.host_channel reads) and whether the jobs cron is still
    installed — the window review 141607 raced a runner through."""
    from mojo.apps.aws.services import capacity

    tmp = tempfile.mkdtemp(prefix="item9315_")
    root = os.path.join(tmp, "opt", "api")
    stub_dir = os.path.join(tmp, "stub")
    systemd_dir = os.path.join(tmp, "systemd")
    cron_dir = os.path.join(tmp, "cron.d")
    calls = os.path.join(tmp, "calls.log")
    hostname = os.path.join(tmp, "hostname")
    for path in (os.path.join(root, "var"), os.path.join(root, "bin"),
                 stub_dir, systemd_dir, cron_dir):
        os.makedirs(path)
    with open(hostname, "w") as fh:
        fh.write(SOURCE_HOSTNAME)
    if with_profile:
        with open(os.path.join(root, "var", "profile"), "w") as fh:
            fh.write(marker)
    # The shipped jobs cron (templates/cron.d/3_mojo_jobs) names the project
    # root; certbot's does not and is left alone.
    jobs_cron = os.path.join(cron_dir, "3_mojo_jobs")
    with open(jobs_cron, "w") as fh:
        fh.write(f"* * * * * ec2-user /usr/bin/python3 -E -P -m mojo.deploy.jobman "
                 f"--root {root} start\n")
    with open(os.path.join(cron_dir, "1_certbot"), "w") as fh:
        fh.write("0 3 * * * root certbot renew -q\n")
    for name in STUBS:
        _stub(os.path.join(stub_dir, name), f'echo "{name} $*" >> "{calls}"\n')
    window = (f'echo "window identity=$(cat "{hostname}") '
              f'cron=$([ -f "{jobs_cron}" ] && echo installed || echo gone)" >> "{calls}"\n')
    # IMDS answers the token PUT with a token and the instance-id GET with IID.
    _stub(os.path.join(stub_dir, "curl"),
          f'echo "curl $*" >> "{calls}"\n'
          f'case "$*" in *instance-id*) echo "{IID}";; *) echo token;; esac\n')
    _stub(os.path.join(stub_dir, "hostnamectl"),
          f'echo "hostnamectl $*" >> "{calls}"\n'
          f'[ "$1" = "set-hostname" ] && printf "%s" "$2" > "{hostname}"\n')
    active = 'mojo-asgi.service' if stop_fails else '__none__'
    _stub(os.path.join(stub_dir, "systemctl"),
          f'echo "systemctl $*" >> "{calls}"\n'
          f'[ "$1" = "stop" ] && {window}'
          f'if [ "$1" = "is-active" ]; then [ "$3" = "{active}" ] && exit 0; exit 3; fi\n')
    _stub(os.path.join(root, "bin", "jobman"),
          f'echo "jobman $*" >> "{calls}"\n' + window
          + ("exit 1\n" if stop_fails else ""))

    script = os.path.join(tmp, "user_data.sh")
    with open(script, "w") as fh:
        fh.write(capacity.node_user_data("mojo-api-a", root=root,
                                         systemd_dir=systemd_dir, cron_dir=cron_dir))
    env = {"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": tmp}
    proc = subprocess.run(["/bin/bash", script], env=env, capture_output=True,
                          text=True, timeout=30)
    log = open(calls).read() if os.path.exists(calls) else ""
    return proc, log, root, systemd_dir, cron_dir


def _scheduled_starts(cron_dir, root):
    """What cron would run next minute for this project: every cron.d file
    that still names the root."""
    found = []
    for name in sorted(os.listdir(cron_dir)):
        path = os.path.join(cron_dir, name)
        if os.path.isfile(path) and root in open(path).read():
            found.append(name)
    return found


@th.django_unit_test("a clone with no var/profile is quarantined and powered off (THE regression)")
def test_markerless_clone_refuses(opts):
    proc, log, root, systemd_dir, cron_dir = _run_user_data(with_profile=False)
    assert_true(proc.returncode != 0,
                f"a markerless clone must fail its first boot, got rc={proc.returncode}")
    assert_true("var/profile is missing" in proc.stderr,
                f"the refusal must say why, stderr was: {proc.stderr!r}")
    assert_true("quarantine incomplete" not in proc.stderr,
                f"a clean quarantine was reported incomplete: {proc.stderr!r}")
    assert_true("jobman stop" in log, f"the job engine must be stopped, calls were:\n{log}")
    assert_true("systemctl poweroff" in log, f"the box must power off, calls were:\n{log}")
    assert_true("restart" not in log,
                f"nothing may be restarted, calls were:\n{log}")
    assert_true("systemctl start" not in log and "node_setup" not in log,
                f"config-sync and node_setup must not run, calls were:\n{log}")


@th.django_unit_test("the refusal outlives the script: units gated on the marker, jobs cron gone")
def test_markerless_refusal_persists(opts):
    proc, log, root, systemd_dir, cron_dir = _run_user_data(with_profile=False)
    # The next start from anywhere — config-sync's restart_app, the timer, an
    # operator, the next boot — goes through systemd, which skips a unit whose
    # condition fails. Each gated unit carries the condition on the marker.
    for unit in UNITS:
        path = os.path.join(systemd_dir, f"{unit}.d", DROPIN)
        assert_true(os.path.exists(path), f"{unit} has no quarantine drop-in")
        text = open(path).read()
        assert_true(text.startswith("[Unit]\n"), f"{unit} drop-in is not a [Unit] section: {text!r}")
        assert_true(f"ConditionFileNotEmpty={root}/var/profile\n" in text,
                    f"{unit} is not gated on the marker: {text!r}")
    assert_true("systemctl daemon-reload" in log,
                f"the drop-ins were never loaded, calls were:\n{log}")
    # The next scheduled engine start is the jobs cron. Advancing a minute
    # past the refusal must find nothing that would start it.
    assert_eq(_scheduled_starts(cron_dir, root), [],
              "a cron file naming the project root is still scheduled")
    assert_true(os.path.exists(os.path.join(cron_dir + ".mojo-quarantine", "3_mojo_jobs")),
                "the jobs cron was deleted, not set aside for recovery")
    assert_true(os.path.exists(os.path.join(cron_dir, "1_certbot")),
                "a cron file that does not name the project was touched")


@th.django_unit_test("a runner started mid-quarantine can never answer as the clone the join leg admits")
def test_runner_during_quarantine_cannot_be_admitted(opts):
    from mojo.apps.aws.services import capacity

    proc, log, root, systemd_dir, cron_dir = _run_user_data(with_profile=False)
    admitted = capacity.expected_runner_id(capacity.expected_node_id("mojo-api-a", IID))
    assert_true("hostnamectl" not in log,
                f"a markerless clone took its own hostname, calls were:\n{log}")
    windows = [line for line in log.splitlines() if line.startswith("window ")]
    assert_true(windows, f"no stop was observed, calls were:\n{log}")
    for line in windows:
        identity = line.split("identity=", 1)[1].split(" ", 1)[0]
        assert_true(f"{identity}-engine" != admitted,
                    f"an engine started mid-quarantine would register as the admitted "
                    f"runner {admitted}: {line}")
        assert_true(line.endswith("cron=gone"),
                    f"the jobs cron was still scheduled while the quarantine ran: {line}")


@th.django_unit_test("an EMPTY var/profile is refused like a missing one")
def test_empty_marker_refuses(opts):
    proc, log, root, systemd_dir, cron_dir = _run_user_data(with_profile=True, marker="")
    assert_true(proc.returncode != 0, f"an empty marker booted, rc={proc.returncode}")
    assert_true("systemctl poweroff" in log and "hostnamectl" not in log,
                f"an empty marker was not quarantined, calls were:\n{log}")


@th.django_unit_test("a stop that fails is reported as an incomplete quarantine, and the box still powers off")
def test_failed_stop_is_not_a_clean_quarantine(opts):
    proc, log, root, systemd_dir, cron_dir = _run_user_data(with_profile=False,
                                                            stop_fails=True)
    assert_true(proc.returncode != 0, f"rc={proc.returncode}")
    assert_true("quarantine incomplete" in proc.stderr
                and "jobman-stop" in proc.stderr
                and "active:mojo-asgi.service" in proc.stderr,
                f"the failed stops were not named: {proc.stderr!r}")
    assert_true("systemctl poweroff" in log,
                f"an incomplete quarantine must still power off, calls were:\n{log}")


@th.django_unit_test("a clone with var/profile boots exactly as before")
def test_profiled_clone_boots(opts):
    proc, log, root, systemd_dir, cron_dir = _run_user_data(with_profile=True)
    assert_eq(proc.returncode, 0, f"stderr: {proc.stderr!r}")
    order = [line.split(" ", 1)[0] + " " + line.split(" ", 2)[1]
             for line in log.strip().splitlines() if line.startswith(("systemctl", "python3", "jobman"))]
    assert_eq(order, ["systemctl start", "python3 -m", "systemctl restart", "jobman restart"],
              f"config-sync, node_setup, app restart, engine restart, in order; calls were:\n{log}")
    from mojo.apps.aws.services import capacity
    assert_true(f"hostnamectl set-hostname {capacity.expected_node_id('mojo-api-a', IID)}" in log,
                f"a profiled clone did not take its own hostname, calls were:\n{log}")
    assert_true(log.index("hostnamectl") < log.index("systemctl start"),
                f"the hostname must be set before config-sync, calls were:\n{log}")
    assert_true("systemctl stop" not in log and "poweroff" not in log,
                f"nothing is stopped, calls were:\n{log}")
    assert_eq(os.listdir(systemd_dir), [], "a profiled clone got quarantine drop-ins")
    assert_eq(_scheduled_starts(cron_dir, root), ["3_mojo_jobs"],
              "a profiled clone lost its jobs cron")
