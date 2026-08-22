#!/bin/bash
# Post-checkout deployment — run by update.sh after every code change, shipped
# inside the django-mojo package (mojo/deploy/scripts/) and executed through
# each project's aws/post_deploy.sh shim. The shim contract, the input table
# and the var/deploy layout are documented in django-mojo
# docs/django_developer/deploy/README.md.
#
# THIS FILE IS REPLACED MID-RUN, AND THAT IS FINE: the pip install below swaps
# this file for a new inode inside site-packages. The bash executing this run
# keeps its fd on the OLD inode, so the in-flight run completes on the code it
# started with; the NEXT run resolves the new copy through the shim's
# `locate`. On success this run snapshots the installed copy to
# ${PROJ_PATH}/var/deploy/post_deploy.sh — the shim's fallback for rolling
# back through a framework version that cannot be located.
#
# Usage: sudo bash post_deploy.sh [--framework <version>] [--migrate]
#
#   --framework <v>  install django-mojo==<v> (the fleet deploy's pinned
#                    version — pinned across nodes, never across time)
#   --migrate        run `manage.py migrate_locked --noinput` (the canary run)
#   (bare)           latest django-mojo, no migration. Bare stays valid: it is
#                    what `update.sh --manual` and provisioning invoke.
#
# Project inputs (exported by the shim; every default matches the skeleton):
#   PROJ_PATH     the deployed tree                     (default /opt/api)
#   PROBE_URL     post-restart health gate              (default https://127.0.0.1/api/version)
#   APP_USER      owns the tree and runs the engines    (default ec2-user)
#   WEB_USER      runs the asgi app behind nginx        (default www)
#   ASGI_WORKERS  uvicorn worker count (@WORKERS@)      (default 4)
#   NGINX_ETC / SYSTEMD_ETC / CRON_ETC / LOGROTATE_ETC  test seams, prod defaults
#   MOJOSEC_ETC / MOJOSEC_STABLE_HELPER                 test seams, prod defaults
#
# The release-critical path fails loudly: dependencies, migrations, rendered
# web configuration, the web service and real request probes. Host housekeeping
# (security sensor, auxiliary units/timers, cron and log ownership) reports a
# deployment warning and continues. A healthy release must not be rolled back
# because an auxiliary control plane is temporarily unhealthy.

set -euo pipefail

PROJ_PATH="${PROJ_PATH:-/opt/api}"
# HTTPS, and -k below, for the reason remote.py and sanity.py both give:
# the shipped :80 vhost 301s everything except the ACME path. `curl -f`
# does not fail on a 3xx, so a plain-http gate PASSED on nginx's redirect
# alone — with the app dead behind it. A health gate that cannot fail is
# worse than none, because it is reported as evidence.
PROBE_URL="${PROBE_URL:-https://127.0.0.1/api/version}"
APP_USER="${APP_USER:-ec2-user}"
WEB_USER="${WEB_USER:-www}"
ASGI_WORKERS="${ASGI_WORKERS:-4}"
MOJOSEC_MODE="${MOJOSEC_MODE:-enrolled}"
MOJOSEC_DEPLOY_CRITICALITY="${MOJOSEC_DEPLOY_CRITICALITY:-enrolled}"
# Test seams: prod-identical defaults, overridable only by the shell harness.
NGINX_ETC="${NGINX_ETC:-/etc/nginx}"
SYSTEMD_ETC="${SYSTEMD_ETC:-/etc/systemd/system}"
CRON_ETC="${CRON_ETC:-/etc/cron.d}"
LOGROTATE_ETC="${LOGROTATE_ETC:-/etc/logrotate.d}"
MOJOSEC_ETC="${MOJOSEC_ETC:-/etc/mojosec}"
MOJOSEC_PYTHON="${MOJOSEC_PYTHON:-/usr/bin/python3}"
cd "$PROJ_PATH"

DEPLOY_DIR="${PROJ_PATH}/var/deploy"
DEPLOY_WARNINGS="${PROJ_PATH}/var/deploy_warnings"
rm -f -- "$DEPLOY_WARNINGS"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "[$(date '+%H:%M:%S')] FATAL: $*" >&2; exit 1; }
warn() { echo "[$(date '+%H:%M:%S')] WARN: $*" >&2; }

# ── phase timing ─────────────────────────────────────────────────────────────
#
# The same append-only file update.sh writes, in the same format:
#
#   <pass> <name> <value> <unit>      e.g.  deploy framework 18042 ms
#
# The PASS comes from var/deploy/phase_pass, NOT from argv. A rollback
# re-enters this script, and a rollback is also the one time the caller may be
# a NEWER update.sh than this file: pip has already downgraded the framework,
# so the shim can locate an older post_deploy.sh. A new flag would `die
# "unknown argument"` there and turn a rollback into an unknown-state node. An
# older copy ignores an unread file, which is exactly right.
#
# Under `set -euo pipefail` every one of these has to be airtight: no bare
# `((X++))` (it returns 1 when X is 0), no unguarded command substitution, no
# `test && assign` as the last statement of a function, and a `return 0` on
# every path. A missing timing is nothing; a failed deploy is an outage.
PHASE_DIR="${PROJ_PATH}/var/deploy"
PHASE_FILE="${PHASE_DIR}/phase_timings"
PHASE_PASS="deploy"
PHASE_NAME=""
PHASE_START=""
_phase_probe="$(date +%s%3N 2>/dev/null || true)"
case "$_phase_probe" in
    ''|*[!0-9]*) PHASE_MS=0 ;;
    *)           PHASE_MS=1 ;;
esac
_phase_pass="$(head -c 16 "${PHASE_DIR}/phase_pass" 2>/dev/null | tr -d '\r\n' || true)"
case "$_phase_pass" in
    deploy|rollback) PHASE_PASS="$_phase_pass" ;;
esac
mkdir -p "$PHASE_DIR" 2>/dev/null || true
# BOTH scripts append here, and they run as different users: post_deploy.sh is
# root (sudo), update.sh is the app user. Root touching the file first left it
# 0644 root:<web group>, so every one of update.sh's own timings was a
# "Permission denied" on the redirection — and the phase table reaching the
# platform was missing exactly the half that wraps the deploy. var/deploy is
# setgid to the web group and the app user is a member, so group-writable is
# all it takes.
touch "$PHASE_FILE" 2>/dev/null || true
chmod 0664 "$PHASE_FILE" 2>/dev/null || true

phase_now() {
    if [ "$PHASE_MS" = "1" ]; then
        date +%s%3N 2>/dev/null || true
    else
        date +%s 2>/dev/null || true
    fi
}

phase_begin() { # name
    PHASE_NAME="$1"
    PHASE_START="$(phase_now)"
    return 0
}

phase_record() { # name start — for a span whose start was kept elsewhere
    local name="$1" start="$2" end="" delta=0 unit="s"
    case "$start" in ''|*[!0-9]*) return 0 ;; esac
    end="$(phase_now)"
    case "$end" in ''|*[!0-9]*) return 0 ;; esac
    if [ "$PHASE_MS" = "1" ]; then unit="ms"; fi
    delta=$(( end - start ))
    [ "$delta" -ge 0 ] 2>/dev/null || delta=0
    printf '%s %s %s %s\n' "$PHASE_PASS" "$name" "$delta" "$unit" \
        >> "$PHASE_FILE" 2>/dev/null || true
    return 0
}

phase_end() { # [name]
    phase_record "${1:-$PHASE_NAME}" "$PHASE_START"
    PHASE_START=""
    return 0
}

# Only fixed phase names enter the durable incident system. Command output is
# still visible in update.sh's bounded private capture, but is never copied to
# an operator-facing incident where it could disclose credentials.
record_warning() {
    local phase="$1" message="$2"
    warn "$message (phase=${phase}; deploy continues)"
    if grep -Fqx "$phase" "$DEPLOY_WARNINGS" 2>/dev/null; then
        return 0
    fi
    printf '%s\n' "$phase" >> "$DEPLOY_WARNINGS" 2>/dev/null || true
    python3 "${PROJ_PATH}/bin/manage.py" deploy_warning "$phase" \
        >/dev/null 2>&1 || warn "could not file deploy warning incident for ${phase}"
}

FRAMEWORK=""
MIGRATE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --framework) FRAMEWORK="${2:-}"; shift 2 || die "--framework needs a value" ;;
        --migrate)   MIGRATE=1; shift ;;
        *)           die "unknown argument: $1" ;;
    esac
done

# Copy a file that is expected to exist. A missing source is a real problem
# (someone renamed or deleted it) and should be visible, not shrugged off.
install_file() {
    local src="$1" dest="$2"
    [[ -f "$src" ]] || die "expected file is missing: $src"
    trusted_change rendered-host-config "$dest" -- cp -f "$src" "$dest"
}

# Converge a repository vhost without throwing away a node's valid Certbot
# lineage. The repository remains authoritative for every byte except the two
# certificate path values. Preservation is deliberately fail-closed: it needs
# one TLS server on both sides, the same normalized server_name set, one
# canonical same-lineage Certbot pair, and root-safe installed material. The
# child owns validation, the final snapshot check, and os.replace(), keeping
# the no-follow decision and mutation inside one trusted-change operation.
install_vhost() {
    local src="$1" dest="$2"
    [[ -f "$src" ]] || die "expected vhost source is missing"
    if ! trusted_change rendered-host-config "$dest" -- \
            "$MOJOSEC_PYTHON" "${MOJOSEC_PY_FLAGS[@]}" - \
            __mojo_tls_vhost__ "$src" "$dest" "$NGINX_ETC" <<'PY'; then
import hashlib
import os
import re
import secrets
import stat
import sys


class Refused(Exception):
    pass


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
        raise Refused("invalid-utf8-config")
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
                        raise Refused("invalid-escape")
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
                    raise Refused("invalid-escape")
                value.append(text[index])
                index += 1
                continue
            value.append(char)
            index += 1
        else:
            if quoted:
                raise Refused("unterminated-quote")
        if quoted and (index == 0 or text[index - 1] != quote):
            raise Refused("unterminated-quote")
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
            raise Refused("unbalanced-server")
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
                    raise Refused("unbalanced-nested")
            elif nested == 0 and value == ";":
                if pending:
                    directives.append(pending)
                pending = []
            elif nested == 0:
                pending.append(stream[cursor])
            cursor += 1
        if pending or nested:
            raise Refused("incomplete-directive")
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
        raise Refused("ambiguous-tls-server")
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
                raise Refused("unsafe-server-name")
            value = raw_value.lower().rstrip(".")
            if token[3] or not SERVER_NAME.fullmatch(value):
                raise Refused("unsafe-server-name")
            values.append(value)
    if not values:
        raise Refused("missing-server-name")
    return tuple(sorted(set(values)))


def sole_value(block, directive):
    matches = block.get(directive, [])
    if len(matches) != 1 or len(matches[0]) != 2 or matches[0][1][3]:
        raise Refused("ambiguous-certificate-directive")
    return matches[0][1]


def safe_metadata(path, owner, kind):
    try:
        info = os.lstat(path)
    except OSError:
        raise Refused("missing-lineage-material")
    expected = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if stat.S_ISLNK(info.st_mode) or not expected(info.st_mode):
        raise Refused("unsafe-lineage-material")
    if info.st_uid != owner or info.st_mode & 0o022:
        raise Refused("unsafe-lineage-metadata")
    return info


def validated_pair(block, nginx_etc, owner):
    le_root = "/etc/letsencrypt" if nginx_etc == "/etc/nginx" else \
        os.path.join(os.path.dirname(nginx_etc), "letsencrypt")
    live_root = os.path.join(le_root, "live")
    archive_root = os.path.join(le_root, "archive")
    cert = sole_value(block, "ssl_certificate")[0]
    key = sole_value(block, "ssl_certificate_key")[0]
    cert_parent, cert_name = os.path.split(cert)
    key_parent, key_name = os.path.split(key)
    lineage = os.path.basename(cert_parent)
    if not LINEAGE.fullmatch(lineage) or lineage in (".", ".."):
        raise Refused("unsafe-lineage")
    expected_parent = os.path.join(live_root, lineage)
    if cert_parent != expected_parent or key_parent != expected_parent or \
            cert_name != "fullchain.pem" or key_name != "privkey.pem":
        raise Refused("noncanonical-live-path")

    for directory in (le_root, live_root, expected_parent, archive_root,
                      os.path.join(archive_root, lineage)):
        safe_metadata(directory, owner, "directory")

    revisions = []
    for path, stem in ((cert, "fullchain"), (key, "privkey")):
        link = os.lstat(path)
        if not stat.S_ISLNK(link.st_mode) or link.st_uid != owner:
            raise Refused("nonstandard-live-link")
        target = os.readlink(path)
        match = re.fullmatch(
            r"\.\./\.\./archive/" + re.escape(lineage) + "/" +
            stem + r"([1-9][0-9]*)\.pem", target)
        if not match:
            raise Refused("nonstandard-live-link")
        archive_path = os.path.join(archive_root, lineage,
                                    stem + match.group(1) + ".pem")
        if os.path.realpath(path) != os.path.realpath(archive_path):
            raise Refused("out-of-tree-live-link")
        material = safe_metadata(archive_path, owner, "file")
        if stem == "privkey" and stat.S_IMODE(material.st_mode) & 0o077:
            raise Refused("unsafe-private-key-mode")
        revisions.append(match.group(1))
    if revisions[0] != revisions[1]:
        raise Refused("mixed-lineage-revision")
    return cert, key


def certbot_candidate(block, nginx_etc, owner):
    cert = sole_value(block, "ssl_certificate")[0]
    key = sole_value(block, "ssl_certificate_key")[0]
    le_root = "/etc/letsencrypt" if nginx_etc == "/etc/nginx" else \
        os.path.join(os.path.dirname(nginx_etc), "letsencrypt")
    live_root = os.path.join(le_root, "live")
    live_prefix = live_root + os.sep
    values = (cert, key)
    if not all(os.path.isabs(value) for value in values):
        raise Refused("relative-certificate-path")
    canonical = all(os.path.normpath(value) == value for value in values)
    under_live = tuple(value.startswith(live_prefix) for value in values)
    if not any(under_live):
        if not canonical:
            raise Refused("noncanonical-certificate-path")
        # A complete, absolute pair wholly outside the derived Certbot live
        # root is explicitly non-Certbot. It contributes no bytes.
        return None
    if not all(under_live) or not canonical:
        raise Refused("mixed-or-noncanonical-live-path")
    return validated_pair(block, nginx_etc, owner)


def overlay(repo_data, repo_block, cert, key):
    replacements = [
        (sole_value(repo_block, "ssl_certificate")[1:3], cert),
        (sole_value(repo_block, "ssl_certificate_key")[1:3], key),
    ]
    try:
        output = repo_data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("invalid-utf8-config")
    for (start, end), value in sorted(replacements, reverse=True):
        output = output[:start] + value + output[end:]
    return output.encode("utf-8")


def main():
    if len(sys.argv) != 5 or sys.argv[1] != "__mojo_tls_vhost__":
        raise Refused("invalid-invocation")
    source, destination, nginx_etc = map(os.path.abspath, sys.argv[2:])
    source_prior = snapshot(source)
    try:
        installed_prior = snapshot(destination, required=False)
    except Refused:
        # Existing symlinks and non-regular destinations are never replaced.
        raise

    output = source_prior[2]
    owner = 0 if nginx_etc == "/etc/nginx" else os.geteuid()
    if installed_prior is not None:
        info = installed_prior[0]
        installed_safe = info[6] == owner and not (info[5] & 0o022)
        if not installed_safe:
            raise Refused("unsafe-installed-metadata")
        repo_block = tls_block(source_prior[2], allow_none=True)
        installed_block = tls_block(installed_prior[2], allow_none=True)
        if repo_block is None and installed_block is not None:
            raise Refused("repository-dropped-tls-server")
        if repo_block is not None and installed_block is not None:
            if names(repo_block) != names(installed_block):
                raise Refused("server-name-mismatch")
            candidate = certbot_candidate(installed_block, nginx_etc, owner)
            if candidate is not None:
                cert, key = candidate
                output = overlay(source_prior[2], repo_block, cert, key)

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


try:
    main()
except (OSError, Refused, ValueError):
    print("TLS vhost convergence refused unsafe or changed state", file=sys.stderr)
    raise SystemExit(1)
PY
        die "TLS vhost convergence refused unsafe or changed state"
    fi
}

install_aux_file() {
    local phase="$1" src="$2" dest="$3"
    if [[ ! -f "$src" ]]; then
        record_warning "$phase" "auxiliary source is missing: $src"
        return 1
    fi
    if ! trusted_change rendered-host-config "$dest" -- cp -f "$src" "$dest"; then
        record_warning "$phase" "could not install auxiliary host config: $dest"
        return 1
    fi
}

# The producer helper deliberately lives outside site-packages. A release that
# first ships it copies it here while the integrity profile is still inactive;
# later releases use the already-installed, root-owned copy before pip replaces
# any package code. That stable process derives exact paths from wheel and
# installed RECORDs, spans the mutating pip child, and aborts on child failure.
MOJOSEC_STABLE_HELPER="${MOJOSEC_STABLE_HELPER:-/usr/local/lib/mojosec/mojosec_changes.py}"
MOJOSEC_PY_FLAGS=(-E)
if (cd / && "$MOJOSEC_PYTHON" -E -c \
        'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)'); then
    MOJOSEC_PY_FLAGS+=(-P)
fi
MOJOSEC_CHANGE_SEQUENCE=0
stable_helper_is_safe() {
    [ -f "$MOJOSEC_STABLE_HELPER" ] && [ ! -L "$MOJOSEC_STABLE_HELPER" ] && \
        [ "$(stat -c %u "$MOJOSEC_STABLE_HELPER")" = "0" ] && \
        [ $((8#$(stat -c %a "$MOJOSEC_STABLE_HELPER") & 022)) -eq 0 ]
}
MOJOSEC_STABLE_HELPER_AVAILABLE=0
stable_helper_is_safe && MOJOSEC_STABLE_HELPER_AVAILABLE=1
MOJOSEC_CHANGES_AVAILABLE=0
[ -e "${MOJOSEC_ETC}/config.json" ] && \
    [ "$MOJOSEC_STABLE_HELPER_AVAILABLE" = "1" ] && \
    MOJOSEC_CHANGES_AVAILABLE=1

run_change_helper() {
    (cd / && "$MOJOSEC_PYTHON" "${MOJOSEC_PY_FLAGS[@]}" \
        "$MOJOSEC_STABLE_HELPER" "$@")
}

trusted_change() {
    local kind="$1"
    shift
    local change_args=()
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
        change_args+=(--path "$1")
        shift
    done
    [ "$#" -gt 0 ] || die "trusted change is missing child separator"
    shift
    [ "$#" -gt 0 ] || die "trusted change is missing child command"
    if [ "$MOJOSEC_CHANGES_AVAILABLE" = "1" ]; then
        MOJOSEC_CHANGE_SEQUENCE=$((MOJOSEC_CHANGE_SEQUENCE+1))
        run_change_helper run \
            --operation-id "post-deploy-$$-${MOJOSEC_CHANGE_SEQUENCE}" \
            --kind "$kind" --deployment-id "post-deploy-$$" \
            "${change_args[@]}" -- "$@"
    else
        "$@"
    fi
}

trusted_pip() {
    if [ "$MOJOSEC_CHANGES_AVAILABLE" = "1" ]; then
        MOJOSEC_CHANGE_SEQUENCE=$((MOJOSEC_CHANGE_SEQUENCE+1))
        run_change_helper pip-run \
            --operation-id "post-deploy-$$-${MOJOSEC_CHANGE_SEQUENCE}" \
            --deployment-id "post-deploy-$$" -- "$@"
    else
        "$@"
    fi
}

# ── framework resolution ─────────────────────────────────────────────────────
#
# The API being deployed already passed its own tests; the framework step must
# never veto it over resolution trivia. PyPI's JSON API is the fresh authority
# (purged at upload); pip resolves through the Simple index, a DIFFERENT
# cached document that can lag it by minutes — and pip's own catalog cache
# honors that lag since pip 26.2. So: confirm the target against the JSON API,
# converge onto it with bounded retries whose retries bypass every pip cache
# (`--no-cache-dir` — works on every pip ever shipped, no feature-detected
# flags), and if it STILL will not install, deploy on the framework already
# present and file a deploy warning naming both versions. The fleet dashboards
# already show each node's installed framework, so divergence is a visible
# fact, never a failed deploy. The ONLY fatal state is a node with no
# framework at all: nothing could serve.

FRAMEWORK_RETRIES="${FRAMEWORK_RETRIES:-6}"
FRAMEWORK_RETRY_DELAY="${FRAMEWORK_RETRY_DELAY:-30}"

installed_framework() {
    python3 -c "import mojo; print(mojo.__version__)" 2>/dev/null || true
}

# exists|absent|unknown for one exact version, per the JSON API. `absent` is
# definitive (a 404 means waiting cannot help); transport failures retry
# briefly and then answer `unknown`, which still attempts the install — pip
# may reach a mirror this probe cannot.
pypi_framework_state() {
    local code attempt
    for attempt in 1 2 3; do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
            "https://pypi.org/pypi/django-mojo/${1}/json" 2>/dev/null || echo 000)"
        case "$code" in
            200) echo exists; return 0 ;;
            404) echo absent; return 0 ;;
        esac
        sleep 2
    done
    echo unknown
}

pypi_latest_framework() {
    curl -fsS --max-time 10 "https://pypi.org/pypi/django-mojo/json" 2>/dev/null \
        | grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 \
        | sed 's/.*"\([^"]*\)"$/\1/' || true
}

converge_framework() { # exact version -> 0 once installed
    local target="$1" attempt=1
    local args=()
    while :; do
        if trusted_pip pip install "${args[@]}" "django-mojo==${target}"; then
            return 0
        fi
        [ "$attempt" -ge "$FRAMEWORK_RETRIES" ] && return 1
        attempt=$((attempt+1))
        args=(--no-cache-dir)
        log "  django-mojo==${target} not resolvable yet; retry ${attempt}/${FRAMEWORK_RETRIES} in ${FRAMEWORK_RETRY_DELAY}s..."
        sleep "$FRAMEWORK_RETRY_DELAY"
    done
}

# ── dependencies ─────────────────────────────────────────────────────────────
#
# Project deps first (exact pins from requirements.txt — regenerated with
# `uv export`, django-mojo deliberately excluded from it), the framework LAST
# so its resolver can lift any of its own dependencies past those pins.
# Fleet deploys pass --framework: the orchestrator resolves the newest
# django-mojo ONCE per deploy and every node installs exactly that — pinned
# across nodes for the seconds a deploy takes, never across time. Bare runs
# converge onto the JSON API's latest. A framework that will not install is a
# deploy WARNING, never a veto — see the framework resolution block above.

# A project whose only dependency IS django-mojo legitimately has nothing to
# pin, and dying on the missing file is a worse failure than the one that check
# was protecting against: it makes every deploy on such a project fail at this
# line, after the tree has already moved. The file being absent is a fact about
# the project; the file being present and unusable is still fatal.
phase_begin deps
if [ -f "${PROJ_PATH}/requirements.txt" ]; then
    log "Installing project dependencies..."
    trusted_pip pip install -r "${PROJ_PATH}/requirements.txt" \
        || die "dependency install failed — refusing to restart with an incomplete environment"
else
    log "no requirements.txt — installing the framework only. Export one with \
\`uv export --no-emit-project --no-hashes\` (excluding django-mojo) if this \
project has dependencies of its own."
fi
phase_end

phase_begin framework
CURRENT_FRAMEWORK="$(installed_framework)"
TARGET_FRAMEWORK="$FRAMEWORK"
if [ -z "$TARGET_FRAMEWORK" ]; then
    # Bare runs converge onto the JSON API's latest — never a blind
    # `--upgrade` through a possibly-stale Simple index, which silently
    # installs the PREVIOUS version and looks like success.
    TARGET_FRAMEWORK="$(pypi_latest_framework)"
    if [ -n "$TARGET_FRAMEWORK" ]; then
        log "Resolved django-mojo latest=${TARGET_FRAMEWORK} from the PyPI JSON API"
    fi
fi
if [ -z "$TARGET_FRAMEWORK" ]; then
    record_warning framework "could not resolve a django-mojo target (no pin, PyPI JSON unreachable); deploying on installed ${CURRENT_FRAMEWORK:-none}"
elif [ "$TARGET_FRAMEWORK" = "$CURRENT_FRAMEWORK" ]; then
    log "django-mojo ${CURRENT_FRAMEWORK} already installed (matches target)"
else
    FRAMEWORK_STATE="$(pypi_framework_state "$TARGET_FRAMEWORK")"
    if [ "$FRAMEWORK_STATE" = "absent" ]; then
        record_warning framework "django-mojo==${TARGET_FRAMEWORK} is not on PyPI (JSON API 404 — waiting cannot help); deploying on installed ${CURRENT_FRAMEWORK:-none}"
    else
        log "Installing django-mojo==${TARGET_FRAMEWORK} (fleet target, JSON API says ${FRAMEWORK_STATE})..."
        if converge_framework "$TARGET_FRAMEWORK"; then
            log "  django-mojo==${TARGET_FRAMEWORK} installed"
        else
            record_warning framework "django-mojo==${TARGET_FRAMEWORK} would not install after ${FRAMEWORK_RETRIES} attempts; deploying on installed ${CURRENT_FRAMEWORK:-none}"
        fi
    fi
fi
FINAL_FRAMEWORK="$(installed_framework)"
[ -n "$FINAL_FRAMEWORK" ] \
    || die "no django-mojo is installed at all — nothing can serve without a framework"
log "  framework for this deploy: django-mojo ${FINAL_FRAMEWORK}"
phase_end

# Refresh the stable producer only after the framework install is complete.
# When an old stable copy exists, it journals its own exact replacement; the
# running interpreter already has the old inode loaded, so self-replacement is
# safe in the same way as this shell script's packaged inode replacement.
MOJOSEC_CURRENT_HELPER="$(cd / && "$MOJOSEC_PYTHON" "${MOJOSEC_PY_FLAGS[@]}" -c \
    'import mojo.deploy.mojosec_changes as m; print(m.__file__)' 2>/dev/null || true)"
if [ -n "$MOJOSEC_CURRENT_HELPER" ] && [ -f "$MOJOSEC_CURRENT_HELPER" ] && \
        [ ! -L "$MOJOSEC_CURRENT_HELPER" ]; then
    helper_ok=1
    if [ "$MOJOSEC_STABLE_HELPER_AVAILABLE" = "1" ]; then
        trusted_change mojosec-producer-helper \
            "$(dirname "$MOJOSEC_STABLE_HELPER")" "$MOJOSEC_STABLE_HELPER" -- \
            install -D -m 0755 "$MOJOSEC_CURRENT_HELPER" "$MOJOSEC_STABLE_HELPER" \
            || helper_ok=0
    else
        install -D -m 0755 "$MOJOSEC_CURRENT_HELPER" "$MOJOSEC_STABLE_HELPER" \
            || helper_ok=0
    fi
    if [ "$helper_ok" = "1" ] && stable_helper_is_safe; then
        MOJOSEC_STABLE_HELPER_AVAILABLE=1
        [ ! -e "${MOJOSEC_ETC}/config.json" ] || MOJOSEC_CHANGES_AVAILABLE=1
    else
        MOJOSEC_STABLE_HELPER_AVAILABLE=0
        MOJOSEC_CHANGES_AVAILABLE=0
        record_warning mojosec_helper \
            "could not refresh the stable MojoSec trusted-change helper"
    fi
fi

# ── migrations ───────────────────────────────────────────────────────────────
#
# Migrations run ONLY when the deploy says so (--migrate — the canary run),
# under the real Postgres advisory lock inside migrate_locked. A per-box flag
# file cannot serialise anything; the advisory lock can — a concurrent
# migrate exits non-zero instead of racing.

if [ "$MIGRATE" = "1" ]; then
    log "Running migrations (locked)..."
    phase_begin migrate
    python3 "${PROJ_PATH}/bin/manage.py" migrate_locked --noinput \
        || die "migration failed — the schema is in an unknown state, NOT restarting the app"
    phase_end
else
    log "Skipping migrations (deploy did not request them)."
fi

log "Collecting static files..."
phase_begin collectstatic
python3 "${PROJ_PATH}/bin/manage.py" collectstatic --noinput \
    || die "collectstatic failed"
phase_end

# ── render ───────────────────────────────────────────────────────────────────
#
# The JUST-INSTALLED framework's cron/systemd templates, rendered with this
# node's parameters into var/deploy/, plus any project extras from aws/. All
# /etc convergence below installs FROM var/deploy/, and check_node audits
# /etc AGAINST var/deploy/ — so an empty or failed render must stop the
# deploy here, before anything touches /etc.

log "Rendering node templates into var/deploy..."
phase_begin render
# This unchanged argv is also the first-upgrade nginx runtime repair hook:
# the newly installed Python module converges and verifies the persistent
# spill paths before this old-inode shell can reach MojoSec or nginx reload.
python3 -m mojo.deploy render --dest "$DEPLOY_DIR" \
        --project-path "$PROJ_PATH" --app-user "$APP_USER" \
        --web-user "$WEB_USER" --workers "$ASGI_WORKERS" \
    || die "template render failed — not converging /etc against an unknown contract"
compgen -G "${DEPLOY_DIR}/cron.d/*" > /dev/null \
    || record_warning cron "render produced no cron entries"
[[ -f "${DEPLOY_DIR}/systemd/mojo-asgi.service" ]] \
    || die "render did not produce mojo-asgi.service — the web app cannot start"
phase_end

# ── nginx ────────────────────────────────────────────────────────────────────

log "Updating nginx configs..."
phase_begin nginx
install_file "${PROJ_PATH}/aws/nginx/nginx.conf"  "${NGINX_ETC}/nginx.conf"
install_file "${PROJ_PATH}/aws/nginx/asgi.inc"    "${NGINX_ETC}/asgi.inc"
MOJOSEC_DJANGO_EXISTED=0
MOJOSEC_DJANGO_BACKUP=""
if [ -e "${NGINX_ETC}/django.inc" ]; then
    [ ! -L "${NGINX_ETC}/django.inc" ] \
        || die "refusing symlink nginx django.inc"
    [ -f "${NGINX_ETC}/django.inc" ] \
        || die "nginx django.inc is not a regular file"
fi
install_file "${PROJ_PATH}/aws/nginx/django.inc"  "${NGINX_ETC}/django.inc"
# MojoSec may augment django.inc, but its rollback baseline is the NEW release
# contract installed above — never the previous release's routes. Otherwise an
# auxiliary sensor failure can silently resurrect stale auth routing.
if MOJOSEC_DJANGO_BACKUP="$(mktemp "${DEPLOY_DIR}/.mojosec-django.XXXXXX")" && \
        cp -p "${NGINX_ETC}/django.inc" "$MOJOSEC_DJANGO_BACKUP"; then
    MOJOSEC_DJANGO_EXISTED=1
else
    [ -z "$MOJOSEC_DJANGO_BACKUP" ] || rm -f -- "$MOJOSEC_DJANGO_BACKUP"
    MOJOSEC_DJANGO_BACKUP=""
    record_warning mojosec "could not snapshot the nginx baseline; skipping MojoSec"
fi

if compgen -G "${PROJ_PATH}/aws/nginx/sec.d/*.conf" > /dev/null; then
    if trusted_change rendered-nginx "${NGINX_ETC}/sec.d" -- \
            mkdir -p "${NGINX_ETC}/sec.d"; then
        for source in "${PROJ_PATH}/aws/nginx/sec.d/"*.conf; do
            install_aux_file nginx_security \
                "$source" "${NGINX_ETC}/sec.d/$(basename "$source")" || true
        done
    else
        record_warning nginx_security "could not prepare nginx security includes"
    fi
fi

# Repo-owned vhosts, converged on every deploy — the repo's aws/nginx/conf.d/
# IS the set of fleet-served vhosts. `.example` files are templates for
# operators and are deliberately not installed. Other vhosts on a node
# (single-node domains) stay node-managed and are not touched from git;
# retiring a once-shipped name is aws/node_retired.conf's job below.

trusted_change rendered-nginx "${NGINX_ETC}/conf.d" -- \
    mkdir -p "${NGINX_ETC}/conf.d"
VHOSTS=0
if compgen -G "${PROJ_PATH}/aws/nginx/conf.d/*.conf" > /dev/null; then
    for vhost in "${PROJ_PATH}/aws/nginx/conf.d/"*.conf; do
        name="$(basename "$vhost")"
        case "$name" in *.example*) continue ;; esac
        install_vhost "$vhost" "${NGINX_ETC}/conf.d/${name}"
        VHOSTS=$((VHOSTS+1))
    done
fi
log "  converged ${VHOSTS} vhost(s) from aws/nginx/conf.d"

# The nginx.conf installed above may itself declare (or drop) the
# $connection_upgrade map the render-phase converge decided around — a
# bootstrap upgraded to the bootstrap-owned contract would otherwise leave
# two declarations in the graph until the next root render. Re-decide against
# the post-install graph; exactly one declaration must survive. Root-gated
# exactly like the render-phase converge itself (a non-root run cannot own
# /etc and is a harness, not a deploy).
if [ "$(id -u)" = "0" ]; then
    trusted_change rendered-host-config "${NGINX_ETC}/conf.d/00_django_mojo_runtime.conf" -- \
        python3 -m mojo.deploy.nginx_runtime reconcile --nginx-etc "$NGINX_ETC" \
        || die "nginx upgrade-map reconcile failed — the host graph would not converge"
else
    log "  skipping upgrade-map reconcile (not root)"
fi

# MojoSec root assets come from the installed package, never from var/deploy
# or the application-writable project tree. `observe` reports only; all ban
# policy remains central. best_effort makes an unenrolled old node a warning,
# while required makes a configured fleet fail closed at deploy time.
converge_mojosec() {
log "Converging MojoSec (${MOJOSEC_MODE}, ${MOJOSEC_DEPLOY_CRITICALITY})..."
restore_mojosec_django() {
    if [ "$MOJOSEC_DJANGO_EXISTED" = "1" ]; then
        trusted_change mojosec-rollback "${NGINX_ETC}/django.inc" -- \
            cp -p "$MOJOSEC_DJANGO_BACKUP" "${NGINX_ETC}/django.inc"
    else
        trusted_change mojosec-rollback "${NGINX_ETC}/django.inc" -- \
            rm -f -- "${NGINX_ETC}/django.inc"
    fi
    rm -f -- "$MOJOSEC_DJANGO_BACKUP"
}
MOJOSEC_PRIOR_ACTIVE=0
MOJOSEC_PRIOR_ENABLED=0
MOJOSEC_LIFECYCLE_SNAPSHOTTED=0
MOJOSEC_DOWNGRADE_HANDOFF=0
MOJOSEC_AUDIT_HELPER="${MOJOSEC_AUDIT_HELPER:-/usr/local/lib/mojosec/mojosec_audit.py}"
MOJOSEC_AUDIT_STATE="${MOJOSEC_AUDIT_STATE:-${MOJOSEC_ETC}/audit-state.json}"
MOJOSEC_AUDIT_PYTHON="${MOJOSEC_AUDIT_PYTHON:-/usr/bin/python3}"
snapshot_mojosec_lifecycle() {
    [ "$MOJOSEC_LIFECYCLE_SNAPSHOTTED" = "0" ] || return 0
    systemctl is-active --quiet mojosec.service && MOJOSEC_PRIOR_ACTIVE=1 || true
    systemctl is-enabled --quiet mojosec.service && MOJOSEC_PRIOR_ENABLED=1 || true
    MOJOSEC_LIFECYCLE_SNAPSHOTTED=1
}
quiesce_mojosec_for_downgrade() {
    snapshot_mojosec_lifecycle
    systemctl stop mojosec.service
    ! systemctl is-active --quiet mojosec.service
}
restore_mojosec_service_after_failure() {
    [ "$MOJOSEC_LIFECYCLE_SNAPSHOTTED" = "1" ] || return 0
    systemctl stop mojosec.service 2>/dev/null || true
    if [ "$MOJOSEC_PRIOR_ENABLED" = "1" ]; then
        systemctl enable mojosec.service
    else
        systemctl disable mojosec.service 2>/dev/null || true
    fi
    if [ "$MOJOSEC_PRIOR_ACTIVE" = "1" ]; then systemctl start mojosec.service; fi
}
retire_mojosec_provenance_assets() {
    [ "$MOJOSEC_DOWNGRADE_HANDOFF" = "1" ] || return 0
    systemctl disable --now mojosec-audit-health.timer 2>/dev/null || true
    assets=(
        /etc/systemd/system/mojosec-audit-health.service
        /etc/systemd/system/mojosec-audit-health.timer
        /etc/sudoers.d/70-mojo-firewall-broker
        /usr/local/sbin/mojo-firewall-broker
        "$MOJOSEC_AUDIT_HELPER"
        /run/mojosec/audit-health.json
    )
    rm -f -- "${assets[@]}"
    systemctl daemon-reload
    for asset in "${assets[@]}"; do
        [ ! -e "$asset" ] && [ ! -L "$asset" ] \
            || return 1
    done
}

# Distinguish the current provenance-aware module, an installed older module,
# and a package with no MojoSec deployment support. A downgrade must restore
# the exact pre-feature Audit state before the old module is allowed to run.
set +e
(cd / && "$MOJOSEC_PYTHON" "${MOJOSEC_PY_FLAGS[@]}" -c \
    'import importlib.util,sys
spec=importlib.util.find_spec("mojo.deploy.mojosec")
if spec is None: sys.exit(3)
from mojo.deploy import mojosec
sys.exit(0 if getattr(mojosec,"PROVENANCE_CAPABILITY",0)==1 else 4)')
module_rc=$?
set -e
case "$module_rc" in
    0) MOJOSEC_MODULE_AVAILABLE=1; MOJOSEC_PROVENANCE_AVAILABLE=1 ;;
    4) MOJOSEC_MODULE_AVAILABLE=1; MOJOSEC_PROVENANCE_AVAILABLE=0 ;;
    3) MOJOSEC_MODULE_AVAILABLE=0; MOJOSEC_PROVENANCE_AVAILABLE=0 ;;
    *) restore_mojosec_django; die "cannot preflight MojoSec deployment module" ;;
esac

if [ "$MOJOSEC_PROVENANCE_AVAILABLE" = "0" ] && \
   { [ -f "$MOJOSEC_AUDIT_STATE" ] || [ -f "$MOJOSEC_AUDIT_HELPER" ]; }; then
    log "Restoring pre-feature Audit state for MojoSec package downgrade"
    [ -f "$MOJOSEC_AUDIT_HELPER" ] && [ ! -L "$MOJOSEC_AUDIT_HELPER" ] && \
    [ "$(stat -c %u "$MOJOSEC_AUDIT_HELPER")" = "0" ] \
        || { restore_mojosec_django; die "cannot trust MojoSec Audit downgrade helper"; }
    quiesce_mojosec_for_downgrade \
        || { restore_mojosec_django; restore_mojosec_service_after_failure; \
             die "cannot quiesce MojoSec for downgrade"; }
    "$MOJOSEC_AUDIT_PYTHON" -E -P "$MOJOSEC_AUDIT_HELPER" flush-pending \
        || { restore_mojosec_django; restore_mojosec_service_after_failure; \
             die "cannot flush MojoSec pending events"; }
    if [ -f "$MOJOSEC_AUDIT_STATE" ]; then
        "$MOJOSEC_AUDIT_PYTHON" -E -P "$MOJOSEC_AUDIT_HELPER" restore \
            || { restore_mojosec_django; restore_mojosec_service_after_failure; \
                 die "cannot restore pre-feature Audit state"; }
    fi
    MOJOSEC_DOWNGRADE_HANDOFF=1
fi

if [ "$MOJOSEC_MODULE_AVAILABLE" = "1" ]; then
    MOJOSEC_CONVERGE_ARGS=(--mode "$MOJOSEC_MODE" \
                          --criticality "$MOJOSEC_DEPLOY_CRITICALITY")
    if [ "$MOJOSEC_PROVENANCE_AVAILABLE" = "1" ]; then
        MOJOSEC_CONVERGE_ARGS+=(--project-path "$PROJ_PATH")
    fi
    # Declare only destinations this journal is permitted to journal. MojoSec
    # control state (/etc/mojosec, /var/lib/mojosec, /run/mojosec — including
    # the Audit rollback record audit-state.json) belongs to the converge child
    # below, which transacts it itself, and validate_paths rejects it by design.
    # Never add a path under those roots here (item 2014).
    if ! trusted_change mojosec-converge \
            "${SYSTEMD_ETC}/mojosec.service" \
            "${NGINX_ETC}/conf.d/00_mojosec.conf" \
            "${NGINX_ETC}/snippets/mojosec_receiver.conf" \
            "${NGINX_ETC}/django.inc" \
            "${LOGROTATE_ETC}/mojosec" \
            /etc/audit/rules.d /etc/audit/audit.rules \
            /etc/systemd/system/mojosec-audit-health.service \
            /etc/systemd/system/mojosec-audit-health.timer \
            /etc/sudoers.d/70-mojo-firewall-broker \
            /usr/local/sbin/mojo-firewall-broker \
            /usr/local/lib/mojosec/mojosec_audit.py -- \
            "$MOJOSEC_PYTHON" "${MOJOSEC_PY_FLAGS[@]}" \
            -m mojo.deploy.mojosec converge \
            "${MOJOSEC_CONVERGE_ARGS[@]}"; then
        restore_mojosec_django
        restore_mojosec_service_after_failure
        nginx -t || die "MojoSec failed and the exact prior django.inc is invalid"
        systemctl reload nginx \
            || die "cannot reload exact prior nginx graph after MojoSec failure"
        die "MojoSec deployment failed"
    fi
    if [ "$MOJOSEC_DOWNGRADE_HANDOFF" = "1" ]; then
        retire_mojosec_provenance_assets \
            || { restore_mojosec_django; restore_mojosec_service_after_failure; \
                 die "cannot retire MojoSec provenance assets after old-module handoff"; }
    fi
else
    # A downgrade can replace the package while this script keeps running from
    # its old inode. Enrolled lifecycle cannot be guessed when enrollment
    # exists; only an explicitly off or genuinely unenrolled node is retired.
    fallback_mode="$MOJOSEC_MODE"
    # This script may still be the new shim while pip has just installed a
    # pre-broker django-mojo. Restore the exact pre-feature Audit state with
    # the root-owned stable helper before removing broker-only assets. Legacy
    # direct firewall grants deliberately remain usable for this generation.
    snapshot_mojosec_lifecycle
    quiesce_mojosec_for_downgrade \
        || { restore_mojosec_django; restore_mojosec_service_after_failure; \
             die "cannot quiesce MojoSec module-absent cleanup"; }
    if [ "$fallback_mode" = "enrolled" ]; then
        [ ! -e "${MOJOSEC_ETC}/enrollment.json" ] \
            || { restore_mojosec_django; restore_mojosec_service_after_failure; \
                 die "old package cannot resolve enrolled MojoSec lifecycle"; }
        fallback_mode=off
    fi
    [ "$fallback_mode" = "off" ] \
        || { restore_mojosec_django; restore_mojosec_service_after_failure; \
             die "old package cannot deploy MojoSec observe mode"; }
    log "MojoSec module absent; applying transactional pre-feature off cleanup"
    fallback_dir="$(mktemp -d "${DEPLOY_DIR}/.mojosec-off.XXXXXX")"
    managed=("${NGINX_ETC}/conf.d/00_mojosec.conf" "${LOGROTATE_ETC}/mojosec")
    index=0
    for path in "${managed[@]}"; do
        [ ! -L "$path" ] || { restore_mojosec_django; \
            restore_mojosec_service_after_failure; \
            die "refusing symlink during MojoSec cleanup: $path"; }
        if [ -e "$path" ]; then
            [ -f "$path" ] && [ "$(stat -c %u "$path")" = "0" ] \
                || { restore_mojosec_django; restore_mojosec_service_after_failure; \
                     die "refusing unsafe MojoSec cleanup target: $path"; }
            cp -p "$path" "$fallback_dir/$index"
        else
            : > "$fallback_dir/$index.absent"
        fi
        index=$((index+1))
    done
    if [ -e "${SYSTEMD_ETC}/mojosec.service" ] || \
            [ "$MOJOSEC_PRIOR_ACTIVE" = "1" ] || [ "$MOJOSEC_PRIOR_ENABLED" = "1" ]; then
        systemctl disable --now mojosec.service \
            || { restore_mojosec_django; restore_mojosec_service_after_failure; \
                 die "cannot stop pre-feature MojoSec service"; }
    fi
    rm -f -- "${managed[@]}"
    fallback_ok=1
    nginx -t || fallback_ok=0
    [ "$fallback_ok" = "0" ] || systemctl reload nginx || fallback_ok=0
    if [ "$fallback_ok" = "0" ]; then
        index=0
        for path in "${managed[@]}"; do
            if [ -f "$fallback_dir/$index" ]; then
                cp -p "$fallback_dir/$index" "$path"
            else
                rm -f -- "$path"
            fi
            index=$((index+1))
        done
        restore_mojosec_django
        nginx -t || { restore_mojosec_service_after_failure; \
            die "pre-feature cleanup rollback restored an invalid graph"; }
        systemctl reload nginx || { restore_mojosec_service_after_failure; \
            die "cannot reload pre-feature cleanup rollback"; }
        restore_mojosec_service_after_failure
        die "pre-feature MojoSec off cleanup failed and was rolled back"
    fi
    rm -f -- "$fallback_dir/0" "$fallback_dir/0.absent" \
              "$fallback_dir/1" "$fallback_dir/1.absent"
    rmdir "$fallback_dir"
    retire_mojosec_provenance_assets \
        || { restore_mojosec_django; restore_mojosec_service_after_failure; \
             die "cannot retire MojoSec provenance assets after module-absent handoff"; }
fi
rm -f -- "$MOJOSEC_DJANGO_BACKUP"
}

# MojoSec is an observe-and-report subsystem, not an application availability
# gate. Run it with strict internal rollback, then continue from the new
# release's nginx baseline if convergence itself fails. The later nginx test
# remains fatal, so this can never wave through a broken serving graph.
phase_end nginx
phase_begin mojosec
MOJOSEC_RC=0
if [ -n "$MOJOSEC_DJANGO_BACKUP" ]; then
    set +e
    ( set -e; converge_mojosec )
    MOJOSEC_RC=$?
    set -e
fi
if [ "$MOJOSEC_RC" -ne 0 ]; then
    if [ -f "$MOJOSEC_DJANGO_BACKUP" ]; then
        cp -p "$MOJOSEC_DJANGO_BACKUP" "${NGINX_ETC}/django.inc" \
            || die "cannot restore the new-release nginx baseline after MojoSec failure"
        rm -f -- "$MOJOSEC_DJANGO_BACKUP"
    fi
    record_warning mojosec "MojoSec convergence failed and restored its prior state"
fi
phase_end

# ── retired names ────────────────────────────────────────────────────────────
#
# Names this project ONCE shipped into cron.d/conf.d and has since retired,
# declared in aws/node_retired.conf (lines `cron.d/<name>` / `conf.d/<name>`,
# `#` comments). An explicit list, never a discovery sweep, for the files the
# structural cron sweep below cannot catch: entries that never mention
# PROJ_PATH, and conf.d vhosts (where a mentions-the-project rule would
# delete live node-managed domains). Removals are logged, never silent. The
# file lives OUTSIDE aws/cron.d/ so the cron convergence glob can never copy
# the list itself into /etc.

RETIRED_LIST="${PROJ_PATH}/aws/node_retired.conf"
if [[ -f "$RETIRED_LIST" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        line="${line//[[:space:]]/}"
        [[ -n "$line" ]] || continue
        case "$line" in
            cron.d/*) target="${CRON_ETC}/${line#cron.d/}" ;;
            conf.d/*) target="${NGINX_ETC}/conf.d/${line#conf.d/}" ;;
            *) log "  WARN: node_retired.conf line not understood: ${line}"; continue ;;
        esac
        case "${line#*/}" in
            ""|*/*|.|..) log "  WARN: refusing suspicious retired name: ${line}"; continue ;;
        esac
        if [[ -f "$target" ]]; then
            log "  retiring declared name: ${line}"
            trusted_change retired-node-config "$target" -- rm -f "$target" \
                || record_warning retired_config \
                    "could not retire declared host config: ${line}"
        fi
    done < "$RETIRED_LIST"
fi

# Gate the reload on the config test, and treat a failed test as fatal. nginx
# keeps serving the old config either way, but continuing the deploy would
# restart the app behind a web server running configuration that no longer
# matches the code that was just installed.
nginx -t || die "nginx config test failed — not reloading, and not restarting the app"
systemctl reload nginx || die "nginx passed validation but could not reload"

# ── systemd ──────────────────────────────────────────────────────────────────
#
# From the RENDERED set, timers as well as services. Copying only *.service
# is why config-sync.timer was never installed on any box despite the unit
# existing in the repo.

log "Updating systemd units..."
UNIT_SRC="${DEPLOY_DIR}/systemd"
# The ASGI unit is the application. It remains release-critical.
install_file "${UNIT_SRC}/mojo-asgi.service" "${SYSTEMD_ETC}/mojo-asgi.service"
systemctl daemon-reload || die "systemd could not load the mojo-asgi unit"

# Everything else is background operation. A bad timer or maintenance service
# is visible and repairable, but cannot roll back a web app that can serve.
if compgen -G "${UNIT_SRC}/*.service" > /dev/null; then
    for source in "${UNIT_SRC}/"*.service; do
        [[ "$(basename "$source")" = "mojo-asgi.service" ]] && continue
        install_aux_file auxiliary_systemd \
            "$source" "${SYSTEMD_ETC}/$(basename "$source")" || true
    done
fi
if compgen -G "${UNIT_SRC}/*.timer" > /dev/null; then
    for source in "${UNIT_SRC}/"*.timer; do
        install_aux_file auxiliary_systemd \
            "$source" "${SYSTEMD_ETC}/$(basename "$source")" || true
    done
fi
systemctl daemon-reload \
    || record_warning auxiliary_systemd "systemd rejected auxiliary unit changes"

# Enable any timer we ship. Enabling is idempotent, and a timer that is
# present but never enabled does nothing while looking installed.
for unit in "${UNIT_SRC}/"*.timer; do
    [[ -e "$unit" ]] || continue
    name="$(basename "$unit")"
    if systemctl enable --now "$name"; then
        log "  enabled $name"
    else
        record_warning timers "could not enable auxiliary timer: $name"
    fi
done

# ── cron ─────────────────────────────────────────────────────────────────────
#
# The RENDERED var/deploy/cron.d/ IS the set, not merely a subset that gets
# added to. The sweep DISCOVERS what to retire instead of naming it: a
# hardcoded list of legacy filenames cannot survive contact with a real node —
# fleets have carried `1mojocron`, `2certbot` and `3mojo_jogs` (yes, a typo),
# none of which any guess would have matched, each firing alongside its
# replacement forever. The rule is structural: a file in cron.d that invokes
# PROJ_PATH is ours; if it is ours and the rendered set no longer ships that
# filename, it is stale and goes. Files that never mention PROJ_PATH
# (0hourly, distro entries) are not ours and are left alone (retire those via
# aws/node_retired.conf above). Removals are logged, never silent.

log "Updating cron entries..."
if compgen -G "${DEPLOY_DIR}/cron.d/*" > /dev/null; then
    for source in "${DEPLOY_DIR}/cron.d/"*; do
        install_aux_file cron "$source" "${CRON_ETC}/$(basename "$source")" || true
    done
fi
for installed in "${CRON_ETC}"/*; do
    [[ -f "$installed" ]] || continue
    name="$(basename "$installed")"
    # Ours? (mentions the project path)  Still shipped? (rendered set has it)
    grep -q "${PROJ_PATH}" "$installed" 2>/dev/null || continue
    [[ -f "${DEPLOY_DIR}/cron.d/${name}" ]] && continue
    log "  retiring stale project cron: ${name}"
    trusted_change retired-project-cron "$installed" -- rm -f "$installed" \
        || record_warning cron "could not retire stale project cron: ${name}"
done

# The root-run manage.py steps above can be the FIRST writer of a framework
# log file (edge/github/jobs on a fresh install), leaving it root-owned in
# the web-group directory — which then blocks the web app and the app-user
# engine from appending. Re-assert the standard ownership (provisioning
# establishes the full APP_USER:WEB_USER setgid model).
if [[ -d "${PROJ_PATH}/var/logs" ]]; then
    chown -R "${APP_USER}:${WEB_USER}" "${PROJ_PATH}/var/logs" 2>/dev/null \
        || record_warning log_ownership "could not normalize var/logs ownership"
fi

# ── restart ──────────────────────────────────────────────────────────────────

log "Restarting mojo-asgi..."
phase_begin restart
systemctl restart mojo-asgi || die "mojo-asgi failed to restart"
phase_end

# Snapshot the installed copy of this script next to the rendered contract.
# This is the shim's rollback self-heal: when a rollback reinstalls a
# framework so old that `locate` cannot resolve post_deploy.sh, the shim
# falls back to this snapshot. cmp first: when this run IS the snapshot
# (fallback mode), copying a file onto itself is not progress.
snapshot_self() {
    local snap="${DEPLOY_DIR}/post_deploy.sh" self="${BASH_SOURCE[0]}"
    if [[ -f "$self" ]] && ! cmp -s "$self" "$snap" 2>/dev/null; then
        cp -f "$self" "$snap" 2>/dev/null \
            || log "WARN: could not snapshot post_deploy.sh into var/deploy"
    fi
}

# A restart that returns 0 only means systemd accepted the unit. Confirm the
# app is actually answering before calling the deploy done — through
# PROBE_URL, which the shim points at whatever vhost proxies straight to the
# asgi socket (a port-80 server that 301s everything would false-pass
# `curl -f`).
phase_begin probe
for _ in $(seq 1 15); do
    if curl -fsSk -o /dev/null --max-time 2 "$PROBE_URL" 2>/dev/null; then
        phase_end
        snapshot_self
        log "Post-deploy complete — app responding."
        exit 0
    fi
    sleep 2
done
phase_end
die "app did not answer ${PROBE_URL} within 30s of restart — on an edge-converged node the port-80 catch-all returns 444; export a vhost-true PROBE_URL from the shim"
