"""Settings-free root-floor contracts; all filesystem state is temporary."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from testit import helpers as th


def _floor():
    return dict(schema=1, installation_uuid=str(uuid.uuid4()), activation_uuid=str(uuid.uuid4()),
                generation=1, roster_digest="a" * 64,
                artifact=dict(epoch=1, source_sha="b" * 40, manifest_sha256="c" * 64,
                              release="1.38.0", wheel_sha256="d" * 64))


def _identity(floor):
    return {key: value for key, value in floor["artifact"].items() if key != "wheel_sha256"}


def _root(directory):
    root = Path(directory)
    root.chmod(0o700)
    (root / "update.lock").touch(mode=0o600)
    return root


def _error(call, reason=None):
    from mojo.deploy.acme_hub_floor import FloorError
    try:
        call()
    except FloorError as error:
        assert reason is None or str(error) == reason, f"expected {reason}, got {error}"
    else:
        raise AssertionError("unsafe floor operation must refuse")


@th.django_unit_test()
def test_floor_single_session_lock_and_immutable_retry(opts):
    from mojo.deploy import acme_hub_floor as module
    floor = _floor()
    with tempfile.TemporaryDirectory() as directory:
        root = _root(directory)
        session = module.Session(root, uid=os.getuid(), loader=lambda: _identity(floor))
        try:
            receipt = session.handle(dict(operation="prepare_hold", floor=floor, nonce="1" * 32))
            assert receipt["held"] is True, "prepared helper must hold the real lock"
            other = module.Session(root, uid=os.getuid(), loader=lambda: _identity(floor))
            _error(lambda: other.handle(dict(operation="status")), "update_busy")
            _error(lambda: other.handle(dict(operation="confirm_hold", nonce="1" * 32,
                                            helper_session=receipt["helper_session"])), "session_unavailable")
            confirmed = session.handle(dict(operation="confirm_hold", nonce="1" * 32,
                                            helper_session=receipt["helper_session"]))
            assert confirmed["helper_pid"] == receipt["helper_pid"], "confirmation must be the same actual process"
            assert confirmed["helper_session"] == receipt["helper_session"], "confirmation must bind its own session"
        finally:
            session.close()
        assert module.read_floor(root, uid=os.getuid()) == floor, "EOF/close must preserve the monotonic stamp"
        again = module.Session(root, uid=os.getuid(), loader=lambda: _identity(floor))
        try:
            retry = again.handle(dict(operation="prepare_hold", floor=floor, nonce="2" * 32))
            assert retry["floor"] == floor and retry["nonce"] != receipt["nonce"], "retry keeps activation identity with fresh nonce"
            again.handle(dict(operation="release", nonce="2" * 32, helper_session=retry["helper_session"]))
        finally:
            again.close()
        conflict = dict(floor, activation_uuid=str(uuid.uuid4()))
        rejected = module.Session(root, uid=os.getuid(), loader=lambda: _identity(floor))
        _error(lambda: rejected.handle(dict(operation="prepare_hold", floor=conflict, nonce="3" * 32)), "activation_conflict")
        assert module.read_floor(root, uid=os.getuid()) == floor, "conflict cannot lower/rebind existing floor"


@th.django_unit_test()
def test_floor_status_is_read_only_and_unavailable_is_not_idle(opts):
    from mojo.deploy import acme_hub_floor as module
    with tempfile.TemporaryDirectory() as directory:
        root = _root(directory)
        (root / "active").mkdir(mode=0o700)
        (root / "preparing.42").mkdir(mode=0o700)
        before = sorted(path.name for path in root.iterdir())
        result = module.Session(root, uid=os.getuid()).handle(dict(operation="status"))
        assert result == dict(floor=None, active=True, preparing=True), "status must report actual existing transaction state"
        assert sorted(path.name for path in root.iterdir()) == before, "status must not clean preparing or recover ACTIVE"
        (root / module.FLOOR_NAME).symlink_to(root / "missing")
        try:
            module.Session(root, uid=os.getuid()).handle(dict(operation="status"))
        except (OSError, module.FloorError):
            pass
        else:
            raise AssertionError("symlink/unavailable floor cannot be reported as idle")


@th.django_unit_test()
def test_floor_snapshot_guards_saved_recovery_before_mutation(opts):
    from mojo.deploy import acme_hub_floor as module
    floor = _floor()
    with tempfile.TemporaryDirectory() as directory:
        root = _root(directory)
        session = module.Session(root, uid=os.getuid(), loader=lambda: _identity(floor))
        session.handle(dict(operation="prepare_hold", floor=floor, nonce="1" * 32))
        session.close()
        active = root / "active"
        active.mkdir(mode=0o700)
        for name, text in {"previous_sha": "e" * 40, "previous_framework": "1.38.0",
                           "previous_post.sh": "reviewed lifecycle\n", "previous_requirements.txt": "reviewed requirements\n"}.items():
            (active / name).write_text(text)
        loader = lambda: _identity(floor)
        _error(lambda: module.check_update(dict(phase="admit", framework="1.37.3"), root,
                                          uid=os.getuid(), loader=loader), "incompatible_framework")
        _error(lambda: module.check_update(dict(phase="rollback", framework="1.38.0"), root,
                                          uid=os.getuid(), loader=loader))
        module.check_update(dict(phase="snapshot", framework="1.38.0"), root, uid=os.getuid(), loader=loader)
        module.check_update(dict(phase="rollback", framework="1.38.0"), root, uid=os.getuid(), loader=loader)
        (active / "previous_post.sh").write_text("changed lifecycle\n")
        _error(lambda: module.check_update(dict(phase="admit", framework="1.38.0"), root,
                                          uid=os.getuid(), loader=loader), "recovery_unavailable")
        assert active.is_dir(), "unsafe interrupted recovery must retain ACTIVE"


@th.django_unit_test()
def test_floor_deadline_closes_lock_without_erasing_stamp(opts):
    from mojo.deploy import acme_hub_floor as module
    floor = _floor()
    with tempfile.TemporaryDirectory() as directory:
        root = _root(directory)
        clock = [10.0]
        session = module.Session(root, uid=os.getuid(), clock=lambda: clock[0], loader=lambda: _identity(floor))
        receipt = session.handle(dict(operation="prepare_hold", floor=floor, nonce="1" * 32))
        clock[0] += module.HEARTBEAT_SECONDS + 1
        _error(lambda: session.handle(dict(operation="confirm_hold", nonce="1" * 32,
                                          helper_session=receipt["helper_session"])), "session_expired")
        assert session.lock_fd is None, "expired session must release its actual descriptor"
        assert module.read_floor(root, uid=os.getuid()) == floor, "expiry must never clear the persistent floor"


@th.django_unit_test()
def test_floor_partial_frame_deadline_and_eof_release_real_pipe(opts):
    from mojo.deploy import acme_hub_floor as module
    floor = _floor()
    with tempfile.TemporaryDirectory() as directory:
        root = _root(directory)
        script = (
            "import json,os,sys\nfrom mojo.deploy import acme_hub_floor as m\n"
            f"floor=json.loads({json.dumps(json.dumps(floor))})\n"
            f"s=m.Session({str(root)!r},uid=os.getuid(),loader=lambda:{{k:v for k,v in floor['artifact'].items() if k!='wheel_sha256'}})\n"
            "s.heartbeat=s.clock()+0.3\n"
            "try: m.serve(sys.stdin.buffer,sys.stdout.buffer,s)\n"
            "except m.FloorError: sys.exit(7)\n")
        child = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            child.stdin.write(b'{"operation":')
            child.stdin.flush()
            child.wait(timeout=3)
            assert child.returncode == 7, "partial input cannot defeat the helper's deadline"
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()
        script = script.replace("s.heartbeat=s.clock()+0.3\n", "")
        child = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            child.stdin.write(module.encoded(dict(operation="prepare_hold", floor=floor, nonce="4" * 32)) + b"\n")
            child.stdin.flush()
            receipt = json.loads(child.stdout.readline())
            assert receipt["held"] is True, "real child must hold the lock across frames"
            child.stdin.close()
            child.wait(timeout=3)
            assert child.returncode == 0, "EOF must end the held session cleanly"
            after = module.Session(root, uid=os.getuid()).handle(dict(operation="status"))
            assert after["floor"] == floor, "EOF preserves stamp and frees lock for the next real process"
        finally:
            if child.poll() is None:
                child.kill()
            child.stdout.close()
            child.stderr.close()


@th.django_unit_test()
def test_floor_strict_frames_and_conservative_release_policy(opts):
    from mojo.deploy import acme_hub_floor as module
    for raw in (b'{"operation":"status","operation":"release"}', b'{"a":NaN}', b'[]', b'{}' * 40000):
        _error(lambda: module.decode(raw))
    for value in ("1.38.0.dev1", "1.38.0rc1", "1.38.0+local", "v1.38.0", "garbage"):
        _error(lambda: module.release(value), "artifact_unavailable")
