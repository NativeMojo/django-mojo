"""Driving a launched node over SSH until it actually serves.

`apply` gets an instance to "running". That is not the same as "the portal
answers", and the gap between the two is where every interesting failure lives:
cloud-init still working, a config that never arrived, migrations nobody ran,
an app running on the wrong settings profile. This module closes that gap and —
the part that matters — PROVES it closed, rather than reporting success because
no command happened to exit non-zero.

SUBPROCESS SSH, NOT PARAMIKO, mirroring `check_node.build_runner`. It is the
same decision for the same reasons: the operator's own `~/.ssh/config`,
`ProxyJump`, agent forwarding and hardware keys all work with no code, and this
package acquires no dependency it would then have to ship to every node.
`BatchMode=yes` means a box that wants a password fails in seconds instead of
hanging a provisioning run forever.

THE LAST STEP IS THE ONE THAT EXISTS TO FAIL. Reading `var/profile` back and
requiring it to say `prod` is not a formality: a node whose profile file is
missing boots `settings.local`, ignores every endpoint just provisioned, and
looks completely healthy from the outside — nginx serves, the app responds,
`/api/version` returns 200. It is the failure that costs an afternoon, so it is
checked explicitly and FAILS loudly with the remedy named.
"""

import subprocess
import time

from mojo.deploy.provision import report


STEP = "converge"

PROJ_PATH = "/opt/api"
BOOTSTRAP_CONF = f"{PROJ_PATH}/var/bootstrap.conf"
PROFILE_PATH = f"{PROJ_PATH}/var/profile"
SERVICE = "mojo-asgi"

SSH_USER = "ec2-user"

# cloud-init on a fresh AL2023 node plus the whole of stage 1 — the OS packages,
# certbot's own venv, pip, nginx — is minutes, not seconds.
CLOUD_INIT_TIMEOUT = 1800
COMMAND_TIMEOUT = 900
MIGRATE_TIMEOUT = 1800

# The local health probe, polled rather than assumed: systemd returns as soon as
# it has forked, and uvicorn takes a moment to bind.
#
# HTTPS with -k, deliberately. The shipped :80 vhost 301s everything except the
# ACME path to HTTPS, so a plain-http probe can only ever see nginx's redirect —
# never the app (the live MojoLand run failed convergence on exactly that). The
# :443 vhost serves the snakeoil certificate until the certificate step runs,
# so -k is a fact of this moment in the sequence, not a shortcut: the probe's
# subject is "does nginx reach uvicorn's socket and does the app answer",
# certificate validity is the NEXT step's subject.
PROBE_URL = "https://127.0.0.1/api/version"
PROBE_ATTEMPTS = 30
PROBE_SLEEP = 4

WANTED_PROFILE = "prod"


def build_runner(ssh_host, user=SSH_USER, identity=None):
    """Return `run(cmd, timeout) -> (rc, stdout, stderr)`.

    `ssh_host` of None runs locally, which is what makes every path in this
    module testable without a network and is the same seam `check_node` uses.
    """
    target = f"{user}@{ssh_host}" if (ssh_host and user) else ssh_host

    def run(cmd, timeout=COMMAND_TIMEOUT):
        if ssh_host:
            argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                    "-o", "StrictHostKeyChecking=accept-new"]
            if identity:
                argv += ["-i", identity]
            argv += [target, cmd]
        else:
            argv = ["/bin/sh", "-c", cmd]
        try:
            done = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout}s"
        except OSError as err:
            return 127, "", str(err)
        return done.returncode, done.stdout.strip(), done.stderr.strip()

    return run


def _fail(findings, code, message, remedy):
    findings.append(report.Finding(STEP, report.BLIND, code, message, remedy))
    return findings


def wait_for_cloud_init(run, host, findings):
    """Block until stage 0 and stage 1 have finished, or say why they have not.

    `cloud-init status --wait` is the only honest way to ask: it returns when
    the boot pipeline is done, whatever that took. Anything else is a sleep with
    a number somebody guessed.
    """
    rc, out, err = run("cloud-init status --wait", timeout=CLOUD_INIT_TIMEOUT)
    if rc == 0:
        findings.append(report.existing(
            STEP, "cloud_init.done", f"{host}: cloud-init finished"))
        return True
    _fail(findings, "cloud_init.failed",
          f"{host}: cloud-init did not finish cleanly ({out or err})",
          f"ssh in and read /var/log/mojo-stage0.log and "
          f"/var/log/mojo-stage1.log — stage 1 logs every step it ran")
    return False


def sync_config(run, host, findings):
    rc, out, err = run(
        f"sudo python3 -m mojo.deploy.config_sync --config {BOOTSTRAP_CONF}")
    if rc == 0:
        findings.append(report.existing(
            STEP, "config_sync.ok", f"{host}: django.conf is in place"))
        return True
    _fail(findings, "config_sync.failed",
          f"{host}: config_sync exited {rc} ({err or out})",
          "check the node role can read the config bucket, and that "
          "`configure` published django.conf under the prefix in "
          "var/bootstrap.conf")
    return False


def migrate(run, host, findings):
    """Schema migration, on node 0 only.

    `migrate_locked` rather than `migrate`: it takes an advisory lock, so the
    command is safe even if a second node runs it, and this only calls it on
    one node because a fleet-wide migration storm is a real way to deadlock a
    fresh cluster.
    """
    rc, out, err = run(
        f"cd {PROJ_PATH} && sudo -u ec2-user python3 bin/manage.py "
        f"migrate_locked --noinput", timeout=MIGRATE_TIMEOUT)
    if rc == 0:
        findings.append(report.existing(
            STEP, "migrate.ok", f"{host}: migrations applied"))
        return True
    _fail(findings, "migrate.failed",
          f"{host}: migrate_locked exited {rc} ({err or out})",
          "usually the database endpoint or password in the published "
          "django.conf — check it reaches the Aurora writer from this node")
    return False


def start_app(run, host, findings):
    rc, out, err = run(f"sudo systemctl enable --now {SERVICE}")
    if rc != 0:
        _fail(findings, "service.failed",
              f"{host}: systemctl enable --now {SERVICE} exited {rc} "
              f"({err or out})",
              f"ssh in and read `journalctl -u {SERVICE} -n 100`")
        return False
    findings.append(report.existing(
        STEP, "service.ok", f"{host}: {SERVICE} is enabled and running"))
    return True


def wait_for_api(run, host, findings, attempts=PROBE_ATTEMPTS,
                 sleep=PROBE_SLEEP, sleeper=time.sleep):
    """Poll the node's own loopback until the app answers.

    Loopback rather than the public name on purpose: this proves the APP is up,
    with no opinion about DNS, the certificate or a balancer — all of which are
    separate problems with separate remedies, and conflating them is how an
    operator ends up debugging TLS when uvicorn never started.
    """
    for attempt in range(attempts):
        rc, out, _ = run(
            f"curl -fsSk -o /dev/null -w '%{{http_code}}' {PROBE_URL}",
            timeout=30)
        if rc == 0 and out.strip() == "200":
            findings.append(report.existing(
                STEP, "api.ok", f"{host}: {PROBE_URL} answers 200"))
            return True
        if attempt < attempts - 1:
            sleeper(sleep)
    _fail(findings, "api.unreachable",
          f"{host}: {PROBE_URL} never answered 200",
          f"ssh in and read `journalctl -u {SERVICE} -n 100` plus "
          f"{PROJ_PATH}/var/logs/ — nginx is up (it answered the probe socket) "
          f"but the app behind it is not")
    return False


def check_profile(run, host, findings):
    """The read-back that catches the silent failure.

    A node missing `var/profile` runs `settings.local`: it serves, it answers
    /api/version, and it talks to none of the infrastructure just built.
    """
    rc, out, err = run(f"cat {PROFILE_PATH}", timeout=30)
    profile = (out or "").strip()
    if rc == 0 and profile == WANTED_PROFILE:
        findings.append(report.existing(
            STEP, "profile.ok", f"{host}: running the {WANTED_PROFILE} profile"))
        return True
    _fail(findings, "profile.wrong",
          f"{host}: {PROFILE_PATH} reads {profile or '(nothing)'}, not "
          f"{WANTED_PROFILE!r} — this node is running settings.local and is "
          f"ignoring every endpoint that was just provisioned "
          f"{('(' + err + ')') if err else ''}".strip(),
          f"stage 1 writes this file between ec2_deploy.sh and config_sync; "
          f"re-run /opt/api/var/stage1.sh, or as a stop-gap: "
          f"`echo {WANTED_PROFILE} | sudo tee {PROFILE_PATH} && sudo chown "
          f"ec2-user:www {PROFILE_PATH} && sudo systemctl restart {SERVICE}`")
    return False


def converge_node(host, primary=False, runner=None, sleeper=time.sleep):
    """Take one node from "running" to "proven serving". Returns findings.

    Ordered, and it stops at the first failure: every later step reads
    something the failed one was supposed to produce, so continuing would turn
    one clear error into five confusing ones.
    """
    findings = []
    run = runner or build_runner(host)

    if not wait_for_cloud_init(run, host, findings):
        return findings
    if not sync_config(run, host, findings):
        return findings
    if primary and not migrate(run, host, findings):
        return findings
    if not start_app(run, host, findings):
        return findings
    if not wait_for_api(run, host, findings, sleeper=sleeper):
        return findings
    check_profile(run, host, findings)
    return findings


def converge(hosts, runner_for=None, sleeper=time.sleep):
    """Every node, in order, with node 0 carrying the migration.

    Sequential rather than concurrent: the first node runs the migration and
    the rest would only be waiting on the advisory lock anyway, and a serial
    run produces a log an operator can actually read.
    """
    findings = []
    for index, host in enumerate(hosts or []):
        runner = runner_for(host) if runner_for else None
        findings.extend(converge_node(host, primary=(index == 0),
                                      runner=runner, sleeper=sleeper))
    return findings
