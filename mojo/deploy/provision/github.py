"""The two things GitHub has to know about a provisioned environment.

A node's tree, its credential to fetch that tree, and the signature it checks
on a push notification are three halves of one mechanism, and django-mojo's
deploy plane needs all of them:

1. **the deploy key** — the node's public half registered on the repository,
   which is what lets `git fetch origin` work at all; and
2. **the push webhook** — pointed at `/api/github/deploy/webhook` and signed
   with this environment's `github_webhook_secret`, which is what makes a push
   to `main` deploy the fleet.

Both go through `gh`, and BOTH ARE BEST EFFORT ON PURPOSE. `gh` is very often
authenticated as an account with no admin rights on the repository being
deployed — a plain 404, indistinguishable from "not installed" — and an
environment is not broken by that: it costs the operator one paste into a
browser. So every failure here prints the exact manual recipe and reports as
MANUAL rather than exiting non-zero on an estate that is otherwise finished.

NOTHING IN THIS MODULE PUTS THE SECRET IN ARGV. Request bodies go to `gh api`
on stdin, because argv is world-readable in `ps` for the life of the call, and
the one place a value must be shown to a human — the manual fallback — says so
explicitly rather than printing it into a log by accident.
"""

import json
import subprocess

from mojo.deploy.provision import report


STEP = "github"

WEBHOOK_PATH = "/api/github/deploy/webhook"
GH_TIMEOUT = 60


def webhook_url(apex):
    return f"https://{apex}{WEBHOOK_PATH}"


def _gh(args, stdin=None, timeout=GH_TIMEOUT):
    """Run `gh`. Returns (ok, stdout, message) — never raises."""
    try:
        done = subprocess.run(["gh"] + list(args), input=stdin,
                              capture_output=True, text=True, timeout=timeout,
                              check=False)
    except FileNotFoundError:
        return False, "", "gh is not installed"
    except subprocess.TimeoutExpired:
        return False, "", f"gh timed out after {timeout}s"
    if done.returncode == 0:
        return True, done.stdout, ""
    detail = (done.stderr or done.stdout or "").strip().splitlines()
    return False, "", (detail[-1] if detail else f"gh exited {done.returncode}")


# ── deploy keys ─────────────────────────────────────────────────────────────

def ensure_deploy_key(run, host, repo):
    """Register this node's public key on `repo`. One node, one key.

    Per node rather than per environment: each box generates its own key in
    `ec2_bootstrap.sh`, and a fleet where every node fetches under its own
    credential is one where a single compromised box is revoked on its own.
    """
    findings = []
    if not repo:
        return findings

    rc, pubkey, _ = run("cat /home/ec2-user/.ssh/id_ed25519.pub", 30)
    pubkey = (pubkey or "").strip()
    if rc != 0 or not pubkey:
        findings.append(report.manual(
            STEP, "deploy_key.unreadable",
            f"{host}: could not read /home/ec2-user/.ssh/id_ed25519.pub",
            "ec2_bootstrap.sh generates it — re-run stage 1 on this node"))
        return findings

    if _key_present(repo, pubkey):
        findings.append(report.existing(
            STEP, "deploy_key.ok",
            f"{host}: its deploy key is already on {repo}"))
        return findings

    title = f"ec2-user@{host}"
    ok, _, message = _gh(
        ["repo", "deploy-key", "add", "/dev/stdin", "--repo", repo,
         "--title", title], stdin=pubkey + "\n")
    if ok:
        findings.append(report.existing(
            STEP, "deploy_key.added",
            f"{host}: added its deploy key to {repo} as {title}"))
        return findings

    findings.append(report.manual(
        STEP, "deploy_key.manual",
        f"{host}: could not add its deploy key to {repo} ({message})",
        f"add it at https://github.com/{repo}/settings/keys — {pubkey}"))
    return findings


def _key_present(repo, pubkey):
    """Is this exact key already registered? An unreadable list reads False.

    GitHub normalises a key to `<type> <material>`, dropping the comment, so
    the first two fields are what can be compared.
    """
    ok, out, _ = _gh(["api", f"repos/{repo}/keys", "--paginate"])
    if not ok:
        return False
    try:
        keys = json.loads(out or "[]")
    except ValueError:
        return False
    wanted = " ".join(pubkey.split()[:2])
    return any(" ".join(str(k.get("key", "")).split()[:2]) == wanted
               for k in keys)


# ── the push webhook ────────────────────────────────────────────────────────

def ensure_webhook(repo, apex, secret):
    """A push hook on `repo` delivering to this environment, signed.

    An existing hook at the same URL is PATCHed rather than left alone. The
    secret is write-only over the API — GitHub never gives it back — so
    "a hook is already there" is not evidence that it agrees with the
    `django.conf` this run just published. Rewriting it is the only way to
    know they match, and it is idempotent.
    """
    findings = []
    if not repo or not apex:
        return findings

    url = webhook_url(apex)
    if not secret:
        findings.append(report.manual(
            STEP, "webhook.no_secret",
            f"this environment has no github_webhook_secret, so a hook on "
            f"{repo} could not be signed",
            "re-run `provision apply` — it mints the field into "
            "bootstrap-secrets.json and publishes it to django.conf"))
        return findings

    config = {"url": url, "content_type": "json", "secret": secret,
              "insecure_ssl": "0"}
    existing = _hook_at(repo, url)
    if existing is None:
        body = {"name": "web", "active": True, "events": ["push"],
                "config": config}
        ok, _, message = _gh(["api", "-X", "POST", f"repos/{repo}/hooks",
                              "--input", "-"], stdin=json.dumps(body))
        verb = "created"
    else:
        body = {"active": True, "events": ["push"], "config": config}
        ok, _, message = _gh(
            ["api", "-X", "PATCH", f"repos/{repo}/hooks/{existing}",
             "--input", "-"], stdin=json.dumps(body))
        verb = "updated"

    if ok:
        findings.append(report.existing(
            STEP, "webhook.ok",
            f"{verb} the push webhook on {repo} → {url}"))
        return findings

    findings.append(report.manual(
        STEP, "webhook.manual",
        f"could not {verb[:-1]} the push webhook on {repo} ({message})",
        f"add it at https://github.com/{repo}/settings/hooks/new — payload "
        f"URL {url}, content type application/json, secret = the "
        f"GITHUB_WEBHOOK_SECRET line in this environment's django.conf, "
        f"events: just the push event"))
    return findings


def _hook_at(repo, url):
    """The id of the existing push hook delivering to `url`, or None."""
    ok, out, _ = _gh(["api", f"repos/{repo}/hooks", "--paginate"])
    if not ok:
        return None
    try:
        hooks = json.loads(out or "[]")
    except ValueError:
        return None
    for hook in hooks:
        if (hook.get("config") or {}).get("url") == url:
            return hook.get("id")
    return None
