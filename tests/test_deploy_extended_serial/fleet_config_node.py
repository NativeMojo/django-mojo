"""Fleet installation evidence and the fixed, bounded node operation."""

import hashlib
import io
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


@th.django_unit_test()
def test_fleet_sync_retries_failed_restart_once(opts):
    from mojo.deploy import config_override, config_sync, fleet_config_node

    revision = "a" * 32
    allowed = list(config_override.VALIDATORS)
    body = config_override.encode_document(
        {"GEOIP_PRIMARY_PROVIDER": "mojo"}, revision, "now", allowed)
    base = b"DEBUG = False\n"
    s3 = mock.Mock()
    s3.head_object.side_effect = lambda **kw: {
        "ETag": '"test"', "ContentLength": len(body),
        "Metadata": {"sha256": hashlib.sha256(
            body if kw["Key"].endswith("json") else base).hexdigest()}}
    s3.download_file.side_effect = lambda bucket, key, path: Path(path).write_bytes(base)
    s3.get_object.side_effect = lambda **kw: {"Body": io.BytesIO(body)}
    config = {"AWS_CONFIG_BUCKET": "bucket", "AWS_CONFIG_PREFIX": "prefix",
              "CONFIG_SYNC_OVERRIDE_ALLOWED_KEYS": allowed, "CONFIG_SYNC_RESTART": "true"}
    restart = mock.Mock(side_effect=[False, True])
    with tempfile.TemporaryDirectory() as root:
        target = str(Path(root) / "django.conf")
        with mock.patch.object(config_sync.request_service, "read", return_value=True):
            th.assert_eq(config_sync.sync(s3, config, target, "django.conf", False,
                                          restart=restart), 1, "Verify config_sync.sync(s3, config, target, 'django.conf', False, restart=restart)")
            receipt = fleet_config_node.read_receipt(target)
            th.assert_eq(receipt["error_code"], "restart_failed", "Verify receipt['error_code']")
            installed_at = receipt["installed_at"]
            th.assert_eq(config_sync.sync(s3, config, target, "django.conf", False,
                                          restart=restart), 0, "Verify config_sync.sync(s3, config, target, 'django.conf', False, restart=restart)")
            th.assert_eq(config_sync.sync(s3, config, target, "django.conf", False,
                                          restart=restart), 0, "Verify config_sync.sync(s3, config, target, 'django.conf', False, restart=restart)")
        th.assert_eq(restart.call_count, 2, 'Verify restart.call_count')
        receipt = fleet_config_node.read_receipt(target)
        th.assert_eq(receipt["installed_at"], installed_at, "Verify receipt['installed_at']")
        th.assert_eq(receipt["status"], "restart_requested", "Verify receipt['status']")
        th.assert_true("mojo" not in Path(target + fleet_config_node.RECEIPT_SUFFIX).read_text(), "Verify 'mojo' not in Path(target + fleet_config_node.RECEIPT_SUFFIX).read_text()")

        os.unlink(target + fleet_config_node.RECEIPT_SUFFIX)
        unchanged_mtime = os.stat(target).st_mtime_ns
        fresh_restart = mock.Mock(return_value=True)
        with mock.patch.object(config_sync.request_service, "read", return_value=True):
            config_sync.sync(s3, config, target, "django.conf", False, restart=fresh_restart)
        th.assert_eq(fresh_restart.call_count, 1, "Matching file without receipt needs one proven activation")
        th.assert_eq(os.stat(target).st_mtime_ns, unchanged_mtime, "Matching bytes must not rewrite config")
        th.assert_eq(fleet_config_node.read_receipt(target)["installed_revision"], revision,
                     "First receipt must bind the matching installed revision")


@th.django_unit_test()
def test_fleet_receipt_rejects_symlinks_and_oversize(opts):
    from mojo.deploy import fleet_config_node as node

    with tempfile.TemporaryDirectory() as root:
        target = str(Path(root) / "django.conf")
        other = Path(root) / "other"
        other.write_text('{"status":"healthy"}')
        receipt = Path(target + node.RECEIPT_SUFFIX)
        receipt.symlink_to(other)
        th.assert_eq(node.read_receipt(target), {}, 'Verify node.read_receipt(target)')
        receipt.unlink()
        receipt.write_text("x" * (node.MAX_RECEIPT + 1))
        th.assert_eq(node.read_receipt(target), {}, 'Verify node.read_receipt(target)')
        node.write_receipt(target, {"status": "downloaded", "secret": "do not persist"})
        th.assert_true("secret" not in receipt.read_text(), "Verify 'secret' not in receipt.read_text()")


@th.django_unit_test()
def test_fleet_trigger_uses_fixed_command(opts):
    from mojo.deploy import fleet_config_node as node
    from django.core import signing
    import socket

    authorization = signing.dumps({"revision": "a" * 32, "operation_id": "b" * 32,
                                  "actor_id": 1, "nodes": [socket.gethostname().lower()]},
                                 salt="mojo.fleet.apply.intent")

    with mock.patch.object(node, "_supported", return_value=True), mock.patch.object(
            node.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
        result = node.trigger({"revision": "a" * 32, "authorization": authorization})
        th.assert_eq(result["status"], "requested", "Verify result['status']")
        th.assert_eq(run.call_args.args[0], ["sudo", "-n", "/usr/bin/systemctl",
                                           "--no-block", "start", "config-sync.service"], 'Verify run.call_args.args[0]')
        try:
            node.trigger({"revision": "a" * 32, "command": "whoami"})
            raise AssertionError("extra command was accepted")
        except ValueError:
            pass
        th.assert_eq(run.call_count, 1, 'Verify run.call_count')


@th.django_unit_test()
def test_fleet_report_requires_new_serving_proof(opts):
    from mojo.deploy import fleet_config_node as node

    revision = "a" * 32
    receipt = {"installed_revision": revision, "status": "restart_requested",
               "installed_at": 100, "target_revision": revision, "installed_digest": "b" * 64}
    with mock.patch.object(node, "_supported", return_value=True), mock.patch.object(
            node, "read_receipt", return_value=receipt), mock.patch.object(
            node, "current_digest", return_value="b" * 64), mock.patch.object(
            node, "_service_state", return_value={"active": True, "pid": 45,
                                                   "started_at": 101}), mock.patch.object(
            node, "_serving_proof", return_value={}) as proof:
        result = node.report({"revision": revision})
        th.assert_true(result["installed"], "Verify result['installed']")
        th.assert_true(not result["healthy"] and not result["restarted"], "Verify not result['healthy'] and (not result['restarted'])")
        th.assert_eq(result["error_code"], "proof_unavailable", "Verify result['error_code']")
        proof.return_value = {"healthy": False}
        th.assert_eq(node.report({"revision": revision})["error_code"], "dependency_health_failed", "Verify node.report({'revision': revision})['error_code']")
        proof.return_value = {"healthy": True}
        result = node.report({"revision": revision})
        th.assert_true(result["healthy"] and result["restarted"], "Verify result['healthy'] and result['restarted']")


@th.django_unit_test()
def test_fleet_sudoers_validation_preserves_previous(opts):
    from mojo.deploy import node_setup

    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "rule"
        path.write_text("old rule\n")
        try:
            node_setup.install_fleet_trigger("ec2-user", str(path), validator=lambda value: False)
            raise AssertionError("invalid sudoers installed")
        except ValueError:
            pass
        th.assert_eq(path.read_text(), "old rule\n", 'Verify path.read_text()')
        node_setup.install_fleet_trigger("ec2-user", str(path), validator=lambda value: True)
        th.assert_eq(path.read_text(), "ec2-user ALL=(root) NOPASSWD: /usr/bin/systemctl --no-block start config-sync.service\n", 'Verify path.read_text()')
        th.assert_eq(os.stat(path).st_mode & 0o777, 0o440, 'Verify os.stat(path).st_mode & 511')


@th.django_unit_test()
def test_fleet_report_detects_same_revision_file_drift(opts):
    from mojo.deploy import fleet_config_node as node

    revision = "a" * 32
    receipt = {"installed_revision": revision, "installed_digest": "b" * 64,
               "status": "restart_requested", "installed_at": 100}
    with mock.patch.object(node, "_supported", return_value=True), mock.patch.object(
            node, "read_receipt", return_value=receipt), mock.patch.object(
            node, "current_digest", return_value="c" * 64):
        result = node.report({"revision": revision})
        th.assert_eq(result["error_code"], "installed_config_drift", "Verify result['error_code']")
        th.assert_true(not result["installed"] and not result["healthy"], "Verify not result['installed'] and (not result['healthy'])")


@th.django_unit_test()
def test_fleet_digest_is_bounded_and_no_follow(opts):
    from mojo.deploy import fleet_config_node as node

    with tempfile.TemporaryDirectory() as root:
        target = Path(root) / "config"
        target.write_bytes(b"example")
        th.assert_eq(node.current_digest(str(target)), hashlib.sha256(b"example").hexdigest(), 'Verify node.current_digest(str(target))')
        link = Path(root) / "link"
        link.symlink_to(target)
        th.assert_eq(node.current_digest(str(link)), None, 'Verify node.current_digest(str(link))')
        target.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
        th.assert_eq(node.current_digest(str(target)), None, 'Verify node.current_digest(str(target))')


@th.django_unit_test()
def test_fleet_socket_permission_error_is_actionable(opts):
    from mojo.deploy import fleet_config_node as node

    with mock.patch.object(node, "_proof_destination", return_value=("/api/account/admin/fleet/proof", {})), mock.patch.object(
            node._UnixHTTPConnection, "request", side_effect=PermissionError()):
        th.assert_eq(node._serving_proof("a" * 32)["error_code"], "proof_socket_permission_denied", "Verify node._serving_proof('a' * 32)['error_code']")


@th.django_unit_test()
def test_fleet_proof_uses_configured_origin_and_prefix(opts):
    from mojo.deploy import fleet_config_node as node
    from mojo.apps.account.services import system_settings
    from mojo.helpers import request

    with mock.patch.object(system_settings, "get_value", return_value="https://example.test"), mock.patch.object(
            request, "API_ROOT", "/custom-api"):
        path, headers = node._proof_destination()
        th.assert_eq(path, "/custom-api/account/admin/fleet/proof", 'Verify path')
        th.assert_eq(headers["Host"], "example.test", "Verify headers['Host']")
        th.assert_eq(headers["X-Forwarded-Proto"], "https", "Verify headers['X-Forwarded-Proto']")


@th.django_unit_test()
def test_fleet_trigger_rejects_unsigned_or_wrong_node_intent(opts):
    from mojo.deploy import fleet_config_node as node
    from django.core import signing

    with mock.patch.object(node.subprocess, "run") as run:
        th.assert_eq(node.trigger({"revision": "a" * 32, "authorization": "forged"})["error_code"],
                     "apply_authorization_invalid", "Verify node.trigger({'revision': 'a' * 32, 'authorization': 'forged'})['error_code']")
        token = signing.dumps({"revision": "a" * 32, "operation_id": "b" * 32,
                               "actor_id": 1, "nodes": ["unrelated-node"]},
                              salt="mojo.fleet.apply.intent")
        th.assert_eq(node.trigger({"revision": "a" * 32, "authorization": token})["error_code"],
                     "apply_authorization_invalid", "Verify node.trigger({'revision': 'a' * 32, 'authorization': token})['error_code']")
        th.assert_eq(run.call_count, 0, 'Verify run.call_count')


@th.django_unit_test()
def test_fleet_config_permissions_survive_deploy_lifecycle(opts):
    from mojo.deploy import config_sync, fleet_config_node, node_setup

    with tempfile.TemporaryDirectory() as root:
        var = Path(root) / "var"
        var.mkdir()
        target = var / "django.conf"
        staging = Path(root) / "download"
        staging.write_text("DATABASE_PASSWORD = 'private'\n")
        config_sync.install(str(staging), str(target), "")
        fleet_config_node.write_receipt(str(target), {"status": "downloaded"})
        receipt = Path(str(target) + fleet_config_node.RECEIPT_SUFFIX)
        node_setup.sync_var_dirs(str(var), "", False)
        th.assert_eq(target.stat().st_mode & 0o777, 0o640,
                     "A later deployment must retain config-sync's private file mode")
        th.assert_eq(receipt.stat().st_mode & 0o777, 0o644,
                     "Deployment must not make non-secret node evidence group-writable")
        log = var / "logs" / "django.conf"
        log.write_text("ordinary nested log")
        os.chmod(log, 0o600)
        os.chmod(target, 0o666)
        node_setup.sync_var_dirs(str(var), "", False)
        th.assert_eq(target.stat().st_mode & 0o777, 0o640,
                     "Previously loosened root configuration must be repaired")
        th.assert_eq(log.stat().st_mode & 0o777, 0o664,
                     "The config exception must not change unrelated nested data behavior")
        staging.write_text("DATABASE_PASSWORD = 'rotated'\n")
        config_sync.install(str(staging), str(target), "")
        node_setup.sync_var_dirs(str(var), "", False)
        th.assert_eq(target.stat().st_mode & 0o777, 0o640,
                     "Configuration rotation followed by deployment remains private")
        th.assert_eq(node_setup.sync_var_dirs(str(var), "", False), [],
                     "Protected configuration modes must converge idempotently")
