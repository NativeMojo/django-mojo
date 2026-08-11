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


def render_http_fragment(root=RUNTIME_ROOT, indent=""):
    return "".join(
        "%s%s %s;\n" % (indent, directive, path)
        for directive, path in runtime_paths(root))


def fragment_path(nginx_etc="/etc/nginx"):
    return os.path.join(nginx_etc, "conf.d", FRAGMENT_NAME)


def parse_worker_user(config):
    users = re.findall(r"(?m)^\s*user\s+([^;\s]+)(?:\s+[^;\s]+)?\s*;", config)
    if len(users) != 1:
        raise NginxRuntimeError(
            "nginx -T must expose exactly one effective worker user; got %r"
            % users)
    return users[0]


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


def converge(web_user, nginx_etc="/etc/nginx", root=RUNTIME_ROOT,
             nginx_binary="nginx"):
    if os.geteuid() != 0:
        raise NginxRuntimeError("nginx runtime convergence requires root")
    # A previously installed fragment with drifted/missing directories can
    # make nginx -T exit non-zero. Its emitted config is still authoritative
    # for resolving the worker, and repair must be able to heal that state.
    before = nginx_dump(nginx_binary, allow_failure=True)
    actual_user = parse_worker_user(before)
    if actual_user != web_user:
        raise NginxRuntimeError(
            "WEB_USER %s does not match nginx worker user %s"
            % (web_user, actual_user))
    user = _resolve_user(web_user)
    _mkdir_exact(os.path.dirname(root), 0, 0, 0o755)
    _mkdir_exact(root, 0, 0, 0o755)
    for _directive, path in runtime_paths(root):
        _mkdir_exact(path, user.pw_uid, 0, 0o700)
    _converge_selinux(root)
    _probe_as_worker(user, root)

    path = fragment_path(nginx_etc)
    prior = None
    try:
        with open(path, "rb") as handle:
            prior = handle.read()
    except FileNotFoundError:
        pass
    _install_fragment(path, render_http_fragment(root))
    try:
        active = nginx_dump(nginx_binary)
        if parse_worker_user(active) != web_user:
            raise NginxRuntimeError("nginx worker identity changed during convergence")
        _verify_active(active, root)
    except Exception as activation_error:
        if prior is None:
            os.unlink(path)
        else:
            _install_fragment(path, prior)
        try:
            nginx_dump(nginx_binary)
        except NginxRuntimeError as rollback_error:
            raise NginxRuntimeError(
                "nginx runtime activation failed (%s) and rollback is invalid (%s)"
                % (activation_error, rollback_error))
        raise activation_error


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
        if fragment_text != render_http_fragment(root):
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
    parser.add_argument("command", choices=("audit",))
    parser.add_argument("--web-user", required=True)
    parser.add_argument("--nginx-etc", default="/etc/nginx")
    parser.add_argument("--runtime-root", default=RUNTIME_ROOT)
    args = parser.parse_args(argv)
    errors = audit(args.web_user, nginx_etc=args.nginx_etc,
                   root=args.runtime_root)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
