"""mojo.deploy.node_role — the sealed role authority, the manifest, the CLI.

Three separable contracts, all fail-closed:

    read_sealed()  the root-sealed /etc/mojo/deploy-role.conf reader, with the
                   same discipline as the request-service authority: anything
                   that is not a root-owned 0600 single-link regular file
                   holding exactly one MOJO_NODE_ROLE=<role> line is REFUSED,
                   never guessed.
    resolve()      sealed > NODE_ROLE > var/bootstrap.conf, and the bootstrap
                   read that must never let a credential line out.
    read_manifest()/foreign()  the aws/node_roles.conf grammar and the
                   "owned by another role" answer the deploy plane deletes on.

The library functions run in-process against a temp `--etc` (the harness seam
checks ownership against the running uid off /etc/mojo); the CLI is exercised
as a real settings-free subprocess, which is how a node invokes it.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

from testit import helpers as th


def _repo_root():
    import mojo
    return os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))


def _settings_free_env():
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env.pop("NODE_ROLE", None)
    env["PYTHONPATH"] = _repo_root()
    return env


def _run(args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "mojo.deploy.node_role"] + args,
        env=env or _settings_free_env(), capture_output=True, text=True,
        timeout=120)


def _tree():
    """(root, etc, proj, repo) — a throwaway node shape, dirs created."""
    root = tempfile.mkdtemp(prefix="testit_node_role.")
    etc = os.path.join(root, "etc")
    proj = os.path.join(root, "proj")
    repo = os.path.join(root, "repo")
    os.makedirs(etc, 0o755)
    os.makedirs(os.path.join(proj, "var"))
    os.makedirs(os.path.join(repo, "aws"))
    return root, etc, proj, repo


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def _write_sealed(etc, text, mode=0o600):
    return _write(os.path.join(etc, "deploy-role.conf"), text, mode)


def _refusal(function, *args, **kwargs):
    """Run something expected to raise NodeRoleError; return its message."""
    from mojo.deploy.node_role import NodeRoleError

    try:
        result = function(*args, **kwargs)
    except NodeRoleError as err:
        return str(err)
    th.assert_true(False,
                   f"{getattr(function, '__name__', function)} must refuse "
                   f"this input, it returned {result!r}")


# ---------------------------------------------------------------------------
# the sealed authority
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_sealed_reader_accepts_exactly_one_valid_line(opts):
    from mojo.deploy import node_role as nr

    root, etc, _, _ = _tree()
    try:
        th.assert_eq(nr.read_sealed(etc), "",
                     "an unsealed node is unlabeled, not an error — the "
                     "absent file must read as empty")

        _write_sealed(etc, "MOJO_NODE_ROLE=api\n")
        th.assert_eq(nr.read_sealed(etc), "api",
                     "a well-formed sealed authority must resolve its role")

        _write_sealed(etc, "MOJO_NODE_ROLE=worker.eu-1_2\n")
        th.assert_eq(nr.read_sealed(etc), "worker.eu-1_2",
                     "dots, dashes and underscores are valid inside a role")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sealed_reader_refuses_every_unsafe_shape(opts):
    from mojo.deploy import node_role as nr

    root, etc, _, _ = _tree()
    try:
        _write_sealed(etc, "MOJO_NODE_ROLE=api\n", 0o644)
        th.assert_in("0600", _refusal(nr.read_sealed, etc),
                     "a world-readable authority must be refused by mode")

        _write_sealed(etc, "MOJO_NODE_ROLE=api\n")
        os.link(os.path.join(etc, "deploy-role.conf"),
                os.path.join(etc, "second-link"))
        th.assert_in("one link", _refusal(nr.read_sealed, etc),
                     "a second hard link means another party can rewrite the "
                     "authority in place — it must be refused")
        os.unlink(os.path.join(etc, "second-link"))

        _write_sealed(etc, "MOJO_NODE_ROLE=api\nMOJO_NODE_ROLE=worker\n")
        th.assert_in("exactly one", _refusal(nr.read_sealed, etc),
                     "two declarations must be refused, never first-wins")

        _write_sealed(etc, "MOJO_NODE_ROLE=API\n")
        th.assert_in("valid role identifier", _refusal(nr.read_sealed, etc),
                     "a role outside the grammar must be refused")

        _write_sealed(etc, "MOJO_NODE_ROLE=" + "a" * 400 + "\n")
        th.assert_in("oversized", _refusal(nr.read_sealed, etc),
                     "the bounded read must refuse an oversized authority")

        path = os.path.join(etc, "deploy-role.conf")
        os.unlink(path)
        with open(path, "wb") as handle:
            handle.write(b"MOJO_NODE_ROLE=\xff\n")
        os.chmod(path, 0o600)
        th.assert_in("ASCII", _refusal(nr.read_sealed, etc),
                     "non-ASCII bytes must be refused, not decoded loosely")

        os.unlink(path)
        _write(os.path.join(etc, "elsewhere.conf"), "MOJO_NODE_ROLE=api\n",
               0o600)
        os.symlink(os.path.join(etc, "elsewhere.conf"), path)
        th.assert_in("cannot open", _refusal(nr.read_sealed, etc),
                     "a symlinked leaf must never be followed")
        os.unlink(path)

        _write_sealed(etc, "MOJO_NODE_ROLE=api\n")
        os.chmod(etc, 0o777)
        th.assert_in("group/world writable", _refusal(nr.read_sealed, etc),
                     "a writable authority directory means anyone can swap "
                     "the file — the whole path must be refused")
        os.chmod(etc, 0o755)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_expected_uid_is_root_only_for_the_real_authority(opts):
    from mojo.deploy import node_role as nr

    th.assert_eq(nr.expected_uid("/etc/mojo"), 0,
                 "the production authority must be graded against uid 0 — the "
                 "harness seam may never loosen the real path")
    th.assert_eq(nr.expected_uid("/etc/mojo/"), 0,
                 "a trailing slash is the same production path")
    th.assert_eq(nr.expected_uid("/tmp/whatever"), os.geteuid(),
                 "a redirected authority is graded against the running uid, "
                 "which is what makes the unit harness possible at all")


@th.django_unit_test()
def test_seal_writes_0600_atomically_and_is_idempotent(opts):
    from mojo.deploy import node_role as nr

    root, etc, _, _ = _tree()
    try:
        th.assert_eq(nr.seal("api", etc=etc), True,
                     "the first seal must report that it changed the node")
        path = os.path.join(etc, "deploy-role.conf")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        th.assert_eq(mode, 0o600,
                     f"the sealed authority must land 0600, got {oct(mode)}")
        with open(path) as handle:
            th.assert_eq(handle.read(), "MOJO_NODE_ROLE=api\n",
                         "the sealed authority is exactly one key=value line")

        before = os.stat(path).st_ino
        th.assert_eq(nr.seal("api", etc=etc), False,
                     "re-sealing an identical role must be a no-op — every "
                     "deploy calls this and a rewrite would be a host mutation")
        th.assert_eq(os.stat(path).st_ino, before,
                     "an idempotent seal must not even replace the inode")

        th.assert_eq(nr.seal("worker", etc=etc), True,
                     "re-labeling a node must rewrite the authority")
        th.assert_eq(nr.read_sealed(etc), "worker",
                     "the re-labeled role must read back")
        leftovers = [n for n in os.listdir(etc) if n.endswith(".tmp")]
        th.assert_eq(leftovers, [],
                     f"the staged file must be renamed into place, never left "
                     f"behind: {leftovers}")

        th.assert_in("invalid role", _refusal(nr.seal, "API", etc=etc),
                     "seal must refuse a role the grammar rejects")

        os.chmod(etc, 0o777)
        th.assert_in("group/world writable", _refusal(nr.seal, "api", etc=etc),
                     "sealing into an unsafe directory must be refused, not "
                     "attempted")
        os.chmod(etc, 0o755)

        fresh = os.path.join(root, "fresh-etc")
        th.assert_eq(nr.seal("api", etc=fresh), True,
                     "an absent authority directory is created, not fatal")
        directory_mode = stat.S_IMODE(os.stat(fresh).st_mode)
        th.assert_eq(directory_mode, 0o755,
                     f"the created authority directory must be 0755, got "
                     f"{oct(directory_mode)}")
        th.assert_eq(nr.read_sealed(fresh), "api",
                     "the freshly created authority must read back")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# resolution order and the bootstrap read
# ---------------------------------------------------------------------------

SECRET_BOOTSTRAP = (
    "AWS_REGION=us-west-2\n"
    "AWS_KEY=AKIAEXAMPLEKEY\n"
    "AWS_SECRET=sup3r-s3cret-value\n"
    "MOJO_NODE_ROLE='worker'\n"
    "CONFIG_SYNC_OWNER=ec2-user:www\n"
)


@th.django_unit_test()
def test_resolution_order_is_sealed_then_env_then_bootstrap(opts):
    from mojo.deploy import node_role as nr

    root, etc, proj, _ = _tree()
    try:
        th.assert_eq(nr.resolve(proj, etc=etc, environ={}), ("", "none"),
                     "a node no authority names has no role and says so")

        _write(os.path.join(proj, "var", "bootstrap.conf"), SECRET_BOOTSTRAP)
        th.assert_eq(nr.resolve(proj, etc=etc, environ={}), ("worker", "bootstrap"),
                     "with nothing else declared the provisioned bootstrap "
                     "role decides, and quotes are stripped")

        th.assert_eq(nr.resolve(proj, etc=etc, environ={"NODE_ROLE": "edge"}),
                     ("edge", "env"),
                     "the shim's export outranks bootstrap.conf")

        _write_sealed(etc, "MOJO_NODE_ROLE=api\n")
        th.assert_eq(nr.resolve(proj, etc=etc, environ={"NODE_ROLE": "edge"}),
                     ("api", "sealed"),
                     "the sealed authority outranks everything — it is the "
                     "only one an application on the node cannot influence")

        th.assert_in("NODE_ROLE is not a valid",
                     _refusal(nr.resolve, proj, etc=os.path.join(root, "gone"),
                              environ={"NODE_ROLE": "../etc"}),
                     "a malformed environment role is refused, never used")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_bootstrap_read_never_lets_a_credential_out(opts):
    from mojo.deploy import node_role as nr

    root, _, proj, _ = _tree()
    try:
        path = os.path.join(proj, "var", "bootstrap.conf")
        th.assert_eq(nr.read_bootstrap_role(proj), "",
                     "an absent bootstrap.conf is undeclared, not an error")

        _write(path, SECRET_BOOTSTRAP)
        th.assert_eq(nr.read_bootstrap_role(proj), "worker",
                     "the role is extracted with its quotes stripped")

        _write(path, "AWS_SECRET=sup3r-s3cret-value\n")
        th.assert_eq(nr.read_bootstrap_role(proj), "",
                     "a bootstrap.conf that declares no role is undeclared — "
                     "no other key may ever answer for MOJO_NODE_ROLE")

        _write(path, "# MOJO_NODE_ROLE=api\nAWS_SECRET=s\n")
        th.assert_eq(nr.read_bootstrap_role(proj), "",
                     "a commented declaration is not a declaration")

        _write(path, "AWS_SECRET=s\nMOJO_NODE_ROLE=None\n")
        message = _refusal(nr.read_bootstrap_role, proj)
        th.assert_in("not a valid role identifier", message,
                     "a value outside the grammar must be refused")
        th.assert_true("None" not in message and "sup3r" not in message
                       and "AWS_SECRET" not in message,
                       f"the refusal must not echo one byte of the file — it "
                       f"holds every downstream credential: {message}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# the manifest grammar
# ---------------------------------------------------------------------------

MANIFEST = """\
# which converged files belong to which role
api        conf.d/api.conf
api        cron.d/5_api_reports
worker     conf.d/worker.conf
worker     systemd/worker-drain.timer
worker\tcron.d/5_api_reports   # a shared name, listed under both
edge
"""


@th.django_unit_test()
def test_manifest_grammar_and_shared_names(opts):
    from mojo.deploy import node_role as nr

    root, _, _, repo = _tree()
    try:
        th.assert_eq(nr.read_manifest(repo), {},
                     "no manifest means no roles — every converged name stays "
                     "shared, exactly as before this feature existed")

        _write(os.path.join(repo, "aws", "node_roles.conf"), MANIFEST)
        manifest = nr.read_manifest(repo)
        th.assert_eq(sorted(manifest), ["api", "edge", "worker"],
                     f"every declared role must appear, including one with no "
                     f"owned files: {sorted(manifest)}")
        th.assert_eq(manifest["api"],
                     {"conf.d/api.conf", "cron.d/5_api_reports"},
                     f"api's owned set must parse exactly: {manifest['api']}")
        th.assert_eq(manifest["edge"], set(),
                     "a bare role line declares the role and owns nothing")
        th.assert_in("cron.d/5_api_reports", manifest["worker"],
                     "a name listed under two roles belongs to both")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_manifest_refuses_every_violation(opts):
    from mojo.deploy import node_role as nr

    root, _, _, repo = _tree()
    path = os.path.join(repo, "aws", "node_roles.conf")
    try:
        cases = (
            ("api logrotate.d/mojo\n", "conf.d/<name>",
             "an unknown kind must be refused"),
            ("api conf.d/../../etc/passwd\n", "plain file name",
             "a traversal-shaped name must be refused"),
            ("api conf.d/..\n", "plain file name",
             "a dot-dot name must be refused"),
            ("api conf.d/sub/dir.conf\n", "plain file name",
             "a nested name must be refused"),
            ("API conf.d/api.conf\n", "valid role identifier",
             "a role outside the grammar must be refused"),
            ("api conf.d/api.conf extra\n", "expected",
             "a third field must be refused rather than ignored"),
            ("api conf.d/00_django_mojo_runtime.conf\n", "package-owned",
             "the runtime fragment is converged after the install loop — "
             "role removal would delete what the deploy just installed"),
            ("api conf.d/00_mojosec.conf\n", "package-owned",
             "the MojoSec fragment is package-owned, never role-scoped"),
            ("api systemd/mojo-asgi.service\n", "package-owned",
             "the ASGI unit's lifecycle belongs to the sealed request-service "
             "authority, not to a role manifest"),
        )
        for text, fragment, why in cases:
            _write(path, text)
            th.assert_in(fragment, _refusal(nr.read_manifest, repo), why)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_foreign_answers_per_kind_and_refuses_an_undeclared_role(opts):
    from mojo.deploy import node_role as nr

    root, _, _, repo = _tree()
    try:
        _write(os.path.join(repo, "aws", "node_roles.conf"), MANIFEST)
        manifest = nr.read_manifest(repo)

        th.assert_eq(nr.foreign(manifest, "api", "conf.d"), ["worker.conf"],
                     "the other role's vhost is foreign to api")
        th.assert_eq(nr.foreign(manifest, "api", "cron.d"), [],
                     "a cron both roles list is never foreign to either")
        th.assert_eq(nr.foreign(manifest, "api", "systemd"),
                     ["worker-drain.timer"],
                     "systemd is answered separately from cron")
        th.assert_eq(nr.foreign(manifest, "worker", "conf.d"), ["api.conf"],
                     "foreignness is symmetric across roles")
        th.assert_eq(nr.foreign(manifest, "edge", "conf.d"),
                     ["api.conf", "worker.conf"],
                     "a role that owns nothing sheds every listed name")
        th.assert_eq(nr.foreign({}, "api", "conf.d"), [],
                     "with no manifest nothing is foreign to anybody")

        th.assert_in("not declared", _refusal(nr.foreign, manifest, "ghost",
                                              "conf.d"),
                     "a role the manifest never declares must fail closed — "
                     "silently treating every listed name as foreign would "
                     "strip the node bare")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# the CLI — how a node actually calls this
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_cli_resolve_prints_one_token_or_nothing(opts):
    root, etc, proj, _ = _tree()
    try:
        done = _run(["resolve", "--project-path", proj, "--etc", etc])
        th.assert_eq(done.returncode, 0,
                     f"an unlabeled node is not a CLI error: {done.stderr}")
        th.assert_eq(done.stdout.strip(), "",
                     f"an unlabeled node prints nothing — post_deploy's empty "
                     f"check depends on it: {done.stdout!r}")

        _write(os.path.join(proj, "var", "bootstrap.conf"), SECRET_BOOTSTRAP)
        done = _run(["resolve", "--project-path", proj, "--etc", etc])
        th.assert_eq(done.returncode, 0, f"resolve must exit 0: {done.stderr}")
        th.assert_eq(done.stdout.strip(), "worker",
                     f"plain resolve prints exactly the role: {done.stdout!r}")
        th.assert_eq(done.stderr.strip(), "",
                     f"a successful resolve is silent on stderr: {done.stderr!r}")

        done = _run(["resolve", "--project-path", proj, "--etc", etc, "--json"])
        payload = json.loads(done.stdout)
        th.assert_eq(payload,
                     {"role": "worker", "source": "bootstrap", "sealed": "",
                      "env": "", "bootstrap": "worker"},
                     f"--json reports each authority's own answer: {payload}")
        th.assert_true("sup3r" not in done.stdout and "AKIA" not in done.stdout,
                       "no byte of bootstrap.conf may reach the JSON report")

        env = _settings_free_env()
        env["NODE_ROLE"] = "edge"
        done = _run(["resolve", "--project-path", proj, "--etc", etc, "--json"],
                    env=env)
        payload = json.loads(done.stdout)
        th.assert_eq((payload["role"], payload["source"], payload["env"]),
                     ("edge", "env", "edge"),
                     f"the environment authority is reported and ranked: {payload}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_cli_seal_then_resolve_reads_the_seal(opts):
    root, etc, proj, _ = _tree()
    try:
        _write(os.path.join(proj, "var", "bootstrap.conf"), SECRET_BOOTSTRAP)
        done = _run(["seal", "--project-path", proj, "--role", "api",
                     "--etc", etc])
        th.assert_eq(done.returncode, 0, f"seal must exit 0: {done.stderr}")

        done = _run(["resolve", "--project-path", proj, "--etc", etc, "--json"])
        payload = json.loads(done.stdout)
        th.assert_eq((payload["role"], payload["source"], payload["sealed"],
                      payload["bootstrap"]),
                     ("api", "sealed", "api", "worker"),
                     f"the seal wins over the bootstrap value it disagrees "
                     f"with, and both are reported: {payload}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_cli_refusals_are_one_stderr_line_and_exit_2(opts):
    root, etc, proj, repo = _tree()
    try:
        _write(os.path.join(repo, "aws", "node_roles.conf"), MANIFEST)

        done = _run(["foreign", "--project-path", repo, "--role", "api",
                     "--kind", "conf.d", "--etc", etc])
        th.assert_eq(done.returncode, 0, f"foreign must exit 0: {done.stderr}")
        th.assert_eq(done.stdout.split(), ["worker.conf"],
                     f"foreign prints one name per line: {done.stdout!r}")

        done = _run(["foreign", "--project-path", repo, "--role", "ghost",
                     "--kind", "conf.d", "--etc", etc])
        th.assert_eq(done.returncode, 2,
                     "an undeclared role must exit 2 — post_deploy dies on it")
        th.assert_eq(len(done.stderr.strip().splitlines()), 1,
                     f"exactly one stderr line, never a traceback: "
                     f"{done.stderr!r}")
        th.assert_eq(done.stdout, "",
                     f"nothing may reach stdout on a refusal — the caller "
                     f"deletes files from it: {done.stdout!r}")

        _write(os.path.join(repo, "aws", "node_roles.conf"),
               "api logrotate.d/mojo\n")
        done = _run(["foreign", "--project-path", repo, "--role", "api",
                     "--kind", "cron.d", "--etc", etc])
        th.assert_eq(done.returncode, 2,
                     "a manifest the grammar refuses must exit 2")
        th.assert_eq(len(done.stderr.strip().splitlines()), 1,
                     f"exactly one stderr line: {done.stderr!r}")

        _write_sealed(etc, "MOJO_NODE_ROLE=api\n", 0o666)
        done = _run(["resolve", "--project-path", proj, "--etc", etc])
        th.assert_eq(done.returncode, 2,
                     "an unsafe sealed authority must exit 2, never resolve")
        th.assert_eq(done.stdout, "",
                     f"a refused resolve prints no role: {done.stdout!r}")
        th.assert_in("node-role:", done.stderr,
                     f"the refusal names the tool: {done.stderr!r}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
