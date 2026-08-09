import copy
import json
import os
import subprocess
import sys
import tempfile

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

        bad = _config(root)
        bad["surprise"] = True
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(bad, handle)
        with th.assert_raises(ConfigError):
            load_config(path)


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

