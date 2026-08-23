#!/usr/bin/env python3
"""Audit one django-mojo node against the on-node deploy-plane contract.

Read-only. Every probe is a stat/test/cmp/grep/systemctl-query/GET — nothing
here mutates the node, so it is safe to run against production mid-deploy, and
safe to hand to an AI session as the "is this node converged?" oracle.

This is the complement of mojo.deploy.check_setup: that tool audits the AWS
account topology through boto3 and never touches a node; this one audits the
node itself — git state, installed cron/systemd/nginx files versus the
rendered contract in var/deploy/, certificates, config-plane wiring, shim
adoption, legacy leftovers — and never calls an AWS API.

    python3 -m mojo.deploy.check_node                     # on the node
    python3 -m mojo.deploy.check_node --ssh api1          # from anywhere, over ssh
    python3 -m mojo.deploy.check_node --json              # machine-readable, for CI
    python3 -m mojo.deploy.check_node --section cron,jobs # one area at a time
    python3 -m mojo.deploy.check_node --require-shims     # forked scripts FAIL

The cron and systemd contracts are the RENDERED copies under
`${PROJ_PATH}/var/deploy/{cron.d,systemd}` — what the last post_deploy run
materialized from the installed framework's templates plus the project's
overlays. Absent, they are INFO ("run post_deploy"), never FAIL: a node that
has not rendered yet is behind, not broken. The nginx contract stays
project-owned (`aws/nginx/` in the repo tree named by --repo-path).

Over --ssh every probe is wrapped in `ssh <host> ...` (BatchMode, so it never
prompts), and --repo-path is a REMOTE path defaulting to --project-path. The
template-freshness check (installed package vs var/deploy) runs only locally —
over ssh the local package version says nothing about the remote node's.

CONF FILES ARE NEVER READ. var/django.conf and var/bootstrap.conf hold every
downstream secret a fleet has. This audit stats them (exists/owner/mode) and
greps for the PRESENCE of fixed key NAMES — output is only the key name, never
a value. Nothing here cats, prints, or copies conf contents, and nothing ever
should. (aws/node_retired.conf, aws/node_overrides.conf and aws/node_roles.conf
are read in full — they are name lists, not secrets.)

There is exactly ONE bounded exception, and it is deliberately narrow: the
roles section learns this node's role, which may be declared by
`MOJO_NODE_ROLE` inside var/bootstrap.conf. It never reads that file itself —
it asks `python3 -m mojo.deploy.node_role resolve --json`, whose every output
field is a validated role identifier or the empty string. No conf byte can
reach this report through it. Do not widen that either.

A finding is one of:

    PASS   matches the contract
    WARN   works, but is a known way to lose an afternoon later
    FAIL   convergence, correctness, or security drift that should be fixed
    INFO   context, no judgement

Exit code is 1 if anything FAILed, else 0 (2 for usage errors) — so this can
gate a deploy. Over ssh each probe is its own connection; enable ssh
multiplexing (ControlMaster) in ~/.ssh/config if the round-trips add up.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys

from mojo.deploy.mojosec import DEPLOY_STATE_PATH as MOJOSEC_DEPLOY_STATE_PATH

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"

# Ordered so the report reads top-down: the tree, what runs on it, what
# schedules and serves it, then the trailing edges (certs, config, shims,
# leftovers).
SECTIONS = (
    "repo", "framework", "roles", "cron", "systemd", "mojosec", "nginx",
    "certs", "config_plane", "shims", "legacy", "var_ownership", "jobs",
)

# The project files the shim contract defines. Each is expected to be a thin
# shim delegating to mojo.deploy; a full copy is a fork that stops receiving
# framework fixes.
SHIM_FILES = (
    "aws/update.sh", "aws/post_deploy.sh",
    "aws/certbot_sync.py", "aws/check_node.py",
)

# Fixed key names whose PRESENCE (never value) is probed in var/django.conf.
WEBHOOK_KEY = "GITHUB_WEBHOOK_SECRET"
CERT_SYNC_KEYS = ("AWS_CERT_BUCKET", "LOAD_BALANCER_DOMAIN", "PRIMARY_BALANCER_HOST")

VERBOSE = False

q = shlex.quote


class Report:
    """Collects findings and prints them grouped by section."""

    def __init__(self):
        self.findings = []

    def add(self, section, status, name, detail, fix=None):
        self.findings.append({
            "section": section,
            "status": status,
            "name": name,
            "detail": detail,
            "fix": fix,
        })

    def passed(self, section, name, detail):
        self.add(section, PASS, name, detail)

    def warn(self, section, name, detail, fix=None):
        self.add(section, WARN, name, detail, fix)

    def fail(self, section, name, detail, fix=None):
        self.add(section, FAIL, name, detail, fix)

    def info(self, section, name, detail):
        self.add(section, INFO, name, detail)

    def counts(self):
        totals = {PASS: 0, WARN: 0, FAIL: 0, INFO: 0}
        for row in self.findings:
            totals[row["status"]] += 1
        return totals

    def render(self):
        marks = {PASS: "  ok  ", WARN: " WARN ", FAIL: " FAIL ", INFO: " info "}
        current = None
        for row in self.findings:
            if row["section"] != current:
                current = row["section"]
                print()
                print(f"── {current.upper()} " + "─" * (66 - len(current)))
            print(f"[{marks[row['status']]}] {row['name']}")
            print(f"          {row['detail']}")
            if row["fix"]:
                print(f"          fix: {row['fix']}")
        totals = self.counts()
        print()
        print("─" * 72)
        print(f"  {totals[PASS]} pass · {totals[WARN]} warn · "
              f"{totals[FAIL]} fail · {totals[INFO]} info")


# ---------------------------------------------------------------------------
# probe plumbing — one run() shared by local and ssh mode
# ---------------------------------------------------------------------------

def build_runner(ssh_host):
    """Return run(cmd) -> (rc, stdout, stderr). cmd is a shell string, executed
    locally via /bin/sh or remotely via `ssh <host> <cmd>` (BatchMode, so a
    node needing a password fails fast instead of hanging the audit)."""

    def run(cmd, timeout=30):
        if ssh_host:
            argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                    ssh_host, cmd]
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


def exists(run, path, flag="-f"):
    return run(f"test {flag} {q(path)}")[0] == 0


def stat_of(run, path, sudo=""):
    """(user, group, octal-mode) or None. -L so lineage symlinks report the
    target's mode, not the link's."""
    rc, out, _ = run(f"{sudo}stat -Lc '%U %G %a' {q(path)} 2>/dev/null")
    parts = out.split()
    if rc != 0 or len(parts) != 3:
        return None
    return parts


def list_names(run, path, sudo=""):
    rc, out, _ = run(f"{sudo}ls -1 {q(path)} 2>/dev/null")
    if rc != 0:
        return None
    return [n for n in out.splitlines() if n.strip()]


def compare(run, src, dst):
    """Byte-compare a contract file against its installed copy, in one probe.
    SAME / DIFF / NODST (not installed) / NOSRC (contract file missing)."""
    rc, out, _ = run(
        f"if [ ! -f {q(src)} ]; then echo NOSRC; "
        f"elif [ ! -f {q(dst)} ]; then echo NODST; "
        f"elif cmp -s {q(src)} {q(dst)}; then echo SAME; else echo DIFF; fi")
    return out if out in ("NOSRC", "NODST", "SAME", "DIFF") else "ERROR"


def compare_set(report, section, run, src_dir, dst_dir, names, fix):
    """The convergence pattern shared by cron and systemd: every file the
    rendered contract ships must be installed byte-identical."""
    for name in sorted(names):
        state = compare(run, f"{src_dir}/{name}", f"{dst_dir}/{name}")
        if state == "SAME":
            report.passed(section, name, f"installed byte-identical in {dst_dir}")
        elif state == "NODST":
            report.fail(section, f"{name} not installed",
                        f"the rendered contract ships {src_dir}/{name} but "
                        f"{dst_dir} does not have it — convergence has not run",
                        fix)
        elif state == "DIFF":
            report.fail(section, f"{name} drifted",
                        f"{dst_dir}/{name} differs from the rendered copy — a "
                        "hand-edit on the node, or a deploy that never "
                        "converged", fix)
        elif state == "NOSRC":
            report.info(section, f"{name}: contract file missing",
                        f"{src_dir}/{name} disappeared mid-audit")
        else:
            report.info(section, f"{name}: compare failed",
                        "could not run cmp on this pair")


def days_until(enddate_line):
    """days from now until an `openssl x509 -enddate` line, or None."""
    raw = enddate_line.split("=", 1)[-1].strip()
    for zone in (" GMT", " UTC"):
        raw = raw.replace(zone, "")
    raw = " ".join(raw.split())
    try:
        when = datetime.datetime.strptime(raw, "%b %d %H:%M:%S %Y")
    except ValueError:
        return None
    when = when.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return (when - now).days


def read_name_list(run, path):
    """Parse a `#`-commented name-list file (node_retired.conf,
    node_overrides.conf) through the runner, so it works over ssh too.
    Missing file = empty list, by design — both files are optional."""
    rc, out, _ = run(f"cat {q(path)} 2>/dev/null")
    names = []
    if rc != 0:
        return names
    for line in out.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def read_retired(run, proj):
    """(retired cron names, retired conf.d names) declared in
    ${PROJ_PATH}/aws/node_retired.conf — the explicit-list complement of
    post_deploy's structural sweep, for files that never mention PROJ_PATH."""
    retired_cron, retired_confd = [], []
    for line in read_name_list(run, f"{proj}/aws/node_retired.conf"):
        if line.startswith("cron.d/"):
            retired_cron.append(line[len("cron.d/"):])
        elif line.startswith("conf.d/"):
            retired_confd.append(line[len("conf.d/"):])
    return retired_cron, retired_confd


# ---------------------------------------------------------------------------
# roles — which kind of box is this, and does it hold only its own files
# ---------------------------------------------------------------------------

# The root-sealed authority mojo.deploy.node_role reads and post_deploy seals.
ROLE_AUTHORITY = "/etc/mojo/deploy-role.conf"

# Where an owned name of each kind is installed on the node.
ROLE_INSTALL_DIRS = {
    "conf.d": "/etc/nginx/conf.d",
    "cron.d": "/etc/cron.d",
    "systemd": "/etc/systemd/system",
}

EMPTY_ROLE_STATE = {
    "declared": False, "manifest": {}, "manifest_error": None,
    "role": "", "source": "none", "sealed": "", "env": "", "bootstrap": "",
    "probe_error": None, "module_missing": False,
    "foreign": {kind: [] for kind in ROLE_INSTALL_DIRS},
}


def read_role_state(run, proj, repo):
    """Everything the roles section grades, resolved ONCE per audit.

    The manifest is read from the REPO tree (that is where the deploy plane
    reads it, and it is what shipped_confd derives from); node_retired.conf's
    {proj} read is the deliberate divergence, because retirement is about what
    is installed on the node rather than what the repo declares.

    The role itself comes through the injected runner as
    `python3 -m mojo.deploy.node_role resolve --json`, so over --ssh the answer
    is about the REMOTE node, and so no conf byte is ever read here.
    """
    from mojo.deploy import node_role as nr

    state = dict(EMPTY_ROLE_STATE)
    state["foreign"] = {kind: [] for kind in ROLE_INSTALL_DIRS}
    rc, out, _ = run(f"cat {q(f'{repo}/aws/node_roles.conf')} 2>/dev/null")
    if rc != 0:
        return state
    state["declared"] = True
    try:
        state["manifest"] = nr.parse_manifest(out + "\n")
    except nr.NodeRoleError as err:
        state["manifest_error"] = str(err)
        return state

    rc, out, err = run(f"python3 -m mojo.deploy.node_role resolve "
                       f"--project-path {q(proj)} --json")
    if rc != 0:
        combined = (err or out or "role probe failed").splitlines()
        detail = combined[-1] if combined else "role probe failed"
        # Only an older FRAMEWORK is "predates the feature". A python3 that
        # cannot import mojo at all is a different (already reported) problem,
        # and must not be excused as one.
        if "No module named" in (err or out) and "node_role" in (err or out):
            state["module_missing"] = True
        state["probe_error"] = detail
        return state
    try:
        payload = json.loads(out)
        for key in ("role", "source", "sealed", "env", "bootstrap"):
            state[key] = str(payload.get(key) or "")
    except (ValueError, AttributeError):
        state["probe_error"] = "the role probe did not answer with JSON"
        return state

    if state["role"] and state["role"] in state["manifest"]:
        for kind in ROLE_INSTALL_DIRS:
            state["foreign"][kind] = nr.foreign(state["manifest"],
                                                state["role"], kind)
    return state


def check_roles(report, run, state, proj, repo):
    if not state["declared"]:
        report.info("roles", "no role manifest",
                    f"{repo}/aws/node_roles.conf is absent — every converged "
                    "name is shared by every node of this project, which is "
                    "the single-role shape most projects want")
        return
    if state["manifest_error"]:
        report.fail("roles", "role manifest is not parseable",
                    state["manifest_error"],
                    "post_deploy refuses this file before it migrates, so "
                    "every node of this project is stuck until the line it "
                    "names is fixed")
        return
    if state["module_missing"]:
        report.info("roles", "framework predates node roles",
                    "the installed django-mojo has no mojo.deploy.node_role, "
                    "so this node converges every declared name — which is "
                    "what that framework version always did")
        return
    if state["probe_error"]:
        report.fail("roles", "this node's role could not be resolved",
                    state["probe_error"],
                    "post_deploy dies on the same condition — repair the "
                    f"sealed {ROLE_AUTHORITY} authority")
        return

    role = state["role"]
    if not role:
        report.fail(
            "roles", "node role is not declared anywhere",
            "the repo declares roles but nothing on this node says which one "
            "it plays, so post_deploy refuses to converge it",
            "label the node in ONE of: the sealed "
            f"{ROLE_AUTHORITY} authority (root writes it, or the "
            "next deploy seals it), a NODE_ROLE export in aws/post_deploy.sh, "
            f"or MOJO_NODE_ROLE in {proj}/var/bootstrap.conf")
        return

    report.info("roles", f"node role: {role}",
                f"declared by the {state['source']} authority")

    if state["source"] in ("env", "bootstrap"):
        report.warn(
            "roles", f"node role is not sealed yet ({state['source']})",
            f"the role comes from {'the shim environment' if state['source'] == 'env' else 'var/bootstrap.conf'}"
            ", which lives in the application-writable half of the node; the "
            "sealed authority is the one an application cannot influence",
            "the next `sudo bash aws/post_deploy.sh` promotes it into "
            f"{ROLE_AUTHORITY} automatically")
    if state["sealed"] and state["bootstrap"] and \
            state["sealed"] != state["bootstrap"]:
        report.warn(
            "roles", "bootstrap.conf disagrees with the sealed role",
            f"the sealed authority says {state['sealed']} and "
            f"var/bootstrap.conf says {state['bootstrap']} — the seal wins, so "
            "a re-labeling that only edited bootstrap.conf has not taken "
            "effect",
            f"re-seal the node (root writes {ROLE_AUTHORITY}) if "
            "the new value is the intended one")

    if role not in state["manifest"]:
        report.fail(
            "roles", f"role {role} is not declared in the manifest",
            f"{repo}/aws/node_roles.conf never names {role}, so post_deploy "
            "cannot decide which files this node owns and refuses the deploy",
            f"add `{role}` to aws/node_roles.conf (a bare role line is legal "
            "and owns nothing), or re-label the node")
        return
    report.passed("roles", f"{role} is declared in the manifest",
                  f"{repo}/aws/node_roles.conf places this node")

    shed = 0
    for kind, install_dir in sorted(ROLE_INSTALL_DIRS.items()):
        for name in state["foreign"][kind]:
            if not exists(run, f"{install_dir}/{name}"):
                continue
            detail = (f"{install_dir}/{name} belongs to another role, but it "
                      f"is installed on this {role} node — it is firing, "
                      "serving, or claiming a name that is not this node's job")
            if kind == "systemd":
                enabled = run(f"systemctl is-enabled {q(name)} 2>&1")[1]
                if enabled == "enabled":
                    detail += " (and it is enabled)"
            report.fail("roles", f"foreign {kind}/{name} installed", detail,
                        "sudo bash aws/post_deploy.sh removes it — one "
                        "surviving means no deploy has converged since this "
                        "node was labeled")
            shed += 1
    if not shed:
        report.passed("roles", "no foreign files installed",
                      f"nothing another role owns is installed on this {role} "
                      "node")

    rendered = {kind: list_names(run, f"{proj}/var/deploy/{kind}")
                for kind in ("cron.d", "systemd")}
    for entry in sorted(state["manifest"][role]):
        kind, _, name = entry.partition("/")
        if kind == "conf.d":
            where = f"{repo}/aws/nginx/conf.d/{name}"
            present = exists(run, where)
        else:
            if rendered[kind] is None:
                # The cron/systemd sections already report an unrendered node.
                continue
            where = f"{proj}/var/deploy/{kind}/{name}"
            present = name in rendered[kind]
        if not present:
            report.warn(
                "roles", f"{entry} is declared but not shipped",
                f"the manifest gives {entry} to {role}, but {where} does not "
                "exist — a typo in the manifest silently gives a name to a "
                "role that has no such file",
                "fix the name in aws/node_roles.conf, or ship the file")


# ---------------------------------------------------------------------------
# repo
# ---------------------------------------------------------------------------

def check_repo(report, run, proj, app_user):
    rc, out, err = run(f"git -C {q(proj)} rev-parse --is-inside-work-tree")
    if rc != 0 or out != "true":
        report.fail("repo", f"{proj} is not a git repo",
                    (err or out or "git failed").splitlines()[0],
                    "the deploy plane is git + post_deploy.sh — a node "
                    "without the repo cannot converge at all")
    else:
        st = stat_of(run, proj)
        if st is None:
            report.info("repo", "ownership", f"could not stat {proj}")
        elif st[0] != app_user:
            report.fail("repo", f"{proj} owned by {st[0]}",
                        f"the deploy engine runs git as {app_user}; a tree it "
                        "does not own breaks every fetch/reset",
                        f"chown -R {app_user} {proj} (var/ stays "
                        f"{app_user}:<web user>)")
        else:
            report.passed("repo", "ownership", f"{proj} owned by {app_user}")

        rc, head, _ = run(f"git -C {q(proj)} rev-parse HEAD")
        if rc != 0 or not head:
            report.info("repo", "HEAD", "could not resolve HEAD")
        else:
            report.info("repo", "HEAD", head[:12])
            rc, out, _ = run(
                f"git -C {q(proj)} ls-remote origin refs/heads/main",
                timeout=20)
            if rc == 0 and out:
                remote_sha, source = out.split()[0], "origin, live"
            else:
                # Network failure is context, not a node problem.
                report.info("repo", "origin unreachable",
                            "comparing against the last-fetched origin/main")
                rc, out, _ = run(f"git -C {q(proj)} rev-parse origin/main")
                remote_sha = out if rc == 0 and out else None
                source = "origin/main, last fetch"
            if remote_sha is None:
                report.info("repo", "origin/main", "no ref to compare against")
            elif remote_sha == head:
                report.passed("repo", "HEAD matches origin/main",
                              f"{head[:12]} ({source})")
            else:
                report.warn(
                    "repo", "HEAD differs from origin/main",
                    f"HEAD {head[:12]} vs main {remote_sha[:12]} ({source})",
                    "aws/update.sh --manual to converge — unless a pinned "
                    "fleet deploy is deliberately holding this sha")

        rc, out, _ = run(f"git -C {q(proj)} status --porcelain")
        if rc != 0:
            report.info("repo", "working tree", "git status failed")
        elif not out:
            report.passed("repo", "working tree", "clean")
        else:
            report.warn("repo", "working tree dirty",
                        f"{len(out.splitlines())} uncommitted path(s) — a "
                        "converged node never has local edits",
                        "commit upstream and redeploy, or git reset --hard "
                        "(the next deploy would anyway)")

    lock = f"{proj}/var/update.lock"
    if not exists(run, lock):
        report.info("repo", "var/update.lock",
                    "absent — no update has run on this box (or var/ was cleared)")
    else:
        rc, _, err = run(f"flock -n {q(lock)} true")
        if rc == 0:
            report.passed("repo", "update lock", "var/update.lock is free")
        elif rc == 127 or "not found" in err:
            report.info("repo", "update lock", "flock unavailable — cannot probe")
        else:
            report.warn("repo", "update in flight",
                        "var/update.lock is held — update.sh is running on "
                        "this box right now",
                        "wait it out; deploy mode queues up to 600s on this lock")

    if exists(run, f"{proj}/var/previous_sha") and \
            exists(run, f"{proj}/var/previous_framework"):
        detail = "var/previous_sha + var/previous_framework present"
        if VERBOSE:
            rc, out, _ = run(f"cat {q(proj)}/var/previous_sha")
            if rc == 0 and out:
                detail += f" (rollback target {out[:12]})"
        report.info("repo", "rollback state recorded", detail)
    else:
        report.info("repo", "no rollback state",
                    "var/previous_sha absent — no pinned deploy has run yet")


# ---------------------------------------------------------------------------
# framework
# ---------------------------------------------------------------------------

def check_framework(report, run, proj):
    rc, out, _ = run("python3 -m pip show django-mojo 2>/dev/null")
    if rc == 0:
        version = next((l.split(":", 1)[1].strip() for l in out.splitlines()
                        if l.startswith("Version:")), "?")
        report.info("framework", "django-mojo", f"version {version}")
    else:
        report.fail("framework", "django-mojo not installed",
                    "pip does not know the framework — nothing on this box "
                    "can serve",
                    "aws/update.sh --manual installs latest and converges")

    rc, out, err = run("python3 --version 2>&1")
    report.info("framework", "python", out or err or "python3 not found")

    rc, _, _ = run("python3 -m pip show django-nativemojo 2>/dev/null")
    if rc == 0:
        report.fail(
            "framework", "django-nativemojo installed",
            "the retired package is present alongside django-mojo — the two "
            "overlap files, so whichever installed last silently owns the "
            "mojo/ tree",
            "pip uninstall django-nativemojo, then reinstall django-mojo "
            "(aws/update.sh --manual) to repair any clobbered files")
    else:
        report.passed("framework", "django-nativemojo",
                      "retired package not installed")

    rc, out, err = run(
        f"cd {q(proj)} && python3 bin/manage.py migrate --check --noinput 2>&1",
        timeout=90)
    combined = out or err
    if rc == 0:
        report.passed("framework", "migrations", "no unapplied migrations")
    elif "Traceback" in combined:
        # The command crashed rather than reporting state — usually conf or DB
        # reachability from this user. That is context, not a schema verdict.
        last = next((l for l in reversed(combined.splitlines()) if l.strip()), "")
        error_class = last.split(":", 1)[0].strip()
        report.info("framework", "migrate --check could not run",
                    f"{error_class} — likely conf/DB not reachable from the "
                    "invoking user; not a migration verdict")
    else:
        report.fail("framework", "unapplied migrations",
                    "migrate --check exited non-zero — the schema is behind "
                    "the code on this box",
                    "deploy with --migrate (the canary), or on the node: "
                    "python3 bin/manage.py migrate_locked --noinput")


# ---------------------------------------------------------------------------
# cron
# ---------------------------------------------------------------------------

def check_cron(report, run, proj):
    src_dir = f"{proj}/var/deploy/cron.d"
    names = list_names(run, src_dir)
    if names is None:
        # Absent contract is INFO, never FAIL: a node that has not rendered
        # yet is behind, not broken — and grading every installed cron stale
        # against an empty set would be noise, so the sweep is skipped too.
        report.info("cron", "no rendered contract",
                    f"{src_dir} is absent — post_deploy has not rendered on "
                    "this framework version yet; run `sudo bash "
                    "aws/post_deploy.sh` to render and converge")
        return
    compare_set(report, "cron", run, src_dir, "/etc/cron.d", names,
                "sudo bash aws/post_deploy.sh (any deploy) converges cron.d")

    # Stale sweep, same rule as post_deploy.sh: a /etc/cron.d file that
    # mentions the project path is ours; if the rendered set no longer ships
    # that name, one surviving means post_deploy has not run since it was
    # retired.
    rc, out, _ = run(f"grep -lF -- {q(proj)} /etc/cron.d/* 2>/dev/null")
    if rc > 1 and not out:
        report.info("cron", "stale sweep skipped", "could not scan /etc/cron.d")
        return
    shipped = set(names)
    stale = [os.path.basename(p) for p in out.splitlines()
             if p.strip() and os.path.basename(p) not in shipped]
    for name in sorted(stale):
        report.fail(
            "cron", f"stale project cron: {name}",
            f"/etc/cron.d/{name} mentions {proj} but the rendered contract "
            "does not ship that name — post_deploy's retirement sweep has not "
            "run since it was retired, so it is still firing alongside its "
            "replacement",
            f"sudo bash aws/post_deploy.sh retires it (or sudo rm /etc/cron.d/{name})")
    if not stale:
        report.passed("cron", "no stale project crons",
                      "every /etc/cron.d file naming the project path is one "
                      "the rendered contract ships")


# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------

def check_systemd(report, run, proj, foreign_units=()):
    src_dir = f"{proj}/var/deploy/systemd"
    # A unit another role owns is not rendered here, so it cannot appear in
    # `units` — the filter matters for a node whose var/deploy predates its
    # label, where grading the leftover enabled/active would contradict the
    # roles section that is telling the operator to shed it.
    foreign_units = set(foreign_units)
    units = [u for u in (list_names(run, src_dir) or [])
             if u.endswith((".service", ".timer")) and u not in foreign_units]
    if not units:
        report.info("systemd", "no rendered contract",
                    f"no units under {src_dir} — post_deploy has not rendered "
                    "on this framework version yet; run `sudo bash "
                    "aws/post_deploy.sh` to render and converge")
    else:
        compare_set(report, "systemd", run, src_dir, "/etc/systemd/system",
                    units,
                    "sudo bash aws/post_deploy.sh installs units and "
                    "daemon-reloads")

    rc, out, err = run("systemctl is-active mojo-asgi.service 2>&1")
    state = out or err
    if rc == 127 or "not found" in state:
        report.info("systemd", "systemctl unavailable",
                    "not a systemd host — service state not audited")
        return
    if state == "active":
        report.passed("systemd", "mojo-asgi", "active")
    else:
        report.fail("systemd", f"mojo-asgi is {state or 'unknown'}",
                    "the app service is not running — nothing answers behind "
                    "nginx",
                    "journalctl -u mojo-asgi -n 50, then "
                    "sudo systemctl restart mojo-asgi")

    # A timer that is shipped but disabled does nothing while looking
    # installed — exactly how config-sync.timer sat unused for months.
    for timer in sorted(u for u in units if u.endswith(".timer")):
        for verb, want in (("is-enabled", "enabled"), ("is-active", "active")):
            rc, out, err = run(f"systemctl {verb} {q(timer)} 2>&1")
            state = out or err
            if state == want:
                report.passed("systemd", f"{timer} {want}",
                              "shipped timer is wired up")
            else:
                report.fail("systemd", f"{timer} not {want} ({state or '?'})",
                            "the rendered contract ships this timer; "
                            "present-but-idle means its job silently never runs",
                            f"sudo systemctl enable --now {timer}")


# ---------------------------------------------------------------------------
# MojoSec — metadata and bounded public status only; never read the credential
# ---------------------------------------------------------------------------

def _secure_metadata(run, path, wanted_mode, kind="file", sudo=""):
    flag = "-d" if kind == "directory" else "-f"
    rc, out, _ = run(
        f"{sudo}test ! -L {q(path)} && {sudo}test {flag} {q(path)} && "
        f"{sudo}stat -c '%U %G %a' {q(path)} 2>/dev/null")
    if rc != 0:
        return None
    parts = out.split()
    if len(parts) != 3:
        return None
    return parts, parts == ["root", "root", wanted_mode]


def _mojosec_python(sudo, arguments, safe_path=True):
    """Run the packaged root interpreter from a trusted cwd with safe-path mode."""
    flags = "-E -P" if safe_path else "-E"
    inner = f"cd / && exec /usr/bin/python3 {flags} {arguments}"
    return f"{sudo}/bin/sh -c {q(inner)}"


def _audit_mojosec_unit(report, run, sudo, mode="observe"):
    launcher = (
        "import importlib.util,os,stat,sys;"
        "s=importlib.util.find_spec('mojo');"
        "o=os.path.realpath(s.origin) if s and s.origin else '';"
        "d=os.path.dirname(o);p=os.path.dirname(d);"
        "trusted=p.startswith(('/usr/local/lib/','/usr/local/lib64/',"
        "'/usr/lib/','/usr/lib64/'));"
        "paths=(p,d,o);"
        "meta=all(os.stat(x).st_uid==0 and not(os.stat(x).st_mode&0o022) "
        "for x in paths);"
        "ok=(sys.version_info>=(3,11) and sys.flags.ignore_environment and "
        "getattr(sys.flags,'safe_path',False) and os.getcwd()=='/' and "
        "'' not in sys.path and '/' not in sys.path and trusted and meta and "
        "stat.S_ISREG(os.stat(o).st_mode));"
        "raise SystemExit(0 if ok else 1)"
    )
    if run(_mojosec_python(sudo, f"-c {q(launcher)}"))[0] == 0:
        report.passed("mojosec", "secure Python launcher",
                      "Python 3.11+ safe-path imports root-owned system packages")
    else:
        grade = report.info if mode == "off" else report.fail
        grade("mojosec", "secure Python launcher unavailable",
              "observe needs /usr/bin/python3 3.11+ and a root-owned Mojo package")

    exact = (
        "import os,stat;from mojo.deploy.mojosec import UNIT_TEXT;"
        "p='/etc/systemd/system/mojosec.service';"
        "fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW);s=os.fstat(fd);"
        "want=UNIT_TEXT.encode();got=os.read(fd,len(want)+1);os.close(fd);"
        "raise SystemExit(0 if stat.S_ISREG(s.st_mode) and s.st_uid==0 and "
        "s.st_gid==0 and s.st_size==len(want) and got==want else 1)"
    )
    if run(_mojosec_python(
            sudo, f"-c {q(exact)}", safe_path=(mode == "observe")))[0] == 0:
        report.passed("mojosec", "service unit bytes",
                      "installed unit matches the package exactly")
    else:
        report.fail("mojosec", "service unit byte drift",
                    "installed root unit differs from the package contract")

    properties = (
        "User", "Group", "UMask", "WorkingDirectory", "Environment",
        "FragmentPath", "DropInPaths",
        "NoNewPrivileges", "PrivateTmp", "ProtectHome", "ProtectSystem",
        "ProtectKernelTunables", "ProtectKernelModules", "ProtectKernelLogs",
        "ProtectControlGroups", "RestrictSUIDSGID", "LockPersonality",
        "RestrictRealtime", "RestrictNamespaces", "RestrictAddressFamilies",
        "CapabilityBoundingSet", "AmbientCapabilities", "ReadWritePaths",
        "BindReadOnlyPaths",
    )
    command = "systemctl show mojosec.service " + " ".join(
        f"--property={name}" for name in properties)
    rc, out, _ = run(command)
    if rc != 0 or len(out) > 16384:
        report.fail("mojosec", "effective systemd sandbox unavailable",
                    "bounded systemctl show failed")
        return
    found = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            found[key] = value.strip()
    scalar = {
        "User": "root", "Group": "root", "UMask": "0077",
        "WorkingDirectory": "/",
        "FragmentPath": "/etc/systemd/system/mojosec.service",
        "DropInPaths": "", "NoNewPrivileges": "yes", "PrivateTmp": "yes",
        "ProtectHome": "tmpfs", "ProtectSystem": "strict",
        "ProtectKernelTunables": "yes", "ProtectKernelModules": "yes",
        "ProtectKernelLogs": "yes", "ProtectControlGroups": "yes",
        "RestrictSUIDSGID": "yes", "LockPersonality": "yes",
        "RestrictRealtime": "yes", "RestrictNamespaces": "yes",
        "AmbientCapabilities": "",
    }
    drift = [f"{key}={found.get(key, '<missing>')}" for key, value in scalar.items()
             if found.get(key) != value]
    if set(found.get("RestrictAddressFamilies", "").split()) != {
            "AF_UNIX", "AF_INET", "AF_INET6"}:
        drift.append("RestrictAddressFamilies=" + found.get(
            "RestrictAddressFamilies", "<missing>"))
    if set(found.get("CapabilityBoundingSet", "").lower().split()) != {
            "cap_dac_read_search"}:
        drift.append("CapabilityBoundingSet=" + found.get(
            "CapabilityBoundingSet", "<missing>"))
    if set(found.get("ReadWritePaths", "").split()) != {
            "/var/lib/mojosec", "/run/mojosec"}:
        drift.append("ReadWritePaths=" + found.get("ReadWritePaths", "<missing>"))
    from mojo.deploy.mojosec import HOME_BIND_PATHS
    effective_binds = {
        item.lstrip("-").split(":", 1)[0]
        for item in found.get("BindReadOnlyPaths", "").split()
    }
    if effective_binds != set(HOME_BIND_PATHS):
        drift.append("BindReadOnlyPaths=" + found.get("BindReadOnlyPaths", "<missing>"))
    try:
        environment = set(shlex.split(found.get("Environment", "")))
    except ValueError:
        environment = set()
    required_environment = {
        "PYTHONUNBUFFERED=1", "PYTHONHOME=", "PYTHONPATH=",
        "PYTHONUSERBASE=", "PYTHONSTARTUP=", "PYTHONINSPECT=",
    }
    if not required_environment.issubset(environment):
        drift.append("Environment lacks cleared Python variables")
    if drift:
        report.fail("mojosec", "effective systemd sandbox drift",
                    ", ".join(drift[:12]))
    else:
        report.passed("mojosec", "effective systemd sandbox",
                      "no drop-ins; exact identity, filesystem, capability, and namespace limits")

    if mode == "observe" and not drift:
        quoted = " ".join(shlex.quote(path) for path in HOME_BIND_PATHS)
        namespace = (
            "p=$(systemctl show --property=MainPID --value mojosec.service); "
            "test \"$p\" -gt 1 && nsenter -t \"$p\" -m -- /bin/sh -c " +
            q("for x in " + quoted + "; do test -e \"$x\" || exit 1; done; "
              "test ! -e /root/.cache")
        )
        if run(f"{sudo}/bin/sh -c {q(namespace)}")[0] == 0:
            report.passed("mojosec", "home mount namespace",
                          "exact persistence binds are visible and unrelated root home stays hidden")
        else:
            report.fail("mojosec", "home mount namespace drift",
                        "effective MojoSec namespace did not preserve exact home isolation")

    analyze_rc, analyze_out, _ = run(
        "systemd-analyze security --no-pager mojosec.service 2>&1")
    if analyze_rc == 0 and len(analyze_out) <= 65536:
        report.passed("mojosec", "systemd security analysis",
                      "systemd-analyze accepted the effective unit")
    else:
        report.fail("mojosec", "systemd security analysis failed",
                    "systemd-analyze could not evaluate the effective unit")


def _audit_exact_mojosec_nginx_assets(report, run, sudo, proxy_cidrs):
    render_check = (
        "import json,pathlib,sys;"
        "from mojo.deploy.mojosec import LOGROTATE_TEXT;"
        "from mojo.deploy.mojosec_nginx import "
        "render_http_log,render_receiver_location;"
        "cidrs=json.loads(sys.argv[1]);"
        "pairs=((pathlib.Path('/etc/nginx/conf.d/00_mojosec.conf'),"
        "render_http_log('/var/log/nginx/mojosec.json.log',cidrs)),"
        "(pathlib.Path('/etc/nginx/snippets/mojosec_receiver.conf'),"
        "render_receiver_location()+'\\n'),"
        "(pathlib.Path('/etc/logrotate.d/mojosec'),LOGROTATE_TEXT));"
        "raise SystemExit(0 if all(p.read_text()==want for p,want in pairs) else 1)"
    )
    cidr_json = json.dumps(proxy_cidrs, separators=(",", ":"))
    if run(_mojosec_python(
            sudo, f"-c {q(render_check)} {q(cidr_json)}"))[0] == 0:
        report.passed("mojosec", "generated nginx assets",
                      "installed bytes match the package render exactly")
    else:
        report.fail("mojosec", "generated nginx asset drift",
                    "fragment, receiver, or rotation bytes differ from package")


def _protected_mojosec_log_path(log_path):
    return log_path == "/var/log/nginx/mojosec.json.log"


def _valid_local_only_status(status):
    counts = (
        status.get("local_only_observed"),
        status.get("local_only_diagnostic_delivered"),
        status.get("local_only_suppressed"),
    ) if isinstance(status, dict) else ()
    diagnostic = status.get("local_only_diagnostic") if isinstance(status, dict) else None
    last_seen = status.get("local_only_last_seen") if isinstance(status, dict) else None
    last_seen_ok = last_seen is None
    if isinstance(last_seen, str):
        try:
            last_seen_ok = datetime.datetime.fromisoformat(
                last_seen.replace("Z", "+00:00")).tzinfo is not None
        except ValueError:
            last_seen_ok = False
    until_ok = False
    if isinstance(diagnostic, dict):
        until = diagnostic.get("until")
        if until == "":
            until_ok = diagnostic.get("active") is False
        elif isinstance(until, str):
            try:
                until_ok = datetime.datetime.fromisoformat(
                    until.replace("Z", "+00:00")).tzinfo is not None
            except ValueError:
                until_ok = False
    return bool(
        len(counts) == 3 and
        all(isinstance(value, int) and not isinstance(value, bool) and
            0 <= value <= 2 ** 63 - 1
            for value in counts) and
        last_seen_ok and
        isinstance(diagnostic, dict) and
        set(diagnostic) == {"active", "until", "error"} and
        isinstance(diagnostic.get("active"), bool) and
        until_ok and diagnostic.get("error") in {
            "", "sidecar_unreadable", "sidecar_unsafe", "sidecar_too_large",
            "sidecar_malformed", "sidecar_until_too_late",
        } and
        (not diagnostic.get("active") or diagnostic.get("error") == "") and
        (diagnostic.get("error") == "" or diagnostic.get("until") == "")
    )


def _valid_provenance_status(process):
    if not isinstance(process, dict):
        return False
    audit_health = process.get("audit_health")
    integer_fields = (process.get("process_nodes"),
                      process.get("pending_firewall"),
                      process.get("engine_anchors"))
    return bool(
        isinstance(audit_health, dict) and audit_health.get("healthy") is True and
        audit_health.get("lost") == 0 and audit_health.get("backlog_limit", 0) >= 8192 and
        all(isinstance(value, int) and not isinstance(value, bool)
            for value in integer_fields) and
        0 <= process["process_nodes"] <= 131072 and
        0 <= process["pending_firewall"] <= 4096 and
        process["engine_anchors"] >= 1)


def check_mojosec(report, run, mode, sudo, expected_sensor_id=""):
    active = run("systemctl is-active mojosec.service 2>&1")[1]
    enabled = run("systemctl is-enabled mojosec.service 2>&1")[1]
    if mode == "auto":
        mode = "observe" if enabled == "enabled" else "off"
        report.info("mojosec", f"auto-derived mode: {mode}",
                    "derived from systemctl is-enabled; pass --mojosec-mode "
                    "to audit a not-yet-converged desired state")
    paths = (
        ("runtime config", "/etc/mojosec/config.json", "600", "file"),
        ("enrollment", "/etc/mojosec/enrollment.json", "600", "file"),
        ("credential", "/etc/mojosec/credential", "600", "file"),
        ("private state", "/var/lib/mojosec", "700", "directory"),
        ("service unit", "/etc/systemd/system/mojosec.service", "644", "file"),
    )
    present = {}
    for name, path, wanted, kind in paths:
        result = _secure_metadata(run, path, wanted, kind=kind, sudo=sudo)
        present[name] = result is not None
        if result is None:
            status = report.info if mode == "off" else report.fail
            status("mojosec", f"{name} absent or unsafe", path,
                   *([] if mode == "off" else ["sudo bash aws/post_deploy.sh"] ))
        elif result[1]:
            report.passed("mojosec", name, f"{path} is root:root {wanted}")
        else:
            report.fail("mojosec", f"{name} metadata drift",
                        f"{path} is {' '.join(result[0])}; want root root {wanted}",
                        "sudo bash aws/post_deploy.sh")

    if present.get("service unit"):
        _audit_mojosec_unit(report, run, sudo, mode)

    if mode == "observe" and present.get("runtime config"):
        command = _mojosec_python(
            sudo, "-m mojo.mojosec --config /etc/mojosec/config.json check")
        rc, out, err = run(command)
        if rc == 0:
            report.passed(
                "mojosec", "RPM ownership capability",
                "isolated system binding, transaction, installed-file index, and DB are ready")
        else:
            detail = (err or out or "MojoSec readiness check returned no detail").splitlines()[0]
            report.fail(
                "mojosec", "RPM ownership capability unavailable", detail[:256],
                "install a compatible system python3-rpm binding and rerun deployment")

    provenance_assets = (
        ("Audit rollback state", "/etc/mojosec/audit-state.json", "600"),
        ("Audit managed policy", "/etc/audit/rules.d/70-mojosec.rules", "600"),
        ("Audit health unit", "/etc/systemd/system/mojosec-audit-health.service", "644"),
        ("Audit health timer", "/etc/systemd/system/mojosec-audit-health.timer", "644"),
        ("Audit stable helper", "/usr/local/lib/mojosec/mojosec_audit.py", "755"),
        ("firewall broker", "/usr/local/sbin/mojo-firewall-broker", "755"),
        ("firewall broker sudoers", "/etc/sudoers.d/70-mojo-firewall-broker", "440"),
    )
    publish_assets = (
        ("publish broker", "/usr/local/sbin/mojo-publish-broker", "755"),
        ("publish broker sudoers", "/etc/sudoers.d/71-mojo-publish-broker", "440"),
    )
    if mode == "observe":
        content_projection = (
            "import json;"
            f"p=json.load(open('{MOJOSEC_DEPLOY_STATE_PATH}'));"
            "print(json.dumps({'roots':p.get('content_roots') or []},"
            "separators=(',',':')))"
        )
        rc_content, content_text, _ = run(f"{sudo}python3 -c {q(content_projection)}")
        try:
            content_roots = (json.loads(content_text).get("roots")
                             if rc_content == 0 else [])
        except json.JSONDecodeError:
            content_roots = []
        # The publish broker exists only on a content node, so audit its assets
        # exactly where they are supposed to exist — and require their ABSENCE
        # where they are not, so a retired content node cannot keep a live sudo
        # grant to a root broker.
        expected_assets = provenance_assets + (
            publish_assets if content_roots else ())
        for name, path, wanted in expected_assets:
            found = _secure_metadata(run, path, wanted, sudo=sudo)
            if found is not None and found[1]:
                report.passed("mojosec", name, f"{path} is root:root {wanted}")
            else:
                report.fail("mojosec", f"{name} absent or unsafe", path,
                            "sudo bash aws/post_deploy.sh")
        if not content_roots:
            stale = [path for _name, path, _wanted in publish_assets
                     if run(f"{sudo}test -e {q(path)}")[0] == 0]
            if stale:
                report.fail(
                    "mojosec", "publish broker present without content enrollment",
                    ", ".join(stale),
                    "sudo bash aws/post_deploy.sh")
            else:
                report.passed("mojosec", "publish broker absent",
                              "no content roots are enrolled and no grant exists")
        rc, audit_status, _ = run(f"{sudo}/sbin/auditctl -s")
        rc_rules, audit_rules, _ = run(f"{sudo}/sbin/auditctl -l")
        projection = (
            "import json;"
            f"p=json.load(open('{MOJOSEC_DEPLOY_STATE_PATH}'));"
            "print(json.dumps({'generation':p.get('audit_generation'),"
            "'rules':p.get('audit_rules_sha256')},separators=(',',':')))"
        )
        rc_expected, expected_text, _ = run(f"{sudo}python3 -c {q(projection)}")
        try:
            expected_audit = json.loads(expected_text) if rc_expected == 0 else {}
        except json.JSONDecodeError:
            expected_audit = {}
        rc_policy, policy_digest, _ = run(
            f"{sudo}sha256sum /etc/audit/rules.d/70-mojosec.rules")
        policy_parts = policy_digest.split(None, 1) if rc_policy == 0 else []
        policy_digest = policy_parts[0] if policy_parts else ""
        generated_exact = run(f"{sudo}/sbin/augenrules --check")[0] == 0
        active_digest = hashlib.sha256(audit_rules.encode()).hexdigest()
        required_status = ("enabled 1", "failure 1", "rate_limit 0", "backlog_limit 8192")
        if (rc == 0 and rc_rules == 0 and
                all(value in audit_status for value in required_status) and
                "lost 0" in audit_status and
                "task,never" not in audit_rules and "never,task" not in audit_rules and
                generated_exact and
                policy_digest == expected_audit.get("generation") and
                active_digest == expected_audit.get("rules") and
                all(key in audit_rules for key in (
                    "mojosec-root-exec", "mojosec-app-exec", "mojosec-sudo"))):
            report.passed("mojosec", "active Audit provenance",
                          "managed selective rules are loaded with zero loss")
        else:
            report.fail("mojosec", "active Audit provenance unhealthy",
                        "active rules/status differ, are lossy, or still disable task auditing")
        sidecar = _secure_metadata(
            run, "/run/mojosec/audit-health.json", "600", sudo=sudo)
        if sidecar is not None and sidecar[1]:
            report.passed("mojosec", "Audit health sidecar",
                          "root-only constrained live health is present")
        else:
            report.fail("mojosec", "Audit health sidecar absent or unsafe",
                        "suppression is disabled without a fresh root-owned sidecar")
        timer = run("systemctl is-active mojosec-audit-health.timer 2>&1")[1]
        capabilities = run(
            "systemctl show mojosec-audit-health.service "
            "-p CapabilityBoundingSet -p AmbientCapabilities --value 2>&1")[1]
        if timer == "active" and capabilities.count("CAP_AUDIT_CONTROL") >= 1:
            report.passed("mojosec", "Audit health publisher",
                          "timer active with constrained Audit control capability")
        else:
            report.fail("mojosec", "Audit health publisher unavailable",
                        "timer/capability drift disables suppression")
        legacy = run(
            f"{sudo}grep -R -E 'NOPASSWD:.*(iptables|ipset)' /etc/sudoers /etc/sudoers.d "
            "2>/dev/null")[1]
        if legacy:
            report.warn("mojosec", "legacy direct firewall grants retained",
                        "transitional rollback authorization is present; direct calls remain ordinary")

    desired_rc = run(
        "test ! -L /opt/api/var/mojosec.json && "
        "test -f /opt/api/var/mojosec.json && "
        "test $(stat -c %s /opt/api/var/mojosec.json) -le 262144")[0]
    if desired_rc == 0:
        report.passed("mojosec", "desired policy source",
                      "/opt/api/var/mojosec.json is regular, nonsecret, and bounded")
    elif mode == "observe":
        report.fail("mojosec", "desired policy source absent or unsafe",
                    "observe requires the fleet-managed nonsecret desired policy")
    else:
        report.info("mojosec", "desired policy source absent",
                    "expected on a legacy node while mode is off")

    expected = _secure_metadata(
        run, "/etc/mojosec/expected_changes.json", "600", sudo=sudo)
    if expected is None:
        report.info("mojosec", "expected-change manifest absent",
                    "FIM still reports every change; no deployment annotations configured")
    elif expected[1]:
        report.passed("mojosec", "expected-change manifest",
                      "root-owned 0600 annotation input")
    else:
        report.fail("mojosec", "expected-change manifest metadata drift",
                    f"got {' '.join(expected[0])}; want root root 600")

    if mode == "observe":
        if active == "active" and enabled == "enabled":
            report.passed("mojosec", "observe lifecycle", "service is enabled and active")
        else:
            report.fail("mojosec", "observe lifecycle drift",
                        f"service is active={active or '?'} enabled={enabled or '?'}",
                        "cd / && sudo /usr/bin/python3 -E -P -m "
                        "mojo.deploy.mojosec converge --mode observe")
    elif active in ("inactive", "failed", "unknown", "") and enabled != "enabled":
        report.passed("mojosec", "off lifecycle", "service is disabled and not active")
    else:
        report.fail("mojosec", "off lifecycle drift",
                    f"service is active={active or '?'} enabled={enabled or '?'}",
                    "cd / && sudo /usr/bin/python3 -E -P -m "
                    "mojo.deploy.mojosec converge --mode off")

    status_meta = _secure_metadata(run, "/run/mojosec/status.json", "640", sudo=sudo)
    if mode == "off":
        report.info("mojosec", "status not graded while off",
                    "a preserved last status is evidence, not an active-health signal")
    elif status_meta is None:
        report.fail("mojosec", "public status absent or unsafe",
                    "/run/mojosec/status.json is not a root-owned regular file",
                    "journalctl -u mojosec -n 50")
    elif status_meta is not None and status_meta[1]:
        # Read one bounded status projection with sudo. The credential,
        # canonical config, SQLite spool and FIM manifest are never opened.
        projection = (
            "import json,os;path='/run/mojosec/status.json';"
            "assert os.path.getsize(path)<=32768;"
            "p=json.load(open(path));"
            "keys=('schema','version','sensor_id','state','updated_at',"
            "'spooled_events','delivery_accepted','collectors','delivery','config',"
            "'integrity','local_only_observed','local_only_diagnostic_delivered',"
            "'local_only_suppressed','local_only_last_seen','local_only_diagnostic',"
            "'provenance');"
            "print(json.dumps({k:p.get(k) for k in keys},separators=(',',':')))"
        )
        command = f"{sudo}python3 -c {q(projection)}"
        rc, out, _ = run(command)
        try:
            status = json.loads(out) if rc == 0 else None
        except (TypeError, json.JSONDecodeError):
            status = None
        if (not isinstance(status, dict) or status.get("schema") != "mojosec.status" or
                status.get("version") != 1 or status.get("state") != "running" or
                not _valid_local_only_status(status)):
            report.fail("mojosec", "public status malformed",
                        "bounded status JSON is absent, stale-schema, or not running")
        else:
            report.passed("mojosec", "public status", "schema v1, running")
            sensor = status.get("sensor_id", "")
            if expected_sensor_id and sensor != expected_sensor_id:
                report.fail("mojosec", "sensor identity mismatch",
                            f"status reports {sensor!r}; expected {expected_sensor_id!r}")
            elif sensor:
                report.passed("mojosec", "sensor identity", sensor)
            try:
                updated = datetime.datetime.fromisoformat(
                    str(status.get("updated_at", "")).replace("Z", "+00:00"))
                age = (datetime.datetime.now(datetime.timezone.utc) - updated).total_seconds()
            except (TypeError, ValueError):
                age = 10 ** 9
            if 0 <= age <= 600:
                report.passed("mojosec", "status freshness", f"updated {int(age)}s ago")
            else:
                report.fail("mojosec", "status stale", f"last update age is {int(age)}s")
            collectors = status.get("collectors") or {}
            unhealthy = [name for name in ("journal", "nginx")
                         if not isinstance(collectors.get(name), dict) or
                         collectors[name].get("ok") is not True]
            if unhealthy:
                report.fail("mojosec", "collector health",
                            "missing or unhealthy core collectors: " + ", ".join(unhealthy))
            else:
                report.passed("mojosec", "collector health", "journal and nginx are healthy")
            process = status.get("provenance")
            if isinstance(process, dict):
                if _valid_provenance_status(process):
                    report.passed("mojosec", "provenance health",
                                  "Audit epoch, bounded graph, and post-cutover engine are healthy")
                else:
                    report.fail("mojosec", "provenance health unhealthy",
                                "suppression is disabled until the current Audit epoch is healthy")
            else:
                report.fail("mojosec", "provenance health absent or malformed",
                            "post-cutover status must report Audit health, graph bounds, "
                            "and at least one live engine anchor")
            integrity = status.get("integrity")
            if integrity is not None:
                identity = integrity.get("identity") if isinstance(integrity, dict) else None
                tiers = integrity.get("tiers") if isinstance(integrity, dict) else None
                healthy = (
                    isinstance(identity, dict) and identity.get("name") and
                    len(str(identity.get("digest", ""))) == 64 and
                    integrity.get("active") is True and
                    integrity.get("digest_drift") is False and
                    # Every profile carries the three host tiers; a content
                    # profile carries a fourth. Requiring exact equality would
                    # report a fully healthy content node as unhealthy, so
                    # require the host floor and hold EVERY reported tier —
                    # including content — to initialized and complete.
                    isinstance(tiers, dict) and
                    {"fast", "slow", "rpm"} <= set(tiers) and len(tiers) <= 8 and
                    all(isinstance(item, dict) and item.get("initialized") and
                        item.get("last_complete") for item in tiers.values())
                )
                if healthy:
                    report.passed(
                        "mojosec", "integrity profile",
                        f"{identity['name']} v{identity.get('version')} "
                        f"{identity['digest'][:12]} ({len(tiers)} tiers)")
                else:
                    report.fail(
                        "mojosec", "integrity profile unhealthy",
                        "profile digest, baseline, or per-tier health is incomplete "
                        "(fast/slow/RPM are required; every reported tier must be "
                        "initialized and complete)")
                content = tiers.get("content") if isinstance(tiers, dict) else None
                if isinstance(content, dict):
                    # Overflow does not degrade quietly — it stops the tier —
                    # so warn while there is still room to add an exclude or
                    # shorten retention, not after the scan has already failed.
                    entries = content.get("entries") or 0
                    ceiling = (content.get("bounds") or {}).get("max_entries") or 0
                    if ceiling and entries >= ceiling * 0.8:
                        report.warn(
                            "mojosec", "content tier approaching its entry bound",
                            f"{entries} of {ceiling} entries; prune retained "
                            "revisions or exclude generated assets before the "
                            "tier overflows")
                    elif ceiling:
                        report.passed(
                            "mojosec", "content tier capacity",
                            f"{entries} of {ceiling} entries")
            spooled = status.get("spooled_events")
            if isinstance(spooled, int) and 0 <= spooled <= 10000:
                report.passed("mojosec", "spool backlog", f"{spooled} pending event(s)")
            else:
                report.warn("mojosec", "spool backlog",
                            f"unexpected or high pending count: {spooled!r}")
            delivery = status.get("delivery") or {}
            if delivery.get("error"):
                report.warn("mojosec", "delivery retrying", str(delivery["error"])[:200])
            elif int(status.get("delivery_accepted") or 0) > 0:
                report.passed("mojosec", "authenticated delivery",
                              f"{status['delivery_accepted']} accepted event(s)")
            else:
                report.info("mojosec", "authenticated delivery unproven",
                            "canary must generate and confirm one centrally accepted event")

            provenance = status.get("config") or {}
            hashes_ok = all(len(str(provenance.get(key, ""))) == 64
                            for key in ("source_sha256", "effective_sha256"))
            if hashes_ok and provenance.get("canonical_revision"):
                report.passed("mojosec", "config provenance",
                              f"source revision {provenance.get('source_revision') or '(none)'}")
            else:
                report.fail("mojosec", "config provenance missing",
                            "status lacks desired/effective configuration hashes")
            if (provenance.get("deployment_mode") == mode and
                    provenance.get("deployment_criticality") in
                    ("best_effort", "required")):
                report.passed("mojosec", "persistent lifecycle source",
                              f"root enrollment persists {mode}/"
                              f"{provenance['deployment_criticality']}")
            else:
                report.fail("mojosec", "persistent lifecycle source drift",
                            "runtime status does not match root-enrolled mode/criticality")

            nginx_rc, nginx_text, nginx_err = run(f"{sudo}nginx -T 2>&1")
            active_nginx = nginx_text or nginx_err
            log_path = provenance.get("nginx_log_path", "")
            required = (
                "log_format mojosec_v1 escape=json",
                f"access_log {log_path} mojosec_v1;",
            )
            missing = [item for item in required if not item or item not in active_nginx]
            route_counts = []
            for route in ("/api/incident/mojosec/batch",
                          "/api/incident/mojosec/batch/"):
                bodies = re.findall(
                    r"location\s*=\s*" + re.escape(route) + r"\s*\{([^{}]*)\}",
                    active_nginx)
                route_counts.append(len(bodies))
                if not bodies or any(
                        "client_max_body_size 512k;" not in body for body in bodies):
                    missing.append(f"512k cap inside every exact {route} block")
            if route_counts[0] != route_counts[1]:
                missing.append("matching slash/unslashed receiver blocks")
            missing.extend(
                f"set_real_ip_from {network};"
                for network in provenance.get("trusted_proxy_cidrs", [])
                if f"set_real_ip_from {network};" not in active_nginx)
            if nginx_rc == 0 and not missing:
                report.passed("mojosec", "active nginx contract",
                              "protected JSON evidence, exact receiver cap, and proxy boundary active")
            else:
                report.fail("mojosec", "active nginx contract drift",
                            "missing: " + ", ".join(missing[:8]))
            if provenance.get("nginx_plane") == "standard":
                for asset_name, asset_path in (
                        ("nginx log fragment", "/etc/nginx/conf.d/00_mojosec.conf"),
                        ("nginx receiver snippet",
                         "/etc/nginx/snippets/mojosec_receiver.conf"),
                        ("log rotation", "/etc/logrotate.d/mojosec")):
                    asset = _secure_metadata(run, asset_path, "644", sudo=sudo)
                    if asset is not None and asset[1]:
                        report.passed("mojosec", asset_name,
                                      f"{asset_path} is root:root 0644")
                    else:
                        report.fail("mojosec", f"{asset_name} metadata drift",
                                    f"{asset_path} must be root:root 0644")
                _audit_exact_mojosec_nginx_assets(
                    report, run, sudo, provenance.get("trusted_proxy_cidrs", []))
                log_meta = _secure_metadata(
                    run, "/var/log/nginx/mojosec.json.log", "600", sudo=sudo)
                if log_meta is not None and log_meta[1]:
                    report.passed("mojosec", "security log metadata",
                                  "dedicated log is root:root 0600")
                else:
                    report.fail("mojosec", "security log metadata drift",
                                "standard dedicated log must be root:root 0600")
                archive_check = (
                    "import glob,os,stat;"
                    "ps=glob.glob('/var/log/nginx/mojosec.json.log.*');"
                    "ok=len(ps)<=32 and all(stat.S_ISREG(os.lstat(p).st_mode) and "
                    "os.lstat(p).st_uid==0 and os.lstat(p).st_gid==0 and "
                    "not(os.lstat(p).st_mode&0o077) for p in ps);"
                    "raise SystemExit(0 if ok else 1)"
                )
                if run(_mojosec_python(
                        sudo, f"-c {q(archive_check)}"))[0] == 0:
                    report.passed("mojosec", "security log archives",
                                  "rotated evidence is bounded and root-only")
                else:
                    report.fail("mojosec", "security log archive metadata drift",
                                "rotated evidence must be regular root:root and non-world-accessible")
                rotation = run(
                    f"{sudo}grep -F 'maxsize 50M' /etc/logrotate.d/mojosec >/dev/null && "
                    f"{sudo}grep -F 'copytruncate' /etc/logrotate.d/mojosec >/dev/null && "
                    f"{sudo}grep -F 'su root root' /etc/logrotate.d/mojosec >/dev/null")[0]
                if rotation == 0:
                    report.passed("mojosec", "bounded log retention",
                                  "50M maxsize and root-owned copytruncate active")
                else:
                    report.fail("mojosec", "bounded log retention drift",
                                "/etc/logrotate.d/mojosec lacks size/metadata contract")
            elif provenance.get("nginx_plane") == "edge":
                log_meta = _secure_metadata(
                    run, "/var/log/nginx/mojosec.json.log", "600", sudo=sudo)
                rotation_meta = _secure_metadata(
                    run, "/etc/logrotate.d/mojosec", "644", sudo=sudo)
                if (_protected_mojosec_log_path(log_path) and
                        log_meta is not None and log_meta[1] and
                        rotation_meta is not None and rotation_meta[1]):
                    report.passed("mojosec", "Edge security log metadata",
                                  "root master-opened log is root:root 0600 with rotation")
                else:
                    report.fail("mojosec", "Edge security log unsafe",
                                "Edge evidence must use the root-owned log and rotation contract")
    else:
        report.fail("mojosec", "public status metadata drift",
                    "/run/mojosec/status.json must be root:root 0640")


# ---------------------------------------------------------------------------
# nginx
# ---------------------------------------------------------------------------

def check_nginx_runtime(report, run, sudo, web_user):
    if not sudo and os.geteuid() != 0:
        report.info("nginx", "runtime spill contract needs root",
                    "rerun with passwordless sudo to inspect private worker paths")
        return
    command = (f"{sudo}/usr/bin/python3 -E -P -m mojo.deploy.nginx_runtime "
               f"audit --web-user {q(web_user)}")
    rc, out, err = run(command, timeout=60)
    if rc == 0:
        report.passed(
            "nginx", "private runtime spill contract",
            "all five active temp paths have exact metadata and pass the worker write probe")
    else:
        detail = (err or out or "runtime audit returned no detail").splitlines()[0]
        report.fail(
            "nginx", "private runtime spill contract drift", detail,
            "run the normal automated deployment; its render step repairs this before reload")


def check_nginx(report, run, repo, sudo, probe_url, retired_confd,
                web_user="www", foreign_confd=()):
    check_nginx_runtime(report, run, sudo, web_user)
    # Repo-owned nginx files: the three top-level files, plus every conf.d
    # vhost the repo ships (post_deploy converges exactly that set; other
    # public vhosts on a node — single-node domains — stay node-managed and
    # are deliberately not audited here). Deriving the conf.d list from the
    # repo also surfaces the shipped-but-never-installed class of bug.
    pairs = [
        ("nginx.conf", "/etc/nginx/nginx.conf"),
        ("asgi.inc", "/etc/nginx/asgi.inc"),
        ("django.inc", "/etc/nginx/django.inc"),
    ]
    # A vhost aws/node_roles.conf gives to ANOTHER role is deliberately not on
    # this node; grading it "not installed" would turn a correct multi-role
    # convergence into a page of FAILs.
    shipped_confd = [n for n in (list_names(run, f"{repo}/aws/nginx/conf.d") or [])
                     if n.endswith(".conf") and ".example" not in n
                     and n not in set(foreign_confd)]
    if not shipped_confd:
        report.info("nginx", "conf.d contract missing",
                    f"cannot list {repo}/aws/nginx/conf.d")
    for name in sorted(shipped_confd):
        pairs.append((f"conf.d/{name}", f"/etc/nginx/conf.d/{name}"))
    for rel, dst in pairs:
        state = compare(run, f"{repo}/aws/nginx/{rel}", dst)
        if rel == "django.inc" and state == "DIFF":
            # Observe mode appends one exact root-owned include marker after
            # post_deploy copies the repo file. Compare the underlying graph
            # after removing only those two package-owned lines.
            source = f"{repo}/aws/nginx/{rel}"
            code = (
                "import pathlib,sys;"
                "s=pathlib.Path(sys.argv[1]).read_bytes();"
                "d=pathlib.Path(sys.argv[2]).read_text();"
                "drop={'# MojoSec exact receiver cap',"
                "'include /etc/nginx/snippets/mojosec_receiver.conf;'};"
                "n=('\\n'.join(x for x in d.splitlines() if x.strip() not in drop)"
                ".rstrip()+'\\n').encode();"
                "raise SystemExit(0 if n==s else 1)"
            )
            if run(f"python3 -c {q(code)} {q(source)} {q(dst)}")[0] == 0:
                state = "SAME"
        if state == "SAME":
            report.passed("nginx", rel, f"{dst} matches the repo")
        elif state in ("NODST", "DIFF"):
            why = "is not installed" if state == "NODST" else "drifted from the repo copy"
            report.fail("nginx", f"{rel} {why}",
                        f"{dst} — this file is repo-owned and converged on "
                        "every deploy; the node copy must be byte-identical",
                        "sudo bash aws/post_deploy.sh reinstalls it (never "
                        "hand-edit these on nodes)")
        else:
            report.info("nginx", f"{rel}: compare failed",
                        f"could not compare against {dst}")

    rc, out, err = run(f"{sudo}nginx -t 2>&1")
    combined = out or err
    if rc == 127 or "not found" in combined:
        report.info("nginx", "nginx unavailable", "nginx binary not on PATH")
    elif rc == 0:
        report.passed("nginx", "nginx -t", "configuration test passed")
    elif "ermission denied" in combined and not sudo:
        report.info("nginx", "nginx -t needs root",
                    "cannot read the TLS keys as this user — rerun as root "
                    "(or a passwordless-sudo user) to audit the config test")
    else:
        last = combined.splitlines()[-1] if combined else "no output"
        report.fail("nginx", "nginx -t failed", last,
                    "fix before the next deploy — post_deploy aborts on a "
                    "failed test (nginx keeps serving the old config meanwhile)")

    rc, out, err = run(
        f"curl -fsS --max-time 3 {q(probe_url)} 2>&1",
        timeout=10)
    if rc != 0:
        first = (out or err or "no output").splitlines()[0]
        report.fail("nginx", "probe URL not answering",
                    f"GET {probe_url} failed ({first})",
                    "check mojo-asgi and whichever vhost serves the probe — "
                    "the deploy plane's health gates (post_deploy.sh, "
                    "sanity_check) use this exact URL")
    elif '"status": true' in out or '"status":true' in out:
        report.passed("nginx", "probe URL",
                      f"GET {probe_url} → 200, envelope status true")
    else:
        report.fail("nginx", "probe answered without status true",
                    f"body starts: {out[:120]}",
                    "the app answered but not with the version envelope — "
                    "check what is bound to the asgi socket")

    # Duplicate default_server / upstream across conf.d is the classic symptom
    # of a retired vhost surviving next to its successor. /dev/null keeps grep
    # printing filename prefixes even when only one conf file exists.
    rc, out, _ = run("grep -E 'default_server|^[[:space:]]*upstream[[:space:]]' "
                     "/etc/nginx/conf.d/*.conf /dev/null 2>/dev/null")
    declared = {}
    for line in out.splitlines():
        path, _, content = line.partition(":")
        tokens = content.strip().split()
        if not tokens or tokens[0].startswith("#"):
            continue
        if tokens[0] == "upstream" and len(tokens) > 1:
            key = f"upstream '{tokens[1].rstrip('{')}'"
        elif "listen" in tokens and "default_server" in content:
            key = f"default_server on :{tokens[tokens.index('listen') + 1].rstrip(';')}"
        else:
            continue
        declared.setdefault(key, []).append(os.path.basename(path))
    dups = {k: f for k, f in declared.items() if len(f) > 1}
    for key, files in sorted(dups.items()):
        report.fail("nginx", f"duplicate {key}",
                    f"declared in {', '.join(sorted(set(files)))} — nginx -t "
                    "refuses duplicates, blocking every future deploy's reload",
                    "remove the retired duplicate vhost (see the retired-name "
                    "warnings below)")
    if not dups:
        report.passed("nginx", "conf.d uniqueness",
                      "no duplicate default_server or upstream declarations")

    for name in retired_confd:
        if exists(run, f"/etc/nginx/conf.d/{name}"):
            report.warn(
                "nginx", f"retired conf.d file present: {name}",
                "a vhost name aws/node_retired.conf has retired is still "
                "installed — post_deploy removes these on every deploy, so "
                "one surviving means no deploy has converged since the "
                "retirement landed; alongside its successor it is the "
                "duplicate default_server/upstream break above",
                "sudo bash aws/post_deploy.sh retires it (or sudo rm "
                f"/etc/nginx/conf.d/{name} && sudo nginx -t && "
                "sudo systemctl reload nginx)")


# ---------------------------------------------------------------------------
# certs
# ---------------------------------------------------------------------------

def check_certs(report, run, sudo):
    names = list_names(run, "/etc/letsencrypt/live", sudo)
    if not names:
        report.info("certs", "no lineages",
                    "/etc/letsencrypt/live is absent, empty, or unreadable "
                    "without root — a snakeoil-only node, or rerun as root")
        return
    names = [n for n in names if n != "README"]
    report.info("certs", "lineages", ", ".join(sorted(names)) or "none")

    for lineage in sorted(names):
        base = f"/etc/letsencrypt/live/{lineage}"
        priv = stat_of(run, f"{base}/privkey.pem", sudo)
        rc, _, _ = run(f"{sudo}test -f {q(base)}/fullchain.pem")
        if rc != 0 or priv is None:
            missing = []
            if rc != 0:
                missing.append("fullchain.pem")
            if priv is None:
                missing.append("privkey.pem")
            report.warn("certs", f"{lineage}: incomplete lineage",
                        f"missing {', '.join(missing)} — nginx cannot serve "
                        "this pair",
                        "primary: certbot renew rebuilds it; replica: "
                        "certbot_sync pulls the full set")
        elif priv[2] != "600":
            report.warn("certs", f"{lineage}: privkey mode {priv[2]}",
                        "the private key should be 0600 — anything wider "
                        "shares the TLS identity with every local reader",
                        f"chmod 600 {base}/privkey.pem")
        else:
            report.passed("certs", f"{lineage}: material",
                          "fullchain + privkey present, key mode 600")

        rc, out, err = run(
            f"{sudo}openssl x509 -enddate -noout -in {q(base)}/fullchain.pem 2>&1")
        if rc != 0:
            report.info("certs", f"{lineage}: expiry unreadable",
                        (out or err or "openssl failed").splitlines()[0])
        else:
            days = days_until(out)
            if VERBOSE:
                report.info("certs", f"{lineage}: {out.strip()}", "raw enddate")
            if days is None:
                report.info("certs", f"{lineage}: expiry", out.strip())
            elif days < 14:
                report.fail("certs", f"{lineage}: expires in {days}d",
                            "inside certbot's own renewal window and still "
                            "not renewed — renewal is broken on this fleet",
                            "on the primary: python3 -m "
                            "mojo.deploy.certbot_sync --renew (the 1_certbot "
                            "cron's job — check it is firing and "
                            "PRIMARY_BALANCER_HOST names that node); replicas "
                            "then pull via the 4_certbot_sync tick")
            elif days < 30:
                report.warn("certs", f"{lineage}: expires in {days}d",
                            "approaching the renewal window — confirm the "
                            "1_certbot cron (certbot_sync --renew) is firing "
                            "on the primary before this becomes urgent")
            else:
                report.passed("certs", f"{lineage}: expiry",
                              f"{days} days remaining")

        rc, _, _ = run(f"{sudo}test -f /etc/letsencrypt/renewal/{q(lineage)}.conf")
        if rc == 0:
            report.info("certs", f"{lineage}: renewal conf present",
                        "certbot renews this lineage locally (primary behavior)")
        else:
            report.info("certs", f"{lineage}: replica lineage",
                        "no renewal conf — certbot_sync-managed, follows the "
                        "primary via S3")


# ---------------------------------------------------------------------------
# config plane
# ---------------------------------------------------------------------------

def check_config_plane(report, run, proj, app_user, web_user):
    bootstrap = f"{proj}/var/bootstrap.conf"
    if not exists(run, bootstrap):
        report.warn(
            "config_plane", "var/bootstrap.conf absent",
            "node is not on the S3 config plane — config-sync has nothing "
            "to pull, so django.conf is managed by hand on this box",
            "write var/bootstrap.conf (AWS_REGION, AWS_CONFIG_BUCKET, "
            f"AWS_CONFIG_PREFIX, CONFIG_SYNC_OWNER={app_user}:{web_user}); "
            "config-sync.timer does the rest")
    else:
        st = stat_of(run, bootstrap)
        if st is None:
            report.info("config_plane", "bootstrap.conf", "present, cannot stat")
        elif st[2] != "600":
            report.warn("config_plane", f"bootstrap.conf mode {st[2]}",
                        "it holds the one bootstrap credential — 0600 only",
                        f"chmod 600 {bootstrap}")
        else:
            report.passed("config_plane", "bootstrap.conf",
                          "present, mode 600 — node is on the S3 config plane")

    conf = f"{proj}/var/django.conf"
    st = stat_of(run, conf)
    if st is None:
        report.fail("config_plane", "var/django.conf missing",
                    "the app cannot start without it",
                    "config-sync pulls it once bootstrap.conf is in place; "
                    "otherwise the operator places it by hand")
        return
    user, group, mode = st
    if user != app_user or group != web_user:
        report.fail("config_plane", f"django.conf owner {user}:{group}",
                    f"must be {app_user}:{web_user} — the web app and the "
                    f"{app_user} job engines both read it",
                    f"chown {app_user}:{web_user} {conf}")
    else:
        report.passed("config_plane", "django.conf owner",
                      f"{app_user}:{web_user}")
    if mode != "640":
        report.warn("config_plane", f"django.conf mode {mode}",
                    "the contract is 0640 — 0600 strands whichever of the "
                    f"web app / {app_user} engines is not the owner, wider "
                    "leaks secrets to every local user",
                    f"chmod 640 {conf}")
    else:
        report.passed("config_plane", "django.conf mode", "640")

    # Presence of fixed key NAMES only. The anchored grep -o prints the
    # matched key name and nothing else — no value ever reaches this audit's
    # output. Never widen this into reading conf contents.
    pattern = "|".join((WEBHOOK_KEY,) + CERT_SYNC_KEYS)
    rc, out, _ = run(
        f"grep -oE '^({pattern})' {q(conf)} 2>/dev/null | sort -u")
    if rc > 1:
        report.info("config_plane", "key presence unknown",
                    "could not grep django.conf key names as this user")
        return
    present = set(out.splitlines())
    if WEBHOOK_KEY in present:
        report.passed("config_plane", f"{WEBHOOK_KEY} present",
                      "key name found (value never read)")
    else:
        report.warn("config_plane", f"{WEBHOOK_KEY} absent",
                    "the GitHub deploy webhook will 403 every delivery — "
                    "pushes to the deploy branch stop reaching this fleet",
                    "operator adds the key to the published conf (this audit "
                    "never reads or writes conf values)")
    missing = [k for k in CERT_SYNC_KEYS if k not in present]
    if not missing:
        report.passed("config_plane", "cert sync keys present",
                      ", ".join(CERT_SYNC_KEYS) + " (values never read)")
    else:
        report.info("config_plane", "cert sync unconfigured",
                    f"missing {', '.join(missing)} — certbot_sync no-ops; "
                    "fine on a single-node box, a gap on a fleet")


# ---------------------------------------------------------------------------
# shims
# ---------------------------------------------------------------------------

def _check_update_executable(report, run, path, grade):
    """`aws/update.sh` is exec'd directly by the fleet deploy plane, so its
    mode is a deploy contract, not a detail. A shim committed without the
    execute bit fails on EVERY node at once, identically, and the mode has to
    be committed — a local chmod is lost on the next clean checkout.

    `aws/post_deploy.sh` is deliberately exempt: update.sh invokes it as
    `sudo bash <path>`, which does not need the bit."""
    st = stat_of(run, path)
    try:
        bits = int(st[2], 8) if st else None
    except ValueError:
        bits = None
    if bits is None:
        report.info("shims", "aws/update.sh mode unknown",
                    "could not stat the shim as this user")
    elif bits & 0o111:
        report.passed("shims", "aws/update.sh is executable", f"mode {st[2]}")
    else:
        grade("shims", "aws/update.sh is not executable",
              f"{path} has mode {st[2]} — the deploy plane exec()s this path "
              "directly, so every node in the fleet refuses the deploy at the "
              "same moment",
              "git update-index --chmod=+x aws/update.sh && commit the mode "
              "(a local chmod does not survive a clean checkout)")


def check_shims(report, run, proj, require_shims):
    """The shim contract: each project aws/ entry point is a thin shim
    delegating to mojo.deploy. Present-with-reference is adoption; present
    WITHOUT a reference is a fork that silently stops receiving framework
    fixes — WARN normally, FAIL under --require-shims. Absent is INFO: not
    every project ships every entry point."""
    grade = report.fail if require_shims else report.warn
    for rel in SHIM_FILES:
        path = f"{proj}/{rel}"
        if not exists(run, path):
            report.info("shims", f"{rel} absent",
                        "no project copy — either this entry point is invoked "
                        "as `python3 -m mojo.deploy.<module>` directly, or "
                        "the project has not adopted it")
            continue
        if rel == "aws/update.sh":
            _check_update_executable(report, run, path, grade)
        rc, _, _ = run(f"grep -qF -- mojo.deploy {q(path)}")
        if rc == 0:
            report.passed("shims", rel,
                          "delegates to the packaged mojo.deploy tooling")
        else:
            grade("shims", f"{rel} is a fork",
                  f"{path} exists but never references mojo.deploy — a full "
                  "copy pinned to whatever state it was forked at, cut off "
                  "from framework fixes",
                  "replace it with the shim from django-mojo "
                  "docs/django_developer/deploy/README.md (project deltas "
                  "become exported variables in the shim)")


def check_collisions(report, run, proj, require_shims):
    """Template-name collisions between the project's aws/ overlays and the
    framework's shipped templates. Undeclared, the framework copy wins at
    render time — which is loud there and reported here, because the author
    of the project file almost certainly expected it to be installed.

    Framework template names come from the LOCAL package; over --ssh that is
    an approximation (the remote may run a different django-mojo), which is
    why this stays advisory rather than byte-comparing anything."""
    try:
        from mojo.deploy.__main__ import TEMPLATE_SETS, list_files, template_dir
    except Exception as err:
        report.info("shims", "collision audit skipped",
                    f"cannot load mojo.deploy templates: {err}")
        return
    declared = set(read_name_list(run, f"{proj}/aws/node_overrides.conf"))
    grade = report.fail if require_shims else report.warn
    for subdir, overlay_rel in TEMPLATE_SETS:
        framework = set(list_files(template_dir(subdir)))
        project = set(list_names(run, f"{proj}/{overlay_rel}") or [])
        for name in sorted(framework & project):
            if name in declared:
                report.passed("shims", f"declared override: {overlay_rel}/{name}",
                              "project copy replaces the framework template "
                              "by declaration in aws/node_overrides.conf")
            else:
                grade("shims", f"undeclared collision: {overlay_rel}/{name}",
                      "the project ships a file whose name collides with a "
                      "framework template and is not declared in "
                      "aws/node_overrides.conf — render ignores the project "
                      "copy (framework wins), which is probably not what its "
                      "author expects",
                      "declare the name in aws/node_overrides.conf to keep "
                      "the project copy, or delete the project file to accept "
                      "the framework template")


def check_template_freshness(report, proj, app_user, web_user, workers,
                             foreign=None):
    """LOCAL MODE ONLY: render the installed package's templates in memory and
    diff against var/deploy/. A difference means the framework's templates
    moved since the last deploy rendered them — INFO, because the last-deploy
    contract in var/deploy is still the truth for what /etc should hold; this
    is the nudge to re-run post_deploy. Declared-override names are skipped:
    their var/deploy copy is legitimately the project's, not the framework's."""
    deploy_dir = os.path.join(proj, "var", "deploy")
    if not os.path.isdir(deploy_dir):
        # The cron/systemd sections already reported the absent contract.
        return
    try:
        from mojo.deploy.__main__ import (build_context, list_files,
                                          substitute, template_dir,
                                          TEMPLATE_SETS)
    except Exception as err:
        report.info("shims", "template freshness skipped",
                    f"cannot load mojo.deploy templates: {err}")
        return
    declared = set()
    try:
        with open(os.path.join(proj, "aws", "node_overrides.conf")) as handle:
            for line in handle:
                line = line.split("#", 1)[0].strip()
                if line:
                    declared.add(line)
    except OSError:
        pass
    context = build_context(proj, app_user, web_user, workers)
    foreign = foreign or {}
    moved = []
    for subdir, _ in TEMPLATE_SETS:
        # A framework template another role owns is deliberately absent from
        # var/deploy — without this skip every role node reports a permanent,
        # false "framework templates moved".
        skip = declared | set(foreign.get(subdir, ()))
        for name in list_files(template_dir(subdir)):
            if name in skip:
                continue
            with open(os.path.join(template_dir(subdir), name)) as handle:
                expected = substitute(handle.read(), context)
            try:
                with open(os.path.join(deploy_dir, subdir, name)) as handle:
                    current = handle.read()
            except OSError:
                moved.append(f"{subdir}/{name}")
                continue
            if current != expected:
                moved.append(f"{subdir}/{name}")
    if moved:
        report.info("shims", "framework templates moved since the last deploy",
                    ", ".join(moved) + " differ from what the installed "
                    "framework renders — run `sudo bash aws/post_deploy.sh` "
                    "to re-render and converge")
    else:
        report.passed("shims", "framework templates current",
                      "var/deploy matches what the installed framework renders")


# ---------------------------------------------------------------------------
# legacy
# ---------------------------------------------------------------------------

def check_legacy(report, run, proj, repo, retired_cron):
    if exists(run, f"{proj}/var/allow_migrate"):
        report.fail(
            "legacy", "var/allow_migrate present",
            "the retired per-box migrate flag races migrate_locked's "
            "advisory lock — with it, two nodes can decide to migrate at once",
            f"rm {proj}/var/allow_migrate — migrations run only via the "
            "deploy plane's --migrate canary now")
    else:
        report.passed("legacy", "var/allow_migrate", "not present")

    node_has = exists(run, f"{proj}/update_prod.sh")
    repo_ships = exists(run, f"{repo}/update_prod.sh")
    if node_has and not repo_ships:
        report.warn("legacy", "update_prod.sh still at project root",
                    "the retired legacy entry path survives on the node "
                    "although the repo no longer ships it",
                    "remove it — aws/update.sh --manual is the hands-on "
                    "path now (a deploy's git clean would also drop it)")
    elif node_has:
        report.info("legacy", "update_prod.sh present",
                    "still shipped by the repo (its retirement commit has "
                    "not landed) — the legacy webhook path may still invoke it")
    else:
        report.passed("legacy", "update_prod.sh", "no legacy entry script")

    if not retired_cron:
        report.info("legacy", "no declared retired crons",
                    "aws/node_retired.conf declares no cron.d names — the "
                    "structural sweep is the only cron retirement on this "
                    "project")
        return
    installed = set(list_names(run, "/etc/cron.d") or [])
    found = [n for n in retired_cron if n in installed]
    for name in found:
        report.fail(
            "legacy", f"retired cron /etc/cron.d/{name}",
            "aws/node_retired.conf retires this name and post_deploy removes "
            "it on every deploy — it surviving means no deploy has converged "
            "since the retirement landed, so it is still firing alongside "
            "its replacement",
            f"sudo rm /etc/cron.d/{name} (or run sudo bash aws/post_deploy.sh)")
    if not found:
        report.passed("legacy", "retired cron names",
                      "none of the names declared in aws/node_retired.conf "
                      "are installed")


# ---------------------------------------------------------------------------
# var ownership
# ---------------------------------------------------------------------------

def check_var_ownership(report, run, proj, app_user, web_user):
    # Sample the load-bearing directories rather than recursing the tree —
    # the tree is large and post_deploy re-normalizes var/logs anyway.
    for rel in ("var", "var/logs", "var/pids"):
        path = f"{proj}/{rel}"
        st = stat_of(run, path)
        if st is None:
            report.warn("var_ownership", f"{rel}/ missing or unreadable",
                        f"{path} should exist on any provisioned node",
                        "provisioning established these; mkdir + chown "
                        f"{app_user}:{web_user} + chmod 2775 to repair")
            continue
        user, group, mode = st
        problems = []
        if user != app_user or group != web_user:
            problems.append(f"owner {user}:{group}")
        if mode != "2775":
            problems.append(f"mode {mode}")
        if problems:
            report.warn(
                "var_ownership", f"{rel}/: {', '.join(problems)}",
                f"contract is {app_user}:{web_user} setgid 2775 "
                "(provisioning). Note mojo-asgi's ExecStartPre re-installs "
                f"these dirs as {web_user}:{web_user} 0775 on every start, "
                "which fights the contract — expect this warning on any node "
                "whose app has restarted",
                f"chown {app_user}:{web_user} {path} && chmod 2775 {path}")
        else:
            report.passed("var_ownership", f"{rel}/",
                          f"{app_user}:{web_user}, setgid 2775")

    rc, out, _ = run(f"find {q(proj)}/var/logs -type f -user root 2>/dev/null")
    if rc != 0 and not out:
        if VERBOSE:
            report.info("var_ownership", "root-owned scan skipped",
                        "could not run find over var/logs")
        return
    rooted = [line for line in out.splitlines() if line.strip()]
    if rooted:
        shown = ", ".join(os.path.basename(p) for p in rooted[:5])
        more = f" (+{len(rooted) - 5} more)" if len(rooted) > 5 else ""
        report.fail(
            "var_ownership", f"{len(rooted)} root-owned file(s) in var/logs",
            f"{shown}{more} — a root-owned log blocks the web app and the "
            f"{app_user} engines from appending, which surfaces as silent "
            "logging loss or startup crashes",
            f"sudo chown -R {app_user}:{web_user} {proj}/var/logs "
            "(post_deploy normalizes this on every deploy)")
    else:
        report.passed("var_ownership", "var/logs files",
                      "no root-owned files")


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

def check_jobs(report, run, proj):
    rc, out, err = run(f"cd {q(proj)} && ./bin/jobman status 2>&1", timeout=30)
    if not out:
        report.info("jobs", "jobman unavailable",
                    (err or "bin/jobman produced no output").splitlines()[0])
        return
    lines = out.splitlines()
    # "extra instances detected: <pid>" is a known cosmetic false positive —
    # jobman counts its own matching pid — and is deliberately ignored here.
    extras = [l for l in lines if "extra instances detected" in l]
    engine = next((l for l in lines
                   if l.startswith("Engine") and "extra" not in l), "")
    sched = next((l for l in lines
                  if l.startswith("Scheduler") and "extra" not in l), "")

    if engine.startswith("Engine running"):
        report.passed("jobs", "job engine", engine)
    else:
        report.fail("jobs", "job engine not running",
                    engine or "no Engine line in jobman output",
                    "3_mojo_jobs cron revives it within a minute — if it "
                    "stays down, read var/logs/jobman.log")
    if sched.startswith("Scheduler running"):
        report.passed("jobs", "job scheduler", sched)
    else:
        report.fail("jobs", "job scheduler not running",
                    sched or "no Scheduler line in jobman output",
                    "3_mojo_jobs cron restarts it — check var/logs/jobman.log "
                    "if it does not come back")
    if extras and VERBOSE:
        report.info("jobs", "extra-instance lines ignored",
                    extras[0] + " — known cosmetic false positive (jobman "
                    "matches its own pid)")
    check_job_channels(report, run, proj)


# `JOBS_CHANNELS` REPLACES the framework's list rather than extending it, so a
# project that writes its own drops every channel it does not re-list — and
# nothing anywhere reports it. The publisher succeeds; the job lands in a
# stream no runner reads; the feature is simply inert.
#
# `edge` is the one that costs a whole afternoon: `platform_deploy.edge_roster()`
# freezes the runners consuming it, and an empty roster makes every push webhook
# answer 503 "edge runner roster unavailable". A fleet in that state serves
# perfectly and takes no deploys. That is what this check exists to catch.
FRAMEWORK_CHANNELS = (
    "default", "priority", "cleanup", "incident_handlers", "renditions",
    "certs", "webhooks", "webhook_fanout", "edge",
)

# What breaks, per channel, in the words of the person who will read this.
CHANNEL_COST = {
    "edge": "push-to-deploy and nginx generation convergence never run",
    "certs": "dnsman certificate sync never runs",
    "incident_handlers": "incident handlers never fire",
    "renditions": "media renditions are never generated",
    "webhook_fanout": "outbound webhook fan-out never delivers",
    "cleanup": "scheduled cleanup never runs",
    "webhooks": "inbound webhook processing never runs",
}


def check_job_channels(report, run, proj):
    rc, out, err = run(
        f"cd {q(proj)} && python3 bin/manage.py shell -c "
        f"'from mojo.apps import jobs; print(\",\".join(jobs.JOB_CHANNELS))' "
        f"2>/dev/null | tail -1", timeout=60)
    consumed = [c.strip() for c in (out or "").split(",") if c.strip()]
    if rc != 0 or not consumed:
        report.info("jobs", "consume channels",
                    "could not read jobs.JOB_CHANNELS from this tree")
        return

    missing = [c for c in FRAMEWORK_CHANNELS if c not in consumed]
    if not missing:
        report.passed("jobs", "consume channels",
                      f"{len(consumed)} channels, framework list complete")
        return

    cost = "; ".join(CHANNEL_COST.get(c, f"{c} work is never consumed")
                     for c in missing)
    say = report.fail if "edge" in missing else report.warn
    say("jobs", "consume channels incomplete",
        f"JOBS_CHANNELS omits {', '.join(missing)} — {cost}",
        "JOBS_CHANNELS replaces the framework's DEFAULT_CHANNELS rather than "
        "extending it; add the missing names to the project's jobs settings "
        "and restart the engine")


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def main(argv):
    global VERBOSE
    parser = argparse.ArgumentParser(
        prog="python3 -m mojo.deploy.check_node",
        description="Audit one node against the on-node deploy-plane contract "
                    "(the complement of check_setup's AWS-side audit)")
    parser.add_argument("--project-path", default="/opt/api",
                        help="the deployed tree being audited (default: /opt/api)")
    parser.add_argument("--repo-path", default=None,
                        help="git tree defining the nginx contract "
                             "(aws/nginx). Default: --project-path. Over "
                             "--ssh this is a REMOTE path.")
    parser.add_argument("--ssh", metavar="HOST", default=None,
                        help="audit HOST instead of this machine — every "
                             "probe runs as `ssh HOST ...` (BatchMode)")
    parser.add_argument("--probe-url",
                        default="http://127.0.0.1/api/version",
                        help="the health-gate URL the deploy plane probes on "
                             "this project (default: "
                             "http://127.0.0.1/api/version)")
    parser.add_argument("--app-user", default="ec2-user",
                        help="user that owns the tree and runs the job "
                             "engines (default: ec2-user)")
    parser.add_argument("--web-user", default="www",
                        help="user the asgi app runs as (default: www)")
    parser.add_argument("--asgi-workers", default="4",
                        help="the @WORKERS@ value the last render used — only "
                             "consulted by the template-freshness diff "
                             "(default: 4)")
    parser.add_argument("--mojosec-mode", choices=("auto", "off", "observe"),
                        default="auto",
                        help="desired MojoSec lifecycle; auto derives it from "
                             "the enabled state so legacy nodes remain INFO "
                             "(default: auto)")
    parser.add_argument("--mojosec-sensor-id", default="",
                        help="expected infrastructure host sensor id (optional)")
    parser.add_argument("--require-shims", action="store_true",
                        help="grade a forked aws/ script or an undeclared "
                             "template collision FAIL instead of WARN")
    parser.add_argument("--section", default="all",
                        help=f"comma-separated subset of: {', '.join(SECTIONS)}")
    parser.add_argument("--json", action="store_true",
                        help="emit findings as JSON")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="extra INFO-level detail")
    args = parser.parse_args(argv)
    VERBOSE = args.verbose

    if args.section == "all":
        wanted = set(SECTIONS)
    else:
        wanted = {s.strip() for s in args.section.split(",")}
        unknown = wanted - set(SECTIONS)
        if unknown:
            parser.error(f"unknown section(s): {', '.join(sorted(unknown))}")

    proj = args.project_path.rstrip("/")
    repo = args.repo_path.rstrip("/") if args.repo_path else proj

    run = build_runner(args.ssh)
    if args.ssh:
        rc, _, err = run("true", timeout=15)
        if rc != 0:
            print(f"cannot reach {args.ssh} over ssh (BatchMode): "
                  f"{err or 'connection failed'}", file=sys.stderr)
            return 2

    # Root-only material (letsencrypt, TLS keys behind nginx -t) is probed
    # through passwordless sudo when available; otherwise those probes degrade
    # to INFO rather than failing the audit blind.
    sudo = "sudo -n " if run("sudo -n true 2>/dev/null")[0] == 0 else ""

    retired_cron, retired_confd = read_retired(run, proj)
    # Resolved once: the nginx, systemd and shims sections all need to know
    # which names belong to another role, whether or not `roles` was selected.
    roles = read_role_state(run, proj, repo)

    report = Report()
    if "repo" in wanted:
        check_repo(report, run, proj, args.app_user)
    if "framework" in wanted:
        check_framework(report, run, proj)
    if "roles" in wanted:
        check_roles(report, run, roles, proj, repo)
    if "cron" in wanted:
        check_cron(report, run, proj)
    if "systemd" in wanted:
        check_systemd(report, run, proj, roles["foreign"]["systemd"])
    if "mojosec" in wanted:
        check_mojosec(report, run, args.mojosec_mode, sudo,
                      args.mojosec_sensor_id)
    if "nginx" in wanted:
        check_nginx(report, run, repo, sudo, args.probe_url, retired_confd,
                    args.web_user, roles["foreign"]["conf.d"])
    if "certs" in wanted:
        check_certs(report, run, sudo)
    if "config_plane" in wanted:
        check_config_plane(report, run, proj, args.app_user, args.web_user)
    if "shims" in wanted:
        check_shims(report, run, proj, args.require_shims)
        check_collisions(report, run, proj, args.require_shims)
        if not args.ssh:
            check_template_freshness(report, proj, args.app_user,
                                     args.web_user, args.asgi_workers,
                                     roles["foreign"])
        elif VERBOSE:
            report.info("shims", "template freshness skipped",
                        "only audited locally — the local package version "
                        "says nothing about the remote node's")
    if "legacy" in wanted:
        check_legacy(report, run, proj, repo, retired_cron)
    if "var_ownership" in wanted:
        check_var_ownership(report, run, proj, args.app_user, args.web_user)
    if "jobs" in wanted:
        check_jobs(report, run, proj)

    if args.json:
        print(json.dumps({
            "findings": report.findings,
            "counts": report.counts(),
        }, indent=2))
    else:
        report.render()

    return 1 if report.counts()[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
