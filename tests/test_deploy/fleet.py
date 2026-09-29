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


def _s3(text, override=None, override_error="NoSuchKey", override_metadata=True,
        canonical_sha=None, override_length=None):
    """A mock S3 client recording put_object calls. The canonical key serves
    `text` with its true sha256 in metadata; the fleet override key serves
    `override` bytes when given, otherwise raises a ClientError with
    `override_error` (a missing override by default). `canonical_sha`
    replaces the canonical object's metadata digest; `override_length`
    replaces the override's ContentLength. Every body served is kept on
    `s3.bodies` as (key, body)."""
    from botocore.exceptions import ClientError

    def get_object(**kwargs):
        if kwargs["Key"].endswith("django.override.json"):
            if override is None:
                raise ClientError({"Error": {"Code": override_error}}, "GetObject")
            payload = override
        else:
            payload = text.encode("utf-8")
        body = mock.Mock()
        body.read.side_effect = lambda amount=None: payload if amount is None else payload[:amount]
        s3.bodies.append((kwargs["Key"], body))
        metadata = {"sha256": hashlib.sha256(payload).hexdigest()}
        length = len(payload)
        if not override_metadata and payload is override:
            metadata = {}
        if override_length is not None and payload is override:
            length = override_length
        if canonical_sha and payload is not override:
            metadata = {"sha256": canonical_sha}
        return {"Body": body, "VersionId": "v1", "Metadata": metadata, "ContentLength": length}

    s3 = mock.Mock()
    s3.bodies = []
    s3.get_object.side_effect = get_object
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
        s3 = _s3(CANONICAL, canonical_sha="0" * 64)
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


def _probe_out(state="active", pid="4242", jobs="0", app_age="30", conf_age="600", http="200"):
    """What the sync gate's probe prints on a node: `key=value` lines. The
    defaults are an app that started after its conf was written, healthy."""
    lines = [f"ActiveState={state}", f"MainPID={pid}", f"jobs={jobs}", f"app_age={app_age}"]
    if conf_age is not None:
        lines.append(f"conf_age={conf_age}")
    lines.append(f"http={http}")
    return "\n".join(lines) + "\n"


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
                return 0, "rc=0\n" + expected + "\n", ""
            if "systemctl show" in script:
                return 0, _probe_out(), ""
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
                return 0, "rc=0\nnot-the-canonical-sha\n", ""
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


# ---------------------------------------------------------------------------
# published fleet overrides: the file a node should hold (#5853)
# ---------------------------------------------------------------------------

REVISION = "c" * 32


def _override():
    from mojo.deploy import config_override

    values = dict(config_override.DEFAULTS)
    return config_override.encode_document(
        values, REVISION, "2026-09-29T00:00:00+00:00", values)


def _composed_sha(canonical_text, override):
    from mojo.deploy import config_override

    document = config_override.decode_document(override, config_override.DEFAULTS)
    return hashlib.sha256(config_override.compose(
        canonical_text.encode("utf-8"), document)).hexdigest()


def _two_node_fleet(**extra):
    env = {"region": "us-east-1", "config_bucket": "b",
           "config_key": "config/app/prod/django.conf",
           "config_kms_key_arn": "arn:aws:kms:us-east-1:1:key/x", "bucket_owner": "1",
           "nodes": ["n1", "n2"], "app_root": "/opt/api", "api_host": "api.example"}
    env.update(extra)
    return {"default_env": "prod", "environments": {"prod": env}}


def _sync(root, s3, landed, timer_state=None, probes=None, rc=None, timeout="5", record=None):
    """Run `fleet sync`; each node reports `landed[node]` as its conf sha,
    exits `rc[node]` from the sync (0 by default) and answers the app probe
    with `probes[node]` in turn, its last answer repeating (a current app by
    default). Returns (code, calls, stderr); `record` collects stdout and the
    sleep calls."""
    import contextlib
    import io
    from mojo.deploy import fleet

    calls, sleeps = [], []
    answers = {node: list(seq) for node, seq in (probes or {}).items()}

    def fake_ssh(node, script, timeout=90, runner=None):
        calls.append((node, script))
        if "systemctl stop" in script:
            return 0, "", ""
        if "systemctl start config-sync.service" in script:
            return 0, f"rc={(rc or {}).get(node, 0)}\n{landed[node]}\n", ""
        if script.startswith("systemctl is-active config-sync.timer"):
            return (timer_state or {}).get(node, (3, "inactive\n", ""))
        if "systemctl show" in script:
            seq = answers.get(node) or [_probe_out()]
            return 0, seq.pop(0) if len(seq) > 1 else seq[0], ""
        return 0, "", ""

    session = mock.Mock()
    session.client.return_value = s3
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        code = fleet.main(["sync", "--settle", "70", "--timeout", timeout, "--project", root],
                          session_factory=lambda *a, **k: session, ssh_runner=fake_ssh,
                          sleep=sleeps.append)
    if record is not None:
        record.update(stdout=out.getvalue(), sleeps=sleeps)
    return code, calls, err.getvalue()


def _status(root, s3, conf_sha):
    import contextlib
    import io
    from mojo.deploy import fleet

    def fake_ssh(node, script, timeout=90, runner=None):
        return 0, f"host={node}\nconf_sha={conf_sha}\n", ""

    session = mock.Mock()
    session.client.return_value = s3
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = fleet.main(["nodes", "status", "--project", root],
                          session_factory=lambda *a, **k: session, ssh_runner=fake_ssh,
                          sleep=lambda *_: None)
    return code, out.getvalue()


@th.django_unit_test()
def test_sync_rolls_every_node_holding_the_published_overrides(opts):
    override = _override()
    composed = _composed_sha(CANONICAL, override)
    root = _project(fleet=_two_node_fleet())
    try:
        code, calls, err = _sync(root, _s3(CANONICAL, override), {"n1": composed, "n2": composed})
        th.assert_eq(code, 0, f"a node holding canonical + overrides must pass its gate: {err}")
        synced = [n for n, s in calls if "systemctl start config-sync.service" in s]
        th.assert_eq(synced, ["n1", "n2"], "the roll must continue to node 2")
        code, out = _status(root, _s3(CANONICAL, override), composed)
        th.assert_true(out.count("in sync") == 2 and "DRIFT" not in out,
                       f"status must read in sync for both nodes: {out}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_stops_when_a_node_did_not_apply_the_overrides(opts):
    override = _override()
    canonical = hashlib.sha256(CANONICAL.encode()).hexdigest()
    root = _project(fleet=_two_node_fleet())
    try:
        code, calls, err = _sync(root, _s3(CANONICAL, override), {"n1": canonical, "n2": canonical})
        th.assert_eq(code, 2, "a node without the published overrides must stop the roll")
        th.assert_true("not the published fleet overrides" in err, f"specific message: {err}")
        synced = [n for n, s in calls if "systemctl start config-sync.service" in s]
        th.assert_eq(synced, ["n1"], "node 2 must never be synced")
        code, out = _status(root, _s3(CANONICAL, override), canonical)
        th.assert_true("DRIFT vs S3 (fleet overrides not applied)" in out,
                       f"status must name the missing overrides: {out}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_stops_when_a_canonical_key_differs_under_overrides(opts):
    override = _override()
    changed = CANONICAL.replace("'CONN_MAX_AGE': 60", "'CONN_MAX_AGE': 0", 1)
    wrong = _composed_sha(changed, override)
    root = _project(fleet=_two_node_fleet())
    try:
        code, calls, err = _sync(root, _s3(CANONICAL, override), {"n1": wrong, "n2": wrong})
        th.assert_eq(code, 2, "a changed canonical key must still stop the roll")
        th.assert_true("canonical object plus the published fleet overrides" in err,
                       f"generic drift message expected: {err}")
        synced = [n for n, s in calls if "systemctl start config-sync.service" in s]
        th.assert_eq(synced, ["n1"], "node 2 must never be synced")
        code, out = _status(root, _s3(CANONICAL, override), wrong)
        th.assert_true("DRIFT vs S3" in out and "not applied" not in out and "in sync" not in out,
                       f"status must report plain drift: {out}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_expected_digest_is_the_canonical_one_without_an_override(opts):
    from mojo.deploy import fleet

    canonical = hashlib.sha256(CANONICAL.encode()).hexdigest()
    root = _project(fleet=_two_node_fleet())
    try:
        spec = fleet.load_fleet(root, None)
        session = mock.Mock()
        session.client.return_value = _s3(CANONICAL)
        th.assert_eq(fleet.expected_node_digest(session, spec), (canonical, canonical),
                     "with no override object the expected file is the canonical object")
        th.assert_eq(fleet.override_key(spec), "config/app/prod/django.override.json",
                     "the override sits beside config_key, as the node looks for it")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_refuses_an_unverifiable_or_unreadable_override_before_any_node(opts):
    for s3, fragment in (
            (_s3(CANONICAL, _override(), override_metadata=False), "no sha256 metadata"),
            (_s3(CANONICAL, override_error="AccessDenied"), "config/app/prod/django.override.json")):
        root = _project(fleet=_two_node_fleet())
        try:
            code, calls, err = _sync(root, s3, {})
            th.assert_eq(code, 2, f"sync must refuse: {err}")
            th.assert_true(fragment in err, f"expected {fragment!r} in: {err}")
            th.assert_eq(calls, [], "no node may be touched, not even a timer hold")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    canonical = hashlib.sha256(CANONICAL.encode()).hexdigest()
    root = _project(fleet=_two_node_fleet(config_override_name=None))
    try:
        s3 = _s3(CANONICAL, override_error="AccessDenied")
        code, calls, err = _sync(root, s3, {"n1": canonical, "n2": canonical})
        th.assert_eq(code, 0, f"config_override_name null must compare canonical only: {err}")
        keys = [c.kwargs["Key"] for c in s3.get_object.call_args_list]
        th.assert_true(all(not k.endswith("django.override.json") for k in keys),
                       f"no override fetch may be made when it is turned off: {keys}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_held_timer_warning_reports_each_timers_live_state(opts):
    root = _project(fleet=_two_node_fleet())
    try:
        code, _, err = _sync(root, _s3(CANONICAL), {"n1": "wrong", "n2": "wrong"},
                             timer_state={"n1": (0, "active\n", ""), "n2": (3, "inactive\n", "")})
        th.assert_eq(code, 2, "the gate failure must still stop the roll")
        held = [line for line in err.splitlines() if "still held on" in line]
        running = [line for line in err.splitlines() if "running again on" in line]
        th.assert_true(len(held) == 1 and "n2" in held[0] and "n1" not in held[0],
                       f"only node 2's timer is still held: {err}")
        th.assert_true(len(running) == 1 and "n1" in running[0] and "n2" not in running[0],
                       f"node 1's timer must be reported running again: {err}")

        code, _, err = _sync(root, _s3(CANONICAL), {"n1": "wrong", "n2": "wrong"})
        held = [line for line in err.splitlines() if "still held on" in line]
        th.assert_true(len(held) == 1 and "n1" in held[0] and "n2" in held[0]
                       and "running again" not in err,
                       f"both inactive timers are named as still held: {err}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_held_timer_is_unknown_when_ssh_fails_whatever_it_printed(opts):
    import contextlib
    import io
    import subprocess
    from mojo.deploy import fleet

    root = _project(fleet=_two_node_fleet())
    try:
        for n1, n2 in (((255, "inactive\n", "connection lost"), (255, "active\n", "connection lost")),
                       ((0, "", ""), (1, "inactive\n", "sudo: a password is required"))):
            code, _, err = _sync(root, _s3(CANONICAL), {"n1": "wrong", "n2": "wrong"},
                                 timer_state={"n1": n1, "n2": n2})
            th.assert_eq(code, 2, f"the gate failure must still stop the roll: {err}")
            th.assert_true("node conf sha does not match" in err,
                           f"the roll's own error must survive the timer check: {err}")
            unknown = [line for line in err.splitlines() if "state unknown on" in line]
            th.assert_true(len(unknown) == 1 and "n1" in unknown[0] and "n2" in unknown[0]
                           and "still held on" not in err and "running again on" not in err,
                           f"a failed or empty timer query must read unknown for {n1}, {n2}: {err}")

        def timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired("ssh", 90)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            fleet.report_held_timers(timeout, ["n1"], "config-sync.timer")
        th.assert_true("state unknown on n1" in err.getvalue(),
                       f"a timed-out query must read unknown: {err.getvalue()}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_override_fetch_bounds_its_read_and_closes_the_body(opts):
    from mojo.deploy import config_override, fleet

    cap = config_override.MAX_DOCUMENT_BYTES
    root = _project(fleet=_two_node_fleet())
    try:
        spec = fleet.load_fleet(root, None)
        # an honest ContentLength over the cap is refused before any read
        s3 = _s3(CANONICAL, b"x" * (cap + 10))
        try:
            fleet.fetch_override(s3, spec)
            th.assert_true(False, "an oversized override must be refused")
        except fleet.FleetError as err:
            th.assert_true("maximum document size" in str(err), str(err))
        body = s3.bodies[-1][1]
        th.assert_eq(body.read.call_count, 0, "no byte may be read past an oversized ContentLength")
        th.assert_true(body.close.called, "the refused body must still be closed")

        # a ContentLength that understates the body: the read is bounded
        s3 = _s3(CANONICAL, b"x" * (cap * 64), override_length=10)
        try:
            fleet.fetch_override(s3, spec)
            th.assert_true(False, "an oversized override must be refused")
        except fleet.FleetError as err:
            th.assert_true("maximum document size" in str(err), str(err))
        body = s3.bodies[-1][1]
        body.read.assert_called_once_with(cap + 1)
        th.assert_true(body.close.called, "the refused body must be closed")

        # a good override is read with the same bound and closed
        override = _override()
        s3 = _s3(CANONICAL, override)
        th.assert_eq(fleet.fetch_override(s3, spec), override, "a good override is returned whole")
        body = s3.bodies[-1][1]
        body.read.assert_called_once_with(cap + 1)
        th.assert_true(body.close.called, "the body must be closed after a good read")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# the app gate: running the current conf, not "restarted during this roll" (#5967)
# ---------------------------------------------------------------------------

def _timer_restores(calls):
    return [n for n, s in calls if "systemctl start config-sync.timer" in s]


@th.django_unit_test()
def test_sync_passes_an_already_current_node_without_waiting(opts):
    canonical = hashlib.sha256(CANONICAL.encode()).hexdigest()
    root = _project(fleet=_two_node_fleet())
    try:
        record = {}
        code, calls, err = _sync(root, _s3(CANONICAL), {"n1": canonical, "n2": canonical},
                                 probes={"n1": [_probe_out(app_age="86400", conf_age="90000")],
                                         "n2": [_probe_out(app_age="86400", conf_age="90000")]},
                                 timeout="0", record=record)
        th.assert_eq(code, 0, f"an already-current node must pass: {err}")
        th.assert_eq(record["sleeps"], [], "no --settle sleep and no polling for a current node")
        th.assert_eq(record["stdout"].count("no restart needed"), 2, record["stdout"])
        th.assert_eq(_timer_restores(calls), ["n1", "n2"], "each timer is restored")
        th.assert_true("still held" not in err and "to restore" not in err,
                       f"nothing is left held: {err}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_waits_for_a_restart_the_sync_queued(opts):
    canonical = hashlib.sha256(CANONICAL.encode()).hexdigest()
    root = _project(fleet=_two_node_fleet())
    try:
        record = {}
        pending = _probe_out(jobs="1", app_age="86400", conf_age="3")
        restarted = _probe_out(app_age="2", conf_age="80")
        code, calls, err = _sync(root, _s3(CANONICAL), {"n1": canonical, "n2": canonical},
                                 probes={"n1": [pending, restarted], "n2": [pending, restarted]},
                                 record=record)
        th.assert_eq(code, 0, f"a restarted, healthy node must pass: {err}")
        th.assert_eq(record["sleeps"], [70, 70], "each node waits --settle for its restart")
        th.assert_true("waiting for mojo-asgi.service restart" in record["stdout"]
                       and "no restart needed" not in record["stdout"], record["stdout"])
        th.assert_eq(_timer_restores(calls), ["n1", "n2"], "each timer is restored")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_stops_on_an_app_older_than_its_conf(opts):
    canonical = hashlib.sha256(CANONICAL.encode()).hexdigest()
    root = _project(fleet=_two_node_fleet())
    try:
        code, calls, err = _sync(root, _s3(CANONICAL), {"n1": canonical, "n2": canonical},
                                 probes={"n1": [_probe_out(app_age="9000", conf_age="600")]},
                                 timeout="0")
        th.assert_eq(code, 2, "a node still running the old conf must stop the roll")
        th.assert_true("app started before the conf on disk" in err, err)
        synced = [n for n, s in calls if "systemctl start config-sync.service" in s]
        th.assert_eq(synced, ["n1"], "node 2 must never be synced")
        th.assert_eq(_timer_restores(calls), [], "the timers stay held")
        th.assert_true("still held on n1, n2" in err, f"both held timers are named: {err}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_stops_when_the_sync_service_fails(opts):
    canonical = hashlib.sha256(CANONICAL.encode()).hexdigest()
    root = _project(fleet=_two_node_fleet())
    try:
        code, calls, err = _sync(root, _s3(CANONICAL), {"n1": canonical, "n2": canonical},
                                 rc={"n1": 1})
        th.assert_eq(code, 2, "a failed sync service must stop the roll")
        th.assert_true("config-sync.service exited 1 — see journalctl -u config-sync.service"
                       in err, err)
        synced = [n for n, s in calls if "systemctl start config-sync.service" in s]
        th.assert_eq(synced, ["n1"], "node 2 must never be synced")
        th.assert_true(not any("systemctl show" in s for _, s in calls),
                       "no app probe after a failed sync")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_never_treats_an_unreadable_start_time_as_fresh(opts):
    canonical = hashlib.sha256(CANONICAL.encode()).hexdigest()
    root = _project(fleet=_two_node_fleet())
    try:
        for probe in (_probe_out(pid="", app_age=""), _probe_out(pid="0", app_age="")):
            code, calls, err = _sync(root, _s3(CANONICAL), {"n1": canonical, "n2": canonical},
                                     probes={"n1": [probe]}, timeout="0")
            th.assert_eq(code, 2, f"an unreadable start time must not pass: {err}")
            th.assert_true("timed out" in err
                           and "cannot read mojo-asgi.service start time" in err, err)
            th.assert_eq(_timer_restores(calls), [], "the timers stay held")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_held_timer_report_prints_a_restore_loop(opts):
    canonical = hashlib.sha256(CANONICAL.encode()).hexdigest()
    root = _project(fleet=_two_node_fleet())
    try:
        code, _, err = _sync(root, _s3(CANONICAL), {"n1": canonical, "n2": canonical},
                             probes={"n1": [_probe_out(http="502")]}, timeout="0",
                             timer_state={"n1": (3, "inactive\n", ""),
                                          "n2": (255, "", "connection lost")})
        th.assert_eq(code, 2, "an unhealthy node must stop the roll")
        th.assert_true("health 502" in err, err)
        th.assert_true("for n in n1 n2; do ssh \"$n\" sudo systemctl start config-sync.timer; done"
                       in err, f"a copyable loop over the held and unknown nodes: {err}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run_probe_shell(jobs_rc=0, jobs_out=""):
    """Run the generated probe in real bash with `systemctl`, `ps`, `stat` and
    `date` stubbed on PATH: an active app 30s old, a conf 600s old, health 200.
    `list-jobs` prints `jobs_out` and exits `jobs_rc`. Returns (current, reason)."""
    import stat as stat_mod
    import subprocess
    from mojo.deploy import fleet

    bin_dir = tempfile.mkdtemp(prefix="testit_probe.")
    stubs = {
        "systemctl": ("case \"$1\" in\n"
                      "  show) case \"$3\" in *ActiveState*) echo ActiveState=active;; esac;"
                      " echo MainPID=4242;;\n"
                      "  list-jobs) printf '%s' \"$FAKE_JOBS_OUT\"; exit \"$FAKE_JOBS_RC\";;\n"
                      "esac\n"),
        "ps": "echo '   30'\n",
        "stat": "echo 1000\n",
        "date": "echo 1600\n",
    }
    try:
        for name, body in stubs.items():
            path = os.path.join(bin_dir, name)
            with open(path, "w") as handle:
                handle.write("#!/bin/bash\n" + body)
            os.chmod(path, stat_mod.S_IRWXU)
        script = fleet._app_probe("mojo-asgi.service", "/opt/api/var/django.conf", "echo 200")
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
                   FAKE_JOBS_RC=str(jobs_rc), FAKE_JOBS_OUT=jobs_out)
        done = subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True,
                              env=env, timeout=30)
        th.assert_eq(done.returncode, 0, f"probe script failed: {done.stderr}")
        return fleet._app_verdict(fleet._parse_key_values(done.stdout), "mojo-asgi.service")
    finally:
        shutil.rmtree(bin_dir, ignore_errors=True)


@th.django_unit_test()
def test_probe_shell_counts_only_the_asgi_units_own_jobs(opts):
    th.assert_eq(_run_probe_shell(), (True, ""), "no jobs: a current app passes")
    th.assert_eq(_run_probe_shell(jobs_out="7 other-mojo-asgi.service start running\n"),
                 (True, ""), "a job for a unit whose name contains ours is not ours")
    th.assert_eq(_run_probe_shell(jobs_out="8 mojo-asgi.service restart waiting\n"),
                 (False, "restart still pending"), "the unit's own queued restart is pending")


@th.django_unit_test()
def test_probe_shell_never_reads_a_failed_job_query_as_no_jobs(opts):
    current, reason = _run_probe_shell(jobs_rc=1)
    th.assert_true(not current and reason == "cannot read the job queue for mojo-asgi.service",
                   f"a failed list-jobs must not pass the gate: {current}, {reason}")
