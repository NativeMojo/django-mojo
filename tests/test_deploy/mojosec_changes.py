import importlib.util
import json
import os
import stat
import tempfile
import zipfile

from testit import helpers as th


@th.django_unit_test()
def test_exact_change_journal_completes_v2_and_abort_never_annotates(opts):
    from mojo.deploy.mojosec_changes import ChangeJournal, _snapshot, run_trusted_change
    from mojo.mojosec.expected_changes import annotation, load_manifest

    with tempfile.TemporaryDirectory() as root:
        watched = os.path.join(root, "host.conf")
        journal = ChangeJournal(
            journal_path=os.path.join(root, "journal.json"),
            lock_path=os.path.join(root, "journal.lock"),
            manifest_path=os.path.join(root, "expected.json"),
            allowed_roots=[root],
        )
        journal.begin("deploy-1", "rendered-config", [watched], ttl_seconds=900)
        with open(watched, "w", encoding="utf-8") as handle:
            handle.write("trusted bytes\n")
        completed = journal.complete("deploy-1")
        th.assert_eq(len(completed), 1,
                     "a completed exact mutation should produce one v2 annotation")
        entries = load_manifest(os.path.join(root, "expected.json"), require_root=False)
        expected = annotation(entries, watched, "created", None, _snapshot(watched))
        th.assert_eq(expected["operation_id"], "deploy-1",
                     "v2 annotations must retain bounded operation identity")
        th.assert_eq(expected["operation_kind"], "rendered-config",
                     "the receiver-visible annotation must name its trusted producer kind")

        failed = os.path.join(root, "failed.conf")

        def mutate_then_fail():
            with open(failed, "w", encoding="utf-8") as handle:
                handle.write("changed but failed\n")
            raise RuntimeError("mutation failed")

        with th.assert_raises(RuntimeError):
            run_trusted_change(
                "deploy-2", "rendered-config", [failed], mutate_then_fail,
                journal=journal)
        manifest = json.load(open(os.path.join(root, "expected.json"), encoding="utf-8"))
        th.assert_true(all(item["path"] != failed for item in manifest["entries"]),
                       "failed/aborted operations must leave their mutation unexplained")
        th.assert_eq(journal.status()["active_operations"], 0,
                     "abort must remove the active operation without a blind window")


@th.django_unit_test()
def test_expected_change_evidence_matches_files_links_and_directories(opts):
    from mojo.deploy.mojosec_changes import ChangeJournal, _snapshot
    from mojo.mojosec.expected_changes import annotation, load_manifest

    with tempfile.TemporaryDirectory() as root:
        regular = os.path.join(root, "regular")
        link = os.path.join(root, "link")
        directory = os.path.join(root, "directory")
        journal = ChangeJournal(
            journal_path=os.path.join(root, "journal.json"),
            lock_path=os.path.join(root, "journal.lock"),
            manifest_path=os.path.join(root, "expected.json"),
            allowed_roots=[root],
        )
        paths = [regular, link, directory]
        with open(regular, "w", encoding="utf-8") as handle:
            handle.write("old\n")
        os.symlink("regular", link)
        os.mkdir(directory)
        journal.begin("typed-evidence", "rendered-config", paths)
        with open(regular, "w", encoding="utf-8") as handle:
            handle.write("new content\n")
        os.chmod(regular, 0o600)
        os.unlink(link)
        os.symlink("new-target", link)
        os.chmod(directory, 0o700)
        journal.complete("typed-evidence")
        entries = load_manifest(journal.manifest_path, require_root=False)
        for path in paths:
            value = annotation(entries, path, "modified", None, _snapshot(path))
            th.assert_eq(value["operation_id"], "typed-evidence",
                         f"content/permissions/link evidence must agree for {path}")


@th.django_unit_test()
def test_snapshot_detects_timestamp_atomic_replace_and_parent_metadata(opts):
    from mojo.deploy.mojosec_changes import ChangeJournal, _snapshot
    from mojo.mojosec.expected_changes import annotation, load_manifest

    with tempfile.TemporaryDirectory() as root:
        timestamped = os.path.join(root, "timestamped")
        replaced = os.path.join(root, "replaced")
        parent = os.path.join(root, "parent")
        for path in (timestamped, replaced):
            with open(path, "wb") as handle:
                handle.write(b"identical bytes\n")
        os.mkdir(parent)
        journal = ChangeJournal(
            journal_path=os.path.join(root, "journal.json"),
            lock_path=os.path.join(root, "journal.lock"),
            manifest_path=os.path.join(root, "expected.json"),
            allowed_roots=[root],
        )
        operation = journal.begin(
            "comparison-metadata", "rendered-config",
            [timestamped, replaced, parent], ttl_seconds=900)

        prior = os.stat(timestamped, follow_symlinks=False)
        os.utime(timestamped, ns=(prior.st_atime_ns, prior.st_mtime_ns + 1_000_000_000))

        replacement = os.path.join(root, "replacement.tmp")
        with open(replacement, "wb") as handle:
            handle.write(b"identical bytes\n")
        replaced_info = os.stat(replaced, follow_symlinks=False)
        os.chmod(replacement, stat.S_IMODE(replaced_info.st_mode))
        os.utime(replacement, ns=(replaced_info.st_atime_ns, replaced_info.st_mtime_ns))
        os.replace(replacement, replaced)

        parent_info = os.stat(parent, follow_symlinks=False)
        os.utime(parent, ns=(
            parent_info.st_atime_ns, parent_info.st_mtime_ns + 1_000_000_000))

        completed = journal.complete("comparison-metadata")
        th.assert_eq({entry["path"] for entry in completed},
                     {timestamped, replaced, parent},
                     "FIM comparison-only changes must all produce manifest entries")
        replacement_before = operation["before"][replaced]
        replacement_after = _snapshot(replaced)
        th.assert_eq(replacement_before["sha256"], replacement_after["sha256"],
                     "the atomic replacement regression must keep byte content identical")
        th.assert_true(replacement_before["inode"] != replacement_after["inode"],
                       "byte-identical atomic replacement must be detected by file identity")
        entries = load_manifest(journal.manifest_path, require_root=False)
        for path in (timestamped, replaced, parent):
            value = annotation(entries, path, "modified", None, _snapshot(path))
            th.assert_eq(value["operation_id"], "comparison-metadata",
                         f"full comparison evidence must annotate {path}")


@th.django_unit_test()
def test_expired_active_operations_are_reaped_before_status_and_begin(opts):
    from mojo.deploy.mojosec_changes import ChangeJournal

    with tempfile.TemporaryDirectory() as root:
        journal = ChangeJournal(
            journal_path=os.path.join(root, "journal.json"),
            lock_path=os.path.join(root, "journal.lock"),
            manifest_path=os.path.join(root, "expected.json"),
            allowed_roots=[root],
        )
        path = os.path.join(root, "host.conf")
        journal.begin("crashed", "rendered-config", [path], ttl_seconds=1)
        with open(journal.journal_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["operations"]["crashed"]["started_at"] = "2000-01-01T00:00:00Z"
        with open(journal.journal_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(journal.journal_path, 0o600)
        status = journal.status()
        th.assert_eq(status["reaped_operations"], 1,
                     "status must atomically reap a crashed expired operation")
        journal.begin("replacement", "rendered-config", [path], ttl_seconds=60)
        th.assert_eq(journal.active_paths(), [path],
                     "reaped capacity must be immediately reusable by a new operation")


@th.django_unit_test()
def test_change_scope_rejects_application_trees_and_outside_paths(opts):
    from mojo.deploy.mojosec_changes import ChangeError, validate_paths

    with th.assert_raises(ChangeError):
        validate_paths(["/opt/api/release.py"])
    with th.assert_raises(ChangeError):
        validate_paths(["/tmp/undeclared"])
    for path in ("/etc/mojosec/config.json",
                 "/etc/mojosec/audit-state.json",
                 "/var/lib/mojosec/events.db",
                 "/run/mojosec/control.sock"):
        with th.assert_raises(ChangeError):
            validate_paths([path])


@th.django_unit_test()
def test_control_state_declarations_are_dropped_not_fatal(opts):
    """A caller-declared control-state path is ignored, never fatal (item 2014).

    The 1.11.9/1.11.10 post-deploy bodies both declare
    /etc/mojosec/audit-state.json, and the body driving an upgrade is the
    previously installed generation's — so the validator, refreshed from the
    newly installed wheel, is what has to tolerate it. validate_paths itself
    stays strict; only the caller-declared `run` intake filters.
    """
    from mojo.deploy.mojosec_changes import (
        ChangeError, ChangeJournal, split_control_state_paths, validate_paths,
    )

    shipped = [
        "/etc/systemd/system/mojosec.service",
        "/etc/nginx/conf.d/00_mojosec.conf",
        "/etc/nginx/snippets/mojosec_receiver.conf",
        "/etc/nginx/django.inc",
        "/etc/logrotate.d/mojosec",
        "/etc/audit/rules.d",
        "/etc/audit/audit.rules",
        "/etc/mojosec/audit-state.json",
        "/etc/systemd/system/mojosec-audit-health.service",
        "/etc/systemd/system/mojosec-audit-health.timer",
        "/etc/sudoers.d/70-mojo-firewall-broker",
        "/usr/local/sbin/mojo-firewall-broker",
        "/usr/local/lib/mojosec/mojosec_audit.py",
    ]
    kept, dropped = split_control_state_paths(shipped)
    th.assert_eq(dropped, ["/etc/mojosec/audit-state.json"],
                 "only MojoSec control state may be dropped from a declaration")
    th.assert_eq(kept, [path for path in shipped if path not in dropped],
                 "every other declared destination must survive, in order")

    for path in ("/etc/mojosec", "/var/lib/mojosec/events.db",
                 "/run/mojosec/control.sock"):
        th.assert_eq(split_control_state_paths([path]), ([], [path]),
                     f"{path} is converge-owned control state and must be dropped")

    th.assert_eq(split_control_state_paths(["/etc/mojosec-lookalike"]),
                 (["/etc/mojosec-lookalike"], []),
                 "the prefix test is a path boundary, not a name prefix")

    kept_app, dropped_app = split_control_state_paths(["/opt/api/release.py"])
    th.assert_eq((kept_app, dropped_app), (["/opt/api/release.py"], []),
                 "application trees are not silently dropped — they stay fatal")
    with th.assert_raises(ChangeError):
        validate_paths(kept_app)

    with tempfile.TemporaryDirectory() as root:
        journal = ChangeJournal(
            journal_path=os.path.join(root, "journal.json"),
            lock_path=os.path.join(root, "journal.lock"),
            manifest_path=os.path.join(root, "expected.json"),
            allowed_roots=[root])
        declared = [os.path.join(root, "mojosec.service"),
                    "/etc/mojosec/audit-state.json"]
        filtered, _ = split_control_state_paths(declared)
        operation = journal.begin("op-2014", "mojosec-converge", filtered,
                                  ttl_seconds=60)
        th.assert_eq(operation["paths"], [os.path.join(root, "mojosec.service")],
                     "a shipped-body declaration must open an operation, "
                     "carrying no control-state path into the journal")


@th.django_unit_test()
def test_change_scope_bounds_unique_paths_after_deduplication(opts):
    from mojo.deploy.mojosec_changes import (
        ChangeError, MAX_PACKAGE_PATHS, MAX_PATHS, validate_paths,
    )

    repeated = ["/usr/local/bin/mojo-tool"] * (MAX_PATHS + 1)
    th.assert_eq(
        validate_paths(repeated), ["/usr/local/bin/mojo-tool"],
        "repeated exact paths must be deduplicated before enforcing the safety bound")

    distinct = [f"/usr/local/bin/mojo-tool-{index}" for index in range(MAX_PATHS + 1)]
    with th.assert_raises(ChangeError):
        validate_paths(distinct)
    th.assert_eq(
        len(validate_paths(distinct, max_paths=MAX_PACKAGE_PATHS)), MAX_PATHS + 1,
        "mechanically derived package paths must not inherit the declared-path ceiling")


@th.django_unit_test()
def test_change_journal_fits_bounded_wheel_evidence(opts):
    from mojo.deploy.mojosec_changes import (
        ChangeError, ChangeJournal, MAX_BYTES, MAX_PACKAGE_PATHS,
    )

    with tempfile.TemporaryDirectory() as root:
        journal = ChangeJournal(
            journal_path=os.path.join(root, "journal.json"),
            lock_path=os.path.join(root, "journal.lock"),
            manifest_path=os.path.join(root, "expected.json"),
            allowed_roots=[root],
        )
        component = "package_destination_" + ("x" * 100)
        paths = [os.path.join(root, f"{component}_{index:04d}")
                 for index in range(5000)]
        with th.assert_raises(ChangeError):
            journal.begin("declared-too-broad", "rendered-config", paths)
        journal.begin(
            "large-wheel", "pip-package-change", paths,
            max_paths=MAX_PACKAGE_PATHS)
        size = os.path.getsize(journal.journal_path)
        th.assert_true(size > 256 * 1024 and size <= MAX_BYTES,
                       "valid wheel evidence must fit above the old 256 KiB cap")


@th.django_unit_test()
def test_expected_change_reader_accepts_large_bounded_manifests(opts):
    from mojo.mojosec.expected_changes import MAX_BYTES, load_manifest

    with tempfile.TemporaryDirectory() as root:
        manifest_path = os.path.join(root, "expected.json")
        entries = [{
            "path": f"/usr/local/lib/python3.12/site-packages/large_package/{index:04d}/"
                    + ("module_name_" * 8) + ".py",
            "change": "modified", "sha256": "a" * 64,
            "expires_at": "2099-01-01T00:00:00Z",
            "deployment_id": "large-package-deploy",
        } for index in range(3000)]
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump({
                "schema": "mojosec.expected_changes", "version": 1,
                "entries": entries,
            }, handle)
        os.chmod(manifest_path, 0o600)
        size = os.path.getsize(manifest_path)
        th.assert_true(size > 256 * 1024 and size < MAX_BYTES,
                       "fixture must exercise the old reader-size failure")
        th.assert_eq(len(load_manifest(manifest_path, require_root=False)), len(entries),
                     "the sensor must read every entry the producer can write")



@th.django_unit_test()
def test_wheel_record_maps_packages_metadata_pyc_and_entry_points(opts):
    from mojo.deploy.mojosec_changes import _wheel_record_paths

    with tempfile.TemporaryDirectory() as root:
        wheel = os.path.join(root, "sample-1-py3-none-any.whl")
        record = "\n".join((
            "sample/__init__.py,,",
            "sample-1.dist-info/METADATA,,",
            "sample-1.dist-info/WHEEL,,",
            "sample-1.dist-info/entry_points.txt,,",
            "sample-1.dist-info/RECORD,,",
        ))
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("sample/__init__.py", "")
            archive.writestr("sample-1.dist-info/METADATA",
                             "Name: sample\nVersion: 1\n")
            archive.writestr("sample-1.dist-info/WHEEL", "Root-Is-Purelib: true\n")
            archive.writestr("sample-1.dist-info/entry_points.txt",
                             "[console_scripts]\nsample-tool = sample:main\n")
            archive.writestr("sample-1.dist-info/RECORD", record)
        scheme = {
            "purelib": "/usr/local/lib/python3.11/site-packages",
            "platlib": "/usr/local/lib64/python3.11/site-packages",
            "scripts": "/usr/local/bin", "data": "/usr/local",
            "include": "/usr/local/include/python3.11",
        }
        paths = set(_wheel_record_paths(wheel, scheme))
        th.assert_in("/usr/local/lib/python3.11/site-packages/sample/__init__.py",
                     paths, "wheel RECORD file must become an exact destination")
        th.assert_in(importlib.util.cache_from_source(
            "/usr/local/lib/python3.11/site-packages/sample/__init__.py"), paths,
            "installer-derived bytecode destination must be predeclared")
        th.assert_in("/usr/local/bin/sample-tool", paths,
                     "generated console scripts must be predeclared")
        th.assert_in(
            "/usr/local/lib/python3.11/site-packages/sample-1.dist-info/INSTALLER",
            paths, "pip metadata outside the wheel RECORD must be predeclared")


@th.django_unit_test()
def test_immediate_parent_directories_are_journaled_and_matchable(opts):
    """Writing a file explains its directory's metadata change too."""
    from mojo.deploy.mojosec_changes import ChangeJournal, _snapshot
    from mojo.mojosec.expected_changes import annotation, load_manifest

    with tempfile.TemporaryDirectory() as root:
        conf_dir = os.path.join(root, "conf.d")
        os.mkdir(conf_dir)
        target = os.path.join(conf_dir, "app.conf")
        journal = ChangeJournal(
            journal_path=os.path.join(root, "journal.json"),
            lock_path=os.path.join(root, "journal.lock"),
            manifest_path=os.path.join(root, "expected.json"),
            allowed_roots=[root],
        )
        operation = journal.begin(
            "parent-coverage", "rendered-host-config", [target],
            ttl_seconds=900)
        th.assert_true(conf_dir in operation["paths"],
                       "begin must auto-journal the declared path's parent")
        th.assert_true(root not in operation["paths"],
                       "an approved root itself must never be auto-journaled")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("rendered host config\n")
        info = os.stat(conf_dir, follow_symlinks=False)
        os.utime(conf_dir, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        completed = journal.complete("parent-coverage")
        th.assert_eq({entry["path"] for entry in completed},
                     {target, conf_dir},
                     "both the file and its perturbed parent directory must annotate")
        entries = load_manifest(
            os.path.join(root, "expected.json"), require_root=False)
        matched = annotation(
            entries, conf_dir, "modified", None, _snapshot(conf_dir))
        th.assert_true(matched is not None and
                       matched["operation_kind"] == "rendered-host-config",
                       "the sensor-side exact match must explain the directory change")


@th.django_unit_test()
def test_retry_after_hot_parent_drift_stays_idempotent(opts):
    """A concurrent write to an auto-journaled parent must not fail a retry."""
    from mojo.deploy.mojosec_changes import ChangeError, ChangeJournal

    with tempfile.TemporaryDirectory() as root:
        conf_dir = os.path.join(root, "conf.d")
        os.mkdir(conf_dir)
        target = os.path.join(conf_dir, "app.conf")
        journal = ChangeJournal(
            journal_path=os.path.join(root, "journal.json"),
            lock_path=os.path.join(root, "journal.lock"),
            manifest_path=os.path.join(root, "expected.json"),
            allowed_roots=[root],
        )
        journal.begin("retry-drift", "rendered-host-config", [target],
                      ttl_seconds=900)
        info = os.stat(conf_dir, follow_symlinks=False)
        os.utime(conf_dir, ns=(
            info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        retried = journal.begin(
            "retry-drift", "rendered-host-config", [target], ttl_seconds=900)
        th.assert_true(conf_dir in retried["paths"],
                       "the idempotent retry must return the active operation")
        with th.assert_raises(ChangeError):
            journal.begin("retry-drift", "rendered-host-config",
                          [os.path.join(conf_dir, "other.conf")],
                          ttl_seconds=900)
        journal.abort("retry-drift")


@th.django_unit_test()
def test_converge_journal_catalog_stays_within_approved_roots(opts):
    from mojo.deploy.mojosec import _journalable_converge_paths
    from mojo.deploy.mojosec_changes import validate_paths

    paths = _journalable_converge_paths()
    th.assert_eq(len(validate_paths(paths)), len(paths),
                 "every converge journal path must sit under an approved root, "
                 "or begin() would refuse and converge would run unjournaled")
    th.assert_true(all(not path.startswith("/etc/mojosec") and
                       not path.startswith("/var/lib/mojosec") and
                       not path.startswith("/run/mojosec") for path in paths),
                   "converge control state is journal-refused by design and "
                   "must never enter the declared catalog")

@th.django_unit_test()
def test_derived_parents_respect_denied_scopes(opts):
    from mojo.deploy.mojosec_changes import _with_immediate_parents

    parents = _with_immediate_parents(
        ["/etc/nginx/conf.d/app.conf"],
        allowed_roots=["/etc", "/etc/mojosec", "/opt/api"])
    th.assert_true("/etc/nginx/conf.d" in parents,
                   "an approved monitored parent must be derived")
    hostile = _with_immediate_parents(
        ["/etc/mojosec/config.json", "/opt/api/current/app.py"],
        allowed_roots=["/etc", "/etc/mojosec", "/opt/api"])
    th.assert_true(
        all(item in ("/etc/mojosec/config.json", "/opt/api/current/app.py")
            for item in hostile),
        f"control-state and release-tree parents must never derive: {hostile}")
