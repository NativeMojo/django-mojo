#!/usr/bin/env python3
"""Which role this node plays, and which converged files each role owns.

The deploy plane converges ONE set of files onto every node of a project: the
rendered cron/systemd contract plus every vhost in `aws/nginx/conf.d/`. That is
exactly right for a fleet of identical API boxes and wrong for every project
whose repo describes more than one kind of box — the worker node gets the API
vhost, the API node gets the worker's cron, and the only escape has been a
second repo or a hand-managed node.

This module teaches the pass two things:

    - WHICH ROLE THIS NODE IS, from a root-sealed authority
      (`/etc/mojo/deploy-role.conf`) with the same reader discipline as the
      brownfield request-service authority in `request_service.py`: validate the
      real parent directory, open the leaf through its descriptor with
      O_NOFOLLOW, require a root-owned regular 0600 single-link file, and read
      exactly one `MOJO_NODE_ROLE=<role>` line. Anything malformed is refused,
      never guessed.

    - WHICH FILES EACH ROLE OWNS, from `<repo>/aws/node_roles.conf`:

          # role            owned converged file
          api               conf.d/api.conf
          api               cron.d/5_api_reports
          worker            conf.d/worker.conf
          worker            systemd/worker-drain.timer

      A converged name nobody lists is SHARED — every role keeps it. A name
      listed under some role is installed only on nodes of a role that lists
      it, and actively removed from the others.

Three names are refused at parse time, so a manifest can never take them:
`conf.d/00_django_mojo_runtime.conf` and `conf.d/00_mojosec.conf` are
package-owned and converged after the install loop (role removal would delete
what the deploy just installed), and `systemd/mojo-asgi.service` has its
lifecycle decided by the sealed request-service authority instead.

    python3 -m mojo.deploy.node_role resolve --project-path /opt/api
    python3 -m mojo.deploy.node_role resolve --project-path /opt/api --json
    python3 -m mojo.deploy.node_role seal --role api
    python3 -m mojo.deploy.node_role foreign --project-path /opt/api \\
        --role api --kind conf.d

`resolve` answers from the first authority that has an answer: the sealed file,
then the `NODE_ROLE` environment export (the project's shim), then
`MOJO_NODE_ROLE` in `var/bootstrap.conf`. The bootstrap file is NEVER sourced
or echoed — it carries AWS credentials — it is scanned line by line and only a
value matching the role grammar is ever returned.

Pre-Django by contract, like every module in this package: stdlib only, no
`mojo.helpers`. See mojo/deploy/__init__.py.
"""

import argparse
import json
import os
import re
import secrets
import stat
import sys


# Same shape as EDGE_NODE_ID's validator (mojo/apps/edge/settings_validators.py):
# 1-63 chars, lowercase, must start alphanumeric. Every role identifier that
# leaves this module — return value, JSON field, log line — has passed it.
ROLE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

ETC_DIR = "/etc/mojo"
# Deliberately NOT node-role.json: that name is already taken by the opaque
# app-owned role document stage 0 installs. This is the deploy plane's own
# one-line authority and the two must never be confused.
SEALED_NAME = "deploy-role.conf"
SEALED_KEY = "MOJO_NODE_ROLE"
ENV_NAME = "NODE_ROLE"

MAX_SEALED_BYTES = 128
MAX_BOOTSTRAP_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 64 * 1024

MANIFEST_REL = os.path.join("aws", "node_roles.conf")
BOOTSTRAP_REL = os.path.join("var", "bootstrap.conf")

KINDS = ("conf.d", "cron.d", "systemd")

# Names a manifest may not claim for a role — see the module docstring.
REFUSED_NAMES = {
    "conf.d": ("00_django_mojo_runtime.conf", "00_mojosec.conf"),
    "cron.d": (),
    "systemd": ("mojo-asgi.service",),
}


class NodeRoleError(RuntimeError):
    pass


def expected_uid(etc):
    """Root owns the real authority; a harness rooted elsewhere owns its own.

    The same seam `request_service`'s vhost validator uses: a test tree under
    /tmp is checked against the running uid, /etc/mojo against uid 0. It can
    never loosen the production path, because the production path is the
    literal string this compares against.
    """
    return 0 if os.path.abspath(etc) == ETC_DIR else os.geteuid()


def _dir_flags():
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    return flags


def _leaf_flags():
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _open_authority_dir(etc, required_uid):
    """Open and validate the authority directory, or None when it is absent."""
    try:
        parent = os.open(os.path.abspath(etc), _dir_flags())
    except FileNotFoundError:
        return None
    except OSError as err:
        raise NodeRoleError(f"cannot open node-role authority directory: {err}")
    try:
        details = os.fstat(parent)
        if not stat.S_ISDIR(details.st_mode):
            raise NodeRoleError("node-role authority parent is not a directory")
        if details.st_uid != required_uid or stat.S_IMODE(details.st_mode) & 0o022:
            raise NodeRoleError(
                "node-role authority parent must be root-owned and not "
                "group/world writable")
    except BaseException:
        os.close(parent)
        raise
    return parent


def read_sealed(etc=ETC_DIR):
    """The sealed role, or "" when nothing is sealed. Malformed = refused.

    Fail-closed, byte-for-byte the discipline `request_service.read()` uses:
    the parent directory is validated before the leaf is opened through its
    descriptor, so no symlink or directory swap between the two checks can
    redirect the read.
    """
    required_uid = expected_uid(etc)
    parent = _open_authority_dir(etc, required_uid)
    if parent is None:
        return ""
    try:
        try:
            descriptor = os.open(SEALED_NAME, _leaf_flags(), dir_fd=parent)
        except FileNotFoundError:
            return ""
        except OSError as err:
            raise NodeRoleError(f"cannot open the sealed node role: {err}")
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise NodeRoleError("the sealed node role is not a regular file")
            if details.st_uid != required_uid or \
                    stat.S_IMODE(details.st_mode) != 0o600:
                raise NodeRoleError(
                    "the sealed node role must be root-owned mode 0600")
            if details.st_nlink != 1:
                raise NodeRoleError(
                    "the sealed node role must have exactly one link")
            body = os.read(descriptor, MAX_SEALED_BYTES + 1)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)

    if len(body) > MAX_SEALED_BYTES:
        raise NodeRoleError("the sealed node role is oversized")
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError:
        raise NodeRoleError("the sealed node role is not ASCII")
    prefix = SEALED_KEY + "="
    if not text.startswith(prefix) or not text.endswith("\n") or \
            text.count("\n") != 1:
        raise NodeRoleError(
            f"the sealed node role must be exactly one {prefix}<role> line")
    role = text[len(prefix):-1]
    if not ROLE.fullmatch(role):
        raise NodeRoleError(
            "the sealed node role is not a valid role identifier")
    return role


def seal(role, etc=ETC_DIR):
    """Write the sealed authority atomically. True when it changed anything.

    Idempotent: a byte-identical, correctly-owned, correctly-moded file is left
    exactly as it is, so re-sealing on every deploy is not a host mutation.
    """
    if not ROLE.fullmatch(role or ""):
        raise NodeRoleError("refusing to seal an invalid role identifier")
    directory = os.path.abspath(etc)
    required_uid = expected_uid(etc)
    body = (SEALED_KEY + "=" + role + "\n").encode("ascii")

    parent = _open_authority_dir(directory, required_uid)
    if parent is None:
        try:
            os.mkdir(directory, 0o755)
            os.chmod(directory, 0o755)
        except OSError as err:
            raise NodeRoleError(
                f"cannot create the node-role authority directory: {err}")
        parent = _open_authority_dir(directory, required_uid)
        if parent is None:
            raise NodeRoleError(
                "the node-role authority directory vanished while being created")
    try:
        if _sealed_is(parent, required_uid, body):
            return False
        staged = ".deploy-role-" + secrets.token_hex(16) + ".tmp"
        staged_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        staged_flags |= getattr(os, "O_NOFOLLOW", 0)
        staged_flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(staged, staged_flags, 0o600, dir_fd=parent)
        except OSError as err:
            raise NodeRoleError(f"cannot stage the sealed node role: {err}")
        try:
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(body)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise NodeRoleError("short write sealing the node role")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.replace(staged, SEALED_NAME, src_dir_fd=parent,
                       dst_dir_fd=parent)
            staged = ""
            os.fsync(parent)
        except OSError as err:
            raise NodeRoleError(f"cannot seal the node role: {err}")
        finally:
            os.close(descriptor)
            if staged:
                try:
                    os.unlink(staged, dir_fd=parent)
                except OSError:
                    pass
    finally:
        os.close(parent)
    return True


def _sealed_is(parent, required_uid, body):
    """True when the sealed file already holds exactly these bytes, safely."""
    try:
        descriptor = os.open(SEALED_NAME, _leaf_flags(), dir_fd=parent)
    except OSError:
        return False
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            return False
        if details.st_uid != required_uid or \
                stat.S_IMODE(details.st_mode) != 0o600:
            return False
        return os.read(descriptor, MAX_SEALED_BYTES + 1) == body
    finally:
        os.close(descriptor)


def read_bootstrap_role(project_path):
    """`MOJO_NODE_ROLE` out of var/bootstrap.conf, or "" when undeclared.

    That file holds AWS_KEY / AWS_SECRET on real nodes, so it is never sourced,
    echoed, or copied into an error message: a bounded read, an anchored
    per-line key=value scan, and the only thing that can leave this function is
    a value the role grammar accepts.
    """
    path = os.path.join(project_path, BOOTSTRAP_REL)
    try:
        with open(path, "rb") as handle:
            body = handle.read(MAX_BOOTSTRAP_BYTES)
    except OSError:
        return ""
    text = body.decode("utf-8", "replace")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key.strip() != SEALED_KEY:
            continue
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            return ""
        if not ROLE.fullmatch(value):
            raise NodeRoleError(
                f"var/bootstrap.conf declares a {SEALED_KEY} that is not a "
                "valid role identifier (1-63 characters, lowercase letters, "
                "digits, dots, dashes, underscores, starting alphanumeric)")
        return value
    return ""


def resolve(project_path, etc=ETC_DIR, environ=None):
    """(role, source) with source one of sealed / env / bootstrap / none.

    Sealed wins, because it is the only one of the three an application on the
    node cannot influence. `NODE_ROLE` next (the shim's export — how a
    hand-managed node is labeled), then bootstrap, which is where a fleet
    provision already writes the node's role.
    """
    if environ is None:
        environ = os.environ
    sealed = read_sealed(etc)
    if sealed:
        return sealed, "sealed"
    value = (environ.get(ENV_NAME) or "").strip()
    if value:
        if not ROLE.fullmatch(value):
            raise NodeRoleError(
                f"{ENV_NAME} is not a valid role identifier")
        return value, "env"
    bootstrap = read_bootstrap_role(project_path)
    if bootstrap:
        return bootstrap, "bootstrap"
    return "", "none"


def read_manifest(repo_path):
    """{role: {"<kind>/<name>", ...}} from <repo>/aws/node_roles.conf.

    Absent file = {} = every converged name is shared, which is the behavior
    every project had before this feature existed. Anything the grammar does
    not accept raises: a manifest is the thing deciding what gets deleted off a
    node, so it is never partially understood.
    """
    path = os.path.join(repo_path, MANIFEST_REL)
    try:
        with open(path, "rb") as handle:
            body = handle.read(MAX_MANIFEST_BYTES + 1)
    except OSError:
        return {}
    if len(body) > MAX_MANIFEST_BYTES:
        raise NodeRoleError("aws/node_roles.conf is oversized")
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError:
        raise NodeRoleError("aws/node_roles.conf is not ASCII")

    manifest = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) > 2:
            raise NodeRoleError(
                f"aws/node_roles.conf line {number}: expected `<role>` or "
                "`<role> <kind>/<name>`")
        role = fields[0]
        if not ROLE.fullmatch(role):
            raise NodeRoleError(
                f"aws/node_roles.conf line {number}: `{role}` is not a valid "
                "role identifier")
        owned = manifest.setdefault(role, set())
        if len(fields) == 1:
            continue
        entry = fields[1]
        kind, separator, name = entry.partition("/")
        if not separator or kind not in KINDS:
            raise NodeRoleError(
                f"aws/node_roles.conf line {number}: `{entry}` must name one "
                "of conf.d/<name>, cron.d/<name>, systemd/<name>")
        if not name or "/" in name or name in (".", ".."):
            raise NodeRoleError(
                f"aws/node_roles.conf line {number}: `{entry}` is not a plain "
                "file name")
        if name in REFUSED_NAMES[kind]:
            raise NodeRoleError(
                f"aws/node_roles.conf line {number}: {kind}/{name} is "
                "package-owned and cannot be claimed by a role")
        owned.add(kind + "/" + name)
    return manifest


def foreign(manifest, role, kind):
    """Sorted names of `kind` some OTHER role owns and this one does not.

    A name may be listed under several roles — that is how two of three roles
    share a vhost — so ownership by this role always wins over foreignness.
    """
    if kind not in KINDS:
        raise NodeRoleError(f"unknown kind: {kind}")
    if not manifest:
        return []
    if role not in manifest:
        raise NodeRoleError(
            f"node role `{role}` is not declared in aws/node_roles.conf — "
            "add it there before converging a node that claims it")
    prefix = kind + "/"
    mine = {name for name in manifest[role] if name.startswith(prefix)}
    others = set()
    for other, names in manifest.items():
        if other == role:
            continue
        others |= {name for name in names if name.startswith(prefix)}
    return sorted(name[len(prefix):] for name in others - mine)


# ---------------------------------------------------------------------------
# CLI — one stderr line and exit 2 on any refusal, never a traceback
# ---------------------------------------------------------------------------

def cmd_resolve(args):
    role, source = resolve(args.project_path, etc=args.etc)
    if args.json:
        # Every value here is a validated identifier or "" — no byte of any
        # conf file can reach the report through this command.
        environment = (os.environ.get(ENV_NAME) or "").strip()
        if environment and not ROLE.fullmatch(environment):
            raise NodeRoleError(f"{ENV_NAME} is not a valid role identifier")
        print(json.dumps({
            "role": role,
            "source": source,
            "sealed": read_sealed(args.etc),
            "env": environment,
            "bootstrap": read_bootstrap_role(args.project_path),
        }, sort_keys=True))
    elif role:
        print(role)
    return 0


def cmd_seal(args):
    if not ROLE.fullmatch(args.role or ""):
        raise NodeRoleError("--role is not a valid role identifier")
    print("sealed" if seal(args.role, etc=args.etc) else "unchanged")
    return 0


def cmd_foreign(args):
    if not ROLE.fullmatch(args.role or ""):
        raise NodeRoleError("--role is not a valid role identifier")
    repo = args.repo_path or args.project_path
    for name in foreign(read_manifest(repo), args.role, args.kind):
        print(name)
    return 0


def main(argv):
    parser = argparse.ArgumentParser(
        prog="python3 -m mojo.deploy.node_role",
        description="Resolve, seal, and apply this node's deploy role")
    sub = parser.add_subparsers(dest="command", required=True)

    resolve_cmd = sub.add_parser(
        "resolve", help="print the resolved role (nothing when unlabeled)")
    resolve_cmd.add_argument("--project-path", default="/opt/api",
                             help="the deployed tree (default: /opt/api)")
    resolve_cmd.add_argument("--json", action="store_true",
                             help="emit role, source, and each authority's "
                                  "own answer as one JSON object")

    seal_cmd = sub.add_parser(
        "seal", help="write the root-sealed authority for this node")
    seal_cmd.add_argument("--role", required=True, help="the role to seal")
    seal_cmd.add_argument("--project-path", default="/opt/api",
                          help="accepted for call-site symmetry; sealing "
                               "reads nothing from the project tree")

    foreign_cmd = sub.add_parser(
        "foreign", help="names of one kind that belong to another role")
    foreign_cmd.add_argument("--project-path", default="/opt/api",
                             help="the deployed tree (default: /opt/api)")
    foreign_cmd.add_argument("--repo-path", default=None,
                             help="tree holding aws/node_roles.conf "
                                  "(default: --project-path)")
    foreign_cmd.add_argument("--role", required=True,
                             help="the role this node plays")
    foreign_cmd.add_argument("--kind", required=True, choices=KINDS,
                             help="which converged set to answer for")

    for command in (resolve_cmd, seal_cmd, foreign_cmd):
        command.add_argument("--etc", default=ETC_DIR,
                             help=f"authority directory (default: {ETC_DIR})")

    args = parser.parse_args(argv)
    try:
        if args.command == "resolve":
            return cmd_resolve(args)
        if args.command == "seal":
            return cmd_seal(args)
        return cmd_foreign(args)
    except (NodeRoleError, OSError, ValueError) as err:
        print(f"node-role: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
