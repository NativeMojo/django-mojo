"""Turning the unpacked boot tarball into a real git checkout.

Stage 1 installs `/opt/api` from `git archive HEAD` — a tarball, deliberately,
because a node has no deploy key until after it exists and the operator's
commit is the only thing that should ever land on it. The cost is that the
tree arrives with no history, and django-mojo's deploy plane is exactly
`git fetch origin && git reset --hard <sha>` (see `mojo/deploy/scripts/
update.sh`). A node without `.git` therefore accepts no deploy at all —
`check_node.check_repo` reports it as an outright FAIL: *"the deploy plane is
git + post_deploy.sh — a node without the repo cannot converge at all."*

This module pays that cost back, once, at the end of `configure`, after the
deploy key has been registered on the repository:

    git init · remote add origin · fetch · reset --mixed <the archived sha>

**`--mixed`, never `--hard`.** The bytes on disk are already that commit; all
that is missing is HEAD and the index. A `--hard` would rewrite files the node
is actively serving to reach a state it is already in — motion with nothing to
gain and a running app to lose.

**The archived commit must be on the remote, and this refuses when it is not.**
That is the whole reason this is not three lines inline. `git archive HEAD`
happily ships a commit that exists only on the operator's laptop; wiring the
node to origin and resetting it to *something else* — origin/main, say — would
silently move a running node onto code nobody asked for, and it would look
like a success. So the sha is verified present after the fetch, and a node
whose commit was never pushed is reported, left exactly as it is, and told
what to do about it.
"""

import re

from mojo.deploy.provision import report


STEP = "checkout"

PROJ_PATH = "/opt/api"
APP_SHA_PATH = f"{PROJ_PATH}/var/app_sha"

FETCH_TIMEOUT = 300
COMMAND_TIMEOUT = 60

_SHA = re.compile(r"^[0-9a-f]{40}$")

# `git init` leaves HEAD on an unborn branch named by the node's git defaults,
# which on AL2023 is `master`. The deploy plane and `check_node` both compare
# against `origin/main`, so HEAD is pointed at main explicitly rather than
# inherited from whatever init.defaultBranch happens to be.
BRANCH = "main"


def remote_url(repo):
    """SSH, not HTTPS: the deploy key is the node's only credential."""
    return f"git@github.com:{repo}.git"


def ensure_checkout(run, host, repo):
    """Wire `/opt/api` to `repo` at the commit it was provisioned from.

    Idempotent, and cheap on the common path: a node already at the right
    commit costs one `rev-parse`.
    """
    findings = []
    if not repo:
        return findings

    sha = _archived_sha(run, host, findings)
    if sha is None:
        return findings

    rc, head, _ = run(f"git -C {PROJ_PATH} rev-parse HEAD", COMMAND_TIMEOUT)
    if rc == 0 and head.strip() == sha:
        findings.append(report.existing(
            STEP, "checkout.ok",
            f"{host}: {PROJ_PATH} is a checkout of {repo} at {sha[:12]}"))
        return findings

    if not _wire_remote(run, host, repo, findings):
        return findings
    if not _fetch(run, host, repo, findings):
        return findings
    if not _sha_present(run, host, repo, sha, findings):
        return findings
    _adopt(run, host, repo, sha, findings)
    return findings


def _archived_sha(run, host, findings):
    """The commit stage 1 recorded, or None with the reason said out loud."""
    rc, out, _ = run(f"cat {APP_SHA_PATH}", COMMAND_TIMEOUT)
    sha = (out or "").strip()
    if rc == 0 and _SHA.match(sha):
        return sha
    findings.append(report.manual(
        STEP, "checkout.no_sha",
        f"{host}: {APP_SHA_PATH} is missing or unreadable, so this node "
        f"cannot name the commit it runs",
        "the node booted from a payload published before app.sha existed — "
        "re-run `provision apply` to republish it, then replace this node "
        "(or wire it by hand: git init, add origin, fetch, reset --mixed to "
        "the commit it was provisioned from)"))
    return None


def _wire_remote(run, host, repo, findings):
    url = remote_url(repo)
    rc, out, _ = run(
        f"git -C {PROJ_PATH} rev-parse --is-inside-work-tree", COMMAND_TIMEOUT)
    if rc != 0 or out.strip() != "true":
        rc, _, err = run(f"git init -q {PROJ_PATH}", COMMAND_TIMEOUT)
        if rc != 0:
            return _blind(findings, "checkout.init_failed",
                          f"{host}: git init in {PROJ_PATH} failed: "
                          f"{err or 'no output'}",
                          f"{PROJ_PATH} must be writable by the SSH user — "
                          f"ec2_bootstrap.sh chowns it to ec2-user:www")
        run(f"git -C {PROJ_PATH} symbolic-ref HEAD refs/heads/{BRANCH}",
            COMMAND_TIMEOUT)

    # `remote set-url` on a remote that does not exist is an error and
    # `remote add` on one that does is another, so ask first. A pre-existing
    # origin pointing somewhere else is overwritten on purpose: the repo the
    # operator provisioned with is the answer, not whatever a previous run or
    # a hand-edit left behind.
    rc, existing, _ = run(f"git -C {PROJ_PATH} remote get-url origin",
                          COMMAND_TIMEOUT)
    if rc == 0:
        if existing.strip() == url:
            return True
        verb = "set-url"
    else:
        verb = "add"
    rc, _, err = run(f"git -C {PROJ_PATH} remote {verb} origin {url}",
                     COMMAND_TIMEOUT)
    if rc != 0:
        return _blind(findings, "checkout.remote_failed",
                      f"{host}: could not point origin at {url}: "
                      f"{err or 'no output'}",
                      "check the tree's ownership — git refuses a repository "
                      "owned by another user")
    return True


def _fetch(run, host, repo, findings):
    rc, _, err = run(f"git -C {PROJ_PATH} fetch --quiet origin", FETCH_TIMEOUT)
    if rc == 0:
        return True
    return _blind(
        findings, "checkout.fetch_failed",
        f"{host}: could not fetch {repo}: {err or 'no output'}",
        "the node authenticates with /home/ec2-user/.ssh/id_ed25519 — "
        f"`configure` registers its public half as a deploy key on {repo}; "
        "add it by hand at "
        f"https://github.com/{repo}/settings/keys if that step reported a "
        "failure")


def _sha_present(run, host, repo, sha, findings):
    """The check this module exists for."""
    rc, _, _ = run(
        f"git -C {PROJ_PATH} cat-file -e {sha}^{{commit}}", COMMAND_TIMEOUT)
    if rc == 0:
        return True
    findings.append(report.manual(
        STEP, "checkout.sha_unpushed",
        f"{host}: this node was provisioned from {sha[:12]}, which is not on "
        f"{repo} — it is left unwired rather than moved onto a different "
        f"commit",
        f"push that commit to {repo} and re-run `provision configure`; "
        f"until then the node serves correctly but takes no deploys"))
    return False


def _adopt(run, host, repo, sha, findings):
    """HEAD and the index to `sha`. The working tree is already there."""
    rc, _, err = run(f"git -C {PROJ_PATH} reset --mixed --quiet {sha}",
                     COMMAND_TIMEOUT)
    if rc != 0:
        return _blind(findings, "checkout.reset_failed",
                      f"{host}: git reset to {sha[:12]} failed: "
                      f"{err or 'no output'}",
                      "resolve on the node, then re-run `provision configure`")

    findings.append(report.existing(
        STEP, "checkout.wired",
        f"{host}: {PROJ_PATH} is now a checkout of {repo} at {sha[:12]} — "
        f"pushes to {BRANCH} can deploy it"))

    # Tracked files only. A provisioned node always has untracked runtime
    # output under var/, and reporting that as drift would cry wolf on every
    # single run.
    rc, dirty, _ = run(
        f"git -C {PROJ_PATH} status --porcelain --untracked-files=no",
        COMMAND_TIMEOUT)
    if rc == 0 and dirty.strip():
        findings.append(report.drift(
            STEP, "checkout.tree_differs",
            f"{host}: {len(dirty.strip().splitlines())} tracked path(s) differ "
            f"from {sha[:12]} — the running tree is not exactly that commit",
            "the next deploy's `git reset --hard` overwrites them; if they "
            "matter, capture them off the node first"))
    return True


def _blind(findings, code, message, remedy):
    findings.append(report.Finding(STEP, report.BLIND, code, message, remedy))
    return False
