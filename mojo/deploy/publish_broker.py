#!/usr/bin/env python3
"""Root-owned publish broker: an app annotates its OWN content writes.

The broker accepts no command-line arguments. Its sole input is one bounded
JSON object on stdin. It is a BROKER, not a wrapper: `begin` opens an
annotation window and returns, the application performs its own unprivileged
write as itself, and `end` closes the window. The content write never runs as
root, and root never runs anything the application supplies.

What an annotation can and cannot do:

    An application annotation can only ever excuse a change the application
    was already authorized to make.

Root derives every path and computes every evidence digest, so a declaration
cannot claim a change that did not happen or describe one that did. Note also
that ONE OS principal publishes for every tenant on a node: the guarantee is
the sentence above, never "tenant A cannot annotate tenant B". A caller that
can write another tenant's tree could already do so unannotated.
"""

import json
import os
import pwd
import re
import resource
import secrets
import stat
import sys
import syslog
import time

from mojo.deploy.mojosec_changes import (
    CONTENT_PUBLISH_KIND, MAX_CONTENT_PATHS, MAX_CONTENT_SUBTREES,
    MAX_TTL_SECONDS, ChangeError, ChangeJournal, _content_subtree_paths,
)


BROKER_PATH = "/usr/local/sbin/mojo-publish-broker"
SUDOERS_PATH = "/etc/sudoers.d/71-mojo-publish-broker"
ENROLLMENT_PATH = "/etc/mojosec/enrollment.json"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_ENROLLMENT_BYTES = 256 * 1024
ADDRESS_SPACE_BYTES = 256 * 1024 * 1024
DEFAULT_TTL_SECONDS = 900
# A tenant name is one path component and never starts with an underscore, so
# an underscore-prefixed sibling of the tenants — `_certs` being the one that
# matters — can never be declared as a tenant and can never be annotated. TLS
# material is best kept outside the enrolled root entirely; a change to it
# always alarms.
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_OPERATION_ID = re.compile(r"^content-publish-[0-9a-f]{32}$")
_COMMON_FIELDS = {"operation"}
_OP_FIELDS = {
    "begin": ({"root", "subtrees"}, {"ttl_seconds"}),
    "end": ({"operation_id"}, set()),
    "abort": ({"operation_id"}, set()),
}


class PublishError(RuntimeError):
    pass


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublishError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def parse_request(payload):
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_REQUEST_BYTES:
        raise PublishError("request size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_strict_object,
            parse_constant=lambda child: (_ for _ in ()).throw(
                PublishError("constant")),
        )
    except (UnicodeError, json.JSONDecodeError) as err:
        raise PublishError("request is not strict JSON") from err
    if not isinstance(value, dict):
        raise PublishError("request must be an object")
    allowed = _OP_FIELDS.get(value.get("operation"))
    if allowed is None:
        raise PublishError("operation is invalid")
    required, optional = allowed
    fields = set(value)
    if not (_COMMON_FIELDS | required) <= fields or \
            not fields <= (_COMMON_FIELDS | required | optional):
        raise PublishError("request has unknown, missing, or forbidden fields")
    return value


def _read_root_owned_json(path, maximum):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as err:
        raise PublishError(f"cannot read {path}: {err}") from err
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PublishError(f"{path} must be a regular file")
        if info.st_uid != 0 or info.st_mode & 0o077:
            raise PublishError(f"{path} must be root-owned and 0600 or stricter")
        if info.st_size > maximum:
            raise PublishError(f"{path} is too large")
        payload = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    if len(payload) > maximum:
        raise PublishError(f"{path} is too large")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError) as err:
        raise PublishError(f"cannot parse {path}: {err}") from err
    if not isinstance(value, dict):
        raise PublishError(f"{path} must contain a JSON object")
    return value


def load_content_roots(path=ENROLLMENT_PATH):
    """Read the enrolled content roots; an unenrolled node publishes nothing."""
    enrollment = _read_root_owned_json(path, MAX_ENROLLMENT_BYTES)
    roots = enrollment.get("fim_content_roots") or []
    if not isinstance(roots, list) or not roots:
        raise PublishError("this node has no enrolled content roots")
    result = []
    for root in roots:
        # Authoritative validation happens at enrollment install time; this is
        # the broker refusing to act on anything it cannot re-verify itself.
        if (not isinstance(root, str) or not os.path.isabs(root) or
                "\x00" in root or os.path.normpath(root) != root or root == "/"):
            raise PublishError("enrolled content root is not normalized")
        result.append(root)
    return tuple(result)


def resolve_tenant(root, content_roots):
    """Split one declared tenant root into (content root, tenant name)."""
    if (not isinstance(root, str) or not os.path.isabs(root) or "\x00" in root or
            os.path.normpath(root) != root or root == "/"):
        raise PublishError("publish root is not an absolute normalized path")
    content_root = os.path.dirname(root)
    tenant = os.path.basename(root)
    if content_root not in content_roots:
        # Exact immediate child only: a deeper path, the content root itself,
        # or anything outside the enrolled roots is refused outright.
        raise PublishError("publish root is not a tenant of an enrolled content root")
    if not _TENANT.fullmatch(tenant):
        raise PublishError("tenant name is not an accepted single component")
    return content_root, tenant


def _subtrees(value):
    if not isinstance(value, list) or not value or len(value) > MAX_CONTENT_SUBTREES:
        raise PublishError(
            f"publish requires 1-{MAX_CONTENT_SUBTREES} declared subtrees")
    for name in value:
        if not isinstance(name, str) or not _TENANT.fullmatch(name):
            raise PublishError("publish subtree name is not an accepted component")
    return list(value)


def _ttl(value):
    if value is None:
        return DEFAULT_TTL_SECONDS
    if (not isinstance(value, int) or isinstance(value, bool) or
            not 1 <= value <= MAX_TTL_SECONDS):
        raise PublishError(f"publish TTL must be 1-{MAX_TTL_SECONDS} seconds")
    return value


def _operation_id(value):
    if not isinstance(value, str) or not _OPERATION_ID.fullmatch(value):
        # Shape alone is not authorization — complete_derived also checks the
        # recorded kind — but it stops a publisher naming a deploy operation
        # before the journal is ever opened.
        raise PublishError("operation id is not a publish operation")
    return value


def _receipt(kind, operation_id, **values):
    """Bounded operator breadcrumb. Never fails the operation it describes."""
    value = {
        "schema": "mojosec.publish-receipt", "version": 1, "kind": kind,
        "operation_id": operation_id, "broker_pid": os.getpid(),
        "monotonic_ns": time.monotonic_ns(), **values,
    }
    try:
        syslog.openlog(ident="mojo-publish-broker", facility=syslog.LOG_AUTHPRIV)
        syslog.syslog(syslog.LOG_INFO, json.dumps(
            value, sort_keys=True, separators=(",", ":")))
    except (OSError, ValueError):
        pass
    return value


def execute(request, *, journal=None, content_roots=None, enrollment_path=ENROLLMENT_PATH):
    operation = request["operation"]
    roots = content_roots if content_roots is not None else load_content_roots(
        enrollment_path)
    if not roots:
        raise PublishError("this node has no enrolled content roots")
    if operation == "begin":
        content_root, tenant = resolve_tenant(request["root"], roots)
        subtrees = _subtrees(request["subtrees"])
        ttl_seconds = _ttl(request.get("ttl_seconds"))
        operation_id = f"{CONTENT_PUBLISH_KIND}-{secrets.token_hex(16)}"
        opened = journal if journal is not None else ChangeJournal(
            allowed_roots=(content_root,))
        scope = {"root": request["root"], "subtrees": subtrees}
        record = opened.begin(
            operation_id, CONTENT_PUBLISH_KIND, _declared_paths(scope),
            deployment_id=f"{CONTENT_PUBLISH_KIND}:{tenant}",
            ttl_seconds=ttl_seconds, max_paths=MAX_CONTENT_PATHS, scope=scope)
        result = {
            "ok": True, "operation_id": record["operation_id"],
            "tenant": tenant, "paths": len(record["paths"]),
            "ttl_seconds": ttl_seconds,
        }
        _receipt("begin", record["operation_id"], tenant=tenant,
                 content_root=content_root, subtrees=subtrees,
                 paths=len(record["paths"]))
        return result
    operation_id = _operation_id(request["operation_id"])
    opened = journal if journal is not None else ChangeJournal(allowed_roots=roots)
    if operation == "abort":
        removed = opened.abort(operation_id)
        _receipt("abort", operation_id, removed=bool(removed))
        return {"ok": True, "operation_id": operation_id, "aborted": bool(removed)}
    completed = opened.complete_derived(
        operation_id, expect_kind=CONTENT_PUBLISH_KIND)
    _receipt("end", operation_id, annotated=len(completed["entries"]),
             paths=completed["paths"], complete=completed["complete"])
    return {
        "ok": True, "operation_id": operation_id,
        "annotated_paths": len(completed["entries"]),
        "paths": completed["paths"], "complete": completed["complete"],
    }


def _declared_paths(scope):
    """The declared subtree roots themselves; begin's walk derives the rest."""
    found, _complete = _content_subtree_paths(scope["root"], scope["subtrees"])
    # A first publish into a brand-new tenant may name subtrees that do not
    # exist yet. The declared roots are always in scope so their creation is
    # explainable; the walk supplies everything that already exists.
    declared = [os.path.join(scope["root"], name) for name in scope["subtrees"]]
    return list(dict.fromkeys(declared + found))


def _verify_caller():
    if os.geteuid() != 0:
        raise PublishError("broker must run as root")
    raw_uid = os.environ.get("SUDO_UID", "")
    if not raw_uid.isdigit() or int(raw_uid) <= 0:
        raise PublishError("SUDO_UID is missing or invalid")
    try:
        expected = pwd.getpwnam("ec2-user").pw_uid
    except KeyError as err:
        raise PublishError("application account is missing") from err
    if int(raw_uid) != expected:
        raise PublishError("caller is not the application account")
    info = os.stat(BROKER_PATH, follow_symlinks=False)
    if (info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022 or
            not stat.S_ISREG(info.st_mode)):
        raise PublishError("installed broker metadata is unsafe")


def render_sudoers(user="ec2-user"):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", user):
        raise PublishError("sudoers user is invalid")
    # An empty quoted argument is sudoers' exact no-arguments grammar.
    return f"{user} ALL=(root) NOPASSWD: {BROKER_PATH} \"\"\n"


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("publish broker accepts no arguments", file=sys.stderr)
        return 2
    try:
        _verify_caller()
        resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))
        payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        request = parse_request(payload)
        print(json.dumps(execute(request), sort_keys=True, separators=(",", ":")))
        return 0
    except (PublishError, ChangeError, OSError, ValueError) as err:
        print(f"publish broker: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
