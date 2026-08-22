"""`python3 -m mojo.deploy.provision` — the operator-facing bootstrap CLI.

Everything is driven through `main()` IN PROCESS, with the two seams the CLI was
built to have: a scripted `inputs.Console` instead of `builtins.input`, and
`clients.build_clients` / `plan.observe` / `plan.apply` patched instead of AWS.
Patching a builtin would leak into whatever else the runner is doing in this
interpreter; a real AWS call is not a unit test.

THE TWO ASSERTIONS THAT MATTER MOST, and why they are structural rather than
behavioural:

    `--dry-run` asserts `plan.apply` was NOT CALLED. Not "created nothing" —
    not called at all. A dry run whose safety depends on every ensure function
    honouring an `apply=False` argument is one missed branch away from building
    an Aurora cluster, and no assertion about the account's contents would catch
    that before the bill.

    The infrastructure-mode drift test IMPORTS `mojo.helpers.infrastructure` and
    compares the whole value table. `mojo/deploy/` may not import that module
    (it is under `mojo.helpers`, which needs Django settings), so the rule is
    duplicated in `inputs.py` — and a duplicated fail-closed security rule is
    only safe while something proves the copies agree. A test process HAS
    settings configured, so it is the one place both can be read at once.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr
from unittest import mock

from objict import objict
from testit import helpers as th


# ── harness ─────────────────────────────────────────────────────────────────

BASE_ANSWERS = {
    "project": "demo",
    "env": "prod",
    "region": "us-west-2",
    "apex_domain": "example.com",
    "operator_email": "ops@example.com",
    "preset": "small",
    "github_repo": "acme/demo",
    "admin_cidrs": ["203.0.113.9/32"],
}

# The eight answers, in prompt order, for a full `init` run.
FULL_SCRIPT = ["", "us-west-2", "demo", "prod", "example.com",
               "ops@example.com", "small", "acme/demo", "203.0.113.9/32"]
# The five optional questions, all taken at their (off) defaults.
OPTIONAL_DEFAULTS = ["", "", "", "", ""]


def _tempdir():
    return tempfile.mkdtemp(prefix="testit_provision_cli.")


def _write_env(root, **overrides):
    from mojo.deploy.provision import inputs

    answers = dict(BASE_ANSWERS)
    answers.update(overrides)
    path = inputs.env_path(root, answers["env"])
    inputs.save(path, answers)
    return path


class _Script:
    """A scripted terminal: canned answers in, every line out captured."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.lines = []

    def read(self, prompt):
        self.lines.append(str(prompt))
        if not self.answers:
            raise EOFError(f"the script ran out at {prompt!r}")
        return self.answers.pop(0)

    def write(self, line=""):
        self.lines.append(str(line))

    def console(self, interactive=True):
        from mojo.deploy.provision import inputs
        return inputs.Console(reader=self.read, writer=self.write,
                              interactive=interactive)

    @property
    def text(self):
        return "\n".join(self.lines)


def _fake_run(findings=None, blocking=False, validated=True, steps=None,
              observed=None):
    shape = {"account_id": "123456789012"}
    shape.update(observed or {})
    return objict(
        steps=objict(**(steps or {})), observed=objict(**shape),
        worst="PASS", blocking=blocking, validated=validated, problems=[])


def _stub(findings=None, **run_kwargs):
    findings = list(findings or ())

    def call(clients, spec, steps=None):
        return findings, [], _fake_run(findings=findings, **run_kwargs)
    return call


def _drive(argv, script=None, observe=None, apply_result=None,
           interactive=True, identity=None):
    """Run `main()` with AWS replaced. Returns (code, script, observe, apply)."""
    from mojo.deploy.provision import __main__ as cli
    from mojo.deploy.provision import plan

    script = script or _Script()
    observe_mock = mock.Mock(side_effect=observe or _stub())
    apply_mock = mock.Mock(side_effect=apply_result or _stub())
    stderr = io.StringIO()

    with mock.patch.object(cli.clients_module, "build_clients",
                           return_value=object()), \
            mock.patch.object(cli.clients_module, "identify",
                              return_value=identity or {
                                  "account_id": "123456789012",
                                  "arn": "arn:aws:iam::123456789012:user/ops"}), \
            mock.patch.object(plan, "observe", observe_mock), \
            mock.patch.object(plan, "apply", apply_mock), \
            redirect_stderr(stderr):
        code = cli.main(argv, console=script.console(interactive=interactive))

    script.lines.append(stderr.getvalue())
    return code, script, observe_mock, apply_mock


def _settings_free_env():
    import mojo

    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env["PYTHONPATH"] = os.path.dirname(
        os.path.dirname(os.path.abspath(mojo.__file__)))
    return env


def _subprocess(args):
    return subprocess.run([sys.executable] + args, env=_settings_free_env(),
                          capture_output=True, text=True, timeout=120)


# ── init: the eight questions and the file ──────────────────────────────────

@th.django_unit_test("there are exactly eight questions, and the count is asserted")
def test_there_are_eight_prompts(opts):
    from mojo.deploy.provision import inputs

    th.assert_eq(len(inputs.PROMPTS), 8,
                 f"the bootstrap is specified as eight questions — found "
                 f"{len(inputs.PROMPTS)}: "
                 f"{[q.title for q in inputs.PROMPTS]}")

    keys = [field.key for question in inputs.PROMPTS
            for field in question.fields]
    for expected in ("aws_profile", "region", "project", "env", "apex_domain",
                     "operator_email", "preset", "github_repo", "admin_cidrs"):
        th.assert_in(expected, keys,
                     f"{expected} must be one of the things an operator is "
                     f"asked — the eight prompts are {keys}")


@th.django_unit_test()
def test_init_writes_every_answer_and_round_trips(opts):
    from mojo.deploy.provision import __main__ as cli
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        script = _Script(FULL_SCRIPT + OPTIONAL_DEFAULTS)
        with mock.patch.object(inputs, "egress_cidr", return_value=None):
            code = cli.main(["init", "--project-root", root],
                            console=script.console())

        th.assert_eq(code, 0, f"init must succeed: {script.text}")
        path = inputs.env_path(root, "prod")
        th.assert_true(os.path.isfile(path),
                       f"init must write {path}; wrote nothing")

        answers = inputs.load(path)
        th.assert_eq(answers["project"], "demo",
                     f"the project answer must round-trip: {answers}")
        th.assert_eq(answers["region"], "us-west-2",
                     f"the region answer must round-trip: {answers}")
        th.assert_eq(answers["apex_domain"], "example.com",
                     f"the apex domain must round-trip: {answers}")
        th.assert_eq(answers["operator_email"], "ops@example.com",
                     f"the operator email must round-trip: {answers}")
        th.assert_eq(answers["preset"], "small",
                     f"the size must round-trip: {answers}")
        th.assert_eq(answers["github_repo"], "acme/demo",
                     f"the repository must round-trip: {answers}")
        th.assert_eq(answers["admin_cidrs"], ["203.0.113.9/32"],
                     f"the admin CIDRs must round-trip as a list: {answers}")
        th.assert_eq(answers["schema_version"], inputs.SCHEMA_VERSION,
                     f"the file must stamp its schema version: {answers}")
        th.assert_eq(inputs.problems(answers), [],
                     f"a file init just wrote must validate: {answers}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_init_writes_no_file_other_than_the_env_file(opts):
    """No local secrets file, no cache, no state — anywhere under the project."""
    from mojo.deploy.provision import __main__ as cli
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        script = _Script(FULL_SCRIPT + OPTIONAL_DEFAULTS)
        with mock.patch.object(inputs, "egress_cidr", return_value=None):
            cli.main(["init", "--project-root", root],
                     console=script.console())

        written = [os.path.relpath(os.path.join(directory, name), root)
                   for directory, _subs, files in os.walk(root)
                   for name in files]
        th.assert_eq(sorted(written), [os.path.join("aws", "environments",
                                                    "prod.json")],
                     f"init must create exactly one file, the committed "
                     f"environment declaration — found {written}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_init_prefills_and_preserves_unrecognized_keys(opts):
    from mojo.deploy.provision import __main__ as cli
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        path = _write_env(root)
        # A field a newer django-mojo wrote, which this version cannot name.
        raw = json.loads(open(path).read())
        raw["future_field"] = {"kept": True}
        with open(path, "w") as handle:
            handle.write(json.dumps(raw, indent=2, sort_keys=True) + "\n")

        # Every answer taken at its prefilled default, plus the optionals.
        script = _Script([""] * 9 + OPTIONAL_DEFAULTS)
        code = cli.main(["init", "--project-root", root],
                        console=script.console())
        th.assert_eq(code, 0, f"re-init must succeed: {script.text}")

        answers = inputs.load(path)
        th.assert_eq(answers["project"], "demo",
                     f"pressing Enter must keep the existing answer, not blank "
                     f"it: {answers}")
        th.assert_eq(answers["admin_cidrs"], ["203.0.113.9/32"],
                     f"the existing CIDR list must survive a re-init: "
                     f"{answers}")
        th.assert_eq(answers.get("future_field"), {"kept": True},
                     f"a key this version does not recognize must be carried "
                     f"through, not silently dropped: {answers}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_save_refuses_any_key_outside_the_schema(opts):
    """The allowlist, not a secret-shaped-name denylist."""
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        path = inputs.env_path(root, "prod")
        answers = dict(BASE_ANSWERS)
        answers["aws_secret_access_key"] = "AKIAsomethingsecret"

        raised = None
        try:
            inputs.save(path, answers)
        except inputs.EnvFileError as err:
            raised = err

        th.assert_true(raised is not None,
                       "save() must refuse a key outside the documented "
                       "schema — this file is committed to git")
        th.assert_in("aws_secret_access_key", str(raised),
                     f"the refusal must name the offending key: {raised}")
        th.assert_eq(os.path.exists(path), False,
                     "a refused save must write nothing at all")

        # And an honest-but-unknown field is refused the same way, which is the
        # point of an allowlist: it does not depend on guessing what is secret.
        raised = None
        try:
            inputs.save(path, dict(BASE_ANSWERS, ssh_key_pair_name="ops"))
        except inputs.EnvFileError as err:
            raised = err
        th.assert_true(raised is not None,
                       "an unknown non-secret key must be refused too — "
                       "adding a field is a schema change, made deliberately")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_save_is_a_stable_readable_diff(opts):
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        path = inputs.env_path(root, "prod")
        inputs.save(path, dict(BASE_ANSWERS))
        first = open(path).read()
        inputs.save(path, dict(BASE_ANSWERS))
        second = open(path).read()

        th.assert_eq(first, second,
                     "saving the same answers twice must produce identical "
                     "bytes — this file lives in git")
        th.assert_true(first.endswith("}\n"),
                       f"the file must end with exactly one newline: "
                       f"{first[-10:]!r}")
        keys = [line.split('"')[1] for line in first.splitlines()
                if line.startswith('  "')]
        th.assert_eq(keys, sorted(keys),
                     f"keys must be sorted so a diff is readable: {keys}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── validation, at the keyboard and on a hand-edited file ───────────────────

@th.django_unit_test()
def test_invalid_slug_is_rejected_at_the_prompt_and_reasked(opts):
    from mojo.deploy.provision import __main__ as cli
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        script = _Script(["", "us-west-2", "Demo_Project", "demo", "prod",
                          "example.com", "ops@example.com", "small",
                          "acme/demo", "203.0.113.9/32"] + OPTIONAL_DEFAULTS)
        with mock.patch.object(inputs, "egress_cidr", return_value=None):
            code = cli.main(["init", "--project-root", root],
                            console=script.console())

        th.assert_eq(code, 0, f"init must recover from a bad slug: "
                              f"{script.text}")
        th.assert_in("must be lowercase", script.text,
                     f"the operator must be told WHY the slug was refused, at "
                     f"the keyboard rather than at CreateDBCluster: "
                     f"{script.text}")
        answers = inputs.load(inputs.env_path(root, "prod"))
        th.assert_eq(answers["project"], "demo",
                     f"only the corrected slug may be stored: {answers}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_hand_edited_invalid_file_refuses_apply(opts):
    """The prompt check is not the only check — the file is validated on load."""
    root = _tempdir()
    try:
        path = _write_env(root)
        raw = json.loads(open(path).read())
        # A project slug AWS will reject, edited in by hand past the prompt.
        raw["project"] = "Demo_Project"
        with open(path, "w") as handle:
            handle.write(json.dumps(raw) + "\n")

        code, script, observed, applied = _drive(
            ["apply", "--project-root", root, "--yes"])

        th.assert_eq(code, 2, f"a file AWS would reject must exit 2: "
                              f"{script.text}")
        th.assert_eq(applied.called, False,
                     "nothing may be created from an invalid declaration")
        th.assert_eq(observed.called, False,
                     "an invalid file must be caught before the first AWS "
                     "call, not after a full observe sweep")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_missing_env_file_exits_two_and_names_init(opts):
    root = _tempdir()
    try:
        code, script, _observed, applied = _drive(
            ["apply", "--project-root", root, "--yes"])

        th.assert_eq(code, 2,
                     f"a missing environment file is a usage error, not a "
                     f"traceback: {script.text}")
        th.assert_in("init", script.text,
                     f"the error must name the command that creates the file: "
                     f"{script.text}")
        th.assert_eq(applied.called, False,
                     "nothing may be applied without a declaration")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_unreadable_and_non_json_files_exit_two(opts):
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        path = inputs.env_path(root, "prod")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("this is not json\n")

        code, script, _observed, applied = _drive(
            ["apply", "--project-root", root, "--yes"])
        th.assert_eq(code, 2, f"unparseable JSON must exit 2: {script.text}")
        th.assert_in(path, script.text,
                     f"the error must name the path: {script.text}")
        th.assert_eq(applied.called, False, "nothing may be applied")

        with open(path, "w") as handle:
            handle.write(json.dumps(dict(BASE_ANSWERS, schema_version=99)))
        code, script, _observed, applied = _drive(
            ["apply", "--project-root", root, "--yes"])
        th.assert_eq(code, 2,
                     f"an unknown schema_version must refuse rather than guess "
                     f"what the fields mean: {script.text}")
        th.assert_eq(applied.called, False, "nothing may be applied")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── admin CIDRs ─────────────────────────────────────────────────────────────

@th.django_unit_test()
def test_admin_cidrs_accepts_a_list_and_rejects_a_bare_address(opts):
    from mojo.deploy.provision import inputs

    th.assert_eq(inputs.parse_cidrs("203.0.113.9/32, 198.51.100.0/24"),
                 ["203.0.113.9/32", "198.51.100.0/24"],
                 "a comma-separated CIDR list must parse into normalized "
                 "blocks")
    th.assert_eq(inputs.parse_cidrs(""), [],
                 "blank means SSH is opened to nobody, which is a working "
                 "configuration")

    for bad in ("203.0.113.9", "not-a-cidr/32", "203.0.113.9/64"):
        raised = None
        try:
            inputs.parse_cidrs(bad)
        except ValueError as err:
            raised = err
        th.assert_true(raised is not None,
                       f"{bad!r} must be refused — a rule that decides who "
                       f"reaches port 22 is not a place to guess")


@th.django_unit_test()
def test_malformed_cidr_is_reasked_at_the_prompt(opts):
    from mojo.deploy.provision import __main__ as cli
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        script = _Script(FULL_SCRIPT[:-1] + ["203.0.113.9", "203.0.113.9/32"]
                         + OPTIONAL_DEFAULTS)
        with mock.patch.object(inputs, "egress_cidr", return_value=None):
            code = cli.main(["init", "--project-root", root],
                            console=script.console())

        th.assert_eq(code, 0, f"init must recover from a bad CIDR: "
                              f"{script.text}")
        th.assert_in("prefix length", script.text,
                     f"the operator must be told to write /32 rather than have "
                     f"it guessed for them: {script.text}")
        answers = inputs.load(inputs.env_path(root, "prod"))
        th.assert_eq(answers["admin_cidrs"], ["203.0.113.9/32"],
                     f"only the corrected block may be stored: {answers}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_world_open_ssh_needs_a_second_explicit_confirmation(opts):
    from mojo.deploy.provision import __main__ as cli
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        # First offer 0.0.0.0/0 and decline the extra confirmation, then give a
        # real block. The decline must NOT store the world-open value.
        script = _Script(FULL_SCRIPT[:-1]
                         + ["0.0.0.0/0", "no", "203.0.113.9/32"]
                         + OPTIONAL_DEFAULTS)
        with mock.patch.object(inputs, "egress_cidr", return_value=None):
            code = cli.main(["init", "--project-root", root],
                            console=script.console())

        th.assert_eq(code, 0, f"init must continue after the decline: "
                              f"{script.text}")
        th.assert_in("entire internet", script.text,
                     f"the operator must be told exactly what 0.0.0.0/0 does "
                     f"before they can accept it: {script.text}")
        answers = inputs.load(inputs.env_path(root, "prod"))
        th.assert_eq(answers["admin_cidrs"], ["203.0.113.9/32"],
                     f"a declined confirmation must leave the world-open block "
                     f"unrecorded: {answers}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_world_open_ssh_is_allowed_when_explicitly_confirmed(opts):
    """It is a bad idea, not a forbidden one — refusing outright just moves the
    rule into the console where nobody reviews it."""
    from mojo.deploy.provision import __main__ as cli
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        script = _Script(FULL_SCRIPT[:-1] + ["0.0.0.0/0", "yes"]
                         + OPTIONAL_DEFAULTS)
        with mock.patch.object(inputs, "egress_cidr", return_value=None):
            code = cli.main(["init", "--project-root", root],
                            console=script.console())

        th.assert_eq(code, 0, f"an explicit yes must be honoured: "
                              f"{script.text}")
        answers = inputs.load(inputs.env_path(root, "prod"))
        th.assert_eq(answers["admin_cidrs"], ["0.0.0.0/0"],
                     f"the confirmed block must be stored: {answers}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_admin_cidrs_defaults_to_the_operators_own_address(opts):
    from mojo.deploy.provision import __main__ as cli
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        script = _Script(FULL_SCRIPT[:-1] + [""] + OPTIONAL_DEFAULTS)
        with mock.patch.object(inputs, "egress_cidr",
                               return_value="198.51.100.4/32"):
            code = cli.main(["init", "--project-root", root],
                            console=script.console())

        th.assert_eq(code, 0, f"init must succeed: {script.text}")
        answers = inputs.load(inputs.env_path(root, "prod"))
        th.assert_eq(answers["admin_cidrs"], ["198.51.100.4/32"],
                     f"pressing Enter must take the operator's own egress "
                     f"address as a /32, not open SSH to the world: {answers}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── apply: the gate ─────────────────────────────────────────────────────────

@th.django_unit_test("--dry-run does not reach plan.apply at all")
def test_dry_run_never_reaches_plan_apply(opts):
    root = _tempdir()
    try:
        _write_env(root)
        code, script, observed, applied = _drive(
            ["apply", "--project-root", root, "--dry-run"])

        th.assert_eq(code, 0, f"a dry run must exit 0: {script.text}")
        th.assert_eq(observed.called, True,
                     "a dry run must still observe — the preview is the whole "
                     "point of it")
        th.assert_eq(applied.called, False,
                     "plan.apply must be STRUCTURALLY unreachable on the "
                     "--dry-run path; safety that depends on every ensure "
                     "function honouring apply=False is one missed branch from "
                     "building an Aurora cluster")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_non_interactive_apply_without_yes_exits_two(opts):
    root = _tempdir()
    try:
        _write_env(root)
        code, script, _observed, applied = _drive(
            ["apply", "--project-root", root], interactive=False)

        th.assert_eq(code, 2,
                     f"with nobody to confirm and no --yes, apply must refuse: "
                     f"{script.text}")
        th.assert_eq(applied.called, False,
                     "an unconfirmed apply must create nothing")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_yes_skips_the_confirmation_and_applies(opts):
    root = _tempdir()
    try:
        _write_env(root)
        code, script, _observed, applied = _drive(
            ["apply", "--project-root", root, "--yes"], interactive=False)

        th.assert_eq(code, 0, f"a clean apply must exit 0: {script.text}")
        th.assert_eq(applied.called, True,
                     "--yes is the scripted path and must reach plan.apply")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_a_typed_yes_applies_and_anything_else_does_not(opts):
    root = _tempdir()
    try:
        _write_env(root)

        code, script, _observed, applied = _drive(
            ["apply", "--project-root", root], script=_Script(["yes"]))
        th.assert_eq(code, 0, f"a confirmed apply must exit 0: {script.text}")
        th.assert_eq(applied.called, True,
                     "a literal typed yes must proceed")

        # `y` is not yes. Every confirmation here means "I read the plan", and
        # one keystroke is not evidence of that.
        code, script, _observed, applied = _drive(
            ["apply", "--project-root", root], script=_Script(["y"]))
        th.assert_eq(code, 0,
                     f"declining is not an error: {script.text}")
        th.assert_eq(applied.called, False,
                     "'y' must not be accepted as the confirmation")

        code, script, _observed, applied = _drive(
            ["apply", "--project-root", root], script=_Script(["no"]))
        th.assert_eq(applied.called, False,
                     "an explicit no must create nothing")
        th.assert_in("nothing was created", script.text,
                     f"declining must say so plainly: {script.text}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_pending_and_skipped_are_progress_not_failure(opts):
    """A fresh account takes about three applies. The CLI must say so and exit
    0, because a bootstrap that reported failure when the right advice is "run
    it again in ten minutes" would be worse than useless."""
    from mojo.deploy.provision import plan, report

    root = _tempdir()
    try:
        _write_env(root)
        findings = [report.pending("db", "db.creating", "aurora is creating")]
        steps = {"db": objict(status=plan.PENDING, depends_on=[],
                              blocked_by=[], values={}),
                 "nodes": objict(status=plan.SKIPPED, depends_on=["db"],
                                 blocked_by=["db"], values={})}
        stub = _stub(findings, blocking=False, steps=steps)

        code, script, _observed, applied = _drive(
            ["apply", "--project-root", root, "--yes"],
            observe=stub, apply_result=stub, interactive=False)

        th.assert_eq(code, 0,
                     f"PENDING/SKIPPED is the normal state of a five-minute "
                     f"resource and must exit 0: {script.text}")
        th.assert_eq(applied.called, True, "apply must have run")
        th.assert_in("still coming up", script.text,
                     f"the operator must be told which steps are waiting: "
                     f"{script.text}")
        th.assert_in("again", script.text,
                     f"the one instruction that resolves this — run it again — "
                     f"must be printed: {script.text}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_a_blocking_run_exits_non_zero(opts):
    from mojo.deploy.provision import report

    root = _tempdir()
    try:
        _write_env(root)
        findings = [report.Finding("network", report.BLIND, "vpc.denied",
                                   "ec2:DescribeVpcs was denied", "grant it")]
        stub = _stub(findings, blocking=True)

        code, script, _observed, applied = _drive(
            ["apply", "--project-root", root, "--yes"],
            observe=stub, apply_result=stub, interactive=False)

        th.assert_eq(code, 1,
                     f"a BLIND finding means the converge cannot be trusted "
                     f"and must fail the run: {script.text}")
        th.assert_in("Fix what is reported", script.text,
                     f"the operator must be told what to do: {script.text}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_preview_counts_come_from_the_finding_statuses(opts):
    from mojo.deploy.provision import report

    root = _tempdir()
    try:
        _write_env(root)
        findings = [
            report.existing("network", "vpc.ok", "vpc there"),
            report.existing("network", "igw.ok", "gateway there"),
            report.missing("db", "db.missing", "no cluster", "apply creates it"),
            report.drift("storage", "bucket.versioning", "versioning is off",
                         "apply turns it on"),
        ]
        code, script, _observed, _applied = _drive(
            ["apply", "--project-root", root, "--dry-run"],
            observe=_stub(findings))

        th.assert_eq(code, 0, f"a dry run must exit 0: {script.text}")
        th.assert_in("1 create", script.text,
                     f"one MISSING finding is one thing to create: "
                     f"{script.text}")
        th.assert_in("1 modify", script.text,
                     f"one DRIFT finding is one thing to modify: "
                     f"{script.text}")
        th.assert_in("2 leave", script.text,
                     f"two PASS findings are two things left alone — the "
                     f"number that proves a second apply creates nothing: "
                     f"{script.text}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_cost_table_shows_the_nlb_line_exactly_when_one_will_exist(opts):
    root = _tempdir()
    try:
        _write_env(root, preset="small")
        _code, script, _o, _a = _drive(
            ["apply", "--project-root", root, "--dry-run"])
        th.assert_in("load balancer", script.text,
                     f"a two-node preset builds an NLB, so its cost must be "
                     f"shown: {script.text}")

        shutil.rmtree(root, ignore_errors=True)
        root = _tempdir()
        _write_env(root, preset="micro")
        _code, script, _o, _a = _drive(
            ["apply", "--project-root", root, "--dry-run"])
        th.assert_eq("load balancer" in script.text, False,
                     f"micro builds no balancer, and a cost estimate listing a "
                     f"resource the run will not create is worse than none: "
                     f"{script.text}")

        _code, script, _o, _a = _drive(
            ["apply", "--project-root", root, "--dry-run", "--nlb"])
        th.assert_in("load balancer", script.text,
                     f"--nlb on micro is allowed and must be priced: "
                     f"{script.text}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_the_account_id_is_echoed_before_the_preview(opts):
    root = _tempdir()
    try:
        _write_env(root)
        _code, script, _o, _a = _drive(
            ["apply", "--project-root", root, "--dry-run"])

        th.assert_in("123456789012", script.text,
                     f"the account being changed must be named — 'wrong "
                     f"account' is the mistake a list of resource names cannot "
                     f"reveal: {script.text}")
        header = script.text.index("123456789012")
        cost = script.text.index("approximate monthly cost")
        th.assert_true(header < cost,
                       "the account must be echoed BEFORE the preview, not "
                       "after it")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_keyboard_interrupt_exits_130_without_a_traceback(opts):
    from mojo.deploy.provision import __main__ as cli
    from mojo.deploy.provision import plan

    root = _tempdir()
    try:
        _write_env(root)
        script = _Script()
        stderr = io.StringIO()
        with mock.patch.object(cli.clients_module, "build_clients",
                               return_value=object()), \
                mock.patch.object(cli.clients_module, "identify",
                                  return_value={"account_id": "1", "arn": "a"}), \
                mock.patch.object(plan, "observe",
                                  side_effect=KeyboardInterrupt()), \
                redirect_stderr(stderr):
            code = cli.main(["apply", "--project-root", root, "--yes"],
                            console=script.console())

        th.assert_eq(code, 130,
                     "Ctrl-C is 130 by convention, and must not surface as a "
                     "traceback")
        th.assert_in("re-run", stderr.getvalue(),
                     f"an interrupted run must say how to resume: "
                     f"{stderr.getvalue()}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── external mode ───────────────────────────────────────────────────────────

@th.django_unit_test()
def test_external_mode_refuses_apply(opts):
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        path = _write_env(root, infrastructure_mode=inputs.EXTERNAL)
        code, script, _observed, applied = _drive(
            ["apply", "--project-root", root, "--yes"], interactive=False)

        th.assert_eq(code, 3,
                     f"an external declaration must refuse with its own exit "
                     f"code: {script.text}")
        th.assert_eq(applied.called, False,
                     "an environment owned by an external pipeline must not be "
                     "mutated here")
        th.assert_in(path, script.text,
                     f"the refusal must name the file that declares it: "
                     f"{script.text}")
        th.assert_in("--override-external", script.text,
                     f"the refusal must name the deliberate way past it: "
                     f"{script.text}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_override_external_applies_and_never_touches_the_file(opts):
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        path = _write_env(root, infrastructure_mode=inputs.EXTERNAL)
        before = open(path, "rb").read()

        code, script, _observed, applied = _drive(
            ["apply", "--project-root", root, "--override-external"],
            script=_Script(["yes"]))

        th.assert_eq(code, 0, f"a confirmed override must run: {script.text}")
        th.assert_eq(applied.called, True,
                     "the override exists so one run can proceed")
        th.assert_eq(open(path, "rb").read(), before,
                     "the override is per-invocation and must NEVER edit the "
                     "committed declaration — the environment is still external")
        th.assert_in("THIS RUN ONLY", script.text,
                     f"the acknowledgement must be loud and must say the file "
                     f"is unchanged: {script.text}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test("--override-external and --yes together are refused")
def test_override_external_cannot_be_combined_with_yes(opts):
    from mojo.deploy.provision import inputs

    root = _tempdir()
    try:
        _write_env(root, infrastructure_mode=inputs.EXTERNAL)
        code, script, observed, applied = _drive(
            ["apply", "--project-root", root, "--override-external", "--yes"],
            interactive=False)

        th.assert_eq(code, 3,
                     f"overriding a committed team declaration is a decision "
                     f"taken in front of a terminal, never a flag a pipeline "
                     f"carries: {script.text}")
        th.assert_eq(applied.called, False, "nothing may be created")
        th.assert_eq(observed.called, False,
                     "the combination is answered before anything is read — no "
                     "file content makes it sensible")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_infrastructure_mode_fails_closed(opts):
    from mojo.deploy.provision import inputs

    for raw, expected in ((None, inputs.MANAGED),
                          ("", inputs.MANAGED),
                          ("   ", inputs.MANAGED),
                          ("managed", inputs.MANAGED),
                          ("MANAGED", inputs.MANAGED),
                          (" Managed ", inputs.MANAGED),
                          ("external", inputs.EXTERNAL),
                          ("EXTERNAL", inputs.EXTERNAL),
                          ("External ", inputs.EXTERNAL),
                          ("mangaed", inputs.EXTERNAL),
                          (7, inputs.EXTERNAL),
                          (True, inputs.EXTERNAL),
                          ([], inputs.EXTERNAL)):
        answers = {inputs.MODE_KEY: raw}
        th.assert_eq(inputs.infrastructure_mode(answers), expected,
                     f"{raw!r} must resolve to {expected!r} — a switch whose "
                     f"whole job is to refuse must not be turned off by a typo")

    th.assert_eq(inputs.infrastructure_mode({}), inputs.MANAGED,
                 "an absent key is every existing environment, and must stay "
                 "managed")
    th.assert_eq(inputs.infrastructure_mode(None), inputs.MANAGED,
                 "no answers at all must not raise")


@th.django_unit_test("the duplicated fail-closed rule agrees with the helper")
def test_infrastructure_mode_agrees_with_the_django_helper(opts):
    """`mojo/deploy/` may not import `mojo.helpers.infrastructure`, so the value
    table is duplicated in `inputs.py`. This is the only place both can be read
    in one process, and it is what keeps the copies honest."""
    from mojo.helpers import infrastructure
    from mojo.deploy.provision import inputs

    th.assert_eq(inputs.MANAGED, infrastructure.MANAGED,
                 f"the managed literal must match the helper's: "
                 f"{inputs.MANAGED!r} vs {infrastructure.MANAGED!r}")
    th.assert_eq(inputs.EXTERNAL, infrastructure.EXTERNAL,
                 f"the external literal must match the helper's: "
                 f"{inputs.EXTERNAL!r} vs {infrastructure.EXTERNAL!r}")

    table = (None, "", "   ", "managed", "MANAGED", " Managed ", "external",
             "EXTERNAL", "External ", "mangaed", "extenral", 7, True, [],
             {"a": 1})
    for raw in table:
        theirs = infrastructure.infrastructure_mode(
            reader=lambda key, default, value=raw: value)
        ours = inputs.infrastructure_mode({inputs.MODE_KEY: raw})
        th.assert_eq(ours, theirs,
                     f"the CLI and the portal must agree on {raw!r}: the CLI "
                     f"says {ours!r}, mojo.helpers.infrastructure says "
                     f"{theirs!r} — a drift here means one of them refuses "
                     f"where the other proceeds")


# ── status ──────────────────────────────────────────────────────────────────

@th.django_unit_test()
def test_status_lists_the_tag_scoped_inventory(opts):
    root = _tempdir()
    try:
        _write_env(root)
        observed = {
            "vpc": objict(VpcId="vpc-0abc", Tags=[{"Key": "Name",
                                                   "Value": "demo-prod-vpc"}]),
            "instances": [objict(InstanceId="i-0123",
                                 Tags=[{"Key": "Name", "Value": "demo1"}])],
            "db_cluster": objict(
                DBClusterIdentifier="demo-prod-aurora",
                DBClusterArn="arn:aws:rds:us-west-2:123456789012:cluster:"
                             "demo-prod-aurora"),
            "config_bucket": "demo-prod-config",
        }
        code, script, _observe, _apply = _drive(
            ["status", "--project-root", root, "--list-resources"],
            observe=_stub(observed=observed))

        th.assert_eq(code, 0, f"a clean status must exit 0: {script.text}")
        for expected in ("vpc-0abc", "i-0123", "demo-prod-aurora",
                         "demo-prod-config"):
            th.assert_in(expected, script.text,
                         f"{expected} must appear in the inventory — this "
                         f"listing is the input to a teardown checklist: "
                         f"{script.text}")
        th.assert_in("arn:aws:rds:us-west-2:123456789012:cluster:"
                     "demo-prod-aurora", script.text,
                     f"an ARN must be printed where AWS gives one: "
                     f"{script.text}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_status_exits_non_zero_when_the_credential_is_blind(opts):
    from mojo.deploy.provision import report

    root = _tempdir()
    try:
        _write_env(root)
        findings = [report.Finding("network", report.BLIND, "vpc.denied",
                                   "ec2:DescribeVpcs was denied", "grant it")]
        code, script, _o, _a = _drive(
            ["status", "--project-root", root],
            observe=_stub(findings, blocking=True))

        th.assert_eq(code, 1,
                     f"a status that could not read part of the account must "
                     f"not report success: {script.text}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_status_json_is_machine_readable_and_uncontaminated(opts):
    root = _tempdir()
    try:
        _write_env(root)
        code, script, _o, _a = _drive(
            ["status", "--project-root", root, "--json"])

        th.assert_eq(code, 0, f"a clean status must exit 0: {script.text}")
        payload = json.loads(script.lines[0])
        th.assert_eq(payload["project"], "demo",
                     f"the JSON must name the environment: {payload}")
        th.assert_in("findings", payload,
                     f"the JSON must carry the findings: {sorted(payload)}")
        th.assert_in("resources", payload,
                     f"the JSON must carry the inventory: {sorted(payload)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── the pre-Django contract ─────────────────────────────────────────────────

@th.django_unit_test()
def test_help_works_with_no_django_settings(opts):
    """Exactly the invocation an operator types on a laptop with no project
    installed. This package runs against an EMPTY account, so there is nothing
    for Django settings to come from."""
    done = _subprocess(["-m", "mojo.deploy.provision", "--help"])

    th.assert_eq(done.returncode, 0,
                 f"`python3 -m mojo.deploy.provision --help` must exit 0 with "
                 f"no settings configured.\nstdout: {done.stdout}\n"
                 f"stderr: {done.stderr}")
    th.assert_in("usage:", done.stdout,
                 f"argparse usage must be printed: {done.stdout!r}")
    th.assert_in("mojo.deploy.provision", done.stdout,
                 f"prog must name the -m invocation an operator can copy: "
                 f"{done.stdout!r}")
    th.assert_eq(done.stderr.strip(), "",
                 f"--help must be silent on stderr: {done.stderr!r}")


@th.django_unit_test()
def test_the_cli_never_leaves_mojo_helpers_logit_imported(opts):
    """`mojo.helpers.logit` reads a path attribute that only exists once
    settings are configured, so importing it anywhere under `mojo/deploy/`
    breaks the tool on the exact machine it is meant to run on."""
    done = _subprocess(["-c", (
        "import sys, mojo.deploy.provision.inputs, "
        "mojo.deploy.provision.clients, mojo.deploy.provision.__main__; "
        "print('logit' if 'mojo.helpers.logit' in sys.modules else 'clean')")])

    th.assert_eq(done.returncode, 0,
                 f"the probe itself must run: {done.stderr}")
    th.assert_eq(done.stdout.strip(), "clean",
                 "mojo.helpers.logit must not survive importing the CLI with "
                 "no settings configured — mojo.helpers.* is off-limits inside "
                 "mojo/deploy/")


@th.django_unit_test()
def test_main_has_no_module_level_side_effects(opts):
    """Importing `__main__` must not print, prompt or reach the network.

    The package's import-isolation walk imports modules under `mojo/deploy/` to
    prove they work settings-free, and anything that executed at import time
    would fire during that check — or, worse, during a `python3 -m` of a
    sibling module.
    """
    done = _subprocess(["-c", "import mojo.deploy.provision.__main__"])

    th.assert_eq(done.returncode, 0,
                 f"importing the CLI module must succeed with no settings: "
                 f"{done.stderr}")
    th.assert_eq(done.stdout, "",
                 f"importing the CLI must print nothing — every module-level "
                 f"statement is an import, a constant or a def: "
                 f"{done.stdout!r}")
    th.assert_eq(done.stderr.strip(), "",
                 f"importing the CLI must be silent on stderr: "
                 f"{done.stderr!r}")


# ── the credential factory ──────────────────────────────────────────────────

@th.django_unit_test()
def test_profile_and_role_arn_are_mutually_exclusive(opts):
    from mojo.deploy.provision import clients

    raised = None
    try:
        clients.build_session(profile="ops", role_arn="arn:aws:iam::1:role/x")
    except clients.CredentialError as err:
        raised = err

    th.assert_true(raised is not None,
                   "naming two different credentials must be an error, not a "
                   "silent pick — they answer to different people")
    th.assert_in("--profile", str(raised),
                 f"the error must name the flags involved: {raised}")


@th.django_unit_test()
def test_identify_raises_rather_than_returning_an_unnamed_account(opts):
    from mojo.deploy.provision import clients

    connection = mock.Mock()
    connection.get.return_value.get_caller_identity.return_value = {}

    raised = None
    try:
        clients.identify(connection)
    except clients.CredentialError as err:
        raised = err

    th.assert_true(raised is not None,
                   "an identity call that answers without an account id must "
                   "stop the run — provisioning into an account that cannot be "
                   "named is the mistake the echo exists to prevent")


@th.django_unit_test()
def test_identify_returns_the_account_id(opts):
    from mojo.deploy.provision import clients

    connection = mock.Mock()
    connection.get.return_value.get_caller_identity.return_value = {
        "Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/ops",
        "UserId": "AIDA"}

    identity = clients.identify(connection)
    th.assert_eq(identity["account_id"], "123456789012",
                 f"the account id must be returned for the preview header: "
                 f"{identity}")
    th.assert_eq(identity["arn"], "arn:aws:iam::123456789012:user/ops",
                 f"the caller ARN must be returned so an operator can see "
                 f"WHICH credential this is: {identity}")


# ── answers → spec ──────────────────────────────────────────────────────────

@th.django_unit_test()
def test_optional_answers_are_additive_on_top_of_the_preset(opts):
    from mojo.deploy.provision import inputs

    plain = inputs.to_spec(dict(BASE_ANSWERS, preset="small"))
    th.assert_eq(plain.db_readers, 1,
                 f"the small preset already includes a reader: "
                 f"{plain.db_readers}")

    asked = inputs.to_spec(dict(BASE_ANSWERS, preset="small", reader=True,
                                replica=True))
    th.assert_eq(asked.db_readers, 1,
                 "an opt-in reader on a preset that already has one must not "
                 "double it")

    grown = inputs.to_spec(dict(BASE_ANSWERS, preset="micro", reader=True,
                                replica=True))
    th.assert_eq(grown.db_readers, 1,
                 f"asking for a reader on micro must add one: "
                 f"{grown.db_readers}")
    th.assert_eq(grown.cache_replicas, 1,
                 f"asking for a replica on micro must add one: "
                 f"{grown.cache_replicas}")

    off = inputs.to_spec(dict(BASE_ANSWERS, preset="small", reader=False))
    th.assert_eq(off.db_readers, 1,
                 "declining the OPT-IN must never remove what the preset asked "
                 "for — an opt-in that silently downgrades is a trap")


@th.django_unit_test()
def test_the_spec_carries_the_answers_that_change_aws(opts):
    from mojo.deploy.provision import inputs, spec as spec_module

    built = inputs.to_spec(dict(BASE_ANSWERS, backups_days=35,
                                route53_zone=True), nlb=True)

    th.assert_eq(built.admin_cidrs, ["203.0.113.9/32"],
                 f"the admin CIDRs must reach the security group rule: "
                 f"{built.admin_cidrs}")
    th.assert_eq(built.domain, "example.com",
                 f"the apex domain must reach the DNS step: {built.domain}")
    th.assert_eq(built.create_zone, True,
                 "the zone opt-in must reach the DNS step")
    th.assert_eq(built.db_retention_days, 35,
                 f"the backup tier must reach Aurora: "
                 f"{built.db_retention_days}")
    th.assert_eq(spec_module.wants_balancer(built), True,
                 "--nlb must reach the balancer decision")
