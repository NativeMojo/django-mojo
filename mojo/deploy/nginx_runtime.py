"""Persistent nginx worker scratch space used by every serving plane.

This module is deliberately settings-free.  The deployment renderer imports
it after pip installs a new django-mojo wheel, including when the shell still
runs the old ``post_deploy.sh`` inode.
"""

import argparse
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile


RUNTIME_ROOT = "/var/lib/django-mojo/nginx"
FRAGMENT_NAME = "00_django_mojo_runtime.conf"
# The WebSocket upgrade map. Bootstrap-owned by contract since the edge
# renderer stopped emitting it (docs/django_developer/edge/templates.md) —
# but a node provisioned before that contract has no declaration, and any
# include referencing $connection_upgrade then fails every nginx -t. The
# fragment carries the map on exactly those nodes; the decision lives in
# _upgrade_map_decision and always yields to a declaration anywhere else.
UPGRADE_MAP_LINES = (
    "map $http_upgrade $connection_upgrade {",
    "    default upgrade;",
    "    '' close;",
    "}",
)
TEMP_PATHS = (
    ("client_body_temp_path", "client_body"),
    ("proxy_temp_path", "proxy"),
    ("fastcgi_temp_path", "fastcgi"),
    ("uwsgi_temp_path", "uwsgi"),
    ("scgi_temp_path", "scgi"),
)
SELINUX_TYPE = "httpd_sys_rw_content_t"


class NginxRuntimeError(RuntimeError):
    pass


def runtime_paths(root=RUNTIME_ROOT):
    root = os.path.normpath(root)
    return tuple((directive, os.path.join(root, leaf))
                 for directive, leaf in TEMP_PATHS)


def render_http_fragment(root=RUNTIME_ROOT, indent="", include_map=False):
    text = "".join(
        "%s%s %s;\n" % (indent, directive, path)
        for directive, path in runtime_paths(root))
    if include_map:
        text += "".join("%s%s\n" % (indent, line) for line in UPGRADE_MAP_LINES)
    return text


def fragment_path(nginx_etc="/etc/nginx"):
    return os.path.join(nginx_etc, "conf.d", FRAGMENT_NAME)


def parse_worker_user(config):
    users = re.findall(r"(?m)^\s*user\s+([^;\s]+)(?:\s+[^;\s]+)?\s*;", config)
    if len(users) != 1:
        # When nginx -T failed before dumping, `config` is only its error
        # output — quote it, or the operator debugs "got []" while nginx
        # already named the actual breakage.
        errors = [line.strip() for line in config.splitlines()
                  if "[emerg]" in line or "[alert]" in line
                  or "test failed" in line]
        detail = " — nginx reported: %s" % "; ".join(errors[-3:]) if errors else ""
        raise NginxRuntimeError(
            "nginx -T must expose exactly one effective worker user; got %r%s"
            % (users, detail))
    return users[0]


_UPGRADE_MAP_DECL_RE = re.compile(r"map\s+\$http_upgrade\s+\$connection_upgrade\s*{")
_UNKNOWN_UPGRADE_VAR_RE = re.compile(r'unknown "connection_upgrade" variable')
_DUPLICATE_UPGRADE_VAR_RE = re.compile(
    r'(?:duplicate|conflicting)[^\n]{0,60}"connection_upgrade"')


def _dump_sections(dump):
    """{path: text} per `# configuration file <path>:` section of an nginx -T
    dump. {} when the test failed before the dump began (error output only)."""
    sections = {}
    name = None
    lines = []
    for line in dump.splitlines():
        match = re.match(r"^# configuration file (.+):$", line)
        if match:
            if name is not None:
                sections[name] = "\n".join(lines)
            name = match.group(1)
            lines = []
        elif name is not None:
            lines.append(line)
    if name is not None:
        sections[name] = "\n".join(lines)
    return sections


def _upgrade_map_decision(dump, path, prior):
    """Must the runtime fragment carry the $connection_upgrade map?

    Exactly one declaration may exist per graph. Any declaration OUTSIDE the
    fragment (the node bootstrap, a legacy edge generation base) wins and the
    fragment yields; with none anywhere, the fragment carries it so a node
    whose bootstrap predates the bootstrap-owned contract keeps a resolvable
    graph. When nginx -T failed before dumping, nginx's own error decides: an
    unknown-variable failure proves no declaration exists, a duplicate means
    one exists beside ours. Any other breakage keeps the fragment's current
    answer (`prior` bytes, None when absent) — repair must not thrash on an
    unrelated failure.
    """
    sections = _dump_sections(dump)
    if sections:
        for name, text in sections.items():
            if name != path and _UPGRADE_MAP_DECL_RE.search(text):
                return False
        return True
    if _UNKNOWN_UPGRADE_VAR_RE.search(dump):
        return True
    if _DUPLICATE_UPGRADE_VAR_RE.search(dump):
        return False
    if prior is None:
        return False
    return bool(_UPGRADE_MAP_DECL_RE.search(prior.decode("utf-8", "replace")))


def _run(argv, timeout=30):
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise NginxRuntimeError("cannot run %s: %s" % (argv[0], err))
    output = "%s%s" % (done.stdout or "", done.stderr or "")
    if done.returncode != 0:
        tail = output.strip().splitlines()
        raise NginxRuntimeError(
            "%s failed: %s" % (" ".join(argv), tail[-1] if tail else "no output"))
    return output


def nginx_dump(nginx_binary="nginx", allow_failure=False):
    if not allow_failure:
        return _run([nginx_binary, "-T"])
    try:
        done = subprocess.run([nginx_binary, "-T"], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise NginxRuntimeError("cannot run %s -T: %s" % (nginx_binary, err))
    output = "%s%s" % (done.stdout or "", done.stderr or "")
    if not output:
        raise NginxRuntimeError("nginx -T returned no configuration")
    return output


def _assert_absolute_safe(path):
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise NginxRuntimeError("runtime path must be absolute and normalized: %r" % path)
    current = "/"
    for part in path.strip("/").split("/"):
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise NginxRuntimeError("refusing symlink in nginx runtime path: %s" % current)
        if not stat.S_ISDIR(info.st_mode):
            raise NginxRuntimeError("nginx runtime ancestor is not a directory: %s" % current)


def _mkdir_exact(path, uid, gid, mode):
    """Create a directory through no-follow descriptors and set exact metadata."""
    _assert_absolute_safe(path)
    parts = path.strip("/").split("/")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        walked = "/"
        for index, part in enumerate(parts):
            walked = os.path.join(walked, part)
            final = index == len(parts) - 1
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(part, mode if final else 0o755, dir_fd=descriptor)
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor)
            except OSError as err:
                raise NginxRuntimeError(
                    "cannot traverse nginx runtime directory %s: %s" % (walked, err))
            os.close(descriptor)
            descriptor = child
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        info = os.fstat(descriptor)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or
                info.st_gid != gid or stat.S_IMODE(info.st_mode) != mode):
            raise NginxRuntimeError("nginx runtime metadata did not converge: %s" % path)
    finally:
        os.close(descriptor)


def _resolve_user(name):
    try:
        return pwd.getpwnam(name)
    except KeyError:
        raise NginxRuntimeError("configured nginx worker user does not exist: %s" % name)


def _selinux_enforcing():
    binary = shutil.which("getenforce")
    if not binary:
        return False
    done = subprocess.run([binary], capture_output=True, text=True, timeout=10)
    return done.returncode == 0 and done.stdout.strip() == "Enforcing"


def _converge_selinux(root):
    if not _selinux_enforcing():
        return
    semanage = shutil.which("semanage")
    restorecon = shutil.which("restorecon")
    ls = shutil.which("ls")
    if not semanage or not restorecon or not ls:
        raise NginxRuntimeError(
            "SELinux is enforcing but semanage/restorecon/ls is unavailable")
    expression = "%s(/.*)?" % root
    try:
        _run([semanage, "fcontext", "-a", "-t", SELINUX_TYPE, expression])
    except NginxRuntimeError:
        # Existing policy is the idempotent path; -m still fails closed when
        # the -a error had any other cause.
        _run([semanage, "fcontext", "-m", "-t", SELINUX_TYPE, expression])
    _run([restorecon, "-RF", root])
    for _directive, path in runtime_paths(root):
        output = _run([ls, "-Zd", path])
        if ":%s:" % SELINUX_TYPE not in output:
            raise NginxRuntimeError(
                "SELinux label did not converge on %s: %s" % (path, output.strip()))


def _audit_selinux(root):
    if not _selinux_enforcing():
        return []
    binary = shutil.which("ls")
    if not binary:
        return ["SELinux is enforcing but ls -Z is unavailable"]
    errors = []
    for _directive, path in runtime_paths(root):
        try:
            output = _run([binary, "-Zd", path])
        except NginxRuntimeError as err:
            errors.append(str(err))
            continue
        if ":%s:" % SELINUX_TYPE not in output:
            errors.append("wrong SELinux nginx runtime label: %s" % path)
    return errors


def _probe_as_worker(user, root):
    """Prove the actual worker uid can create and unlink in every leaf."""
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            os.setgroups([])
            os.setgid(user.pw_gid)
            os.setuid(user.pw_uid)
            for _directive, path in runtime_paths(root):
                name = ".django-mojo-worker-probe-%d" % os.getpid()
                descriptor = os.open(
                    os.path.join(path, name),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600)
                os.close(descriptor)
                os.unlink(os.path.join(path, name))
            os.write(write_fd, b"ok")
        except Exception as err:
            os.write(write_fd, str(err).encode("utf-8", "replace")[:2048])
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    message = os.read(read_fd, 2048)
    os.close(read_fd)
    _pid, status = os.waitpid(child, 0)
    if status != 0 or message != b"ok":
        raise NginxRuntimeError(
            "nginx worker write probe failed: %s"
            % message.decode("utf-8", "replace"))


def _verify_active(config, root):
    for directive, path in runtime_paths(root):
        pattern = r"(?m)^\s*%s\s+%s\s*;" % (
            re.escape(directive), re.escape(path))
        count = len(re.findall(pattern, config))
        if count != 1:
            raise NginxRuntimeError(
                "active nginx config must contain %s %s exactly once; got %d"
                % (directive, path, count))


def _activation_hint(error, path):
    """Name the installed-but-unread fragment when nginx never saw it.

    `_verify_active`'s zero-count case is a specific, common, and thoroughly
    unhelpful failure: the fragment was written, `nginx -T` succeeded, and the
    directives are simply absent — because the operator's nginx.conf has no
    include for the directory the fragment lives in. The bare message
    ("must contain ... exactly once; got 0") sends people looking for a
    corrupt fragment instead.

    Derived from the activation error BEFORE rollback: a post-rollback re-stat
    would be describing a file that has already been removed.
    """
    if "exactly once; got 0" not in str(error):
        return ""
    return (" — the fragment IS installed at %s but the active config never "
            "reads it: nginx.conf's http block is missing "
            "`include %s/*.conf;`" % (path, os.path.dirname(path)))


def _install_fragment(path, text):
    parent = os.path.dirname(path)
    _mkdir_exact(parent, 0, 0, 0o755)
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise NginxRuntimeError("refusing unsafe nginx runtime fragment: %s" % path)
        if info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022:
            raise NginxRuntimeError("existing nginx runtime fragment is not root-owned: %s" % path)
    except FileNotFoundError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=".django-mojo-runtime.", dir=parent)
    try:
        payload = text if isinstance(text, bytes) else text.encode()
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o644)
        os.fchown(descriptor, 0, 0)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_fragment(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def _restore_fragment(path, prior):
    if prior is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    else:
        _install_fragment(path, prior)


def converge(web_user, nginx_etc="/etc/nginx", root=RUNTIME_ROOT,
             nginx_binary="nginx"):
    if os.geteuid() != 0:
        raise NginxRuntimeError("nginx runtime convergence requires root")
    path = fragment_path(nginx_etc)
    prior = _read_fragment(path)
    # A previously installed fragment with drifted/missing directories can
    # make nginx -T exit non-zero. Its emitted config is still authoritative
    # for resolving the worker, and repair must be able to heal that state.
    before = nginx_dump(nginx_binary, allow_failure=True)
    desired = render_http_fragment(
        root, include_map=_upgrade_map_decision(before, path, prior))
    try:
        actual_user = parse_worker_user(before)
    except NginxRuntimeError:
        # A graph that cannot even resolve its worker. The one breakage this
        # module can repair is a missing/duplicated upgrade map (a node whose
        # bootstrap predates the bootstrap-owned contract): install the
        # corrected fragment first and let the re-dump speak. Anything else —
        # including a fragment already carrying the right answer — keeps the
        # original diagnosis, which now quotes nginx's own [emerg].
        if desired.encode() == (prior if prior is not None else b""):
            raise
        _install_fragment(path, desired)
        try:
            before = nginx_dump(nginx_binary, allow_failure=True)
            actual_user = parse_worker_user(before)
        except NginxRuntimeError:
            _restore_fragment(path, prior)
            raise
    if actual_user != web_user:
        raise NginxRuntimeError(
            "WEB_USER %s does not match nginx worker user %s"
            % (web_user, actual_user))
    user = _resolve_user(web_user)
    _mkdir_exact(os.path.dirname(root), 0, 0, 0o755)
    _mkdir_exact(root, 0, 0, 0o755)
    for _directive, path_ in runtime_paths(root):
        _mkdir_exact(path_, user.pw_uid, 0, 0o700)
    _converge_selinux(root)
    _probe_as_worker(user, root)

    _install_fragment(path, desired)
    try:
        active = nginx_dump(nginx_binary)
        if parse_worker_user(active) != web_user:
            raise NginxRuntimeError("nginx worker identity changed during convergence")
        _verify_active(active, root)
    except Exception as activation_error:
        hint = _activation_hint(activation_error, path)
        _restore_fragment(path, prior)
        try:
            nginx_dump(nginx_binary)
        except NginxRuntimeError as rollback_error:
            raise NginxRuntimeError(
                "nginx runtime activation failed (%s%s) and rollback is invalid (%s)"
                % (activation_error, hint, rollback_error))
        if hint:
            raise NginxRuntimeError("%s%s" % (activation_error, hint))
        raise activation_error


def reconcile_upgrade_map(nginx_etc="/etc/nginx", root=RUNTIME_ROOT,
                          nginx_binary="nginx"):
    """Re-decide who declares the upgrade map after host configs changed.

    post_deploy installs the project's nginx.conf and vhosts AFTER `converge`
    ran, so a bootstrap that newly declares the map (or drops it) would leave
    the graph with two declarations, or none, until the next root render.
    Runs the same decision against the post-install graph and rewrites the
    fragment when the answer moved; restores it when the rewrite does not
    converge. Returns whether the fragment changed. Never performs the first
    install — that is converge's transactional job.
    """
    if os.geteuid() != 0:
        raise NginxRuntimeError("nginx runtime reconcile requires root")
    path = fragment_path(nginx_etc)
    prior = _read_fragment(path)
    if prior is None:
        return False
    dump = nginx_dump(nginx_binary, allow_failure=True)
    desired = render_http_fragment(
        root, include_map=_upgrade_map_decision(dump, path, prior))
    if desired.encode() == prior:
        return False
    _install_fragment(path, desired)
    try:
        nginx_dump(nginx_binary)
    except NginxRuntimeError as err:
        _restore_fragment(path, prior)
        raise NginxRuntimeError(
            "upgrade-map reconcile did not converge (%s); prior fragment "
            "restored" % err)
    return True


def audit(web_user, nginx_etc="/etc/nginx", root=RUNTIME_ROOT,
          nginx_binary="nginx", probe=True):
    errors = []
    try:
        active = nginx_dump(nginx_binary)
        actual = parse_worker_user(active)
        if actual != web_user:
            errors.append("WEB_USER %s != nginx worker %s" % (web_user, actual))
        _verify_active(active, root)
    except NginxRuntimeError as err:
        errors.append(str(err))
        active = ""
    try:
        user = _resolve_user(web_user)
    except NginxRuntimeError as err:
        errors.append(str(err))
        user = None
    for _directive, path in runtime_paths(root):
        try:
            info = os.lstat(path)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                    user is None or info.st_uid != user.pw_uid or
                    info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o700):
                errors.append("unsafe nginx runtime metadata: %s" % path)
        except OSError as err:
            errors.append("cannot inspect %s: %s" % (path, err))
    try:
        _assert_absolute_safe(root)
        for parent in (os.path.dirname(root), root):
            info = os.lstat(parent)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or
                    info.st_gid != 0 or info.st_mode & 0o022):
                errors.append("unsafe nginx runtime parent metadata: %s" % parent)
    except (OSError, NginxRuntimeError) as err:
        errors.append(str(err))
    errors.extend(_audit_selinux(root))
    path = fragment_path(nginx_etc)
    try:
        info = os.lstat(path)
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                info.st_uid != 0 or info.st_gid != 0 or
                stat.S_IMODE(info.st_mode) != 0o644):
            errors.append("unsafe nginx runtime fragment metadata: %s" % path)
    except OSError as err:
        errors.append("cannot inspect %s: %s" % (path, err))
    try:
        with open(path) as handle:
            fragment_text = handle.read()
        # The fragment legitimately exists in two shapes — with the upgrade
        # map (no other declaration in the graph) and without (the bootstrap
        # or a legacy generation owns it). Audit against the shape the active
        # graph calls for; a failed dump keeps the installed answer.
        include_map = _upgrade_map_decision(active, path, fragment_text.encode())
        if fragment_text != render_http_fragment(root, include_map=include_map):
            errors.append("nginx runtime fragment bytes differ from the packaged contract")
    except OSError as err:
        errors.append("cannot read %s: %s" % (path, err))
    if probe and user is not None and not errors:
        try:
            _probe_as_worker(user, root)
        except NginxRuntimeError as err:
            errors.append(str(err))
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m mojo.deploy.nginx_runtime")
    parser.add_argument("command", choices=("audit", "reconcile"))
    parser.add_argument("--web-user")
    parser.add_argument("--nginx-etc", default="/etc/nginx")
    parser.add_argument("--runtime-root", default=RUNTIME_ROOT)
    args = parser.parse_args(argv)
    if args.command == "reconcile":
        try:
            changed = reconcile_upgrade_map(nginx_etc=args.nginx_etc,
                                            root=args.runtime_root)
        except NginxRuntimeError as err:
            print(str(err), file=sys.stderr)
            return 1
        if changed:
            print("nginx upgrade-map ownership moved; runtime fragment rewritten")
        return 0
    if not args.web_user:
        parser.error("--web-user is required for audit")
    errors = audit(args.web_user, nginx_etc=args.nginx_etc,
                   root=args.runtime_root)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
