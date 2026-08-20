"""Wiring a provisioned node's tree to git, and the refusal that guards it.

`/opt/api` arrives as a `git archive` tarball, so it has no history, and
django-mojo's deploy plane is `git fetch origin && git reset --hard <sha>`. A
node that is never wired to a remote silently accepts no deploy at all.

THE ASSERTION THAT MATTERS IS THE REFUSAL. `git archive HEAD` will happily ship
a commit that exists only on the operator's laptop. Wiring such a node to
origin and then resetting it — to origin/main, or to anything that resolves —
would move a running node onto code nobody chose, and would report success
while doing it. So the tests below care less that the happy path resets than
that the unpushed path DOES NOT: no reset command may be issued, and the
finding has to say why.

The shell is the recorder used throughout this suite: commands are captured and
answered by substring, so a test says "cat-file fails" and nothing else moves.
"""

from testit import helpers as th


HOST = "203.0.113.10"
REPO = "Org/project"
SHA = "a" * 40
OTHER_SHA = "b" * 40


class _Runner:
    """A shell that records and answers by substring. Default: success."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(cmd)
        for pattern, result in self.responses:
            if pattern in cmd:
                return result
        return 0, "", ""

    def ran(self, fragment):
        return [cmd for cmd in self.calls if fragment in cmd]


def _codes(findings):
    return [f.code for f in findings]


def _wired_node(**overrides):
    """A node whose tree is the archived commit but has no .git yet."""
    responses = [
        ("cat /opt/api/var/app_sha", (0, SHA, "")),
        # No repository yet: rev-parse HEAD and --is-inside-work-tree both
        # fail, which is what a freshly untarred tree looks like.
        ("rev-parse HEAD", (128, "", "not a git repository")),
        ("rev-parse --is-inside-work-tree", (128, "", "not a git repository")),
        ("remote get-url origin", (1, "", "no such remote")),
    ]
    responses[:0] = list(overrides.pop("responses", ()))
    return _Runner(responses)


@th.django_unit_test("an unpacked tree becomes a checkout at the archived commit")
def test_checkout_wires_an_unwired_node(opts):
    from mojo.deploy.provision import checkout

    run = _wired_node()
    findings = checkout.ensure_checkout(run, HOST, REPO)

    th.assert_true(run.ran("git init -q /opt/api"),
                   "an unwired tree must be initialised as a repository")
    th.assert_true(
        run.ran(f"remote add origin git@github.com:{REPO}.git"),
        "origin must point at the repository the node was provisioned from")
    th.assert_true(run.ran("fetch --quiet origin"),
                   "the history has to be fetched before HEAD can name it")
    th.assert_true(run.ran(f"reset --mixed --quiet {SHA}"),
                   f"HEAD must be adopted at the archived commit: {run.calls}")
    th.assert_in("checkout.wired", _codes(findings),
                 f"the wiring must be reported: {_codes(findings)}")


@th.django_unit_test("the reset is --mixed, so a serving node's files are never rewritten")
def test_checkout_never_rewrites_the_working_tree(opts):
    from mojo.deploy.provision import checkout

    run = _wired_node()
    checkout.ensure_checkout(run, HOST, REPO)

    th.assert_eq(run.ran("reset --hard"), [],
                 "the bytes on disk are already that commit — a --hard reset "
                 "rewrites files a running app is serving to reach a state it "
                 "is already in")
    th.assert_eq(run.ran("checkout"), [],
                 "nothing may check files out over the provisioned tree")


@th.django_unit_test("a commit that is not on the remote is refused, not reset away")
def test_checkout_refuses_a_commit_the_remote_does_not_have(opts):
    from mojo.deploy.provision import checkout, report

    run = _wired_node(responses=[("cat-file -e", (1, "", ""))])
    findings = checkout.ensure_checkout(run, HOST, REPO)

    th.assert_eq(run.ran("reset"), [],
                 "a node provisioned from an unpushed commit must be LEFT "
                 "ALONE — resetting it moves a running node onto code nobody "
                 "chose, and reports success while doing it")
    th.assert_in("checkout.sha_unpushed", _codes(findings),
                 f"the refusal must be reported: {_codes(findings)}")
    manual = [f for f in findings if f.code == "checkout.sha_unpushed"]
    th.assert_eq(manual[0].status, report.MANUAL,
                 "an unpushed commit is the operator's to fix, not a failure "
                 "of the estate")
    th.assert_true(SHA[:12] in manual[0].message,
                   "the finding must name the commit that is missing")


@th.django_unit_test("a node already at the archived commit costs one command")
def test_checkout_is_idempotent(opts):
    from mojo.deploy.provision import checkout

    run = _Runner([
        ("rev-parse --is-inside-work-tree", (0, "true", "")),
        ("remote get-url origin", (0, f"git@github.com:{REPO}.git", "")),
        ("rev-parse HEAD", (0, SHA, "")),
    ])
    findings = checkout.ensure_checkout(run, HOST, REPO)

    th.assert_eq(run.ran("fetch"), [],
                 "a converged node must not be re-fetched on every configure")
    th.assert_eq(run.ran("reset"), [], "nor reset")
    th.assert_in("checkout.ok", _codes(findings),
                 f"the converged state must be reported: {_codes(findings)}")


@th.django_unit_test("a node the deploy plane moved forward is never rolled back")
def test_checkout_leaves_a_deployed_head_alone(opts):
    from mojo.deploy.provision import checkout

    # The node booted at SHA and has since been deployed to OTHER_SHA. This is
    # the ordinary state of any node that has ever taken a push.
    run = _Runner([
        ("cat /opt/api/var/app_sha", (0, SHA, "")),
        ("rev-parse --is-inside-work-tree", (0, "true", "")),
        ("remote get-url origin", (0, f"git@github.com:{REPO}.git", "")),
        ("rev-parse HEAD", (0, OTHER_SHA, "")),
    ])
    findings = checkout.ensure_checkout(run, HOST, REPO)

    th.assert_eq(run.ran("reset"), [],
                 "var/app_sha names the commit this node BOOTED from. Treating "
                 "it as authoritative forever means a configure run — to "
                 "publish a setting, to renew a certificate — silently rolls a "
                 "deployed node back to the commit it was born at")
    th.assert_eq(run.ran("fetch"), [], "and must not fetch to do it")
    th.assert_true(any(OTHER_SHA[:12] in f.message for f in findings),
                   f"the report must name the commit actually deployed: "
                   f"{[f.message for f in findings]}")


@th.django_unit_test("an unwired tree whose origin is wrong is repointed, then adopted")
def test_checkout_adopts_after_repointing(opts):
    from mojo.deploy.provision import checkout

    run = _Runner([
        ("cat /opt/api/var/app_sha", (0, SHA, "")),
        ("rev-parse --is-inside-work-tree", (0, "true", "")),
        ("remote get-url origin", (0, "git@github.com:Someone/else.git", "")),
        ("rev-parse HEAD", (0, OTHER_SHA, "")),
    ])
    findings = checkout.ensure_checkout(run, HOST, REPO)

    th.assert_eq(run.ran("git init -q /opt/api"), [],
                 "an existing repository must not be re-initialised")
    th.assert_true(run.ran(f"reset --mixed --quiet {SHA}"),
                   "a tree pointing at another repository was never wired by "
                   "this environment, so its HEAD proves nothing")
    th.assert_in("checkout.wired", _codes(findings), str(_codes(findings)))


@th.django_unit_test("an origin pointing elsewhere is repointed, not duplicated")
def test_checkout_repoints_a_wrong_origin(opts):
    from mojo.deploy.provision import checkout

    run = _Runner([
        ("cat /opt/api/var/app_sha", (0, SHA, "")),
        ("rev-parse HEAD", (0, OTHER_SHA, "")),
        ("rev-parse --is-inside-work-tree", (0, "true", "")),
        ("remote get-url origin", (0, "git@github.com:Someone/else.git", "")),
    ])
    checkout.ensure_checkout(run, HOST, REPO)

    th.assert_true(
        run.ran(f"remote set-url origin git@github.com:{REPO}.git"),
        "`remote add` on an existing origin is an error — it must be set-url")
    th.assert_eq(run.ran("remote add origin"), [],
                 "and must not also be added")


@th.django_unit_test("a node that cannot name its commit is reported, not guessed at")
def test_checkout_reports_a_node_without_an_archived_sha(opts):
    from mojo.deploy.provision import checkout, report

    run = _Runner([("cat /opt/api/var/app_sha", (1, "", "No such file"))])
    findings = checkout.ensure_checkout(run, HOST, REPO)

    th.assert_eq(run.ran("git init"), [],
                 "nothing may be wired when the target commit is unknown — "
                 "there is no safe commit to guess")
    th.assert_eq(_codes(findings), ["checkout.no_sha"], str(_codes(findings)))
    th.assert_eq(findings[0].status, report.MANUAL,
                 "an estate that predates app.sha is a MANUAL step, not a "
                 "blind failure")


@th.django_unit_test("a fetch that cannot authenticate names the deploy key, not git")
def test_checkout_blames_the_deploy_key_when_the_fetch_fails(opts):
    from mojo.deploy.provision import checkout, report

    run = _wired_node(
        responses=[("fetch --quiet origin", (128, "", "Permission denied"))])
    findings = checkout.ensure_checkout(run, HOST, REPO)

    th.assert_eq(run.ran("reset"), [],
                 "nothing may be reset against a history that never arrived")
    blind = [f for f in findings if f.status == report.BLIND]
    th.assert_eq(len(blind), 1, f"one failure expected: {_codes(findings)}")
    th.assert_true("settings/keys" in (blind[0].remedy or ""),
                   "the remedy must point at where a human fixes it")


@th.django_unit_test("tracked local edits are reported as drift, untracked noise is not")
def test_checkout_reports_only_tracked_drift(opts):
    from mojo.deploy.provision import checkout, report

    run = _wired_node(responses=[
        ("status --porcelain --untracked-files=no", (0, " M config/x.py", ""))])
    findings = checkout.ensure_checkout(run, HOST, REPO)

    th.assert_true(
        run.ran("status --porcelain --untracked-files=no"),
        "a provisioned node always has untracked var/ output — counting it "
        "would report drift on every run")
    drift = [f for f in findings if f.status == report.DRIFT]
    th.assert_eq(len(drift), 1, f"the tracked edit must be drift: "
                                f"{_codes(findings)}")


@th.django_unit_test("no repository configured means nothing is touched")
def test_checkout_does_nothing_without_a_repo(opts):
    from mojo.deploy.provision import checkout

    run = _Runner()
    findings = checkout.ensure_checkout(run, HOST, "")

    th.assert_eq(run.calls, [], "an unconfigured repo must issue no command")
    th.assert_eq(findings, [], "and report nothing")
