"""Startup identity remains observable without Store or journal health."""

import json
import os
import tempfile
import threading

from testit import helpers as th


@th.django_unit_test()
def test_starting_identity_precedes_blocked_store_construction(opts):
    from mojo.mojosec.__main__ import main

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "status.json")
        entered, release = threading.Event(), threading.Event()
        result = []

        def blocked_runtime(config, identity=None):
            entered.set()
            release.wait(3)
            raise OSError("test Store construction blocked")

        thread = threading.Thread(target=lambda: result.append(main(
            ["run"], config_loader=lambda _: {"status_path": path, "sensor_id": "test"},
            runtime_factory=blocked_runtime)))
        thread.start()
        try:
            assert entered.wait(2), "runtime constructor must be reached"
            with open(path) as handle:
                status = json.load(handle)
            assert status["state"] == "starting", "starting identity must exist before Store can block"
            assert status["framework_version"] and status["pid"] == os.getpid(), "minimal status must identify the loaded process"
            assert status["schema"] == "mojosec.status" and status["version"] == 1, "identity must preserve schema v1"
            assert "collectors" not in status, "startup publication must not gather collector/journal health"
        finally:
            release.set()
            thread.join(4)
        assert result == [2], "Store failure must remain the runtime's own failure after identity publication"


@th.django_unit_test()
def test_running_identity_published_after_handlers_before_first_health_poll(opts):
    from mojo.mojosec.runtime import Runtime

    with tempfile.TemporaryDirectory() as root:
        runtime = Runtime.__new__(Runtime)
        runtime.config = {"status_path": os.path.join(root, "status"), "sensor_id": "test", "poll_seconds": 1}
        runtime.identity = {"framework_version": "captured", "pid": 42,
                            "boot_id": "boot", "process_start_ticks": 100,
                            "process_started_at": "2026-01-01T00:00:00+00:00"}
        runtime.running = True
        runtime.stop_event = threading.Event()
        handlers = []

        class Store:
            def close(self):
                pass

        runtime.store = Store()

        def first_poll():
            with open(runtime.config["status_path"]) as handle:
                status = json.load(handle)
            assert len(handlers) == 2, "running identity must follow installed signal handlers"
            assert status["running"] is True, "first health poll must already have a running identity"
            assert status["framework_version"] == "captured", "publication must reuse immutable loaded identity"
            runtime.stop()

        runtime.run_once = first_poll
        runtime._publish_status = lambda: None
        runtime.run(signal_installer=lambda *args: handlers.append(args))


@th.django_unit_test()
def test_identity_publication_does_not_recompute_version_or_generation(opts):
    from mojo.mojosec.output import publish_identity

    with tempfile.TemporaryDirectory() as root:
        config = {"status_path": os.path.join(root, "status"), "sensor_id": "test"}
        identity = {"framework_version": "old-loaded", "pid": 42, "boot_id": "boot",
                    "process_start_ticks": 100, "process_started_at": "2026-01-01T00:00:00+00:00"}
        starting = publish_identity(config, identity)
        running = publish_identity(config, identity, running=True)
        assert all(starting[k] == running[k] == v for k, v in identity.items()), "all process identity fields must remain unchanged across publications"
        assert starting["running"] is False and running["running"] is True, "only lifecycle changes at readiness"
