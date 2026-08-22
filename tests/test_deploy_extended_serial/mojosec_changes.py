"""Moved from tests/test_deploy/mojosec_changes.py (maestro #2558).

These tests mock production module attributes process-globally
(`mojosec_changes._now`, `_snapshot`, `ChangeJournal`, `certbot_sync`
internals, `node_setup.os.path`, `mojo.deploy.mojosec` converge internals) —
unsafe under the parallel default tier. The change-journal evidence, scope,
manifest, and wheel-RECORD contracts stay in the default-tier
tests/test_deploy/mojosec_changes.py.
"""

import datetime
import json
import os
import tempfile
from unittest import mock

from testit import helpers as th


@th.django_unit_test()
def test_long_operation_correlates_from_start_through_post_completion(opts):
    import mojo.deploy.mojosec_changes as changes
    from mojo.mojosec.expected_changes import (
        MAX_OPERATION_CORRELATION_SECONDS, ExpectedChangeError, annotation, load_manifest,
    )

    with tempfile.TemporaryDirectory() as root:
        watched = os.path.join(root, "long-operation.conf")
        journal = changes.ChangeJournal(
            journal_path=os.path.join(root, "journal.json"),
            lock_path=os.path.join(root, "journal.lock"),
            manifest_path=os.path.join(root, "expected.json"),
            allowed_roots=[root],
        )
        completed_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        started_at = completed_at - datetime.timedelta(minutes=12)
        observed_at = started_at + datetime.timedelta(minutes=6)
        with mock.patch.object(changes, "_now", return_value=started_at):
            journal.begin("long-operation", "system-pip", [watched], ttl_seconds=900)
        with open(watched, "w", encoding="utf-8") as handle:
            handle.write("event observed while child is still running\n")
        with mock.patch.object(changes, "_now", return_value=completed_at):
            journal.complete("long-operation")

        entries = load_manifest(journal.manifest_path, require_root=False)
        th.assert_eq(entries[0]["started_at"], changes._timestamp(started_at),
                     "v2 evidence must retain the validated operation start")
        value = annotation(
            entries, watched, "created", None, changes._snapshot(watched),
            now=completed_at + datetime.timedelta(seconds=1), observed_at=observed_at)
        th.assert_eq(value["operation_id"], "long-operation",
                     "an event observed during a long-running child must correlate")
        th.assert_eq(annotation(
            entries, watched, "created", None, changes._snapshot(watched),
            now=completed_at + datetime.timedelta(seconds=1),
            observed_at=started_at - datetime.timedelta(microseconds=1)), None,
            "an observation before operation start must remain unexplained")
        th.assert_eq(annotation(
            entries, watched, "created", None, changes._snapshot(watched),
            now=completed_at + datetime.timedelta(seconds=1),
            observed_at=completed_at + datetime.timedelta(
                seconds=MAX_OPERATION_CORRELATION_SECONDS + 1)), None,
            "post-completion correlation must remain bounded")

        with open(journal.manifest_path, encoding="utf-8") as handle:
            invalid = json.load(handle)
        invalid["entries"][0]["started_at"] = (
            completed_at + datetime.timedelta(seconds=1)).isoformat()
        with open(journal.manifest_path, "w", encoding="utf-8") as handle:
            json.dump(invalid, handle)
        os.chmod(journal.manifest_path, 0o600)
        with th.assert_raises(ExpectedChangeError):
            load_manifest(journal.manifest_path, require_root=False)


@th.django_unit_test()
def test_oversized_wheel_declaration_is_refused(opts):
    """Split out of test_change_journal_fits_bounded_wheel_evidence (#2558):
    the refusal path stubs `_snapshot`, a process-global module patch."""
    from mojo.deploy.mojosec_changes import (
        ChangeError, ChangeJournal, MAX_PACKAGE_PATHS,
    )

    with tempfile.TemporaryDirectory() as root:
        journal = ChangeJournal(
            journal_path=os.path.join(root, "journal.json"),
            lock_path=os.path.join(root, "journal.lock"),
            manifest_path=os.path.join(root, "expected.json"),
            allowed_roots=[root],
        )
        long_tail = "/".join(["y" * 200] * 15)
        oversized = [os.path.join(root, long_tail, f"{index:04d}")
                     for index in range(4096)]
        with mock.patch("mojo.deploy.mojosec_changes._snapshot", return_value=None):
            with th.assert_raises(ChangeError):
                journal.begin(
                    "oversized-wheel", "pip-package-change", oversized,
                    max_paths=MAX_PACKAGE_PATHS)


@th.django_unit_test()
def test_stable_child_nonzero_aborts_without_completing(opts):
    import mojo.deploy.mojosec_changes as changes

    journal = mock.Mock()
    with mock.patch.object(changes, "ChangeJournal", return_value=journal), \
            mock.patch.object(changes.subprocess, "run",
                              return_value=mock.Mock(returncode=17)):
        result = changes.main([
            "run", "--operation-id", "deploy-failure", "--kind", "rendered-config",
            "--path", "/etc/nginx/nginx.conf", "--", "/bin/false",
        ])
    th.assert_eq(result, 17, "stable helper must return the failed child status")
    journal.abort.assert_called_once_with("deploy-failure")
    th.assert_true(not journal.complete.called,
                   "a failed producer must leave its mutation unexplained")


@th.django_unit_test()
def test_certbot_failure_aborts_exact_lineage_operation(opts):
    import mojo.deploy.certbot_sync as certbot
    import mojo.deploy.mojosec_changes as changes

    journal = mock.Mock()
    configured = {
        "AWS_CERT_BUCKET": "cert-bucket", "LOAD_BALANCER_DOMAIN": "example.com",
        "PRIMARY_BALANCER_HOST": "primary",
    }
    failed = mock.Mock(returncode=4, stderr=b"renew failed", stdout=b"")
    with mock.patch.object(certbot, "is_primary", return_value=True), \
            mock.patch.object(certbot, "find_certbot", return_value="/usr/bin/certbot"), \
            mock.patch.object(certbot.os.path, "exists", return_value=True), \
            mock.patch.object(certbot.subprocess, "run", return_value=failed), \
            mock.patch.object(changes, "ChangeJournal", return_value=journal):
        result = certbot.renew(configured, False)
    th.assert_eq(result, 1, "a failed certbot child must fail renewal")
    journal.begin.assert_called_once()
    journal.abort.assert_called_once()
    th.assert_true(not journal.complete.called,
                   "failed renewal bytes must not receive an expected annotation")


@th.django_unit_test()
def test_node_setup_declares_exact_unit_and_cron_destinations(opts):
    import mojo.deploy.mojosec_changes as changes
    import mojo.deploy.node_setup as node_setup

    with tempfile.TemporaryDirectory() as root:
        units = os.path.join(root, "units")
        os.mkdir(units)
        open(os.path.join(units, "mojo.service"), "w", encoding="utf-8").close()
        wrapper = mock.Mock(return_value=[])
        with mock.patch.object(node_setup.os.path, "exists", return_value=True), \
                mock.patch.object(changes, "run_trusted_change", wrapper):
            node_setup.plan(
                "/opt/api", "", "ec2-user", units,
                "/etc/systemd/system", "/etc/cron.d/3_mojo_jobs", False)
    declared = wrapper.call_args.args[2]
    th.assert_eq(declared, [
        "/etc/systemd/system/mojo.service", "/etc/cron.d/3_mojo_jobs"],
        "node_setup must predeclare only its rendered unit and cron destinations")


@th.django_unit_test()
def test_journaled_converge_never_reruns_after_completion_failure(opts):
    import mojo.deploy.mojosec as deploy_mojosec
    from mojo.deploy.mojosec_changes import ChangeError

    calls = {"mutations": 0}

    class FakeJournal:
        def __init__(self, begin_error=None, complete_error=None):
            self.begin_error = begin_error
            self.complete_error = complete_error

        def begin(self, *args, **kwargs):
            if self.begin_error:
                raise self.begin_error

        def complete(self, operation_id):
            if self.complete_error:
                raise self.complete_error

        def abort(self, operation_id):
            pass

    def run(journal):
        calls["mutations"] = 0
        with mock.patch.object(deploy_mojosec, "converge",
                               side_effect=lambda *a, **k: (
                                   calls.__setitem__(
                                       "mutations", calls["mutations"] + 1) or
                                   {"changed": True})), \
                mock.patch.object(deploy_mojosec.os.path, "exists",
                                  return_value=True), \
                mock.patch.object(deploy_mojosec.os, "geteuid",
                                  return_value=0), \
                mock.patch(
                    "mojo.deploy.mojosec_changes.ChangeJournal",
                    return_value=journal):
            return deploy_mojosec._journaled_converge(
                "observe", "best_effort", "/opt/api", "deploy-x")

    result = run(FakeJournal(complete_error=ChangeError("consumed")))
    th.assert_eq((calls["mutations"], result["changed"]), (1, True),
                 "a completion failure must never re-run the converge mutation")
    result = run(FakeJournal(begin_error=ChangeError("wedged")))
    th.assert_eq((calls["mutations"], result["changed"]), (1, True),
                 "a begin failure must fall back to exactly one unjournaled run")
