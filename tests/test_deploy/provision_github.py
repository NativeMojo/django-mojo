"""Registering the push webhook and the node's deploy key, through `gh`.

Two things are asserted here that are easy to get wrong and impossible to
notice afterwards:

    An EXISTING hook is rewritten, not skipped. GitHub never returns a
    webhook's secret, so "a hook is already there" is no evidence at all that
    it agrees with the django.conf this run just published. A skip leaves a
    fleet where every push delivery is rejected as unsigned and nothing
    reports an error — the deploy simply never happens.

    The secret never reaches argv. `ps` is world-readable for the life of a
    call, so request bodies go in on stdin. This is the kind of property that
    survives exactly as long as a test is watching it.

`gh` itself is replaced by a recorder — these tests are about what this module
asks GitHub for, not about the CLI.
"""

import json

from testit import helpers as th


REPO = "Org/project"
APEX = "example.com"
SECRET = "s" * 40
HOOK_URL = f"https://{APEX}/api/github/deploy/webhook"


class _Gh:
    """Records every invocation; answers by argv substring."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args, stdin=None, timeout=None):
        args = list(args)
        self.calls.append({"args": args, "stdin": stdin})
        joined = " ".join(args)
        for pattern, result in self.responses:
            if pattern in joined:
                return result
        return True, "", ""

    def called(self, fragment):
        return [c for c in self.calls if fragment in " ".join(c["args"])]


def _with_gh(recorder, fn):
    from mojo.deploy.provision import github

    original = github._gh
    github._gh = recorder
    try:
        return fn()
    finally:
        github._gh = original


def _codes(findings):
    return [f.code for f in findings]


@th.django_unit_test("a brand-new repository gets a signed push hook created")
def test_webhook_is_created_when_absent(opts):
    from mojo.deploy.provision import github

    gh = _Gh([("api repos/Org/project/hooks --paginate", (True, "[]", ""))])
    findings = _with_gh(
        gh, lambda: github.ensure_webhook(REPO, APEX, SECRET))

    posts = gh.called("-X POST")
    th.assert_eq(len(posts), 1, f"one create expected: {gh.calls}")
    body = json.loads(posts[0]["stdin"])
    th.assert_eq(body["events"], ["push"],
                 "the deploy plane listens for pushes and nothing else")
    th.assert_eq(body["config"]["url"], HOOK_URL,
                 "the hook must deliver to this environment's apex")
    th.assert_eq(body["config"]["content_type"], "json",
                 "the endpoint parses a JSON body")
    th.assert_in("webhook.ok", _codes(findings), str(_codes(findings)))


@th.django_unit_test("an existing hook is rewritten, because its secret cannot be read back")
def test_webhook_existing_hook_is_patched(opts):
    from mojo.deploy.provision import github

    existing = json.dumps([{"id": 99, "config": {"url": HOOK_URL}}])
    gh = _Gh([("api repos/Org/project/hooks --paginate", (True, existing, ""))])
    findings = _with_gh(
        gh, lambda: github.ensure_webhook(REPO, APEX, SECRET))

    th.assert_eq(gh.called("-X POST"), [],
                 "a second hook at the same URL would double every delivery")
    patches = gh.called("-X PATCH repos/Org/project/hooks/99")
    th.assert_eq(len(patches), 1,
                 f"the existing hook must be rewritten so its secret is known "
                 f"to match the published django.conf: {gh.calls}")
    th.assert_eq(json.loads(patches[0]["stdin"])["config"]["secret"], SECRET,
                 "the rewrite is only worth doing if it carries the secret")
    th.assert_in("webhook.ok", _codes(findings), str(_codes(findings)))


@th.django_unit_test("the webhook secret never appears in a command line")
def test_webhook_secret_is_never_in_argv(opts):
    from mojo.deploy.provision import github

    gh = _Gh([("api repos/Org/project/hooks --paginate", (True, "[]", ""))])
    _with_gh(gh, lambda: github.ensure_webhook(REPO, APEX, SECRET))

    for call in gh.calls:
        th.assert_eq(SECRET in " ".join(call["args"]), False,
                     "argv is world-readable in `ps` for the life of the "
                     "call — request bodies belong on stdin")


@th.django_unit_test("a gh that cannot administer the repo prints the manual recipe")
def test_webhook_failure_is_manual_not_fatal(opts):
    from mojo.deploy.provision import github, report

    gh = _Gh([
        ("api repos/Org/project/hooks --paginate", (True, "[]", "")),
        ("-X POST", (False, "", "HTTP 404: Not Found")),
    ])
    findings = _with_gh(
        gh, lambda: github.ensure_webhook(REPO, APEX, SECRET))

    th.assert_eq(_codes(findings), ["webhook.manual"], str(_codes(findings)))
    th.assert_eq(findings[0].status, report.MANUAL,
                 "gh is routinely authenticated as an account with no admin "
                 "rights on the repo being deployed — that is one paste for "
                 "the operator, not a failed estate")
    th.assert_true(HOOK_URL in (findings[0].remedy or ""),
                   "the remedy must give the payload URL to paste")
    th.assert_eq(SECRET in (findings[0].remedy or ""), False,
                 "the remedy must point at django.conf, not print the secret "
                 "into the operator's scrollback")


@th.django_unit_test("no secret means no hook, and it says which command mints one")
def test_webhook_refuses_without_a_secret(opts):
    from mojo.deploy.provision import github

    gh = _Gh()
    findings = _with_gh(gh, lambda: github.ensure_webhook(REPO, APEX, ""))

    th.assert_eq(gh.calls, [],
                 "an unsigned hook would deliver pushes every node rejects — "
                 "worse than no hook, because it looks configured")
    th.assert_eq(_codes(findings), ["webhook.no_secret"], str(_codes(findings)))


@th.django_unit_test("a deploy key already on the repository is not added twice")
def test_deploy_key_is_idempotent(opts):
    from mojo.deploy.provision import github

    pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 ec2-user@old-host"
    listed = json.dumps([{"key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5"}])
    gh = _Gh([("api repos/Org/project/keys", (True, listed, ""))])

    def run(cmd, timeout=None):
        return 0, pubkey, ""

    findings = _with_gh(
        gh, lambda: github.ensure_deploy_key(run, "203.0.113.10", REPO))

    th.assert_eq(gh.called("deploy-key add"), [],
                 "GitHub rejects a duplicate key, so re-running configure "
                 "would report a failure on a node that is already fine")
    th.assert_in("deploy_key.ok", _codes(findings), str(_codes(findings)))
