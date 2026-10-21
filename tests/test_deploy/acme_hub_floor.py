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
import threading
import hashlib
import pwd
import shlex

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
        module.check_update(dict(phase="restored", framework="1.38.0"), root, uid=os.getuid(), loader=loader)
        substituted = dict(_identity(floor), source_sha="f" * 40)
        _error(lambda: module.check_update(dict(phase="restored", framework="1.38.0"), root,
                                          uid=os.getuid(), loader=lambda: substituted), "recovery_unavailable")
        assert active.is_dir(), "same-release substituted recovery must retain ACTIVE before lifecycle"
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
    for value in ("1.38.0.dev1", "1.38.0rc1", "1.38.0+local", "1.38.0.post1", "1!1.38.0", "v1.38.0", "garbage"):
        _error(lambda: module.release(value), "artifact_unavailable")
    for value in ("1.37.4", "1.37.3", "1.37.2", "1.0"):
        _error(lambda: module.artifact(dict(_floor()["artifact"], release=value)), "artifact_unavailable")


@th.django_unit_test()
def test_floor_enrollment_pins_retained_bytes_and_prefloor_policy(opts):
    from mojo.deploy import acme_hub_floor as module
    with tempfile.TemporaryDirectory() as directory:
        root = _root(directory)
        helper, updater, marker = (root / name for name in ("helper", "updater", "enrollment"))
        helper.write_bytes(b"reviewed helper")
        updater.write_bytes(b"reviewed updater")
        helper.chmod(0o755)
        updater.chmod(0o755)
        value = dict(schema=1, helper_sha256=hashlib.sha256(helper.read_bytes()).hexdigest(),
                     updater_sha256=hashlib.sha256(updater.read_bytes()).hexdigest(),
                     app_user=pwd.getpwuid(os.getuid()).pw_name, project_root=str(root), artifact=_floor()["artifact"])
        marker.write_bytes(module.encoded(value))
        marker.chmod(0o644)
        read = lambda: module.enrollment(uid=os.getuid(), marker=marker, helper=helper, updater=updater)
        assert read() == value, "enrollment must bind actual retained helper/updater bytes"
        _error(lambda: module.check_update(dict(phase="admit", framework="1.37.3"), root,
                                          uid=os.getuid(), enrolled=value), "incompatible_framework")
        module.check_update(dict(phase="admit", framework="1.38.0"), root, uid=os.getuid(),
                            enrolled=value, loader=lambda: _identity(_floor()))
        updater.write_bytes(b"substituted updater")
        _error(read, "enrollment_unavailable")
        updater.unlink()
        _error(read)
        assert not (root / module.FLOOR_NAME).exists(), "prefloor enrollment checks cannot stamp or invent activation"


@th.django_unit_test()
def test_floor_retained_lock_open_is_read_only_and_never_creates_or_truncates(opts):
    from mojo.deploy import acme_hub_floor as module
    source = (Path(module.__file__).parent / "project_scripts/update.sh").read_text()
    start = source.index('previous_umask="$(umask)"')
    body = source[start:source.index('if [ "$MANUAL" = "1" ]; then', start)]
    with tempfile.TemporaryDirectory() as directory:
        root = _root(directory)
        lock = root / "update.lock"
        lock.write_bytes(b"existing lock inode")
        check = root / "check.py"
        check.write_text("import os\nfrom mojo.deploy import acme_hub_floor as m\n"
                         f"os.close(m._lock_descriptor({str(root)!r},os.getuid()))\n")
        proof = root / "proof.py"
        proof.write_text("import os,fcntl\n"
                         "assert fcntl.fcntl(9,fcntl.F_GETFL)&os.O_ACCMODE==os.O_RDONLY, 'retained fd must be read-only'\n"
                         "fcntl.flock(9,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                         f"other=os.open({str(lock)!r},os.O_RDWR)\n"
                         "try:\n fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                         "except BlockingIOError:\n pass\n"
                         "else:\n raise AssertionError('real exclusive flock must remain held')\n")
        script = ('set -e\nMOJO_ACME_ROOT_ENTRY=1\nLOCK_FILE=' + shlex.quote(str(lock)) + '\n'
                  + 'acme_helper_exec() { ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(check)) + '; }\n'
                  + body + '\n' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(proof)))
        good = subprocess.run(["bash", "-c", script], capture_output=True, timeout=5)
        assert good.returncode == 0 and lock.read_bytes() == b"existing lock inode", f"qualified retained read-only fd must hold local exclusive flock without truncation: {good.stderr!r}"
        for kind in ("mode", "symlink", "missing"):
            if kind == "mode":
                lock.chmod(0o644)
            elif kind == "symlink":
                lock.unlink()
                lock.symlink_to(root / "missing-target")
            else:
                lock.unlink()
            refused = subprocess.run(["bash", "-c", script], capture_output=True, timeout=5)
            assert refused.returncode != 0 and not (root / "missing-target").exists(), f"unsafe {kind} existing lock must refuse before opening/creation"
            if lock.is_symlink() or lock.exists():
                lock.unlink()
            if kind != "missing":
                lock.write_bytes(b"existing lock inode")
                lock.chmod(0o600)
        assert not lock.exists(), "enrolled missing lock must remain missing after refusal"


@th.django_unit_test()
def test_floor_root_selector_partial_stamp_and_poisoned_candidate(opts):
    from mojo.deploy.acme_hub_entry import ROOT_GATE
    with tempfile.TemporaryDirectory() as directory:
        root = _root(directory)
        state, libexec, poison = (root / name for name in ("state", "libexec", "poison"))
        for path in (state, libexec, poison):
            path.mkdir(mode=0o700)
        selected, executed = root / "selected", root / "poison-executed"
        helper = libexec / "mojo-acme-hub-upgrade"
        helper.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\" > " + str(selected) + "\n")
        helper.chmod(0o755)
        body = helper.read_bytes()
        marker = libexec / "mojo-acme-hub-enrollment.json"
        marker.write_text(json.dumps(dict(helper_sha256=hashlib.sha256(body).hexdigest())))
        marker.chmod(0o644)
        (poison / "sitecustomize.py").write_text("open(" + repr(str(executed)) + ", 'w').write('site')")
        (poison / "mojo.py").write_text("open(" + repr(str(executed)) + ", 'w').write('mojo')")
        # Only private fixture copies replace fixed paths/uid. Production has
        # no path, command, uid or environment selector.
        source = ROOT_GATE.replace('value.st_uid!=0', 'value.st_uid!=' + str(os.getuid()))
        for old, new in (("/var/lib/django-mojo-deploy", str(state)), ("/usr/local/libexec", str(libexec))):
            source = source.replace(old, new)
        source = source.replace('("/var", "/var/lib", "/usr", "/usr/local", "' + str(libexec) + '")', '(' + repr(str(root)) + ',)')
        floor = state / "acme-hub-floor.json"
        floor.write_text("partial stamp")
        environment = dict(os.environ, PYTHONPATH=str(poison))
        done = subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", source, "--framework", "1.38.0"],
                              cwd=poison, env=environment, capture_output=True, timeout=5)
        assert done.returncode == 0 and selected.read_text().splitlines()[0] == "--launch", "partial floor without DB enable must select fixed entry"
        assert not executed.exists(), "root selection must never import candidate Mojo or startup poison"
        selected.unlink()
        for kind in ("writable", "symlink", "hardlink", "digest"):
            if kind == "writable":
                helper.chmod(0o777)
            elif kind == "symlink":
                helper.unlink()
                helper.symlink_to(root / "unreviewed-helper")
                (root / "unreviewed-helper").write_bytes(body)
                (root / "unreviewed-helper").chmod(0o755)
            elif kind == "hardlink":
                os.link(helper, root / "other-link")
            else:
                helper.write_bytes(body + b"# unreviewed replacement\n")
            refused = subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", source], capture_output=True, timeout=5)
            assert refused.returncode != 0 and not selected.exists(), f"{kind} helper must refuse BEFORE executing root canary"
            helper.unlink()
            if (root / "other-link").exists():
                (root / "other-link").unlink()
            helper.write_bytes(body)
            helper.chmod(0o755)
        marker.unlink()
        refused = subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", source], capture_output=True, timeout=5)
        assert refused.returncode != 0 and not selected.exists(), "partial floor without enrollment binding cannot execute or fall back"
        marker.write_text(json.dumps(dict(helper_sha256=hashlib.sha256(body).hexdigest())))
        marker.chmod(0o644)
        helper.unlink()
        refused = subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", source], capture_output=True, timeout=5)
        assert refused.returncode != 0 and not selected.exists(), "missing enrolled entry cannot fall back to current package locator"
        floor.unlink()
        floor.symlink_to(root / "missing")
        refused = subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", source], capture_output=True, timeout=5)
        assert refused.returncode != 0, "unreadable or symlink stamp cannot become absence"


@th.django_unit_test()
def test_floor_sealed_startup_ignores_real_pth_and_dependency_poison(opts):
    from mojo.deploy import acme_hub_floor as module
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / "venv")],
                       check=True, capture_output=True, timeout=15)
        interpreter = root / "venv/bin/python3"
        found = subprocess.run([str(interpreter), "-I", "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"],
                               check=True, capture_output=True, text=True, timeout=5)
        site = Path(found.stdout.strip())
        executed = root / "startup-executed"
        package = site / "packaging"
        package.mkdir()
        (package / "__init__.py").write_text("open(" + repr(str(executed)) + ", 'w').write('dependency')\n")
        (site / "candidate.pth").write_text("import packaging\n")
        # Control proves this is an executable real system-site .pth in the
        # candidate pip environment, not merely PYTHONPATH text.
        subprocess.run([str(interpreter), "-I", "-c", "pass"], check=True, capture_output=True, timeout=5)
        assert executed.exists(), "control interpreter must execute candidate site/dependency poison"
        executed.unlink()
        result = subprocess.run([str(interpreter), "-I", "-S", module.__file__, "--updater-check"],
                                input=b'{}\n', capture_output=True, timeout=5)
        assert result.returncode != 0 and not executed.exists(), "sealed verifier must refuse without executing pip-mutable startup/dependency bytes"


@th.django_unit_test()
def test_floor_stdout_backpressure_obeys_deadline_and_releases_lock(opts):
    from mojo.deploy import acme_hub_floor as module
    with tempfile.TemporaryDirectory() as directory:
        root = _root(directory)
        floor = _floor()
        script = ("import sys,os\nfrom mojo.deploy import acme_hub_floor as m\n"
                  f"floor={floor!r}\ns=m.Session({str(root)!r},uid=os.getuid(),loader=lambda:{{k:v for k,v in floor['artifact'].items() if k!='wheel_sha256'}})\n"
                  "s.deadline=s.clock()+0.6\nm.serve(sys.stdin.buffer,sys.stdout.buffer,s)\n")
        child = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def flood(frame):
            try:
                child.stdin.write(frame * 1000)
                child.stdin.flush()
            except (BrokenPipeError, ValueError):
                pass
        try:
            child.stdin.write(module.encoded(dict(operation="prepare_hold", floor=floor, nonce="6" * 32)) + b"\n")
            child.stdin.flush()
            receipt = json.loads(child.stdout.readline())
            frame = module.encoded(dict(operation="confirm_hold", nonce="6" * 32,
                                         helper_session=receipt["helper_session"])) + b"\n"
            writer = threading.Thread(target=flood, args=(frame,), daemon=True)
            writer.start()
            # Deliberately never drain stdout again: the child must expire
            # even when its next receipt cannot fit in the real pipe.
            child.wait(timeout=3)
            writer.join(timeout=2)
            assert child.returncode != 0, "backpressured held helper must expire, not wait for its reader"
            after = module.Session(root, uid=os.getuid()).handle(dict(operation="status"))
            assert after["floor"] == floor, "output deadline must release flock and preserve irreversible floor"
        finally:
            if child.poll() is None:
                child.kill()
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()
