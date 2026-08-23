"""Converge a repository nginx vhost without discarding a proven Certbot lineage.

`post_deploy.sh` runs this once per file in `aws/nginx/conf.d/`, as the child of
a MojoSec trusted-change operation:

    python3 -E [-P] -m mojo.deploy.vhost_install <source> <destination> <nginx_etc>

The repository file is authoritative for every byte except the two certificate
path values. When the installed vhost already carries a rigorously validated
Certbot pair for the same normalized `server_name` set, those two values — and
only those two — are carried forward into the repository bytes.

Preservation is an ENHANCEMENT, not a gate. Refusing to preserve must never be
more likely to abort a release than the plain `cp -f` this replaced; the only
fatal conditions are the ones that would otherwise perform an unsafe write or
install a certificate path that does not exist on this node.

THE CONTRACT FOR THIS PACKAGE APPLIES HERE (see `mojo/deploy/__init__.py`): no
Django, no `django.conf.settings`, and nothing from `mojo.helpers.*` — this
runs on a node that may have no settings on disk at all. Standard library only.
"""

import hashlib
import os
import re
import secrets
import stat
import sys


class VhostError(Exception):
    """Base for the two outcomes, both of which carry a fixed identifier.

    The code is a diagnostic, and it is ALL an operator gets: 1.16.0 printed
    one generic sentence with no code and took a 46-minute hotfix to diagnose.
    Codes are fixed identifiers and never interpolate a filesystem path — a
    certificate path in a deploy log is a disclosure.
    """

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Refused(VhostError):
    """Abort the deploy. Reserved for the write path — where continuing would
    perform an unsafe write — and for a repository certificate path that does
    not exist on this node.
    """
    pass


class Unpreservable(VhostError):
    """Skip certificate preservation, install the repository bytes, warn.

    Every one of these is reached while INSPECTING the installed file or
    validating a Certbot lineage. Not consuming those untrusted bytes is what
    the check was really protecting; declining to preserve achieves that in
    full, so aborting the release on top of it buys nothing and costs an
    outage. `install_vhost` must never be more likely to abort a deploy than
    the `cp -f` it replaced.
    """
    pass


# Downgrades that mean the node's ISSUED certificate reference was dropped, as
# opposed to a vhost this parser simply could not read. The severities map to
# two incident phases, because they need different operator responses: `tls`
# means a certificate stops being served until the certificate plane catches
# up; `cfg` means preservation never got far enough to matter.
LINEAGE_AT_RISK = frozenset({
    "server-name-mismatch",
    "repository-dropped-tls-server",
    "ambiguous-certificate-directive",
    "missing-lineage-material",
    "unsafe-lineage-material",
    "unsafe-lineage-metadata",
    "unsafe-lineage",
    "noncanonical-live-path",
    "nonstandard-live-link",
    "out-of-tree-live-link",
    "unsafe-private-key-mode",
    "mixed-lineage-revision",
    "mixed-or-noncanonical-live-path",
})


def snapshot(path, required=True):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if required:
            raise Refused("missing-file")
        return None
    except OSError:
        raise Refused("unsafe-file")
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise Refused("non-regular-file")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        identity = (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
            after.st_ctime_ns, after.st_mode, after.st_uid, after.st_gid,
        )
        if identity != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
            before.st_ctime_ns, before.st_mode, before.st_uid, before.st_gid,
        ):
            raise Refused("changed-snapshot")
        data = b"".join(chunks)
        return identity, hashlib.sha256(data).digest(), data
    finally:
        os.close(fd)


def unchanged(path, prior):
    try:
        current = snapshot(path, required=prior is not None)
    except Refused:
        return False
    if prior is None:
        return current is None
    return current[:2] == prior[:2]


def tokens(data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Unpreservable("invalid-utf8-config")
    result = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == "#":
            newline = text.find("\n", index)
            index = size if newline < 0 else newline + 1
            continue
        if char in "{};":
            result.append((char, index, index + 1, False))
            index += 1
            continue
        start = index
        quoted = char in "\"'"
        quote = char if quoted else ""
        if quoted:
            index += 1
        value = []
        while index < size:
            char = text[index]
            if quoted:
                if char == quote:
                    index += 1
                    break
                if char == "\\":
                    index += 1
                    if index >= size:
                        raise Unpreservable("invalid-escape")
                    value.append(text[index])
                    index += 1
                    continue
                value.append(char)
                index += 1
                continue
            if char.isspace() or char in "{};#":
                break
            if char == "\\":
                index += 1
                if index >= size:
                    raise Unpreservable("invalid-escape")
                value.append(text[index])
                index += 1
                continue
            value.append(char)
            index += 1
        else:
            if quoted:
                raise Unpreservable("unterminated-quote")
        if quoted and (index == 0 or text[index - 1] != quote):
            raise Unpreservable("unterminated-quote")
        result.append(("".join(value), start, index, quoted))
    return result


def server_directives(data):
    stream = tokens(data)
    blocks = []
    index = 0
    while index + 1 < len(stream):
        if stream[index][0] != "server" or stream[index + 1][0] != "{":
            index += 1
            continue
        depth = 1
        end = index + 2
        while end < len(stream) and depth:
            if stream[end][0] == "{":
                depth += 1
            elif stream[end][0] == "}":
                depth -= 1
            end += 1
        if depth:
            raise Unpreservable("unbalanced-server")
        directives = []
        cursor = index + 2
        nested = 0
        pending = []
        while cursor < end - 1:
            value = stream[cursor][0]
            if value == "{":
                nested += 1
                pending = []
            elif value == "}":
                nested -= 1
                if nested < 0:
                    raise Unpreservable("unbalanced-nested")
            elif nested == 0 and value == ";":
                if pending:
                    directives.append(pending)
                pending = []
            elif nested == 0:
                pending.append(stream[cursor])
            cursor += 1
        if pending or nested:
            raise Unpreservable("incomplete-directive")
        blocks.append(directives)
        index = end
    return blocks


def tls_block(data, allow_none=False):
    candidates = []
    for block in server_directives(data):
        by_name = {}
        for directive in block:
            by_name.setdefault(directive[0][0], []).append(directive)
        listens = by_name.get("listen", [])
        tls = any(any(token[0] == "ssl" for token in directive[1:])
                  for directive in listens)
        tls = tls or "ssl_certificate" in by_name or \
            "ssl_certificate_key" in by_name
        if tls:
            candidates.append(by_name)
    if not candidates and allow_none:
        return None
    if len(candidates) != 1:
        raise Unpreservable("ambiguous-tls-server")
    return candidates[0]


SERVER_NAME = re.compile(
    r"^(?:\*\.)?[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$", re.I | re.ASCII)
LINEAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")


def names(block):
    values = []
    for directive in block.get("server_name", []):
        for token in directive[1:]:
            raw_value = token[0]
            try:
                raw_value.encode("ascii")
            except UnicodeEncodeError:
                raise Unpreservable("unsafe-server-name")
            value = raw_value.lower().rstrip(".")
            if token[3] or not SERVER_NAME.fullmatch(value):
                raise Unpreservable("unsafe-server-name")
            values.append(value)
    if not values:
        raise Unpreservable("missing-server-name")
    return tuple(sorted(set(values)))


def sole_value(block, directive):
    matches = block.get(directive, [])
    if len(matches) != 1 or len(matches[0]) != 2 or matches[0][1][3]:
        raise Unpreservable("ambiguous-certificate-directive")
    return matches[0][1]


def safe_metadata(path, owner, kind):
    try:
        info = os.lstat(path)
    except OSError:
        raise Unpreservable("missing-lineage-material")
    expected = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if stat.S_ISLNK(info.st_mode) or not expected(info.st_mode):
        raise Unpreservable("unsafe-lineage-material")
    if info.st_uid != owner or info.st_mode & 0o022:
        raise Unpreservable("unsafe-lineage-metadata")
    return info


def letsencrypt_root(nginx_etc):
    if nginx_etc == "/etc/nginx":
        return "/etc/letsencrypt"
    return os.path.join(os.path.dirname(nginx_etc), "letsencrypt")


def validated_pair(block, nginx_etc, owner):
    le_root = letsencrypt_root(nginx_etc)
    live_root = os.path.join(le_root, "live")
    archive_root = os.path.join(le_root, "archive")
    cert = sole_value(block, "ssl_certificate")[0]
    key = sole_value(block, "ssl_certificate_key")[0]
    cert_parent, cert_name = os.path.split(cert)
    key_parent, key_name = os.path.split(key)
    lineage = os.path.basename(cert_parent)
    if not LINEAGE.fullmatch(lineage) or lineage in (".", ".."):
        raise Unpreservable("unsafe-lineage")
    expected_parent = os.path.join(live_root, lineage)
    if cert_parent != expected_parent or key_parent != expected_parent or \
            cert_name != "fullchain.pem" or key_name != "privkey.pem":
        raise Unpreservable("noncanonical-live-path")

    for directory in (le_root, live_root, expected_parent, archive_root,
                      os.path.join(archive_root, lineage)):
        safe_metadata(directory, owner, "directory")

    revisions = []
    for path, stem in ((cert, "fullchain"), (key, "privkey")):
        link = os.lstat(path)
        if not stat.S_ISLNK(link.st_mode) or link.st_uid != owner:
            raise Unpreservable("nonstandard-live-link")
        target = os.readlink(path)
        match = re.fullmatch(
            r"\.\./\.\./archive/" + re.escape(lineage) + "/" +
            stem + r"([1-9][0-9]*)\.pem", target)
        if not match:
            raise Unpreservable("nonstandard-live-link")
        archive_path = os.path.join(archive_root, lineage,
                                    stem + match.group(1) + ".pem")
        if os.path.realpath(path) != os.path.realpath(archive_path):
            raise Unpreservable("out-of-tree-live-link")
        material = safe_metadata(archive_path, owner, "file")
        if stem == "privkey" and stat.S_IMODE(material.st_mode) & 0o077:
            raise Unpreservable("unsafe-private-key-mode")
        revisions.append(match.group(1))
    if revisions[0] != revisions[1]:
        raise Unpreservable("mixed-lineage-revision")
    return cert, key


def certbot_candidate(block, nginx_etc, owner):
    cert = sole_value(block, "ssl_certificate")[0]
    key = sole_value(block, "ssl_certificate_key")[0]
    le_root = letsencrypt_root(nginx_etc)
    live_root = os.path.join(le_root, "live")
    live_prefix = live_root + os.sep
    values = (cert, key)
    if not all(os.path.isabs(value) for value in values):
        raise Unpreservable("relative-certificate-path")
    canonical = all(os.path.normpath(value) == value for value in values)
    under_live = tuple(value.startswith(live_prefix) for value in values)
    if not any(under_live):
        if not canonical:
            raise Unpreservable("noncanonical-certificate-path")
        # A complete, absolute pair wholly outside the derived Certbot live
        # root is explicitly non-Certbot. It contributes no bytes.
        return None
    if not all(under_live) or not canonical:
        raise Unpreservable("mixed-or-noncanonical-live-path")
    return validated_pair(block, nginx_etc, owner)


def overlay(repo_data, repo_block, cert, key):
    replacements = [
        (sole_value(repo_block, "ssl_certificate")[1:3], cert),
        (sole_value(repo_block, "ssl_certificate_key")[1:3], key),
    ]
    try:
        output = repo_data.decode("utf-8")
    except UnicodeDecodeError:
        raise Unpreservable("invalid-utf8-config")
    for (start, end), value in sorted(replacements, reverse=True):
        output = output[:start] + value + output[end:]
    return output.encode("utf-8")


def preserve(repo_data, installed_prior, nginx_etc, owner):
    """The bytes to install: the repository file with this node's proven
    certificate paths overlaid, or the repository file unchanged.

    Everything in here inspects the INSTALLED file or the lineage it points
    at, so every refusal below is an Unpreservable — see that class.
    """
    info = installed_prior[0]
    installed_safe = info[6] == owner and not (info[5] & 0o022)
    if not installed_safe:
        # Overwriting a group-writable conf with a fresh root-owned 0644 file
        # is strictly CORRECTIVE: refusing perpetuates the weakness this
        # detects, on a vhost that need not involve TLS at all. Consuming the
        # untrusted bytes is what it really guards, and skipping preservation
        # already prevents that. The write stays safe because
        # `unsafe-destination-directory` below is still fatal.
        raise Unpreservable("unsafe-installed-metadata")
    repo_block = tls_block(repo_data, allow_none=True)
    installed_block = tls_block(installed_prior[2], allow_none=True)
    if repo_block is None and installed_block is not None:
        raise Unpreservable("repository-dropped-tls-server")
    if repo_block is not None and installed_block is not None:
        if names(repo_block) != names(installed_block):
            raise Unpreservable("server-name-mismatch")
        candidate = certbot_candidate(installed_block, nginx_etc, owner)
        if candidate is not None:
            cert, key = candidate
            return overlay(repo_data, repo_block, cert, key)
    return repo_data


def require_repository_certificates(repo_data):
    """A downgrade installs the repository's OWN certificate paths, so they
    have to exist on this node.

    Without this, renaming a `server_name` and its lineage path to a name
    certbot has not issued yet installs a conf that cannot pass `nginx -t`.
    The deploy then dies ~300 lines later with broken bytes already on disk,
    after MojoSec's internal `nginx -t` has failed its own rollback and logged
    a misleading FATAL about django.inc plus a spurious incident. Two os.stat
    calls turn that into a precise, early refusal with /etc untouched.

    Skipped when the repository file cannot be parsed (the em-dash class):
    nginx is the better judge of a file this parser could not read, and that
    class must stay a pure warning.
    """
    try:
        block = tls_block(repo_data, allow_none=True)
        if block is None:
            return
        paths = (sole_value(block, "ssl_certificate")[0],
                 sole_value(block, "ssl_certificate_key")[0])
    except Unpreservable:
        return
    for path in paths:
        try:
            os.stat(path)
        except OSError:
            raise Refused("repository-certificate-missing")


def converge(argv):
    """Install one vhost. Returns the downgrade code, or None when the node's
    certificate reference was preserved (or there was nothing to preserve).
    """
    if len(argv) != 3:
        raise Refused("invalid-invocation")
    source, destination, nginx_etc = map(os.path.abspath, argv)
    source_prior = snapshot(source)
    try:
        installed_prior = snapshot(destination, required=False)
    except Refused:
        # Existing symlinks and non-regular destinations are never replaced.
        raise

    output = source_prior[2]
    downgrade = None
    owner = 0 if nginx_etc == "/etc/nginx" else os.geteuid()
    if installed_prior is not None:
        # OSError is caught alongside Unpreservable because `validated_pair`
        # has one unguarded probe (`os.lstat` on the live link) while every
        # other read there goes through `safe_metadata`. A live/<lineage>/
        # directory that exists while fullchain.pem does not — a partial
        # `certbot delete`, a hand-renamed lineage, a half-restored backup —
        # would otherwise escape as a CODELESS fatal: the exact failure shape
        # this change exists to remove. The WRITE path below keeps OSError
        # fatal, so this wrapper must not extend past `preserve`.
        try:
            output = preserve(source_prior[2], installed_prior, nginx_etc,
                              owner)
        except (Unpreservable, OSError) as error:
            downgrade = error.code if isinstance(error, Unpreservable) \
                else "unpreservable-io"
            output = source_prior[2]
            require_repository_certificates(source_prior[2])

    directory = os.path.dirname(destination)
    destination_name = os.path.basename(destination)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(directory, directory_flags)
    directory_info = os.fstat(directory_fd)
    if not stat.S_ISDIR(directory_info.st_mode) or \
            directory_info.st_uid != owner or directory_info.st_mode & 0o022:
        os.close(directory_fd)
        raise Refused("unsafe-destination-directory")

    staged = ".mojo-vhost-" + secrets.token_hex(16) + ".tmp"
    staged_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        staged_flags |= os.O_NOFOLLOW
    descriptor = os.open(staged, staged_flags, 0o600, dir_fd=directory_fd)
    try:
        os.fchmod(descriptor, 0o644)
        remaining = memoryview(output)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise Refused("short-stage-write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        if not unchanged(source, source_prior) or \
                not unchanged(destination, installed_prior):
            raise Refused("changed-snapshot")
        staged_info = os.stat(staged, dir_fd=directory_fd,
                              follow_symlinks=False)
        descriptor_info = os.fstat(descriptor)
        if not stat.S_ISREG(staged_info.st_mode) or \
                (staged_info.st_dev, staged_info.st_ino) != \
                (descriptor_info.st_dev, descriptor_info.st_ino):
            raise Refused("changed-stage")
        os.replace(staged, destination_name, src_dir_fd=directory_fd,
                   dst_dir_fd=directory_fd)
        staged = ""
        os.fsync(directory_fd)
    finally:
        os.close(descriptor)
        if staged:
            try:
                os.unlink(staged, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)
    return downgrade


def main(argv=None, *, out=None, err=None):
    """Converge one vhost. Returns 0 when the destination holds the intended
    bytes, 1 when the deploy must abort.

    EXIT CODE 0 WHENEVER THE FILE WAS MUTATED, downgrade or not. A "warned but
    installed" nonzero exit is not available: `trusted_change` routes this
    through `mojosec_changes.py run`, which calls `journal.abort()` on ANY
    nonzero child status, and abort merely pops the operation without
    restoring — so the file would be changed and the change unattested, which
    is exactly what MojoSec flags as unauthorized. A downgrade is therefore
    signalled by one sentinel line on stdout, which the shell maps to a
    `record_warning` phase and then re-emits to the log.

    `out` / `err` default to the process streams AT CALL TIME so a test can
    pass its own buffers instead of reassigning `sys.stdout` — the test runner
    executes modules as threads in one process, where that is a process-wide
    mutation.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    argv = sys.argv[1:] if argv is None else list(argv)
    name = os.path.basename(argv[1]) if len(argv) > 1 else "?"
    try:
        downgrade = converge(argv)
    except (VhostError, OSError, ValueError) as error:
        print("vhost %s: %s" % (name, getattr(error, "code", "write-failed")),
              file=err)
        print("TLS vhost convergence refused unsafe or changed state",
              file=err)
        return 1
    if downgrade:
        severity = "tls" if downgrade in LINEAGE_AT_RISK else "cfg"
        print("MOJO-VHOST-WARN %s %s" % (severity, downgrade), file=out)
        print("vhost %s: %s" % (name, downgrade), file=err)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
