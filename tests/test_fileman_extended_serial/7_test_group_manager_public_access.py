"""Regression coverage for the privacy state of a newly created group FileManager."""
from unittest.mock import patch

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

SYSTEM_PK = -6928


class _AuditBackend:
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


def _get_for_group(group, backend, use=""):
    """The package's one S3 boundary patch for group provisioning."""
    from mojo.apps.fileman.models import FileManager

    with patch("mojo.apps.fileman.backends.get_backend", return_value=backend):
        return FileManager.get_for_group(group=group, use=use)


def _new_group_manager(name, parent_is_public, audit_status, use=""):
    """Create a system default and a group, then provision the group's manager."""
    from mojo.apps.account.models import Group
    from mojo.apps.fileman.models import FileManager

    Group.objects.filter(name=name).delete()
    group = Group.objects.create(name=name)
    # get_for_group() takes the system default with QuerySet.first(); a negative
    # fixture id keeps this row first without touching another test's row.
    FileManager.objects.filter(pk=SYSTEM_PK).delete()
    system_manager = FileManager.objects.create(
        pk=SYSTEM_PK,
        name=f"{name}_system",
        backend_type="s3",
        backend_url="s3://audit-test-bucket/fileman",
        is_active=True,
        is_default=True,
        is_public=parent_is_public,
    )
    backend = _AuditBackend(audit_status)
    manager = _get_for_group(group, backend, use=use)
    manager.refresh_from_db()
    return group, system_manager, manager, backend


def _cleanup(group, system_manager, manager):
    from mojo.apps.fileman.models import FileManager

    FileManager.objects.filter(pk=manager.pk).delete()
    FileManager.objects.filter(pk=system_manager.pk).delete()
    group.delete()


@th.django_unit_test("FileManager: a new group manager under a private parent is not flagged public when the audit is unknown")
def test_private_parent_unknown_audit_stays_private(opts):
    group, system_manager, manager, backend = _new_group_manager(
        "fm_group_audit_private_unknown", parent_is_public=False, audit_status="unknown")
    try:
        assert_eq(manager.parent_id, system_manager.pk, "the child should hang off the system default")
        assert_eq(manager.is_public, False,
                  "a private parent with an unknown audit must not leave the new group manager flagged public")
        assert_eq(manager.public_access_audit["status"], "unknown",
                  f"the unknown evidence should be recorded, got {manager.public_access_audit}")
        assert_eq(backend.policy_mutations, [], "creation must not write bucket policy")
    finally:
        _cleanup(group, system_manager, manager)


@th.django_unit_test("FileManager: a new use-scoped group manager under a private parent is not flagged public when the audit is unknown")
def test_private_parent_unknown_audit_stays_private_with_use(opts):
    group, system_manager, manager, backend = _new_group_manager(
        "fm_group_audit_private_use", parent_is_public=False, audit_status="unknown", use="firmware")
    try:
        assert_eq(manager.use, "firmware", "the use should be stored on the new manager")
        assert_eq(manager.is_public, False,
                  "a private parent with an unknown audit must not leave the new use-scoped manager flagged public")
        assert_eq(backend.policy_mutations, [], "creation must not write bucket policy")
    finally:
        _cleanup(group, system_manager, manager)


@th.django_unit_test("FileManager: a new group manager under a private parent is private when the audit says private")
def test_private_parent_private_audit(opts):
    group, system_manager, manager, backend = _new_group_manager(
        "fm_group_audit_private_private", parent_is_public=False, audit_status="private")
    try:
        assert_eq(manager.is_public, False, "a conclusive private audit must store private")
        assert_eq(manager.public_access_audit["status"], "private",
                  f"the private evidence should be recorded, got {manager.public_access_audit}")
        assert_eq(backend.audit_calls, [None], "creation should audit the prefix exactly once")
    finally:
        _cleanup(group, system_manager, manager)


@th.django_unit_test("FileManager: a new group manager under a public parent stays public when the audit says public")
def test_public_parent_public_audit(opts):
    group, system_manager, manager, backend = _new_group_manager(
        "fm_group_audit_public_public", parent_is_public=True, audit_status="public")
    try:
        assert_eq(manager.is_public, True, "a genuinely public prefix must stay public")
        assert_eq(manager.public_access_audit["status"], "public",
                  f"the public evidence should be recorded, got {manager.public_access_audit}")
        assert_eq(backend.policy_mutations, [], "creation must not write bucket policy")
    finally:
        _cleanup(group, system_manager, manager)


@th.django_unit_test("FileManager: a new group manager under a public parent keeps the inherited value when the audit is unknown")
def test_public_parent_unknown_audit_keeps_inherited(opts):
    group, system_manager, manager, backend = _new_group_manager(
        "fm_group_audit_public_unknown", parent_is_public=True, audit_status="unknown")
    try:
        assert_eq(manager.is_public, True,
                  "unknown evidence preserves the inherited operator value, as it does today")
        assert_eq(manager.public_access_audit["status"], "unknown",
                  f"the unknown evidence should be recorded, got {manager.public_access_audit}")
    finally:
        _cleanup(group, system_manager, manager)


@th.django_unit_test("FileManager: an existing group manager is returned unchanged and is not re-audited")
def test_existing_group_manager_unchanged(opts):
    from mojo.apps.fileman.models import FileManager

    group, system_manager, manager, backend = _new_group_manager(
        "fm_group_audit_existing", parent_is_public=True, audit_status="public")
    try:
        FileManager.objects.filter(pk=system_manager.pk).update(is_public=False)
        again_backend = _AuditBackend("private")
        again = _get_for_group(group, again_backend)
        assert_eq(again.pk, manager.pk, "the existing manager should be returned")
        assert_eq(again.is_public, True, "an existing manager's stored value must not change")
        assert_true(again_backend.audit_calls == [],
                    f"an existing group manager must not be re-audited, got {again_backend.audit_calls}")
    finally:
        _cleanup(group, system_manager, manager)
