"""mojo.deploy.fleet — operator-side canonical config edits and fleet health.

Everything here is pure functions, a temp project tree and mocked boto3/ssh.
A real S3 round trip and a real node are deliberately not faked into looking
covered; the live commands are exercised by hand against a fleet.
"""

import hashlib
import json
import os
import shutil
import tempfile
from unittest import mock

from testit import helpers as th


CANONICAL = "\n".join([
    "# rendered by aws/configure_environment.py",
    "SECRET_KEY = 'private-django-secret'",
    "ALLOWED_HOSTS = ['api.example', 'localhost']",
    "DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql_psycopg2', "
    "'PASSWORD': 'private-db-secret', 'CONN_MAX_AGE': 60, 'CONN_HEALTH_CHECKS': True, "
    "'OPTIONS': {'sslmode': 'require'}, 'HOST': 'writer.example'}, "
    "'readonly': {'ENGINE': 'django.db.backends.postgresql_psycopg2', "
    "'PASSWORD': 'private-db-secret', 'CONN_MAX_AGE': 60, 'CONN_HEALTH_CHECKS': True, "
    "'OPTIONS': {'sslmode': 'require'}, 'HOST': 'reader.example'}}",
    "REDIS_PASSWORD = 'private-cache-secret'",
    "",
]) + "\n"


def _project(fleet=None, conf=None):
    root = tempfile.mkdtemp(prefix="testit_fleet.")
    os.makedirs(os.path.join(root, "aws"))
    os.makedirs(os.path.join(root, "var"))
    if fleet is None:
        fleet = {"default_env": "stage", "environments": {"stage": {
            "region": "us-west-2", "config_bucket": "cfg-bucket",
            "config_key": "config/app/stage/django.conf",
            "config_kms_key_arn": "arn:aws:kms:us-west-2:123456789012:key/x",
            "bucket_owner": "123456789012", "nodes": ["stage-1"],
            "app_root": "/opt/api", "api_host": "api.stage.example"}}}
    with open(os.path.join(root, "aws", "fleet.json"), "w") as handle:
        json.dump(fleet, handle)
    if conf is not None:
        with open(os.path.join(root, "var", "django.conf"), "w") as handle:
            handle.write(conf)
    return root


def _s3(text):
    """A mock S3 client serving `text` with its true sha256 in metadata and
    recording put_object calls."""
    s3 = mock.Mock()
    body = mock.Mock()
    body.read.return_value = text.encode("utf-8")
    s3.get_object.return_value = {
        "Body": body, "VersionId": "v1",
        "Metadata": {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}}
    s3.put_object.return_value = {"VersionId": "v2"}
    return s3


# ---------------------------------------------------------------------------
# parse / patch
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_apply_changes_touches_only_named_paths(opts):
    from mojo.deploy import fleet

    text, changes = fleet.apply_changes(CANONICAL, [
        "DATABASES.default.CONN_MAX_AGE=0", "DATABASES.readonly.CONN_MAX_AGE=0"])
    th.assert_eq([(p, o, n) for p, o, n in changes],
                 [("DATABASES.default.CONN_MAX_AGE", 60, 0),
                  ("DATABASES.readonly.CONN_MAX_AGE", 60, 0)],
                 "both paths must be reported with old and new values")
    before_lines, before = fleet.parse_conf(CANONICAL)
    after_lines, after = fleet.parse_conf(text)
    th.assert_eq(len(after_lines), len(before_lines), "line count must not change")
    for index, line in enumerate(before_lines):
        if not line.startswith("DATABASES = "):
            th.assert_eq(after_lines[index], line,
                         f"line {index + 1} must be byte-identical: only DATABASES changes")
    databases = after["DATABASES"][1]
    th.assert_eq(databases["default"]["CONN_MAX_AGE"], 0, 'databases["default"]["CONN_MAX_AGE"]')
    th.assert_eq(databases["readonly"]["CONN_MAX_AGE"], 0, 'databases["readonly"]["CONN_MAX_AGE"]')
    th.assert_eq(databases["default"]["PASSWORD"], "private-db-secret",
                 "sibling values inside the touched key must survive")
    th.assert_eq(databases["readonly"]["HOST"], "reader.example", 'databases["readonly"]["HOST"]')
    th.assert_true(text.endswith("\n"), "trailing newline must be preserved")


@th.django_unit_test()
def test_apply_changes_refuses_new_keys_bad_paths_and_no_ops(opts):
    from mojo.deploy import fleet

    for assignment, fragment in (
            ("NEW_KEY=1", "refusing to add keys"),
            ("DATABASES.default.NOPE=1", "not found"),
            ("DATABASES.nope.CONN_MAX_AGE=1", "not found"),
            ("ALLOWED_HOSTS.x=1", "not found"),
            ("DATABASES.default.CONN_MAX_AGE=not a literal", "not a Python literal"),
            ("lowercase=1", "settings key"),
            ("DATABASES.default.CONN_MAX_AGE", "expected KEY")):
        try:
            fleet.apply_changes(CANONICAL, [assignment])
        except fleet.FleetError as err:
            th.assert_true(fragment in str(err), f"{assignment!r}: {err}")
        else:
            th.assert_true(False, f"{assignment!r} must be refused")
    try:
        fleet.apply_changes(CANONICAL, ["DATABASES.default.CONN_MAX_AGE=60"])
    except fleet.FleetError as err:
        th.assert_true("nothing to change" in str(err), str(err))
    else:
        th.assert_true(False, "an assignment that changes nothing must not publish")


@th.django_unit_test()
def test_parse_conf_rejects_a_malformed_canonical_file(opts):
    from mojo.deploy import fleet

    for text, fragment in (
            ("SECRET_KEY = 'x'\nBROKEN LINE\n", "not 'KEY = <literal>'"),
            ("SECRET_KEY = 'x'\nSECRET_KEY = 'y'\n", "duplicate key"),
            ("SECRET_KEY = not-a-literal\n", "not a Python literal"),
            ("secret_key = 'x'\n", "not a settings key")):
        try:
            fleet.parse_conf(text)
        except fleet.FleetError as err:
            th.assert_true(fragment in str(err), f"{text!r}: {err}")
        else:
            th.assert_true(False, f"{text!r} must be refused: the fleet boots from this file")


@th.django_unit_test()
def test_redact_hides_secret_like_keys_at_every_depth(opts):
    from mojo.deploy import fleet

    _, keys = fleet.parse_conf(CANONICAL)
    shown = fleet.redact("DATABASES", keys["DATABASES"][1])
    th.assert_eq(shown["default"]["PASSWORD"], fleet.REDACTED, 'shown["default"]["PASSWORD"]')
    th.assert_eq(shown["readonly"]["PASSWORD"], fleet.REDACTED, 'shown["readonly"]["PASSWORD"]')
    th.assert_eq(shown["default"]["HOST"], "writer.example", "non-secret values stay readable")
    th.assert_eq(fleet.redact("SECRET_KEY", "x"), fleet.REDACTED, 'fleet.redact("SECRET_KEY"')
    th.assert_eq(fleet.redact("REDIS_PASSWORD", "x"), fleet.REDACTED, 'fleet.redact("REDIS_PASSWORD"')
    th.assert_eq(fleet.redact("ALLOWED_HOSTS", ["a"]), ["a"], 'fleet.redact("ALLOWED_HOSTS"')


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_load_fleet_defaults_validates_and_names_the_environment(opts):
    from mojo.deploy import fleet

    root = _project()
    try:
        spec = fleet.load_fleet(root, None)
        th.assert_eq(spec["name"], "stage", "default_env must apply when --env is omitted")
        th.assert_eq(spec["asgi_service"], "mojo-asgi.service", "optional keys take defaults")
        th.assert_eq(spec["nodes"], ["stage-1"], 'spec["nodes"]')
        try:
            fleet.load_fleet(root, "prod")
        except fleet.FleetError as err:
            th.assert_true("unknown environment" in str(err), str(err))
        else:
            th.assert_true(False, "an unknown environment must be refused")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    broken = _project(fleet={"environments": {"stage": {"region": "us-west-2"}}})
    try:
        fleet.load_fleet(broken, "stage")
    except fleet.FleetError as err:
        th.assert_true("missing" in str(err) and "config_bucket" in str(err), str(err))
    else:
        th.assert_true(False, "a fleet entry without the required keys must be refused")
    finally:
        shutil.rmtree(broken, ignore_errors=True)


@th.django_unit_test()
def test_build_session_prefers_profile_then_conf_pair_then_ambient(opts):
    from mojo.deploy import fleet

    root = _project(conf="AWS_KEY = 'AKIA-test'\nAWS_SECRET = 'shh'\nAWS_REGION = 'us-east-1'\n")
    try:
        spec = fleet.load_fleet(root, "stage")
        sessions = mock.Mock()
        fleet.build_session(root, spec, profile="ops", session_class=sessions)
        th.assert_eq(sessions.call_args.kwargs, {"profile_name": "ops", "region_name": "us-west-2"},
                     "--profile must win and must not read the conf pair")
        fleet.build_session(root, spec, session_class=sessions)
        kwargs = sessions.call_args.kwargs
        th.assert_eq(kwargs["aws_access_key_id"], "AKIA-test", 'kwargs["aws_access_key_id"]')
        th.assert_eq(kwargs["aws_secret_access_key"], "shh", 'kwargs["aws_secret_access_key"]')
        th.assert_eq(kwargs["region_name"], "us-west-2",
                     "the fleet environment's region wins over the conf's AWS_REGION")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    bare = _project(conf="AWS_KEY = 'only-half'\n")
    try:
        spec = fleet.load_fleet(bare, "stage")
        sessions = mock.Mock()
        fleet.build_session(bare, spec, session_class=sessions)
        th.assert_eq(sessions.call_args.kwargs, {"region_name": "us-west-2"},
                     "half a credential pair must fall through to the ambient chain")
    finally:
        shutil.rmtree(bare, ignore_errors=True)


# ---------------------------------------------------------------------------
# config set end to end (mocked S3)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_config_set_verifies_patches_keeps_rollback_and_publishes_with_sha(opts):
    from mojo.deploy import fleet

    root = _project()
    try:
        s3 = _s3(CANONICAL)
        session = mock.Mock()
        session.client.return_value = s3
        code = fleet.main(["config", "set", "DATABASES.default.CONN_MAX_AGE=0",
                           "DATABASES.readonly.CONN_MAX_AGE=0", "--project", root],
                          session_factory=lambda *a, **k: session)
        th.assert_eq(code, 0, 'code')
        get_kwargs = s3.get_object.call_args.kwargs
        th.assert_eq(get_kwargs["ExpectedBucketOwner"], "123456789012",
                     "reads must pin the bucket owner")
        put = s3.put_object.call_args.kwargs
        published = put["Body"].decode("utf-8")
        _, after = fleet.parse_conf(published)
        th.assert_eq(after["DATABASES"][1]["default"]["CONN_MAX_AGE"], 0, 'after["DATABASES"][1]["default"]["CONN_MAX_AGE"]')
        th.assert_eq(after["DATABASES"][1]["readonly"]["CONN_MAX_AGE"], 0, 'after["DATABASES"][1]["readonly"]["CONN_MAX_AGE"]')
        th.assert_eq(put["Metadata"], {"sha256": hashlib.sha256(published.encode()).hexdigest()},
                     "config_sync requires the sha256 metadata to trust the object")
        th.assert_eq(put["ServerSideEncryption"], "aws:kms", 'put["ServerSideEncryption"]')
        th.assert_eq(put["SSEKMSKeyId"], "arn:aws:kms:us-west-2:123456789012:key/x", 'put["SSEKMSKeyId"]')
        th.assert_eq(put["ExpectedBucketOwner"], "123456789012", 'put["ExpectedBucketOwner"]')
        rollback_dir = os.path.join(root, "var", "fleet", "stage")
        copies = os.listdir(rollback_dir)
        th.assert_eq(len(copies), 1, "exactly one rollback copy of the prior object")
        th.assert_true(copies[0].endswith(".v1"), "rollback copy names the prior version")
        path = os.path.join(rollback_dir, copies[0])
        th.assert_eq(os.stat(path).st_mode & 0o777, 0o600, "rollback copy holds secrets: 0600")
        th.assert_eq(os.stat(rollback_dir).st_mode & 0o777, 0o700, 'os.stat(rollback_dir).st_mode & 0o777')
        with open(path) as handle:
            th.assert_eq(handle.read(), CANONICAL, "rollback copy is the untouched prior object")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_config_set_refuses_an_object_that_fails_its_own_integrity_metadata(opts):
    from mojo.deploy import fleet

    root = _project()
    try:
        s3 = _s3(CANONICAL)
        s3.get_object.return_value["Metadata"] = {"sha256": "0" * 64}
        session = mock.Mock()
        session.client.return_value = s3
        code = fleet.main(["config", "set", "DATABASES.default.CONN_MAX_AGE=0",
                           "--project", root], session_factory=lambda *a, **k: session)
        th.assert_eq(code, 2, "a body that does not match its sha256 metadata is refused")
        th.assert_eq(s3.put_object.call_count, 0, "nothing may be published after a refusal")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_dry_run_never_publishes(opts):
    from mojo.deploy import fleet

    root = _project()
    try:
        s3 = _s3(CANONICAL)
        session = mock.Mock()
        session.client.return_value = s3
        code = fleet.main(["config", "set", "DATABASES.default.CONN_MAX_AGE=0", "--dry-run",
                           "--project", root], session_factory=lambda *a, **k: session)
        th.assert_eq(code, 0, 'code')
        th.assert_eq(s3.put_object.call_count, 0, 's3.put_object.call_count')
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_parse_node_status_maps_processes_sockets_and_http_window(opts):
    from mojo.deploy import fleet

    status = fleet.parse_node_status("\n".join([
        "host=wmx1", "now=14:49:46", "asgi=active", "timer=active",
        "conf_sha=abc", "conf_mtime=2026-09-21 14:37:51",
        "last_sync=Sep 21 14:38 wmx1 restart_jobs_if_idle.sh[1]: config-sync: recycled",
        "proc=100 asgi-parent 674 1", "proc=101 asgi-worker 674 8", "proc=200 jobs-engine 645 12",
        "pg=101 4", "pg=200 8", "pg=999 1", "http=43 0", ""]))
    th.assert_eq(status["procs"]["101"], {"role": "asgi-worker", "uptime": 674, "threads": 8}, 'status["procs"]["101"]')
    th.assert_eq(status["pg"], {"101": 4, "200": 8, "999": 1}, 'status["pg"]')
    th.assert_eq((status["http_total"], status["http_5xx"]), (43, 0), '(status["http_total"]')
    th.assert_eq(status["conf_sha"], "abc", 'status["conf_sha"]')
    th.assert_eq(fleet.human_uptime(674), "11m14s", 'fleet.human_uptime(674)')
    th.assert_eq(fleet.human_uptime(90061), "1d1h", 'fleet.human_uptime(90061)')


@th.django_unit_test()
def test_sync_holds_all_timers_then_rolls_nodes_one_at_a_time(opts):
    from mojo.deploy import fleet

    fleet_doc = {"default_env": "prod", "environments": {"prod": {
        "region": "us-east-1", "config_bucket": "b", "config_key": "k",
        "config_kms_key_arn": "arn:aws:kms:us-east-1:1:key/x", "bucket_owner": "1",
        "nodes": ["n1", "n2"], "app_root": "/opt/api", "api_host": "api.example"}}}
    root = _project(fleet=fleet_doc)
    try:
        expected = hashlib.sha256(CANONICAL.encode()).hexdigest()
        calls = []

        def fake_ssh(node, script, timeout=90, runner=None):
            calls.append((node, script.split("\n")[0][:60]))
            if "systemctl stop" in script:
                return 0, "", ""
            if "systemctl start config-sync.service" in script:
                return 0, expected + "\n", ""
            if "is-active" in script:
                return 0, "active\n2\n200\n", ""
            return 0, "", ""

        s3 = _s3(CANONICAL)
        session = mock.Mock()
        session.client.return_value = s3
        code = fleet.main(["sync", "--settle", "0", "--timeout", "5", "--project", root],
                          session_factory=lambda *a, **k: session, ssh_runner=fake_ssh,
                          sleep=lambda *_: None)
        th.assert_eq(code, 0, 'code')
        stops = [n for n, s in calls if "systemctl stop config-sync.timer" in s]
        th.assert_eq(stops, ["n1", "n2"], "every node's timer is held before the first sync")
        order = [n for n, s in calls if "systemctl start config-sync.service" in s]
        th.assert_eq(order, ["n1", "n2"], "nodes sync strictly in fleet order")
        n1_restore = next(i for i, (n, s) in enumerate(calls)
                          if n == "n1" and "systemctl start config-sync.timer" in s)
        n2_sync = next(i for i, (n, s) in enumerate(calls)
                       if n == "n2" and "systemctl start config-sync.service" in s)
        th.assert_true(n1_restore < n2_sync,
                       "node 1 must be healthy and restored before node 2 is touched")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_stops_the_roll_when_a_node_fails_its_gate(opts):
    from mojo.deploy import fleet

    fleet_doc = {"default_env": "prod", "environments": {"prod": {
        "region": "us-east-1", "config_bucket": "b", "config_key": "k",
        "config_kms_key_arn": "arn:aws:kms:us-east-1:1:key/x", "bucket_owner": "1",
        "nodes": ["n1", "n2"], "app_root": "/opt/api", "api_host": "api.example"}}}
    root = _project(fleet=fleet_doc)
    try:
        calls = []

        def fake_ssh(node, script, timeout=90, runner=None):
            calls.append((node, script))
            if "systemctl start config-sync.service" in script:
                return 0, "not-the-canonical-sha\n", ""
            return 0, "", ""

        s3 = _s3(CANONICAL)
        session = mock.Mock()
        session.client.return_value = s3
        code = fleet.main(["sync", "--settle", "0", "--timeout", "1", "--project", root],
                          session_factory=lambda *a, **k: session, ssh_runner=fake_ssh,
                          sleep=lambda *_: None)
        th.assert_eq(code, 2, "a node whose conf does not land the canonical sha fails the roll")
        touched = [n for n, s in calls if "systemctl start config-sync.service" in s]
        th.assert_eq(touched, ["n1"], "node 2 must never be synced after node 1 fails")
        restored = [n for n, s in calls if "systemctl start config-sync.timer" in s]
        th.assert_eq(restored, [], "timers stay held so the fleet cannot restart itself")
    finally:
        shutil.rmtree(root, ignore_errors=True)
