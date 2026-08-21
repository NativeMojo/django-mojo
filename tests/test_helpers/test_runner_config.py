"""
Tests for testit runner module config loading and parallel infrastructure.
"""
from testit import helpers as th


@th.unit_test("runner options: --all selects every opt-in tier")
def test_all_selects_opt_in_tiers(opts):
    from testit.runner import setup_parser

    parsed = setup_parser(["--all"])

    assert parsed.all is True, "--all should set opts.all for downstream consumers"
    assert parsed.extra_list == ["slow", "extended"], (
        f"--all should select both built-in opt-in tiers, got {parsed.extra_list!r}"
    )


@th.unit_test("runner options: --all preserves explicit extras without expanding duplicates")
def test_all_preserves_explicit_extras(opts):
    from testit.runner import setup_parser

    parsed = setup_parser(["--all", "--extra", "custom,slow,slow"])

    assert parsed.extra_list == ["custom", "slow", "slow", "extended"], (
        "Explicit --extra values and duplicates should be preserved while --all adds "
        f"only missing built-in tiers, got {parsed.extra_list!r}"
    )
    assert parsed.extra == "custom,slow,slow,extended", (
        f"The legacy comma-joined extra value should match extra_list, got {parsed.extra!r}"
    )


@th.unit_test("runner options: retired --full is hidden and selects no opt-in tiers")
def test_full_is_hidden_compatibility_noop(opts):
    import io
    from contextlib import redirect_stdout
    from testit.runner import setup_parser

    output = io.StringIO()
    try:
        with redirect_stdout(output):
            setup_parser(["--help"])
    except SystemExit as error:
        assert error.code == 0, f"--help should exit successfully, got {error.code}"

    parsed = setup_parser(["--full"])

    assert "--all" in output.getvalue(), "Public help should advertise --all"
    assert "--full" not in output.getvalue(), "Retired --full should be hidden from help"
    assert parsed.all is False, "Compatibility --full must not set opts.all"
    assert parsed.extra_list == [], (
        f"Compatibility --full should run the default tier, got extras {parsed.extra_list!r}"
    )


@th.unit_test("runner options: JSON config cannot enable --all")
def test_all_has_no_json_config_key(opts):
    import json
    import os
    import tempfile
    from testit.runner import setup_parser

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
        json.dump({"all": True}, handle)
        config_path = handle.name
    try:
        parsed = setup_parser(["--config", config_path])
    finally:
        os.unlink(config_path)

    assert parsed.all is False, "The public --all selector must remain CLI-only"
    assert parsed.extra_list == [], (
        f"An unsupported JSON all key must not select opt-in tiers, got {parsed.extra_list!r}"
    )


@th.django_unit_test("TESTIT config: loads from __init__.py")
def test_config_loads(opts):
    from testit.runner import _load_module_config
    import os
    from mojo.helpers import paths

    test_root = os.path.join(os.path.dirname(os.path.dirname(__file__)))
    # Use test_job_engine — it's serial for a legitimate reason
    # (JobEngine uses Python signal handlers that only work on the main thread).
    module_path = os.path.join(test_root, "test_job_engine")

    config = _load_module_config(module_path)
    th.assert_true(config.serial is True, "test_job_engine should be serial=True")
    th.assert_true("mojo.apps.jobs" in config.requires_apps,
                    "test_job_engine should require mojo.apps.jobs")


@th.django_unit_test("TESTIT config: edge integration state is isolated")
def test_edge_config_is_serial(opts):
    from testit.runner import _load_module_config
    import os

    test_root = os.path.dirname(os.path.dirname(__file__))
    config = _load_module_config(os.path.join(test_root, "test_edge"))

    th.assert_true(
        config.serial is True,
        "test_edge patches process-wide reporters and settings, so it must be serial")


@th.django_unit_test("TESTIT config: defaults when no TESTIT defined")
def test_config_defaults(opts):
    from testit.runner import _load_module_config
    import tempfile
    import os

    # Create a temp dir with an empty __init__.py
    with tempfile.TemporaryDirectory() as tmpdir:
        init_path = os.path.join(tmpdir, "__init__.py")
        with open(init_path, "w") as fh:
            fh.write("# empty\n")

        config = _load_module_config(tmpdir)
        th.assert_eq(config.serial, False, "Default serial should be False")
        th.assert_eq(config.server_settings, {}, "Default server_settings should be empty")
        th.assert_eq(config.requires_apps, [], "Default requires_apps should be empty")
        th.assert_eq(config.requires_extra, [], "Default requires_extra should be empty")


@th.django_unit_test("TESTIT config: defaults when no __init__.py")
def test_config_no_init(opts):
    from testit.runner import _load_module_config
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        config = _load_module_config(tmpdir)
        th.assert_eq(config.serial, False, "Missing init should default serial=False")
        th.assert_eq(config.requires_apps, [], "Missing init should default requires_apps=[]")


@th.django_unit_test("TESTIT config: partial config merges with defaults")
def test_config_partial(opts):
    from testit.runner import _load_module_config
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        init_path = os.path.join(tmpdir, "__init__.py")
        with open(init_path, "w") as fh:
            fh.write('TESTIT = {"serial": True}\n')

        config = _load_module_config(tmpdir)
        th.assert_eq(config.serial, True, "Partial config should set serial=True")
        th.assert_eq(config.server_settings, {}, "Unset fields should use defaults")
        th.assert_eq(config.requires_apps, [], "Unset fields should use defaults")


@th.django_unit_test("test count: _count_tests_in_file counts test_ functions")
def test_count_tests(opts):
    from testit.runner import _count_tests_in_file
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as fh:
        fh.write("""
def setup_something(opts):
    pass

def test_one(opts):
    pass

def test_two(opts):
    pass

def helper_func():
    pass
""")
        fh.flush()
        count = _count_tests_in_file(fh.name)
        os.unlink(fh.name)

    th.assert_eq(count, 2, "Should count exactly 2 test_ functions")




@th.django_unit_test("thread-local display: per-thread isolation")
def test_display_thread_local(opts):
    import threading
    from testit import helpers

    results = {}

    def thread_fn(thread_id):
        def my_display(event, **kwargs):
            return thread_id
        helpers._set_display_fn(my_display)
        fn = helpers._get_display_fn()
        results[thread_id] = fn("test", name="x")

    t1 = threading.Thread(target=thread_fn, args=(1,))
    t2 = threading.Thread(target=thread_fn, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    th.assert_eq(results[1], 1, "Thread 1 should see its own display fn")
    th.assert_eq(results[2], 2, "Thread 2 should see its own display fn")


@th.django_unit_test("client: last_response captured on request")
def test_client_last_response(opts):
    import testit.client
    client = testit.client.RestClient(opts.host)
    resp = client.get("/api/health")
    th.assert_true(client.last_response is not None, "last_response should be set after request")
    th.assert_true(client.last_response.method == "GET", "last_response method should be GET")
    th.assert_true(client.last_response.status_code is not None, "last_response should have status_code")
    th.assert_true(client.last_response.elapsed_ms >= 0, "last_response should have elapsed_ms")














@th.django_unit_test("dev_server.conf: var/ overrides config/ when both exist")
def test_resolve_conf_var_overrides(opts):
    from mojo.helpers import paths
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as var_dir, tempfile.TemporaryDirectory() as cfg_dir:
        var_root, cfg_root = Path(var_dir), Path(cfg_dir)
        (cfg_root / "dev_server.conf").write_text("host=10.0.0.1\nport=1111\n")
        (var_root / "dev_server.conf").write_text("host=10.0.0.2\nport=2222\n")

        resolved = paths.resolve_conf("dev_server.conf", var_root=var_root, config_root=cfg_root)
        th.assert_eq(str(resolved), str(var_root / "dev_server.conf"),
                     "var/dev_server.conf should win when it exists")


@th.django_unit_test("dev_server.conf: falls back to config/ when var/ absent")
def test_resolve_conf_fallback_to_config(opts):
    from mojo.helpers import paths
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as var_dir, tempfile.TemporaryDirectory() as cfg_dir:
        var_root, cfg_root = Path(var_dir), Path(cfg_dir)
        (cfg_root / "dev_server.conf").write_text("host=10.0.0.1\nport=1111\n")
        # deliberately no var/dev_server.conf

        resolved = paths.resolve_conf("dev_server.conf", var_root=var_root, config_root=cfg_root)
        th.assert_eq(str(resolved), str(cfg_root / "dev_server.conf"),
                     "config/dev_server.conf should be used when var/ is absent")




@th.unit_test("fresh test runs clear framework logs but preserve unrelated files")
def test_reset_test_logs_clears_base_and_backups(opts):
    from pathlib import Path
    import tempfile
    from testit.runner import _reset_test_logs

    with tempfile.TemporaryDirectory() as tmpdir:
        log_root = Path(tmpdir)
        base_log = log_root / "testit.log"
        backup_log = log_root / "testit.log.1"
        unrelated = log_root / "keep.txt"
        base_log.write_text("old run\n", encoding="utf-8")
        backup_log.write_text("older run\n", encoding="utf-8")
        unrelated.write_text("keep me\n", encoding="utf-8")

        failures = _reset_test_logs(log_root)

        assert failures == [], f"Reset should succeed for writable temp logs, got {failures!r}"
        assert base_log.exists(), "Base log should be truncated in place, not removed"
        assert base_log.read_bytes() == b"", (
            f"Base log should be empty after reset, got {base_log.read_bytes()!r}"
        )
        assert not backup_log.exists(), "Numbered backup should be removed for a fresh run"
        assert unrelated.read_text(encoding="utf-8") == "keep me\n", (
            "Files outside the *.log / *.log.N test-log patterns must remain untouched"
        )


@th.unit_test("test log reset failures are isolated and reported")
def test_reset_test_logs_continues_after_one_failure(opts):
    import os
    from pathlib import Path
    import tempfile
    from unittest import mock
    from testit.runner import _reset_test_logs

    with tempfile.TemporaryDirectory() as tmpdir:
        log_root = Path(tmpdir)
        bad_log = log_root / "bad.log"
        good_log = log_root / "good.log"
        bad_log.write_text("cannot clear\n", encoding="utf-8")
        good_log.write_text("clear me\n", encoding="utf-8")
        original_open = os.open

        def flaky_open(path, *args, **kwargs):
            if Path(path).name == "bad.log":
                raise OSError("synthetic reset failure")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(os, "open", flaky_open):
            failures = _reset_test_logs(log_root)

        assert len(failures) == 1, f"Exactly one reset failure should be reported, got {failures!r}"
        assert "bad.log" in failures[0], f"Failure should identify bad.log, got {failures!r}"
        assert good_log.read_bytes() == b"", (
            f"A failure clearing bad.log must not block good.log, got {good_log.read_bytes()!r}"
        )


@th.unit_test("test log reset refuses symlinked base logs")
def test_reset_test_logs_does_not_follow_symlinks(opts):
    from pathlib import Path
    import tempfile
    from testit.runner import _reset_test_logs

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_root = Path(tmpdir)
        log_root = temp_root / "logs"
        log_root.mkdir()
        target = temp_root / "outside.txt"
        target.write_text("must survive\n", encoding="utf-8")
        linked_log = log_root / "linked.log"
        linked_log.symlink_to(target)

        failures = _reset_test_logs(log_root)

        assert target.read_text(encoding="utf-8") == "must survive\n", (
            "A symlinked *.log must never truncate its target"
        )
        assert linked_log.is_symlink(), "Rejected base-log symlink should remain inspectable"
        assert len(failures) == 1 and "linked.log" in failures[0], (
            f"Refused symlink should produce one actionable warning, got {failures!r}"
        )


@th.unit_test("test log reset refuses multiply linked base logs")
def test_reset_test_logs_does_not_truncate_hard_links(opts):
    import os
    from pathlib import Path
    import tempfile
    from testit.runner import _reset_test_logs

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_root = Path(tmpdir)
        log_root = temp_root / "logs"
        log_root.mkdir()
        target = temp_root / "outside.txt"
        target.write_text("must survive\n", encoding="utf-8")
        linked_log = log_root / "linked.log"
        os.link(target, linked_log)

        failures = _reset_test_logs(log_root)

        assert target.read_text(encoding="utf-8") == "must survive\n", (
            "A multiply linked *.log must never truncate the shared inode"
        )
        assert linked_log.read_text(encoding="utf-8") == "must survive\n"
        assert len(failures) == 1 and "linked.log" in failures[0], (
            f"Refused hard link should produce one actionable warning, got {failures!r}"
        )


@th.unit_test("only fresh executing runs reset test logs")
def test_should_reset_test_logs_conditions(opts):
    from objict import objict
    from testit.runner import _should_reset_test_logs

    assert _should_reset_test_logs(objict(resume=False, list_extras=False)) is True, (
        "A fresh test execution should clear prior framework logs"
    )
    assert _should_reset_test_logs(objict(resume=True, list_extras=False)) is False, (
        "--continue is the same logical run and must preserve its existing logs"
    )
    assert _should_reset_test_logs(objict(resume=False, list_extras=True)) is False, (
        "--list-extras executes no tests and must not clear diagnostic logs"
    )


# ---------------------------------------------------------------------------
# Origin-aware module records (maestro item #1839)
# ---------------------------------------------------------------------------

def _roots(tmpdir, consumer=(), repo=(), repo_init=True):
    """Build a consumer root and a parent (repository) root with the named
    packages. Returns (test_root, parent_test_root)."""
    import os
    test_root = os.path.join(tmpdir, "apps_tests")
    parent_root = os.path.join(tmpdir, "repo_tests")
    os.makedirs(test_root, exist_ok=True)
    os.makedirs(parent_root, exist_ok=True)
    for name in consumer:
        pkg = os.path.join(test_root, name)
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "__init__.py"), "w") as fh:
            fh.write("")
        with open(os.path.join(pkg, "test_a.py"), "w") as fh:
            fh.write("def test_one(opts):\n    pass\n")
    for name in repo:
        pkg = os.path.join(parent_root, name)
        os.makedirs(pkg, exist_ok=True)
        if repo_init:
            with open(os.path.join(pkg, "__init__.py"), "w") as fh:
                fh.write("")
        with open(os.path.join(pkg, "test_b.py"), "w") as fh:
            fh.write("def test_one(opts):\n    pass\n")
    return test_root, parent_root


def _collect(test_root, parent_root, **opt_overrides):
    from objict import objict
    from testit.runner import _collect_modules
    opts = objict(test_modules=[], ignore_modules=[], nomojo=False, onlymojo=False)
    opts.update(opt_overrides)
    return _collect_modules(opts, test_root, parent_root)


@th.unit_test("runner records: repository and consumer packages carry origin and path")
def test_records_carry_origin_and_path(opts):
    import tempfile
    from testit.runner import ORIGIN_CONSUMER, ORIGIN_REPO

    with tempfile.TemporaryDirectory() as tmpdir:
        test_root, parent_root = _roots(
            tmpdir, consumer=["test_appthing"], repo=["test_repothing"])
        records = _collect(test_root, parent_root)

    by_name = {r.name: r for r in records}
    assert set(by_name) == {"test_appthing", "test_repothing"}, (
        f"both roots' packages must be collected, got {sorted(by_name)}"
    )
    repo = by_name["test_repothing"]
    consumer = by_name["test_appthing"]
    assert repo.origin == ORIGIN_REPO and parent_root in repo.path, (
        f"the repository package must resolve to the parent root, got {repo}"
    )
    assert consumer.origin == ORIGIN_CONSUMER and test_root in consumer.path, (
        f"the consumer package must resolve to the application root, got {consumer}"
    )
    assert repo.has_init is True, "repo fixture wrote an __init__.py"


@th.unit_test("runner records: a same-named consumer package no longer shadows the repository's")
def test_records_same_name_dedupe(opts):
    import tempfile
    from testit.runner import ORIGIN_REPO

    with tempfile.TemporaryDirectory() as tmpdir:
        test_root, parent_root = _roots(
            tmpdir, consumer=["test_dup"], repo=["test_dup"])
        records = _collect(test_root, parent_root)

    dups = [r for r in records if r.name == "test_dup"]
    assert len(dups) == 1, (
        f"a same-named package must yield exactly one record, never the "
        f"historical duplicate pair, got {len(dups)}"
    )
    assert dups[0].origin == ORIGIN_REPO and parent_root in dups[0].path, (
        f"the repository package must win collection over a same-named "
        f"consumer package, got {dups[0]}"
    )


@th.unit_test("runner records: direct -t pkg.file specs resolve kind, file, and origin")
def test_records_direct_file_spec(opts):
    import tempfile
    from testit.runner import ORIGIN_REPO

    with tempfile.TemporaryDirectory() as tmpdir:
        test_root, parent_root = _roots(tmpdir, repo=["test_only_repo"])
        records = _collect(test_root, parent_root,
                           test_modules=["test_only_repo.test_b"])

    assert len(records) == 1, f"one spec must yield one record, got {records}"
    record = records[0]
    assert record.kind == "file" and record.test_file == "test_b", (
        f"a pkg.file spec must produce a file record naming its file, got {record}"
    )
    assert record.origin == ORIGIN_REPO and parent_root in record.path, (
        f"the file spec must resolve to the repository package, got {record}"
    )


@th.unit_test("runner records: a package without __init__.py is recorded explicitly")
def test_records_missing_init(opts):
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        test_root, parent_root = _roots(
            tmpdir, repo=["test_no_init"], repo_init=False)
        records = _collect(test_root, parent_root)

    record = next(r for r in records if r.name == "test_no_init")
    assert record.has_init is False, (
        "the record must state the missing __init__.py explicitly — the "
        "fail-closed policy state depends on it, not on a permissive default"
    )


@th.unit_test("runner records: --nomojo and --onlymojo select by origin")
def test_records_nomojo_onlymojo(opts):
    import tempfile
    from testit.runner import ORIGIN_CONSUMER, ORIGIN_REPO

    with tempfile.TemporaryDirectory() as tmpdir:
        test_root, parent_root = _roots(
            tmpdir, consumer=["test_appthing"], repo=["test_repothing"])
        nomojo = _collect(test_root, parent_root, nomojo=True)
        onlymojo = _collect(test_root, parent_root, onlymojo=True)

    assert [r.origin for r in nomojo] == [ORIGIN_CONSUMER], (
        f"--nomojo must collect only consumer records, got {nomojo}"
    )
    assert [r.origin for r in onlymojo] == [ORIGIN_REPO], (
        f"--onlymojo must collect only repository records, got {onlymojo}"
    )


@th.unit_test("runner records: targeted -t modules resolve against both roots")
def test_records_targeted_modules(opts):
    import tempfile
    from testit.runner import ORIGIN_CONSUMER, ORIGIN_REPO

    with tempfile.TemporaryDirectory() as tmpdir:
        test_root, parent_root = _roots(
            tmpdir, consumer=["test_appthing"], repo=["test_repothing"])
        records = _collect(test_root, parent_root,
                           test_modules=["test_appthing", "test_repothing"])

    by_name = {r.name: r for r in records}
    assert by_name["test_appthing"].origin == ORIGIN_CONSUMER, (
        f"a consumer-root -t module must record consumer origin, got "
        f"{by_name['test_appthing']}"
    )
    assert by_name["test_repothing"].origin == ORIGIN_REPO, (
        f"a repository-root -t module must record repository origin, got "
        f"{by_name['test_repothing']}"
    )


@th.unit_test("runner records: discovery for a record uses exactly its resolved path")
def test_record_discovery_uses_record_path(opts):
    import tempfile
    from testit.runner import _discover_record_files

    with tempfile.TemporaryDirectory() as tmpdir:
        test_root, parent_root = _roots(
            tmpdir, consumer=["test_dup"], repo=["test_dup"])
        records = _collect(test_root, parent_root)
        record = next(r for r in records if r.name == "test_dup")
        files = _discover_record_files(record)

    names = [name for name, _path in files]
    assert names == ["test_b"], (
        f"discovery must list the repository package's files (test_b), never "
        f"the shadowing consumer package's (test_a), got {names}"
    )
    assert all(record.path in path for _n, path in files), (
        f"every discovered file must live under the record's own path, got {files}"
    )


@th.unit_test("runner config states: ok / missing_init / missing_testit / invalid")
def test_config_states(opts):
    import os
    import tempfile
    from testit.runner import _load_module_config_ex

    with tempfile.TemporaryDirectory() as tmpdir:
        config, state = _load_module_config_ex(tmpdir)
        assert state == "missing_init", (
            f"a directory without __init__.py must report missing_init, got {state}"
        )

        init_path = os.path.join(tmpdir, "__init__.py")
        with open(init_path, "w") as fh:
            fh.write("# empty\n")
        config, state = _load_module_config_ex(tmpdir)
        assert state == "missing_testit", (
            f"an __init__.py without a TESTIT dict must report missing_testit, got {state}"
        )

        with open(init_path, "w") as fh:
            fh.write('TESTIT = {"serial": True, "default_core": True}\n')
        config, state = _load_module_config_ex(tmpdir)
        assert state == "ok" and config.serial is True and config.default_core is True, (
            f"a literal TESTIT dict must report ok and merge, got {state} {config}"
        )

        with open(init_path, "w") as fh:
            fh.write("TESTIT = make_config()\n")
        config, state = _load_module_config_ex(tmpdir)
        assert state == "invalid", (
            f"a computed (non-literal) TESTIT must report invalid — fail-closed, got {state}"
        )
        assert config.default_core is False, (
            "a non-ok state must fall back to the permissive defaults for "
            "backward compatibility (the policy layer is what fails closed)"
        )


@th.unit_test("runner records: a shadowed import is refused, not run")
def test_import_shadow_refused(opts):
    import io
    import tempfile
    from contextlib import redirect_stdout
    from testit.runner import import_module_for_testing

    with tempfile.TemporaryDirectory() as expected_root:
        output = io.StringIO()
        with redirect_stdout(output):
            # `json` imports fine but lives nowhere near expected_root — the
            # exact shape of a same-named package resolving elsewhere.
            module = import_module_for_testing("os", "path", expected_root)

    assert module is None, (
        "an import that resolves outside the record's directory must be "
        "refused rather than executed"
    )
    assert "shadow" in output.getvalue().lower(), (
        f"the refusal must explain the shadowing, got {output.getvalue()!r}"
    )
