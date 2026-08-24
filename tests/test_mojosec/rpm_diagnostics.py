"""Regression coverage for private RPM diagnostics and outward-safe status."""

import io
import json
import os
import sys
import tempfile

from testit import helpers as th


@th.tier("core")
@th.django_unit_test()
def test_rpm_diagnostic_tail_is_sanitized_bounded_and_canonical(opts):
    from mojo.mojosec.collectors.rpm import RpmError
    from mojo.mojosec.output import emit_error

    secret = "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
    private = (
        b"ignored old line\n\n"
        b"credential=" + secret.encode("ascii") + b"\n"
        b"https://user:pass@example.com/rpm?token=private\n"
        b"bad-utf8-\xff-control-\x1b-" + ("\u754c" * 400).encode("utf-8") + b"\n"
    )
    error = RpmError("RPM command failed", private)
    tail = error.diagnostic_tail()
    encoded_tail = tail.encode("utf-8")

    th.assert_true(secret not in tail and "user:pass" not in tail and
                   "token=private" not in tail,
                   f"the local RPM diagnostic retained secret material: {tail!r}")
    th.assert_true("ignored old line" not in tail,
                   f"the diagnostic did not select only the last three lines: {tail!r}")
    th.assert_true(len(encoded_tail) <= 512,
                   f"the diagnostic tail exceeded 512 UTF-8 bytes: {len(encoded_tail)}")
    for line in tail.splitlines():
        th.assert_true(len(line.encode("utf-8")) <= 200,
                       f"one diagnostic line exceeded 200 UTF-8 bytes: {line!r}")

    aggregate = RpmError(
        "RPM command failed",
        (b"a" * 300) + b"\n" + (b"b" * 300) + b"\n" + (b"c" * 300),
    ).diagnostic_tail()
    th.assert_eq(len(aggregate.splitlines()), 3,
                 "the aggregate bound must retain the last three non-empty lines")
    th.assert_true(len(aggregate.encode("utf-8")) <= 512,
                   f"three individually bounded lines exceeded the tail cap: {aggregate!r}")
    th.assert_true(aggregate.splitlines()[0].endswith("[truncated]"),
                   f"aggregate truncation omitted its in-bound marker: {aggregate!r}")

    actual_tail = RpmError(
        "RPM command failed",
        (b"stale-prefix-" + (b"x" * 5000) +
         b"\nactionable one\nactionable two\nactionable three\n"),
    ).diagnostic_tail()
    th.assert_eq(actual_tail,
                 "actionable one\nactionable two\nactionable three",
                 "bounded private stderr must retain the actual last complete lines")

    embedded_url = RpmError(
        "RPM command failed",
        b"download failed: https://user:pass@example.com/rpm?X-Amz-Signature=private",
    ).diagnostic_tail()
    th.assert_eq(embedded_url, "[redacted]",
                 "a prose-wrapped signed URL must not bypass line sanitization")

    stream = io.StringIO()
    emit_error("RPM readiness failed", error, stream=stream)
    raw_record = stream.getvalue()
    record = json.loads(raw_record)
    th.assert_eq(record["error"], "RPM command failed",
                 "the structured local record must retain the fixed classification")
    th.assert_eq(record["diagnostic_tail"], tail,
                 "the structured local record must contain only the sanitized tail")
    th.assert_true("\x1b" not in raw_record and "\ufffd" not in raw_record,
                   f"canonical JSON must escape controls and replacement text: {raw_record!r}")


@th.tier("core")
@th.django_unit_test()
def test_rpm_diagnostics_never_enter_runtime_or_serialized_status(opts):
    from mojo.mojosec.collectors.rpm import RpmError
    from mojo.mojosec.output import write_status
    from mojo.mojosec.runtime import Runtime

    secret = "authorization=Bearer PRIVATE-RPM-TOKEN-123456789"
    error = RpmError("RPM verification wrote unclassifiable stderr", secret.encode())
    local = io.StringIO()

    class Store:
        def __init__(self, identity):
            self.identity = identity
            self.recorded = []

        def active_fim_profile(self):
            return self.identity

        def load_fim_baseline(self, key):
            return {}

        def record_fim_scan(self, key, snapshot, observations, complete):
            self.recorded.append((key, snapshot, observations, complete))

    class FastCollector:
        baseline_key = "profile:fast"
        config = {"interval_seconds": 0}

        def scan(self, previous):
            return {"snapshot": {}, "complete": True}

        def diff(self, baseline, scan):
            return []

    class RpmCollector:
        baseline_key = "profile:rpm"
        config = {"interval_seconds": 0}

        def scan(self, previous, shared_snapshot=None):
            raise error

    identity = {"name": "test", "version": 1, "digest": "a" * 64}
    runtime = Runtime.__new__(Runtime)
    runtime.profile_identity = identity
    runtime.store = Store(identity)
    runtime.integrity_collectors = {"fast": FastCollector(), "rpm": RpmCollector()}
    runtime.last_integrity = {}
    runtime.integrity_scans = {}
    runtime.collector_status = {}
    runtime.diagnostic_stream = local
    runtime._poll_integrity()

    outward = {
        "collector_status": runtime.collector_status,
        "integrity_scans": runtime.integrity_scans,
    }
    serialized = json.dumps(outward, sort_keys=True)
    th.assert_true(secret not in serialized and "PRIVATE-RPM-TOKEN" not in serialized,
                   f"runtime status retained private RPM stderr: {serialized}")
    th.assert_true("diagnostic_tail" not in serialized,
                   f"runtime status serialized a local-only diagnostic: {serialized}")
    th.assert_true(secret not in json.dumps(runtime.store.recorded),
                   f"RPM stderr reached the event/spool recording seam: {runtime.store.recorded!r}")
    local_record = local.getvalue()
    th.assert_true("diagnostic_tail" in local_record and secret not in local_record,
                   f"runtime did not emit one sanitized local diagnostic: {local_record!r}")

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "status.json")
        write_status(path, outward)
        with open(path, encoding="utf-8") as handle:
            status_text = handle.read()
    th.assert_true(secret not in status_text and "diagnostic_tail" not in status_text,
                   f"world-readable status retained private RPM stderr: {status_text}")


@th.tier("core")
@th.django_unit_test()
def test_cli_readiness_keeps_plain_error_fixed_and_detail_local(opts):
    from mojo.mojosec.__main__ import main
    from mojo.mojosec.collectors.rpm import RpmError

    secret = "password=CLI-RPM-SECRET-123456789"
    config = {
        "version": 1,
        "sensor_id": "rpm-diagnostic-test",
        "collectors": {"rpm": {"enabled": True}},
    }
    stdout = io.StringIO()
    stderr = io.StringIO()

    def fail_probe(rpm_config):
        raise RpmError("RPM command failed", secret.encode())

    result = main(
        ["--config", "/unused/test-config.json", "check"],
        stdout=stdout, stderr=stderr,
        config_loader=lambda path: config,
        rpm_probe=fail_probe,
    )
    lines = stderr.getvalue().splitlines()
    th.assert_eq(result, 2,
                 "an RPM readiness failure must preserve the established exit code")
    th.assert_eq(stdout.getvalue(), "",
                 "an RPM readiness failure must not emit successful stdout JSON")
    th.assert_eq(lines[0], "mojosec: RPM command failed",
                 "the plain CLI error must contain only the fixed classification")
    th.assert_true(secret not in stderr.getvalue(),
                   f"the CLI stderr stream retained secret-bearing RPM output: {lines!r}")
    diagnostic = json.loads(lines[1])
    th.assert_eq(diagnostic["diagnostic_tail"], "[redacted]",
                 "the CLI local record must contain only the sanitized diagnostic")


@th.tier("core")
@th.django_unit_test()
def test_empty_and_output_bound_rpm_failures_have_no_diagnostic_tail(opts):
    from mojo.mojosec.collectors.rpm import RpmCollector, RpmError

    empty = RpmError("RPM command failed", b" \n\t")
    bounded = RpmError("RPM command output exceeded its bound", b"private material")
    th.assert_eq(empty.diagnostic_tail(), "",
                 "whitespace-only stderr must not create a local diagnostic")
    th.assert_eq(bounded.diagnostic_tail(), "",
                 "an output-bound failure must discard every captured diagnostic byte")

    collector = RpmCollector(
        {"interpreter": sys.executable, "max_output_bytes": 32,
         "timeout_seconds": 2},
        {"name": "probe", "digest": "0" * 64},
    )
    try:
        collector._run([
            sys.executable, "-c",
            "import sys;sys.stderr.buffer.write(b'secret=' + b'x' * 100)",
        ])
    except RpmError as err:
        th.assert_eq(str(err), "RPM command output exceeded its bound",
                     "oversized stderr must retain the fixed output-bound classification")
        th.assert_eq(err.private_stderr, b"",
                     "oversized stderr must not survive in the private diagnostic channel")
        th.assert_eq(err.diagnostic_tail(), "",
                     "oversized stderr must never produce a local diagnostic tail")
    else:
        th.assert_true(False, "oversized RPM command stderr must fail closed")


@th.tier("core")
@th.django_unit_test()
def test_every_rpm_command_consumer_preserves_only_private_sanitized_stderr(opts):
    from mojo.mojosec.collectors.rpm import RpmCollector, RpmError

    secret = "credential=RPM-CONSUMER-SECRET-123456789"
    config = {
        "interpreter": "/usr/bin/python3", "max_output_bytes": 4096,
        "timeout_seconds": 2, "max_packages": 4,
    }

    def assert_private_failure(call, classification):
        try:
            call()
        except RpmError as err:
            th.assert_eq(str(err), classification,
                         "an RPM consumer must expose only its fixed classification")
            th.assert_eq(err.diagnostic_tail(), "[redacted]",
                         "an RPM consumer must retain only sanitized local stderr")
        else:
            th.assert_true(False, f"the RPM consumer must reject stderr: {classification}")

    inventory = RpmCollector(
        config, {"name": "probe", "digest": "0" * 64},
        runner=lambda argv, accepted=(0,): (0, "", secret),
    )
    assert_private_failure(
        inventory._inventory,
        "RPM inventory command wrote unexpected stderr",
    )

    roots = RpmCollector(
        config, {"name": "probe", "digest": "0" * 64},
        runner=lambda argv, accepted=(0,): (0, "[]", secret),
    )
    assert_private_failure(
        roots.discover_site_roots,
        "system Python wrote unexpected site-root stderr",
    )

    verification = RpmCollector(
        config, {"name": "probe", "digest": "0" * 64},
        runner=lambda argv, accepted=(0,): (1, "", secret),
    )
    assert_private_failure(
        lambda: verification._verify_packages({"example-1-1.x86_64"}, ["/usr"]),
        "RPM verification wrote unclassifiable stderr",
    )
