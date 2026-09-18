"""Root role proof cannot be replaced by application-writable receipts."""

import json
import os
import stat
import tempfile
from unittest import mock

from testit import helpers as th


REVISION = "a" * 32
DIGEST = "b" * 64


@th.django_unit_test()
def test_role_roundtrip_binds_both_revision_and_digest(opts):
    from mojo.deploy import fleet_config_role as role

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "role.json")
        uid = os.geteuid()
        for required in (True, False):
            th.assert_true(role.write(REVISION, DIGEST, required, path, required_uid=uid),
                           "root config-sync must publish each explicit node role")
            th.assert_eq(stat.S_IMODE(os.stat(path).st_mode), 0o644,
                         "role evidence is public-readable but never app-writable")
            document = role.read(REVISION, DIGEST, path, required_uid=uid)
            th.assert_true(isinstance(document, dict), "sealed evidence must return its binding")
            th.assert_eq(document["request_service_required"], required,
                         "only the exact installed configuration receives the role")
            th.assert_eq(role.read("c" * 32, DIGEST, path, required_uid=uid), None,
                         "a predecessor role cannot authorize a later revision")
            th.assert_eq(role.read(REVISION, "d" * 64, path, required_uid=uid), None,
                         "a changed installed file cannot reuse role authority")


@th.django_unit_test()
def test_role_requires_exact_schema_and_boolean(opts):
    from mojo.deploy import fleet_config_role as role

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "role.json")
        uid = os.geteuid()
        valid = {"revision": REVISION, "digest": DIGEST,
                 "request_service_required": False, "installed_at": 123.0}
        for invalid in (dict(valid, request_service_required=0),
                        dict(valid, request_service_required=None),
                        dict(valid, extra=True), dict(valid, installed_at=False),
                        dict(valid, installed_at=float("nan")),
                        dict(valid, installed_at=10 ** 350),
                        dict(valid, installed_at=-1), {}, [valid]):
            with open(path, "w") as handle:
                json.dump(invalid, handle)
            os.chmod(path, 0o644)
            th.assert_eq(role.read(REVISION, DIGEST, path, required_uid=uid), None,
                         "malformed or non-boolean evidence must fail closed")
        th.assert_true(not role.write(REVISION, DIGEST, 0, path, required_uid=uid),
                       "integer false must never be published as a disabled role")


@th.django_unit_test()
def test_role_refuses_unsafe_file_and_parent(opts):
    from mojo.deploy import fleet_config_role as role

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "role.json")
        uid = os.geteuid()
        th.assert_true(role.write(REVISION, DIGEST, False, path, required_uid=uid),
                       "the fixture must start with valid sealed evidence")
        os.chmod(path, 0o666)
        th.assert_eq(role.read(REVISION, DIGEST, path, required_uid=uid), None,
                     "an app-writable file cannot disable request proof")
        os.chmod(path, 0o644)
        os.chmod(directory, 0o777)
        try:
            th.assert_eq(role.read(REVISION, DIGEST, path, required_uid=uid), None,
                         "a replaceable parent invalidates even a sealed file")
            th.assert_true(not role.write(REVISION, DIGEST, False, path, required_uid=uid),
                           "config-sync must not publish into an unsafe parent")
        finally:
            os.chmod(directory, 0o700)
        th.assert_eq(role.read(REVISION, DIGEST, path, required_uid=uid + 1), None,
                     "foreign-owned evidence must never prove a node role")
        th.assert_true(not role.write(REVISION, DIGEST, False, path, required_uid=uid + 1),
                       "an unprivileged writer must fail before changing authority")


@th.django_unit_test()
def test_role_refuses_symlinks_hardlinks_and_missing_evidence(opts):
    from mojo.deploy import fleet_config_role as role

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "role.json")
        alias = os.path.join(directory, "alias.json")
        uid = os.geteuid()
        th.assert_eq(role.read(REVISION, DIGEST, path, required_uid=uid), None,
                     "missing role is unknown, never implicit worker-only")
        th.assert_true(role.write(REVISION, DIGEST, False, path, required_uid=uid),
                       "the fixture must publish role evidence")
        os.symlink(path, alias)
        th.assert_eq(role.read(REVISION, DIGEST, alias, required_uid=uid), None,
                     "a symlink cannot redirect role authority")
        os.unlink(alias)
        os.link(path, alias)
        th.assert_eq(role.read(REVISION, DIGEST, path, required_uid=uid), None,
                     "multiply-linked evidence cannot establish authority")
        os.unlink(alias)
        os.symlink(directory, alias)
        th.assert_eq(role.read(REVISION, DIGEST, os.path.join(alias, "role.json"),
                              required_uid=uid), None,
                     "a symlinked authority directory must fail closed")


@th.django_unit_test()
def test_role_refuses_oversized_or_changed_evidence(opts):
    from mojo.deploy import fleet_config_role as role

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "role.json")
        uid = os.geteuid()
        with open(path, "wb") as handle:
            handle.write(b"x" * (role.MAX_BYTES + 1))
        os.chmod(path, 0o644)
        th.assert_eq(role.read(REVISION, DIGEST, path, required_uid=uid), None,
                     "oversized documents cannot establish role authority")
        th.assert_true(role.write(REVISION, DIGEST, False, path, required_uid=uid),
                       "the fixture must restore valid evidence")
        original = role.os.fstat
        count = 0

        def changing_stat(fd):
            nonlocal count
            found = original(fd)
            count += 1
            if count != 3:
                return found
            return mock.Mock(st_size=found.st_size, st_mtime_ns=found.st_mtime_ns + 1,
                             st_ctime_ns=found.st_ctime_ns)

        with mock.patch.object(role.os, "fstat", side_effect=changing_stat):
            th.assert_eq(role.read(REVISION, DIGEST, path, required_uid=uid), None,
                         "evidence modified while being read must be rejected")


@th.django_unit_test()
def test_role_failed_atomic_replace_preserves_old_evidence(opts):
    from mojo.deploy import fleet_config_role as role

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "role.json")
        uid = os.geteuid()
        th.assert_true(role.write(REVISION, DIGEST, True, path, required_uid=uid),
                       "the fixture must publish its initial request role")
        with mock.patch.object(role.os, "replace", side_effect=OSError("disk failure")):
            th.assert_true(not role.write(REVISION, DIGEST, False, path, required_uid=uid),
                           "failed publication must return failure to config-sync")
        th.assert_eq(role.read(REVISION, DIGEST, path, required_uid=uid)["request_service_required"], True,
                     "failed publication must preserve the prior complete evidence")
        th.assert_eq(os.listdir(directory), ["role.json"],
                     "failed publication must clean its temporary evidence")


@th.django_unit_test()
def test_role_activation_time_is_root_generated_and_binding_specific(opts):
    from mojo.deploy import fleet_config_role as role

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "role.json")
        uid = os.geteuid()
        for revision, digest, required, now, expected in (
                (REVISION, DIGEST, False, 1000.0, 1000.0),
                (REVISION, DIGEST, False, 2000.0, 1000.0),
                (REVISION, "c" * 64, False, 3000.0, 3000.0),
                (REVISION, "c" * 64, True, 4000.0, 4000.0),
                ("d" * 32, "c" * 64, True, 5000.0, 5000.0)):
            with mock.patch.object(role.time, "time", return_value=now):
                th.assert_true(role.write(revision, digest, required, path, required_uid=uid),
                               "root must publish each verified configuration binding")
                document = role.read(revision, digest, path, required_uid=uid)
                th.assert_eq(document["installed_at"], expected,
                             "only an identical sealed binding preserves its activation time")


@th.django_unit_test()
def test_role_clear_is_root_only_anchored_and_idempotent(opts):
    from mojo.deploy import fleet_config_role as role

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "role.json")
        target = os.path.join(directory, "keep.json")
        uid = os.geteuid()
        th.assert_true(role.write(REVISION, DIGEST, False, target, required_uid=uid),
                       "fixture must create the unrelated authority target")
        os.symlink(target, path)
        th.assert_true(not role.clear(path, required_uid=uid + 1),
                       "unprivileged callers cannot clear sealed authority")
        os.chmod(directory, 0o777)
        try:
            th.assert_true(not role.clear(path, required_uid=uid),
                           "clearing must refuse an unsafe authority directory")
        finally:
            os.chmod(directory, 0o700)
        th.assert_true(role.clear(path, required_uid=uid),
                       "root can remove the selected authority name")
        th.assert_true(os.path.isfile(target),
                       "clearing a symlink must preserve its destination")
        th.assert_true(role.clear(path, required_uid=uid),
                       "repeated clearing of missing authority must succeed")
