"""Data-only root runtime checks and fixed nonroot launch; never project imports."""
import ctypes
import hashlib
import os
import pwd
import stat
import subprocess
from pathlib import PurePosixPath

if __package__:
    from . import acme_hub_operator_protocol as wire
else:
    import acme_hub_operator_protocol as wire

INTERPRETER = '/usr/bin/python3.12'
ROOT = '/usr/local/libexec/mojo-acme-operator'
USERS = {'coordinator': 'mojo-acme-coord', 'verifier': 'mojo-acme-verify',
         'provider_witness': 'mojo-acme-provider', 'producer_qualifier': 'mojo-acme-producer'}
MAX_FILE = 64 * 1024 * 1024


def relative_path(value):
    wire.text(value, 512)
    path = PurePosixPath(value)
    wire.require(not path.is_absolute() and str(path) == value and '..' not in path.parts
                 and all(part not in ('', '.') for part in path.parts), 'runtime_unavailable')
    wire.require(not value.endswith(('.pth', '.pyc')) and path.name not in ('sitecustomize.py', 'usercustomize.py'),
                 'runtime_unavailable')
    return path


def open_parent(path):
    """Walk every absolute ancestor with O_NOFOLLOW and retain its descriptor."""
    parsed = PurePosixPath(path)
    wire.require(parsed.is_absolute() and str(parsed) == path and '..' not in parsed.parts,
                 'runtime_unavailable')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parsed.parts[1:]:
            previous = fd
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=previous)
            os.close(previous)
            info = os.fstat(fd)
            wire.require(info.st_uid == 0 and not info.st_mode & 0o022, 'runtime_unavailable')
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_owned(path, mode, maximum=wire.MAX_FRAME):
    parsed = PurePosixPath(path)
    parent = open_parent(str(parsed.parent))
    try:
        fd = os.open(parsed.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            wire.require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                         and stat.S_IMODE(before.st_mode) == mode and 0 < before.st_size <= maximum,
                         'runtime_unavailable')
            chunks = []
            remaining = maximum + 1
            while remaining:
                part = os.read(fd, min(remaining, 65536))
                if not part:
                    break
                chunks.append(part)
                remaining -= len(part)
            raw = b''.join(chunks)
            after = os.fstat(fd)
            wire.require(len(raw) == before.st_size and
                (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns), 'runtime_unavailable')
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def validate_manifest(value):
    wire.fields(value, ('version', 'kind', 'interpreter', 'entry', 'files', 'source_sha', 'profile_sha256'))
    wire.require(value['version'] == 1 and type(value['version']) is int and value['kind'] in USERS)
    wire.require(type(value['source_sha']) is str and wire.SHA.fullmatch(value['source_sha']))
    wire.digest(value['profile_sha256'])
    wire.fields(value['interpreter'], ('path', 'sha256'))
    wire.require(value['interpreter']['path'] == INTERPRETER, 'runtime_unavailable')
    wire.digest(value['interpreter']['sha256'])
    relative_path(value['entry'])
    wire.require(type(value['files']) is list and 1 <= len(value['files']) <= 20000, 'runtime_unavailable')
    seen = set()
    for entry in value['files']:
        wire.fields(entry, ('path', 'sha256', 'size', 'mode'))
        relative_path(entry['path'])
        wire.require(entry['path'] not in seen and type(entry['size']) is int
                     and 0 < entry['size'] <= MAX_FILE and entry['mode'] in (0o444, 0o555), 'runtime_unavailable')
        wire.digest(entry['sha256'])
        seen.add(entry['path'])
    wire.require(value['entry'] in seen, 'runtime_unavailable')
    return value


def verify_runtime(path, expected_digest, kind):
    wire.require(path == ROOT + '/runtimes/' + expected_digest, 'runtime_unavailable')
    wire.digest(expected_digest)
    raw = read_owned(path + '/runtime-manifest.json', 0o444)
    wire.require(hashlib.sha256(raw).hexdigest() == expected_digest, 'runtime_unavailable')
    manifest = validate_manifest(wire.decode(raw))
    wire.require(manifest['kind'] == kind, 'runtime_unavailable')
    interpreter = read_owned(manifest['interpreter']['path'], 0o755, MAX_FILE)
    wire.require(hashlib.sha256(interpreter).hexdigest() == manifest['interpreter']['sha256'], 'runtime_unavailable')
    for entry in manifest['files']:
        data = read_owned(path + '/' + entry['path'], entry['mode'], MAX_FILE)
        wire.require(len(data) == entry['size'] and hashlib.sha256(data).hexdigest() == entry['sha256'],
                     'runtime_unavailable')
    # Reject all unlisted leaves; directory traversal does not execute Python.
    listed = {entry['path'] for entry in manifest['files']} | {'runtime-manifest.json'}
    for directory, directories, files in os.walk(path, followlinks=False):
        for name in directories:
            candidate = directory + '/' + name
            wire.require(not os.path.islink(candidate), 'runtime_unavailable')
            parent = open_parent(candidate)
            os.close(parent)
        for name in files:
            relative = os.path.relpath(directory + '/' + name, path)
            wire.require(relative in listed, 'runtime_unavailable')
    return manifest


def launch(path, digest, kind, channel_fd, input_fds=()):
    """Only named locked principals, after complete retained-byte verification."""
    wire.require(os.geteuid() == 0 and kind in USERS, 'runtime_unavailable')
    manifest = verify_runtime(path, digest, kind)
    account = pwd.getpwnam(USERS[kind])
    wire.require(account.pw_uid != 0 and account.pw_shell in ('/usr/sbin/nologin', '/sbin/nologin', '/bin/false'),
                 'runtime_unavailable')
    def drop():
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(4, 0, 0, 0, 0) != 0:
            os._exit(126)
    # A fixed code fragment loads only the validated immutable image AFTER UID drop.
    bootstrap = ('import runpy,sys;sys.path.insert(0,sys.argv[1]);'
                 'sys.argv=[sys.argv[2],sys.argv[3]];runpy.run_path(sys.argv[0],run_name="__main__")')
    return subprocess.Popen([INTERPRETER, '-I', '-S', '-c', bootstrap, path,
                             path + '/' + manifest['entry'], str(channel_fd)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, pass_fds=(channel_fd,) + tuple(input_fds), preexec_fn=drop,
        env={'LANG': 'C.UTF-8', 'PATH': '/usr/bin:/bin'}, cwd='/', start_new_session=True)
