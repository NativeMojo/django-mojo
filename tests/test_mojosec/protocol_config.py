import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from unittest import mock

from testit import helpers as th


def _golden_path():
    return os.path.join(os.path.dirname(__file__), "golden", "batch_v1.json")


def _config(root):
    return {
        "version": 1,
        "sensor_id": "web-prod-i-0123456789",
        "endpoint": "https://incident.example/api/incident/mojosec/batch",
        "state_dir": os.path.join(root, "state"),
        "status_path": os.path.join(root, "run", "status.json"),
        "credential_path": os.path.join(root, "credential"),
        "collectors": {
            "fim": {
                "targets": [{"path": os.path.join(root, "watched"), "recursive": True}],
            },
        },
    }


@th.django_unit_test()
def test_protocol_accepts_checked_in_v1_golden_batch(opts):
    from mojo.mojosec.protocol import validate_batch

    with open(_golden_path(), encoding="utf-8") as handle:
        batch = json.load(handle)

    validated = validate_batch(batch)
    th.assert_eq(validated["schema"], "mojosec.batch",
                 "the checked-in golden fixture must remain a valid v1 MojoSec batch")
    th.assert_eq(len(validated["events"]), 2,
                 "the golden fixture should cover immediate and aggregated events")


@th.django_unit_test()
def test_protocol_rejects_unknown_and_unbounded_event_data(opts):
    from mojo.mojosec.protocol import ProtocolError, validate_batch

    with open(_golden_path(), encoding="utf-8") as handle:
        batch = json.load(handle)

    unknown = copy.deepcopy(batch)
    unknown["events"][0]["raw_log"] = "secret"
    with th.assert_raises(ProtocolError):
        validate_batch(unknown)

    oversized = copy.deepcopy(batch)
    oversized["events"][0]["attributes"]["sample"] = "x" * 9000
    with th.assert_raises(ProtocolError):
        validate_batch(oversized)


@th.django_unit_test()
def test_local_only_diagnostic_sidecar_is_exact_bounded_and_not_protocol_config(opts):
    import datetime
    import types

    import mojo.mojosec.disposition as disposition
    from mojo.mojosec.config import DEFAULTS

    now = datetime.datetime.now(datetime.timezone.utc)
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "local_only_diagnostic.json")

        def write(value):
            with open(path, "w", encoding="utf-8") as handle:
                if isinstance(value, str):
                    handle.write(value)
                else:
                    json.dump(value, handle)
            os.chmod(path, 0o600)

        real_fstat = disposition.os.fstat

        def root_fstat(descriptor):
            info = real_fstat(descriptor)
            return types.SimpleNamespace(
                st_mode=info.st_mode, st_uid=0, st_gid=0,
                st_size=info.st_size, st_mtime=info.st_mtime,
            )

        def other_fstat(descriptor):
            info = root_fstat(descriptor)
            info.st_uid = 501
            return info

        def other_group_fstat(descriptor):
            info = root_fstat(descriptor)
            info.st_gid = 501
            return info

        active_until = now + datetime.timedelta(minutes=10)
        write({
            "schema": "mojosec.local_only_diagnostic", "version": 1,
            "until": active_until.isoformat(),
        })
        with mock.patch.object(disposition.os, "fstat", side_effect=root_fstat):
            active = disposition.diagnostic_override(path, now=now)
        th.assert_eq(active["active"], True,
                     "one exact root-owned sidecar may temporarily deliver diagnostics")
        th.assert_eq(active["error"], "",
                     "a safe active sidecar must expose no status error")

        write({
            "schema": "mojosec.local_only_diagnostic", "version": 1,
            "until": (now - datetime.timedelta(seconds=1)).isoformat(),
        })
        with mock.patch.object(disposition.os, "fstat", side_effect=root_fstat):
            expired = disposition.diagnostic_override(path, now=now)
        th.assert_eq(expired["active"], False,
                     "an expired safe sidecar must leave local-only suppression active")
        th.assert_eq(expired["error"], "",
                     "expiry is normal inactive state, not a malformed-sidecar error")

        os.chmod(path, 0o640)
        with mock.patch.object(disposition.os, "fstat", side_effect=root_fstat):
            unsafe = disposition.diagnostic_override(path, now=now)
        th.assert_eq(unsafe["error"], "sidecar_unsafe",
                     "group-readable diagnostic policy must fail closed")
        os.chmod(path, 0o600)
        with mock.patch.object(disposition.os, "fstat", side_effect=other_fstat):
            wrong_owner = disposition.diagnostic_override(path, now=now)
        th.assert_eq(wrong_owner["error"], "sidecar_unsafe",
                     "a non-root-owned diagnostic sidecar must fail closed")
        with mock.patch.object(disposition.os, "fstat", side_effect=other_group_fstat):
            wrong_group = disposition.diagnostic_override(path, now=now)
        th.assert_eq(wrong_group["error"], "sidecar_unsafe",
                     "a non-root-group diagnostic sidecar must fail closed")
        os.chmod(path, 0o4600)
        with mock.patch.object(disposition.os, "fstat", side_effect=root_fstat):
            special_mode = disposition.diagnostic_override(path, now=now)
        th.assert_eq(special_mode["error"], "sidecar_unsafe",
                     "special mode bits must not be accepted as exact 0600")
        os.chmod(path, 0o600)
        link = os.path.join(root, "diagnostic-link.json")
        os.symlink(path, link)
        linked = disposition.diagnostic_override(link, now=now)
        th.assert_eq(linked["error"], "sidecar_unreadable",
                     "O_NOFOLLOW must reject a diagnostic sidecar symlink")
        fifo = os.path.join(root, "diagnostic.fifo")
        os.mkfifo(fifo, 0o600)
        started = time.monotonic()
        nonregular = disposition.diagnostic_override(fifo, now=now)
        th.assert_true(time.monotonic() - started < 1,
                       "O_NONBLOCK must reject a FIFO without waiting for a writer")
        th.assert_eq(nonregular["error"], "sidecar_unsafe",
                     "a nonregular diagnostic descriptor must fail closed")

        with open(path, "wb") as handle:
            handle.write(b" " * 513)
        os.chmod(path, 0o600)
        with mock.patch.object(disposition.os, "fstat", side_effect=root_fstat):
            oversized = disposition.diagnostic_override(path, now=now)
        th.assert_eq(oversized["error"], "sidecar_too_large",
                     "the sidecar byte ceiling must be enforced from descriptor metadata")

        write({
            "schema": "mojosec.local_only_diagnostic", "version": 1,
            "until": active_until.isoformat(),
        })
        future_mtime = now.timestamp() + disposition.MAX_MTIME_FUTURE_SECONDS + 30
        os.utime(path, (future_mtime, future_mtime))
        with mock.patch.object(disposition.os, "fstat", side_effect=root_fstat):
            future_metadata = disposition.diagnostic_override(path, now=now)
        th.assert_eq(future_metadata["error"], "sidecar_unsafe",
                     "a sidecar mtime beyond clock-skew tolerance must fail closed")

        write('{"schema":"mojosec.local_only_diagnostic","version":1,'
              '"version":1,"until":"2026-08-11T12:00:00Z"}')
        with mock.patch.object(disposition.os, "fstat", side_effect=root_fstat):
            duplicate = disposition.diagnostic_override(path, now=now)
        th.assert_eq(duplicate["error"], "sidecar_malformed",
                     "duplicate JSON fields must fail closed with a fixed bounded status")

        write({
            "schema": "mojosec.local_only_diagnostic", "version": 1,
            "until": (now + datetime.timedelta(hours=2)).isoformat(),
        })
        with mock.patch.object(disposition.os, "fstat", side_effect=root_fstat):
            future = disposition.diagnostic_override(path, now=now)
        th.assert_eq(future["error"], "sidecar_until_too_late",
                     "an override may never outlive one hour from its descriptor mtime")

        write({
            "schema": "mojosec.local_only_diagnostic", "version": 2,
            "until": active_until.isoformat(),
        })
        with mock.patch.object(disposition.os, "fstat", side_effect=root_fstat):
            newer = disposition.diagnostic_override(path, now=now)
        th.assert_eq(newer, {"active": False, "until": "", "error": ""},
                     "an unsupported sidecar version must be ignored, not interpreted")
        th.assert_true("local_only_diagnostic" not in DEFAULTS,
                       "the emergency sidecar must not become effective configuration")

        with open(_golden_path(), encoding="utf-8") as handle:
            batch = json.load(handle)
        th.assert_true("local_only_diagnostic" not in batch,
                       "the emergency sidecar must not extend the wire protocol")
        poisoned = copy.deepcopy(batch)
        poisoned["local_only_diagnostic"] = {"until": active_until.isoformat()}
        from mojo.mojosec.protocol import ProtocolError, validate_batch
        with th.assert_raises(ProtocolError):
            validate_batch(poisoned)


@th.django_unit_test()
def test_receiver_projects_only_safe_expected_change_annotation(opts):
    from mojo.apps.incident.services.mojosec import _expected_change_projection

    attributes = {
        "path": "/etc/nginx/nginx.conf", "change": "modified",
        "expected_change": {
            "deployment_id": "deploy-20260808.1",
            "expires_at": "2026-08-08T12:30:00Z",
        },
        "untrusted": {"raw": "must-not-project"},
    }
    projected = _expected_change_projection("fim.change", attributes)
    th.assert_eq(projected, attributes["expected_change"],
                 "central projection may retain only the digest/expiry-bound annotation")
    poisoned = dict(attributes)
    poisoned["expected_change"] = {**attributes["expected_change"], "raw": "secret"}
    th.assert_eq(_expected_change_projection("fim.change", poisoned), None,
                 "extra sensor-controlled annotation fields must be dropped centrally")
    v2 = {
        "deployment_id": "deploy-20260808.1",
        "expires_at": "2026-08-08T12:30:00Z",
        "operation_id": "deploy-20260808.1-nginx",
        "operation_kind": "rendered-config",
        "completed_at": "2026-08-08T12:00:00Z",
    }
    th.assert_eq(
        _expected_change_projection("fim.change", {"expected_change": v2}), v2,
        "expected-change v2 operation identity must be additive on wire version 1")
    invalid_v2 = dict(v2, completed_at="2026-08-08T13:00:00Z")
    th.assert_eq(
        _expected_change_projection("fim.change", {"expected_change": invalid_v2}), None,
        "an operation completed after its expiry must never be projected as trusted")


@th.django_unit_test()
def test_config_is_strict_and_applies_safe_defaults(opts):
    from mojo.mojosec.config import ConfigError, load_config

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "mojosec.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(_config(root), handle)
        os.chmod(path, 0o600)

        loaded = load_config(path)
        th.assert_eq(loaded["delivery"]["batch_events"], 100,
                     "strict config loading should fill the bounded batch default")
        th.assert_eq(loaded["collectors"]["fim"]["targets"][0]["recursive"], True,
                     "explicit targeted FIM settings must survive default merging")
        th.assert_eq(loaded["expected_changes_path"],
                     "/etc/mojosec/expected_changes.json",
                     "deployment annotations must have one root-owned default path")

        bad = _config(root)
        bad["surprise"] = True
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(bad, handle)
        with th.assert_raises(ConfigError):
            load_config(path)

        trailing = _config(root)
        trailing["endpoint"] += "/"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(trailing, handle)
        with th.assert_raises(ConfigError):
            load_config(path)


@th.django_unit_test()
def test_config_security_is_checked_on_the_descriptor_that_is_parsed(opts):
    import mojo.mojosec.config as config_module
    from mojo.mojosec.config import ConfigError, load_config

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "mojosec.json")
        replacement = os.path.join(root, "replacement.json")
        original = _config(root)
        swapped = _config(root)
        swapped["sensor_id"] = "attacker-replacement"
        for target, value in ((path, original), (replacement, swapped)):
            with open(target, "w", encoding="utf-8") as handle:
                json.dump(value, handle)
            os.chmod(target, 0o600)

        real_open = config_module.os.open
        opened = []

        def open_then_replace(open_path, flags):
            descriptor = real_open(open_path, flags)
            opened.append(descriptor)
            if open_path == path:
                os.replace(replacement, path)
            return descriptor

        with mock.patch.object(config_module.os, "open", side_effect=open_then_replace):
            loaded = load_config(path, require_root=False)

        th.assert_eq(len(opened), 1,
                     "config loading must open the pathname exactly once")
        th.assert_eq(loaded["sensor_id"], original["sensor_id"],
                     "security checks and parsing must use the already-opened inode, not a replacement path")

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(original, handle)
        os.chmod(path, 0o640)
        with th.assert_raises(ConfigError):
            load_config(path, require_root=False)


@th.django_unit_test()
def test_effective_profile_requires_exact_packaged_fim_and_rpm_graph(opts):
    from mojo.mojosec.config import (
        ConfigError, build_config, validate_effective_config,
    )

    supplied = {
        "version": 1,
        "profile": "al2023-web-v2",
        "sensor_id": "prod-web-i-0123456789abcdef0",
        "endpoint": "https://incident.example/api/incident/mojosec/batch",
    }
    effective = build_config(supplied)
    th.assert_eq(validate_effective_config(copy.deepcopy(effective)), effective,
                 "the exact packaged profile graph must be a valid effective artifact")
    v1_effective = build_config(dict(supplied, profile="al2023-web-v1"))
    th.assert_eq(v1_effective["profile"], "al2023-web-v1",
                 "published v1 policy selection must remain valid for rollback compatibility")

    mutations = (
        (("collectors", "fim", "targets"), [{"path": "/etc", "recursive": True}]),
        (("collectors", "fim", "tiers", "fast", "interval_seconds"), 61),
        (("collectors", "rpm", "interpreter"), "/usr/local/bin/python3"),
        (("collectors", "rpm", "max_packages"), 9999),
        (("collectors", "rpm", "max_file_bytes"), 1024),
    )
    for keys, replacement in mutations:
        changed = copy.deepcopy(effective)
        parent = changed
        for key in keys[:-1]:
            parent = parent[key]
        parent[keys[-1]] = replacement
        with th.assert_raises(ConfigError):
            validate_effective_config(changed)

    for collectors in (
            {"fim": {"targets": [{"path": "/etc", "recursive": True}]}},
            {"fim": {"tiers": {}}},
            {"rpm": {"max_packages": 9999}}):
        desired = dict(supplied, collectors=collectors)
        with th.assert_raises(ConfigError):
            build_config(desired)


@th.django_unit_test()
def test_effective_config_loader_is_root_required_and_descriptor_safe(opts):
    import mojo.mojosec.config as config_module
    from mojo.mojosec.config import ConfigError, build_config, load_effective_config

    effective = build_config({
        "version": 1,
        "profile": "al2023-web-v2",
        "sensor_id": "prod-web-i-0123456789abcdef0",
        "endpoint": "https://incident.example/api/incident/mojosec/batch",
    })
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(effective, handle)
        os.chmod(path, 0o600)

        real_open = config_module.os.open
        real_fstat = config_module.os.fstat
        opened = []

        def tracked_open(open_path, flags):
            opened.append((open_path, flags))
            return real_open(open_path, flags)

        def owned_fstat(uid):
            def inspect(descriptor):
                values = list(real_fstat(descriptor))
                values[4] = uid
                return os.stat_result(values)
            return inspect

        with mock.patch.object(config_module.os, "open", side_effect=tracked_open), \
                mock.patch.object(config_module.os, "fstat", side_effect=owned_fstat(0)):
            loaded = load_effective_config(path)
        th.assert_eq(loaded, effective,
                     "the root-owned effective artifact must load without policy expansion")
        th.assert_eq(len(opened), 1,
                     "effective loading must check and parse one already-opened inode")
        th.assert_true(opened[0][1] & getattr(os, "O_NOFOLLOW", 0),
                       "effective loading must refuse a symlink at descriptor open")

        with th.assert_raises(TypeError):
            load_effective_config(path, require_root=False)
        with mock.patch.object(config_module.os, "fstat", side_effect=owned_fstat(501)):
            with th.assert_raises(ConfigError):
                load_effective_config(path)

        os.chmod(path, 0o640)
        with mock.patch.object(config_module.os, "fstat", side_effect=owned_fstat(0)):
            with th.assert_raises(ConfigError):
                load_effective_config(path)
        os.chmod(path, 0o600)

        link = os.path.join(root, "config-link.json")
        os.symlink(path, link)
        with th.assert_raises(ConfigError):
            load_effective_config(link)

        invalid_payloads = (
            b'{"version":1,"version":1}',
            b'{"value":NaN}',
            b"{" + (b" " * config_module.MAX_CONFIG_BYTES) + b"}",
        )
        for payload in invalid_payloads:
            with open(path, "wb") as handle:
                handle.write(payload)
            os.chmod(path, 0o600)
            with mock.patch.object(config_module.os, "fstat", side_effect=owned_fstat(0)):
                with th.assert_raises(ConfigError):
                    load_effective_config(path)


@th.django_unit_test()
def test_cli_uses_effective_loader_only_for_exact_canonical_path(opts):
    import mojo.mojosec.__main__ as cli

    config = {
        "sensor_id": "prod-web-i-0123456789abcdef0",
        "version": 1,
    }
    output = io.StringIO()
    with mock.patch.object(cli, "load_effective_config", return_value=config) as effective, \
            mock.patch.object(cli, "load_config", return_value=config) as desired, \
            mock.patch.object(cli.sys, "stdout", output):
        th.assert_eq(cli.main(["check"]), 0,
                     "the omitted service path must use the canonical effective artifact")
        th.assert_eq(cli.main(["--config", cli.CANONICAL_CONFIG_PATH, "check"]), 0,
                     "the explicit service path must use the canonical effective artifact")
        alias = "/etc/mojosec/./config.json"
        th.assert_eq(cli.main(["--config", alias, "check"]), 0,
                     "an alternate spelling remains valid only as caller policy")

    th.assert_eq(effective.call_count, 2,
                 "only omitted and exact canonical paths may use effective loading")
    desired.assert_called_once_with(alias)


@th.django_unit_test()
def test_cli_help_imports_without_django_settings(opts):
    import mojo

    root = os.path.dirname(os.path.dirname(os.path.abspath(mojo.__file__)))
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    env["PYTHONPATH"] = root
    done = subprocess.run(
        [sys.executable, "-m", "mojo.mojosec", "--help"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    th.assert_eq(done.returncode, 0,
                 f"MojoSec CLI must work before Django settings exist: {done.stderr}")
    th.assert_in("run", done.stdout,
                 f"MojoSec CLI help must expose its service command: {done.stdout}")
