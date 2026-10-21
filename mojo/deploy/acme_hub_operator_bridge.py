"""Linux inherited-channel identities. JSON fields do not authenticate a peer."""
import array
import hashlib
import os
from pathlib import Path
import select
import socket
import struct
import sys
import time

if __package__:
    from . import acme_hub_operator_protocol as wire
else:
    import acme_hub_operator_protocol as wire

FRAME_FIELDS = ('version', 'operation', 'revision', 'generation', 'session', 'nonce',
                'sequence', 'action', 'data')


def linux_required():
    wire.require(sys.platform == 'linux' and hasattr(os, 'pidfd_open') and
                 hasattr(socket, 'SO_PASSCRED') and hasattr(socket, 'SCM_CREDENTIALS'),
                 'session_unavailable')


def process_identity(pid):
    linux_required()
    wire.require(type(pid) is int and pid > 0, 'session_unavailable')
    try:
        base = Path('/proc') / str(pid)
        raw = (base / 'stat').read_text()
        # comm may contain spaces/parentheses: start fields after its LAST ')'.
        tail = raw[raw.rfind(')') + 2:].split()
        status = dict(line.split(':', 1) for line in (base / 'status').read_text().splitlines() if ':' in line)
        uid = [int(item) for item in status['Uid'].split()]
        gid = [int(item) for item in status['Gid'].split()]
        groups = [int(item) for item in status['Groups'].split()]
        cgroup = (base / 'cgroup').read_text().strip()
        exe = os.readlink(base / 'exe')
        argv = (base / 'cmdline').read_bytes()
        wire.require(len(argv) <= 32768 and argv and not exe.endswith(' (deleted)') and
                     all(item == uid[0] for item in uid) and all(item == gid[0] for item in gid)
                     and cgroup.startswith('0::/') and '\n' not in cgroup,
                     'session_unavailable')
        return {'pid': pid, 'start': int(tail[19]), 'ppid': int(tail[1]),
                'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'uid': uid[0], 'gid': gid[0], 'groups': groups, 'exe': exe,
                'argv_sha256': hashlib.sha256(argv).hexdigest(), 'cgroup': cgroup}
    except (OSError, ValueError, KeyError, IndexError):
        raise wire.Refused('session_unavailable') from None


def socketpair():
    linux_required()
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    for endpoint in (left, right):
        endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        endpoint.set_inheritable(False)
        endpoint.setblocking(False)
    return left, right


class Channel:
    """Exact spawned peer+socket incarnation, no listener or UID override."""
    def __init__(self, endpoint, peer_pid, expected_uid, expected_gid, expected_exe,
                 expected_cgroup, operation, root_revision, generation, nonce):
        self.pidfd = None
        self.endpoint = endpoint
        self.closed = False
        try:
            linux_required()
            wire.uuid_text(operation); wire.revision(root_revision)
            wire.uuid_text(generation); wire.digest(nonce)
            wire.require(endpoint.family == socket.AF_UNIX and endpoint.type == socket.SOCK_SEQPACKET,
                         'session_unavailable')
            self.peer = process_identity(peer_pid)
            wire.require(self.peer['uid'] == expected_uid and self.peer['gid'] == expected_gid
                         and self.peer['exe'] == expected_exe and self.peer['cgroup'] == expected_cgroup,
                         'session_unavailable')
            self.pidfd = os.pidfd_open(peer_pid, 0)
            self.socket_inode = os.fstat(endpoint.fileno()).st_ino
            endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            endpoint.setblocking(False)
            self.operation = operation
            self.root_revision = root_revision
            self.generation = generation
            self.nonce = nonce
            self.session = None
            self.receive_sequence = 0
            self.send_sequence = 0
            self.started = time.monotonic()
            self.deadline = self.started + 600
            self.heartbeat = self.started + 30
        except BaseException:
            self.close()
            raise

    def check(self):
        wire.require(not self.closed and time.monotonic() < min(self.deadline, self.heartbeat)
                     and not select.select([self.pidfd], [], [], 0)[0]
                     and os.fstat(self.endpoint.fileno()).st_ino == self.socket_inode
                     and process_identity(self.peer['pid']) == self.peer, 'session_unavailable')

    def _wait(self, sending, deadline):
        while True:
            self.check()
            remaining = min(deadline, self.deadline, self.heartbeat) - time.monotonic()
            wire.require(remaining > 0, 'session_unavailable')
            readable, writable, unused = select.select(
                [self.pidfd] + ([] if sending else [self.endpoint]),
                [self.endpoint] if sending else [], [], remaining)
            self.check()
            if (sending and writable) or (not sending and self.endpoint in readable):
                return

    def receive(self, timeout=5):
        wire.require(type(timeout) in (int, float) and 0 < timeout <= 30, 'invalid_frame')
        deadline = time.monotonic() + timeout
        while True:
            self._wait(False, deadline)
            try:
                raw, ancillary, flags, unused = self.endpoint.recvmsg(wire.MAX_FRAME + 1, socket.CMSG_SPACE(12))
                break
            except BlockingIOError:
                continue
            except OSError:
                raise wire.Refused('session_unavailable') from None
        # Even an invalid sender must not leak an SCM_RIGHTS descriptor.
        for level, kind, content in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                descriptors = array.array('i')
                descriptors.frombytes(content[:len(content) - len(content) % descriptors.itemsize])
                for descriptor in descriptors:
                    os.close(descriptor)
        wire.require(not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) and raw and len(raw) <= wire.MAX_FRAME,
                     'invalid_frame')
        wire.require(len(ancillary) == 1, 'session_unavailable')
        level, kind, credentials = ancillary[0]
        wire.require(level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(credentials) == 12,
                     'session_unavailable')
        pid, uid, gid = struct.unpack('3i', credentials)
        wire.require((pid, uid, gid) == (self.peer['pid'], self.peer['uid'], self.peer['gid']), 'session_unavailable')
        self.check()
        frame = wire.fields(wire.decode(raw), FRAME_FIELDS, 'invalid_frame')
        wire.require(frame['version'] == 1 and type(frame['version']) is int
                     and frame['operation'] == self.operation and frame['revision'] == self.root_revision
                     and frame['generation'] == self.generation and frame['nonce'] == self.nonce
                     and type(frame['sequence']) is int and frame['sequence'] == self.receive_sequence + 1,
                     'session_unavailable')
        wire.uuid_text(frame['session']); wire.identifier(frame['action'])
        wire.require(type(frame['data']) is dict, 'invalid_frame')
        if self.session is None:
            self.session = frame['session']
        wire.require(frame['session'] == self.session, 'session_unavailable')
        self.receive_sequence = frame['sequence']
        self.heartbeat = min(time.monotonic() + 30, self.deadline)
        return frame

    def send(self, action, data, timeout=5):
        self.check()
        wire.identifier(action)
        wire.require(type(timeout) in (int, float) and 0 < timeout <= 30, 'invalid_frame')
        wire.require(self.session is not None and type(data) is dict, 'session_unavailable')
        sequence = self.send_sequence + 1
        raw = wire.encoded({'version': 1, 'operation': self.operation,
            'revision': self.root_revision, 'generation': self.generation,
            'session': self.session, 'nonce': self.nonce, 'sequence': sequence,
            'action': action, 'data': data})
        deadline = time.monotonic() + timeout
        while True:
            self._wait(True, deadline)
            try:
                count = self.endpoint.send(raw)
                break
            except BlockingIOError:
                continue
            except OSError:
                raise wire.Refused('session_unavailable') from None
        # SOCK_SEQPACKET sends one full packet atomically or refuses. No stream
        # fragment retry or new socket/nonce may replace this exact channel.
        wire.require(count == len(raw), 'session_unavailable')
        self.send_sequence = sequence

    def close(self):
        if not self.closed:
            self.closed = True
            self.endpoint.close()
            if self.pidfd is not None:
                os.close(self.pidfd)
                self.pidfd = None


class Registry:
    """Guardian memory owns live authority; durable mirrors cannot reload it."""
    def __init__(self, operation, root_revision):
        import secrets
        import uuid
        wire.require(os.geteuid() == 0, 'operator_not_authorized')
        wire.uuid_text(operation); wire.revision(root_revision)
        self.operation = operation; self.root_revision = root_revision
        self.generation = str(uuid.uuid4()); self.nonce = secrets.token_hex(32)
        self.children = {}
        self.admission = None
        self.permit = None

    def register(self, role, endpoint, child, expected_exe, cgroup):
        import pwd
        if __package__:
            from .acme_hub_operator_runtime import USERS
        else:
            from acme_hub_operator_runtime import USERS
        channel = None
        try:
            wire.require(role in USERS and role not in self.children and child.pid != os.getpid(), 'session_unavailable')
            user = pwd.getpwnam(USERS[role])
            channel = Channel(endpoint, child.pid, user.pw_uid, user.pw_gid, expected_exe,
                              cgroup, self.operation, self.root_revision, self.generation, self.nonce)
            wire.require(channel.peer['ppid'] == os.getpid(), 'session_unavailable')
            self.children[role] = channel
            return channel
        except BaseException:
            if channel is not None:
                channel.close()
            else:
                endpoint.close()
            raise

    def admit(self, channel, frame):
        wire.require(self.children.get('coordinator') is channel and self.admission is None
                     and frame['action'] == 'ADMISSION_READY', 'session_unavailable')
        data = wire.fields(frame['data'], ('backend_pid', 'backend_started_at', 'coordinator_nonce', 'tuple_sha256'))
        wire.require(type(data['backend_pid']) is int and data['backend_pid'] > 0)
        wire.text(data['backend_started_at'], 64); wire.digest(data['coordinator_nonce']); wire.digest(data['tuple_sha256'])
        channel.check()
        self.admission = dict(data)

    def commit_permit(self, channel, review, attestation, assertion_reference, now, expires):
        # Called only by guardian after its own current verifier/helper checks.
        wire.require(self.children.get('coordinator') is channel and self.admission is not None
                     and self.permit is None and 0 < expires - now <= 5, 'session_unavailable')
        wire.digest(review); wire.uuid_text(assertion_reference)
        channel.check()
        self.permit = {'review': review, 'attestation': attestation,
                       'assertion_reference': assertion_reference, 'expires': expires,
                       **self.admission}
        channel.send('COMMIT_PERMIT', self.permit)

    def close(self):
        for channel in self.children.values():
            channel.close()
        self.children.clear(); self.admission = None; self.permit = None
