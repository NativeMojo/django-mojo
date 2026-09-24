"""Fleet additions/removals and file-valued settings, without live AWS."""

import contextlib
import io
import os
import shutil
from pathlib import Path
from unittest import mock

from testit import helpers as th
from .fleet import CANONICAL, _project, _s3


def _main(fleet, *args, **kwargs):
    try:
        return fleet.main(*args, **kwargs)
    except SystemExit as err:
        raise AssertionError("Fleet CLI must accept the requested operation") from err


@th.django_unit_test()
def test_add_preserves_existing_bytes_and_requires_explicit_flag(opts):
    from mojo.deploy import fleet

    for original in (CANONICAL, CANONICAL.rstrip("\n"), CANONICAL.replace("\n", "\r\n"), ""):
        updated, changes = fleet.apply_changes(original, ["NEW_KEY=None", "ENABLED=True"], add=True)
        assert updated.startswith(original), "Appending keys must preserve all original bytes"
        _, keys = fleet.parse_conf(updated)
        assert keys["NEW_KEY"][1] is None, "None is a valid added value, not a missing marker"
        assert keys["ENABLED"][1] is True, "Multiple new keys must be appended"
        assert len(changes) == 2, "Both additions must appear in the diff"
    for assignment, add, message in (
            ("NEW_KEY=1", False, "--add"),
            ("NEW_KEY.child=1", True, "top-level"),
            ("DATABASES.default.NOPE=1", True, "not found")):
        try:
            fleet.apply_changes(CANONICAL, [assignment], add=add)
        except fleet.FleetError as err:
            assert message in str(err), "Refusal must explain how to correct the command"
        else:
            assert False, "The add flag must not bypass typo checks for nested paths"


@th.django_unit_test()
def test_add_and_edit_same_command_preserves_siblings(opts):
    from mojo.deploy import fleet

    updated, _ = fleet.apply_changes(CANONICAL, [
        "NEW_SETTING={'enabled': False}", "NEW_SETTING.enabled=True",
        "DATABASES.default.CONN_MAX_AGE=0"], add=True)
    _, keys = fleet.parse_conf(updated)
    assert keys["NEW_SETTING"][1] == {"enabled": True}, "Later assignments see earlier additions"
    assert keys["DATABASES"][1]["readonly"]["CONN_MAX_AGE"] == 60, "Sibling paths must survive"
    assert "SECRET_KEY = 'private-django-secret'\n" in updated, "Untouched lines must survive verbatim"


@th.django_unit_test()
def test_unset_removes_only_requested_top_level_lines(opts):
    from mojo.deploy import fleet

    original = "# keep\r\nFIRST = None\r\n\r\nSECOND = 'stay'\r\n# trailing"
    updated, changes = fleet.apply_unsets(original, ["FIRST"])
    assert updated == original.replace("FIRST = None\r\n", ""), "Only the requested line may disappear"
    assert len(changes) == 1, "A None-valued setting is still a real removal"
    for name in ("MISSING", "SECOND.child", "lowercase"):
        try:
            fleet.apply_unsets(original, [name])
        except fleet.FleetError:
            pass
        else:
            assert False, "Unknown keys and nested paths must be refused"


@th.django_unit_test()
def test_file_value_round_trips_publish_sync_and_settings_loader(opts):
    from mojo.deploy import config_sync, fleet
    from mojo.helpers.settings.parser import DjangoConfigLoader

    root = _project()
    try:
        pem = b"-----BEGIN PRIVATE KEY-----\r\nexample+/=\r\n-----END PRIVATE KEY-----\r\n"
        source = Path(root, "AuthKey.p8")
        source.write_bytes(pem)
        s3 = _s3(CANONICAL)
        session = mock.Mock()
        session.client.return_value = s3
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = _main(fleet, ["config", "set", "--add", f"APPLE_PRIVATE_KEY=@{source}",
                               "APPLE_CLIENT_ID='com.example.web'", "--project", root],
                              session_factory=lambda *a, **k: session)
        assert code == 0, "Adding an integration must publish successfully"
        assert "+ APPLE_PRIVATE_KEY" in output.getvalue(), "Diff must mark new keys as additions"
        assert "example+/=" not in output.getvalue(), "The PEM must never appear in the diff"
        put = s3.put_object.call_args.kwargs
        payload = put["Body"]
        assert put["Metadata"] == {"sha256": fleet.sha256_text(payload.decode())}, "Publish carries integrity metadata"
        copies = list(Path(root, "var/fleet/stage").iterdir())
        assert len(copies) == 1 and copies[0].read_bytes() == CANONICAL.encode(), "Rollback holds the original bytes"
        assert copies[0].stat().st_mode & 0o777 == 0o600, "Rollback secrets must be private"
        node = mock.Mock()
        node.head_object.return_value = {"ETag": '"v2"', "Metadata": put["Metadata"]}
        node.download_file.side_effect = lambda bucket, key, path: Path(path).write_bytes(payload)
        target = Path(root, "node/django.conf")
        code = config_sync.sync(node, {"AWS_CONFIG_BUCKET": "cfg-bucket", "AWS_CONFIG_PREFIX": "config/app/stage",
                                      "CONFIG_SYNC_REQUIRE_SHA": "true"}, str(target), "django.conf", False)
        assert code == 0 and target.read_bytes() == payload, "Config sync must install the exact published bytes"
        context = {}
        DjangoConfigLoader(target).load_config(context)
        assert context["APPLE_PRIVATE_KEY"].encode() == pem, "PEM must survive file, repr, S3, sync and loader byte-exact"
    finally:
        shutil.rmtree(root)


@th.django_unit_test()
def test_add_and_unset_dry_runs_and_unset_publish(opts):
    from mojo.deploy import fleet

    for action, dry in (("set", True), ("unset", True), ("unset", False)):
        root = _project()
        try:
            s3 = _s3(CANONICAL)
            session = mock.Mock()
            session.client.return_value = s3
            command = ["config", action, "--project", root]
            command += ["APPLE_PRIVATE_KEY='never-print-this'", "--add"] if action == "set" else ["SECRET_KEY"]
            if dry:
                command.append("--dry-run")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = _main(fleet, command, session_factory=lambda *a, **k: session)
            assert code == 0, "Add and unset must support the same publish workflow"
            assert s3.put_object.call_count == (0 if dry else 1), "Dry-run must never publish"
            assert "never-print-this" not in output.getvalue() and "private-django-secret" not in output.getvalue(), "Diffs redact secrets"
            assert ("+ APPLE_PRIVATE_KEY" if action == "set" else "- SECRET_KEY") in output.getvalue(), "Diff marks additions and removals"
            assert len(os.listdir(Path(root, "var/fleet/stage"))) == 1, "Dry-run and unset retain a rollback copy"
            if not dry:
                put = s3.put_object.call_args.kwargs
                expected = CANONICAL.replace("SECRET_KEY = 'private-django-secret'\n", "")
                assert put["Body"] == expected.encode(), "Unset publishes only the intended deletion"
                assert put["Metadata"] == {"sha256": fleet.sha256_text(expected)}, "Unset preserves sha256 metadata"
        finally:
            shutil.rmtree(root)


@th.django_unit_test()
def test_file_errors_and_secret_noops_do_not_disclose_values(opts):
    from mojo.deploy import fleet

    root = _project()
    try:
        path = Path(root, "bad.pem")
        path.write_bytes(b"\xff")
        for source in (path, Path(root, "missing.pem")):
            try:
                fleet.parse_assignment(f"APPLE_PRIVATE_KEY=@{source}")
            except fleet.FleetError as err:
                assert "file" in str(err), "File read errors must be actionable FleetErrors"
            else:
                assert False, "Missing and non-UTF-8 files must be refused"
        assert fleet.parse_assignment("LABEL='@literal'")[1] == "@literal", "Quoted @ strings stay literals"
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            try:
                fleet.apply_changes(CANONICAL, ["SECRET_KEY='private-django-secret'"])
            except fleet.FleetError:
                pass
        assert "private-django-secret" not in output.getvalue(), "No-op notices must redact secrets too"
    finally:
        shutil.rmtree(root)
