#!/usr/bin/env python3
"""Settings-free firewall enrollment; safe to retain across framework rollback."""
import argparse
import contextlib
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile

CONFIG_PATH = "/etc/mojo-firewall.json"
BROKER_CONFIG_PATH = "/etc/mojo-firewall-broker.json"
BROKER_PATH = "/usr/local/sbin/mojo-firewall-broker"
SUDOERS_PATH = "/etc/sudoers.d/70-mojo-firewall-broker"
BROKER_WRAPPER_TEXT = "#!/bin/sh\nexec /usr/bin/python3 -E -P -m mojo.deploy.firewall_broker\n"
SUDOERS_TEXT = 'ec2-user ALL=(root) NOPASSWD: /usr/local/sbin/mojo-firewall-broker ""\n'
MAX_BYTES = 4096


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate firewall configuration key")
        result[key] = value
    return result


def permanent_name(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,27}", value)
            or value.endswith("_tmp")):
        raise ValueError("invalid permanent firewall set name")
    return value


class Lifecycle:
    def __init__(self, root="", owner_uid=0, validator=None):
        self.root = root
        self.owner_uid = owner_uid
        self.validator = validator or self.validate_sudoers

    def path(self, path):
        return self.root + path

    def read(self, path):
        try:
            fd = os.open(self.path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid
                    or info.st_nlink != 1 or info.st_mode & 0o022):
                raise ValueError("unsafe firewall file metadata")
            payload = os.read(fd, MAX_BYTES + 1)
            if len(payload) > MAX_BYTES:
                raise ValueError("firewall file exceeds size bound")
            return payload, stat.S_IMODE(info.st_mode)
        finally:
            os.close(fd)

    def config(self, path):
        snapshot = self.read(path)
        if snapshot is None:
            return None
        if snapshot[1] != 0o600:
            raise ValueError("firewall configuration must be private")
        return json.loads(snapshot[0], object_pairs_hook=strict_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("invalid constant")))

    def enrolled(self):
        value = self.config(CONFIG_PATH)
        if value is None:
            return False
        if (not isinstance(value, dict) or set(value) != {"version", "enrolled"}
                or type(value["version"]) is not int or value["version"] != 1
                or type(value["enrolled"]) is not bool):
            raise ValueError("invalid firewall enrollment")
        return value["enrolled"]

    def identity(self):
        value = self.config(BROKER_CONFIG_PATH)
        if not isinstance(value, dict) or set(value) != {"permanent_set_name"}:
            raise ValueError("firewall permanent identity missing or malformed")
        return permanent_name(value["permanent_set_name"])

    def directory(self, path):
        parent = os.path.dirname(self.path(path))
        # Walk every ancestor: root execution must never traverse an app-owned
        # directory or a symlink when replacing its authority files.
        cursor = parent
        while cursor and cursor != self.root:
            info = os.lstat(cursor)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner_uid
                    or info.st_mode & 0o022):
                raise ValueError("unsafe firewall installation directory")
            previous = cursor
            cursor = os.path.dirname(cursor)
            if cursor == previous:
                break
        return parent

    def write(self, path, snapshot):
        parent = self.directory(path)
        if snapshot is None:
            try:
                os.unlink(self.path(path))
            except FileNotFoundError:
                pass
        else:
            payload, mode = snapshot
            fd, temporary = tempfile.mkstemp(prefix=".mojo-firewall-", dir=parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    if self.owner_uid == 0:
                        os.fchown(handle.fileno(), 0, 0)
                    os.fchmod(handle.fileno(), mode)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path(path))
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def locked(self):
        self.directory(CONFIG_PATH)
        fd = os.open(self.path(CONFIG_PATH + ".lock"),
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid
                    or info.st_nlink != 1 or info.st_mode & 0o077):
                raise ValueError("unsafe firewall lifecycle lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def validate_sudoers(self, path):
        result = subprocess.run(["/usr/sbin/visudo", "-c", "-f", path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=10, check=False)
        if result.returncode:
            raise ValueError("firewall sudoers validation failed")

    def apply(self, enrolled, name=None):
        paths = (CONFIG_PATH, BROKER_CONFIG_PATH, BROKER_PATH, SUDOERS_PATH)
        snapshots = {path: self.read(path) for path in paths}
        try:
            if enrolled:
                current = self.config(BROKER_CONFIG_PATH)
                if current is not None and self.identity() != name:
                    raise ValueError("permanent set identity cannot change during enrollment")
                self.write(BROKER_CONFIG_PATH, (
                    json.dumps({"permanent_set_name": name}).encode(), 0o600))
                # Validate a private candidate before enabling the sudo grant.
                parent = self.directory(SUDOERS_PATH)
                fd, candidate = tempfile.mkstemp(prefix=".mojo-firewall-check-", dir=parent)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(SUDOERS_TEXT.encode())
                    self.validator(candidate)
                finally:
                    os.unlink(candidate)
                self.write(BROKER_PATH, (BROKER_WRAPPER_TEXT.encode(), 0o755))
                self.write(CONFIG_PATH, (b'{"version":1,"enrolled":true}\n', 0o600))
                self.write(SUDOERS_PATH, (SUDOERS_TEXT.encode(), 0o440))
            else:
                # Revoke the grant first. Kernel state and permanent identity
                # are deliberately retained for a future explicit enrollment.
                self.write(SUDOERS_PATH, None)
                self.write(CONFIG_PATH, (b'{"version":1,"enrolled":false}\n', 0o600))
                self.write(BROKER_PATH, None)
        except Exception:
            for path in reversed(paths):
                self.write(path, snapshots[path])
            raise
        return self.check()

    def enroll(self, name="mojo_blocked"):
        name = permanent_name(name)
        with self.locked():
            self.enrolled()  # Refuse malformed pre-existing enrollment.
            return self.apply(True, name)

    def converge(self):
        with self.locked():
            if not self.enrolled():
                return self.check()
            return self.apply(True, self.identity())

    def off(self):
        with self.locked():
            self.enrolled()
            return self.apply(False)

    def check(self):
        if not self.enrolled():
            return {"status": "unenrolled"}
        name = self.identity()
        if (self.read(BROKER_PATH) != (BROKER_WRAPPER_TEXT.encode(), 0o755) or
                self.read(SUDOERS_PATH) != (SUDOERS_TEXT.encode(), 0o440)):
            return {"status": "drift", "permanent_set_name": name}
        return {"status": "ready", "permanent_set_name": name}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enroll", "converge", "off", "check"))
    parser.add_argument("--permanent-set", default="mojo_blocked")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("firewall lifecycle requires root")
    try:
        lifecycle = Lifecycle()
        result = (lifecycle.enroll(args.permanent_set) if args.action == "enroll"
                  else getattr(lifecycle, args.action)())
        print(json.dumps(result, sort_keys=True))
        return 1 if result["status"] == "drift" else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print('{"status":"unavailable"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
