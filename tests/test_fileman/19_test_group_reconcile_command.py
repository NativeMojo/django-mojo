"""Coverage for reconcile_fileman_public_access on group-scoped S3 managers."""
import io
import re

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


class _StubBackend:
    """An S3 backend stand-in whose audit answers one fixed status."""

    def __init__(self, status):
        self.status = status
        self.audit_calls = []
        self.policy_mutations = []

    def test_connection(self):
        return True

    def check_public_access_for_prefix(self, file_path=None):
        self.audit_calls.append(file_path)
        return self.status == "public", [f"audit answered {self.status}"], {
            "status": self.status,
            "method": "policy",
        }

    def make_path_public(self):
        self.policy_mutations.append("public")

    def make_path_private(self):
        self.policy_mutations.append("private")


def _run(backends, *args, failing=()):
    """Run the command with a stand-in backend per manager id.

    The command's per-manager step is the seam: every manager it checks gets a
    stub backend before the audit, so no run reaches AWS. A manager this test
    does not own answers unknown, and callers only persist with --manager.
    """
    from django.core.management import call_command
    from mojo.apps.fileman.management.commands.reconcile_fileman_public_access import Command

    fallback = _StubBackend("unknown")

    class _StubbedCommand(Command):
        def check_manager(self, manager, dry_run):
            if manager.pk in failing:
                raise RuntimeError("simulated manager failure")
            manager._backend = backends.get(manager.pk, fallback)
            return super().check_manager(manager, dry_run)

    output = io.StringIO()
    errors = io.StringIO()
    call_command(_StubbedCommand(), *args, stdout=output, stderr=errors)
    return output.getvalue(), errors.getvalue()


def _group(name):
    from mojo.apps.account.models import Group

    Group.objects.filter(name=name).delete()
    return Group.objects.create(name=name)


def _user(username):
    from mojo.apps.account.models import User

    User.objects.filter(username=username).delete()
    return User.objects.create(username=username, email=f"{username}@example.com")


def _manager(name, is_public, group=None, user=None, **extra):
    from mojo.apps.fileman.models import FileManager

    fields = {"backend_type": "s3", "is_active": True}
    fields.update(extra)
    return FileManager.objects.create(
        name=name,
        backend_url=f"s3://audit-test-bucket/fileman/{name}",
        group=group,
        user=user,
        is_public=is_public,
        **fields,
    )


def _line(output, manager):
    prefix = f"FileManager {manager.pk} ({manager.name}): "
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _summary(output, key):
    match = re.search(rf"\b{key}=(\d+)", output)
    return int(match.group(1)) if match else None


# (flag before, audit status) -> (flag after, per-store line, summary counter)
GROUP_CASES = [
    (True, "private", False, "private (is_public True -> False)", "changed"),
    (True, "public", True, "public", None),
    (False, "public", False, "public (policy is public, is_public False kept)", None),
    (True, "unknown", True, "unknown (is_public True kept, not verified)", "unverified_public"),
]


@th.django_unit_test("FileManager command: a group manager is corrected from public to private only")
def test_group_manager_one_way_correction(opts):
    for index, (before, status, after, line, counter) in enumerate(GROUP_CASES):
        group = _group(f"fm_group_reconcile_{index}")
        manager = _manager(f"fm_group_reconcile_{index}", before, group=group)
        backend = _StubBackend(status)
        try:
            output, errors = _run({manager.pk: backend}, "--manager", str(manager.pk))
            manager.refresh_from_db()

            assert_eq(errors, "", f"case {before}/{status} should not fail")
            assert_eq(manager.is_public, after,
                      f"group manager flagged {before} with a {status} audit must end {after}")
            assert_eq(manager.public_access_audit["status"], status,
                      f"the {status} audit should be saved, got {manager.public_access_audit}")
            assert_eq(_line(output, manager), line,
                      f"unexpected line for {before}/{status} in {output}")
            assert_eq(_summary(output, "changed"), 1 if counter == "changed" else 0,
                      f"changed count is wrong for {before}/{status} in {output}")
            assert_eq(_summary(output, "unverified_public"),
                      1 if counter == "unverified_public" else 0,
                      f"unverified_public count is wrong for {before}/{status} in {output}")
            assert_eq(backend.policy_mutations, [], "the command must never write bucket policy")
        finally:
            manager.delete()
            group.delete()


@th.django_unit_test("FileManager command: a dry run on group managers saves nothing")
def test_group_manager_dry_run(opts):
    for index, (before, status, after, line, counter) in enumerate(GROUP_CASES):
        group = _group(f"fm_group_reconcile_dry_{index}")
        manager = _manager(f"fm_group_reconcile_dry_{index}", before, group=group)
        backend = _StubBackend(status)
        try:
            output, errors = _run(
                {manager.pk: backend}, "--manager", str(manager.pk), "--dry-run")
            manager.refresh_from_db()

            assert_eq(errors, "", f"dry-run case {before}/{status} should not fail")
            assert_eq(manager.is_public, before, "a dry run must not change is_public")
            assert_true(manager.public_access_audit is None,
                        f"a dry run must not save audit metadata, got {manager.public_access_audit}")
            expected = line.replace("(is_public True -> False)",
                                    "(would change is_public True -> False)")
            assert_eq(_line(output, manager), expected,
                      f"unexpected dry-run line for {before}/{status} in {output}")
            assert_eq(_summary(output, "would_change"), 1 if counter == "changed" else 0,
                      f"would_change count is wrong for {before}/{status} in {output}")
            assert_true(_summary(output, "changed") is None,
                        f"a dry run must not report changed=, got {output}")
            assert_eq(backend.policy_mutations, [], "the command must never write bucket policy")
        finally:
            manager.delete()
            group.delete()


@th.django_unit_test("FileManager command: a manager with a user and a group is treated as a group manager")
def test_user_and_group_manager_is_group_scoped(opts):
    group = _group("fm_group_reconcile_both")
    user = _user("fm_group_reconcile_both_user")
    manager = _manager("fm_group_reconcile_both", False, group=group, user=user)
    backend = _StubBackend("public")
    try:
        output, _ = _run({manager.pk: backend}, "--manager", str(manager.pk))
        manager.refresh_from_db()

        assert_eq(manager.is_public, False,
                  "a manager with a group must never be turned public by the command")
        assert_eq(_line(output, manager), "public (policy is public, is_public False kept)",
                  f"unexpected line in {output}")
        assert_eq(backend.policy_mutations, [], "the command must never write bucket policy")
    finally:
        manager.delete()
        group.delete()
        user.delete()


@th.django_unit_test("FileManager command: --manager skips system, inactive, non-S3 and missing managers")
def test_named_manager_skips(opts):
    from mojo.apps.fileman.models import FileManager

    group = _group("fm_group_reconcile_skip")
    system = _manager("fm_group_reconcile_skip_system", True)
    inactive = _manager("fm_group_reconcile_skip_inactive", True, group=group, is_active=False)
    local = _manager("fm_group_reconcile_skip_local", True, group=group, backend_type="file")
    missing_pk = -700619
    FileManager.objects.filter(pk=missing_pk).delete()
    backend = _StubBackend("private")
    rows = (system, inactive, local)
    try:
        output, errors = _run(
            {row.pk: backend for row in rows},
            "--manager", str(system.pk), "--manager", str(inactive.pk),
            "--manager", str(local.pk), "--manager", str(missing_pk),
            "--manager", str(system.pk))

        assert_eq(errors, "", "skipped managers are not failures")
        assert_eq(_line(output, system), "skipped (system manager)", f"got {output}")
        assert_eq(_line(output, inactive), "skipped (inactive)", f"got {output}")
        assert_eq(_line(output, local), "skipped (not an S3 manager)", f"got {output}")
        assert_true(f"FileManager {missing_pk}: skipped (not found)" in output, f"got {output}")
        assert_eq(_summary(output, "skipped"), 4,
                  f"a manager id given twice is counted once, got {output}")
        assert_eq(backend.audit_calls, [], "a skipped manager must not be audited")
        for row in rows:
            row.refresh_from_db()
            assert_eq(row.is_public, True, f"skipped manager {row.name} must be untouched")
            assert_true(row.public_access_audit is None,
                        f"skipped manager {row.name} must have no audit saved")
    finally:
        for row in rows:
            row.delete()
        group.delete()


@th.django_unit_test("FileManager command: a personal manager named with --manager is corrected both ways")
def test_named_personal_manager_two_way(opts):
    user = _user("fm_group_reconcile_personal_user")
    to_private = _manager("fm_group_reconcile_personal_a", True, user=user)
    to_public = _manager("fm_group_reconcile_personal_b", False, user=user)
    backends = {to_private.pk: _StubBackend("private"), to_public.pk: _StubBackend("public")}
    try:
        output, errors = _run(
            backends, "--manager", str(to_private.pk), "--manager", str(to_public.pk))
        to_private.refresh_from_db()
        to_public.refresh_from_db()

        assert_eq(errors, "", "personal managers should not fail")
        assert_eq(to_private.is_public, False, "a personal manager is still corrected to private")
        assert_eq(to_public.is_public, True, "a personal manager is still corrected to public")
        assert_eq(_line(output, to_private), "private (is_public True -> False)", f"got {output}")
        assert_eq(_line(output, to_public), "public (is_public False -> True)", f"got {output}")
        assert_eq(_summary(output, "changed"), 2, f"got {output}")
        for backend in backends.values():
            assert_eq(backend.policy_mutations, [], "the command must never write bucket policy")
    finally:
        to_private.delete()
        to_public.delete()
        user.delete()


@th.django_unit_test("FileManager command: without --groups a group manager is counted, not checked")
def test_default_run_leaves_group_managers(opts):
    group = _group("fm_group_reconcile_default")
    manager = _manager("fm_group_reconcile_default", True, group=group)
    backend = _StubBackend("private")
    try:
        # A dry run, so this default-scope run writes nothing to rows other tests own.
        output, _ = _run({manager.pk: backend}, "--dry-run")
        manager.refresh_from_db()

        assert_true(_line(output, manager) is None,
                    f"a group manager must have no line of its own by default, got {output}")
        assert_eq(backend.audit_calls, [], "a group manager must not be audited by default")
        assert_eq(manager.is_public, True, "a default run must not touch a group manager")
        assert_true(manager.public_access_audit is None,
                    "a default run must not save a group manager's audit")
        match = re.search(r"(\d+) group-scoped S3 managers not checked \(use --groups\)", output)
        assert_true(match is not None and int(match.group(1)) >= 1,
                    f"the default run should count the unchecked group managers, got {output}")

        listed, _ = _run({manager.pk: backend}, "--groups", "--dry-run")
        manager.refresh_from_db()
        assert_eq(_line(listed, manager), "private (would change is_public True -> False)",
                  f"--groups --dry-run should list the group manager, got {listed}")
        assert_true("not checked" not in listed,
                    f"--groups must not print the not-checked line, got {listed}")
        assert_eq(manager.is_public, True, "a dry run must not change is_public")
        assert_eq(backend.policy_mutations, [], "the command must never write bucket policy")
    finally:
        manager.delete()
        group.delete()


@th.django_unit_test("FileManager command: one failing group manager does not stop the next")
def test_group_manager_failure_isolation(opts):
    group = _group("fm_group_reconcile_failure")
    bad = _manager("fm_group_reconcile_failure_bad", True, group=group)
    good = _manager("fm_group_reconcile_failure_good", True, group=group)
    backend = _StubBackend("private")
    try:
        output, errors = _run(
            {good.pk: backend}, "--manager", str(bad.pk), "--manager", str(good.pk),
            failing={bad.pk})
        bad.refresh_from_db()
        good.refresh_from_db()

        assert_true(f"FileManager {bad.pk} ({bad.name}): failed: simulated manager failure" in errors,
                    f"the failing manager should be reported, got {errors}")
        assert_eq(bad.is_public, True, "a failed manager must be left unchanged")
        assert_eq(good.is_public, False, "the command should continue to the next manager")
        assert_eq(_summary(output, "failed"), 1, f"got {output}")
        assert_eq(_summary(output, "changed"), 1, f"got {output}")
    finally:
        bad.delete()
        good.delete()
        group.delete()
