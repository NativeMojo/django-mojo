"""mojo.deploy.config_sync — the node config fetcher.

Everything here is filesystem + mocked-boto3. A real S3 round trip, a real
systemctl restart, and os.chown to a foreign uid (which needs root) are out of
reach and are deliberately not faked into looking covered.
"""

import hashlib
import os
import pwd
import shutil
import tempfile
from unittest import mock

from testit import helpers as th


def _tempdir():
    return tempfile.mkdtemp(prefix="testit_config_sync.")


def _write(path, text):
    with open(path, "w") as handle:
        handle.write(text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _s3_publishing(payload, sha256=None, etag="abc"):
    """A mock S3 client that publishes `payload` and, unless told otherwise,
    advertises its real sha256 in object metadata."""
    if sha256 is None:
        sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    metadata = {} if sha256 is False else {"sha256": sha256}
    s3 = mock.Mock()
    s3.head_object.return_value = {"ETag": '"%s"' % etag, "Metadata": metadata}

    def _download(bucket, key, path):
        with open(path, "w") as handle:
            handle.write(payload)

    s3.download_file.side_effect = _download
    return s3


def _staging_leftovers(directory):
    return [n for n in os.listdir(directory) if n.startswith("config_sync.")]


class _RecordingOS:
    """Proxies the real `os` module but records the calls install() makes, in
    order. Patched onto the module under test rather than onto `os` itself, so
    a parallel test module is never affected."""

    def __init__(self, calls):
        self.calls = calls

    def __getattr__(self, name):
        return getattr(os, name)

    def chmod(self, path, mode):
        self.calls.append(("chmod", mode))
        os.chmod(path, mode)

    def chown(self, path, uid, gid):
        self.calls.append(("chown", uid, gid))

    def replace(self, src, dst):
        self.calls.append(("replace", os.path.exists(dst)))
        os.replace(src, dst)


# ---------------------------------------------------------------------------
# config parsing
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_read_config_skips_junk_and_strips_quotes(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        path = os.path.join(root, "bootstrap.conf")
        _write(path, "\n".join([
            "# a comment",
            "",
            "   ",
            "no_equals_sign_here",
            'AWS_REGION="us-east-1"',
            "AWS_CONFIG_BUCKET = 'cfg-bucket' ",
            "DATABASE_URL=postgres://u:p@h/db?sslmode=require&x=1",
        ]) + "\n")
        config = cs.read_config(path)

        th.assert_eq(config.get("AWS_REGION"), "us-east-1",
                     "surrounding double quotes must be stripped")
        th.assert_eq(config.get("AWS_CONFIG_BUCKET"), "cfg-bucket",
                     "surrounding single quotes and whitespace must be stripped")
        th.assert_eq(config.get("DATABASE_URL"),
                     "postgres://u:p@h/db?sslmode=require&x=1",
                     "only the FIRST '=' may split a line, or every connection "
                     "string with a query parameter is silently truncated")
        th.assert_true("no_equals_sign_here" not in config,
                       "a line with no '=' must be skipped, not raise")
        th.assert_eq(len(config), 3,
                     f"comments and blank lines must not become keys: {config}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_read_config_missing_file_is_not_fatal(opts):
    from mojo.deploy import config_sync as cs

    config = cs.read_config("/nonexistent/definitely/not/here.conf")
    th.assert_eq(config, {},
                 "a missing bootstrap.conf must return {} — an unconfigured "
                 "box is dormant, not broken")


@th.django_unit_test()
def test_as_bool_spellings(opts):
    from mojo.deploy import config_sync as cs

    for value in ("1", "true", "TRUE", " Yes ", "on"):
        th.assert_true(cs.as_bool(value), f"{value!r} must read as true")
    for value in ("0", "false", "no", "off", "", None, "maybe"):
        th.assert_true(not cs.as_bool(value), f"{value!r} must read as false")


# ---------------------------------------------------------------------------
# install — the security-critical part
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_install_lands_at_0600(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        staged = os.path.join(root, "staged")
        dest = os.path.join(root, "django.conf")
        _write(staged, "SECRET=1\n")
        os.chmod(staged, 0o644)

        cs.install(staged, dest, None)

        mode = os.stat(dest).st_mode & 0o777
        th.assert_eq(oct(mode), oct(0o600),
                     f"the installed config must be 0600, got {oct(mode)} — it "
                     "holds every downstream credential the app has")
        th.assert_true(not os.path.exists(staged),
                       "the staged file must have been moved, not copied")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_install_sets_mode_and_owner_before_replacing(opts):
    """THE ordering property: chmod -> chown -> os.replace.

    If replace happened first the file would exist at the destination, readable,
    for the window before chmod ran. Asserts the order, that the destination did
    not exist when replace was called, and that replace was called at all — a
    rewrite to open()+write() would otherwise satisfy an order-only assertion by
    never firing the recorder.
    """
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        staged = os.path.join(root, "staged")
        dest = os.path.join(root, "django.conf")
        _write(staged, "SECRET=1\n")
        me = pwd.getpwuid(os.getuid()).pw_name

        calls = []
        with mock.patch.object(cs, "os", _RecordingOS(calls)):
            cs.install(staged, dest, me)

        names = [c[0] for c in calls]
        th.assert_eq(names, ["chmod", "chown", "replace"],
                     f"install must chmod then chown then replace, got {names}")
        th.assert_in("replace", names,
                     "os.replace must actually be called — the atomic rename is "
                     "the whole point of staging on the same filesystem")
        replace_call = [c for c in calls if c[0] == "replace"][0]
        th.assert_true(replace_call[1] is False,
                       "the destination must not exist yet when replace runs, "
                       "so the config is never visible with the wrong mode")
        th.assert_eq(calls[0][1], cs.FILE_MODE,
                     f"chmod must use FILE_MODE (0600), got {oct(calls[0][1])}")
        th.assert_true(os.path.exists(dest),
                       "the config must be in place after install()")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_resolve_owner_degrades_to_none(opts):
    from mojo.deploy import config_sync as cs

    th.assert_eq(cs.resolve_owner(None), (None, None),
                 "an unset CONFIG_SYNC_OWNER must resolve to (None, None)")
    th.assert_eq(cs.resolve_owner(""), (None, None),
                 "an empty CONFIG_SYNC_OWNER must resolve to (None, None)")
    th.assert_eq(cs.resolve_owner("nosuchuser_xyzzy:nosuchgroup_xyzzy"),
                 (None, None),
                 "an unresolvable owner must warn and degrade — refusing to "
                 "install config over a wrong owner is worse than the wrong owner")


# ---------------------------------------------------------------------------
# locking
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_acquire_lock_refuses_a_second_run(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        target = os.path.join(root, "django.conf")
        first = cs.acquire_lock(target)
        th.assert_true(first is not None,
                       "the first caller must get the lock")
        second = cs.acquire_lock(target)
        th.assert_true(second is None,
                       "a second concurrent config_sync must be refused — two "
                       "writers on one destination is how a config gets truncated")
        first.close()
        third = cs.acquire_lock(target)
        th.assert_true(third is not None,
                       "the lock must be reacquirable once released")
        third.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_sync_installs_when_the_sha_matches(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        target = os.path.join(root, "var", "django.conf")
        payload = "SECRET=published\n"
        s3 = _s3_publishing(payload)
        config = {"AWS_CONFIG_BUCKET": "b", "AWS_CONFIG_PREFIX": "/p/"}

        code = cs.sync(s3, config, target, "django.conf", False)

        th.assert_eq(code, 0, "a verified download must install and return 0")
        with open(target) as handle:
            th.assert_eq(handle.read(), payload,
                         "the published bytes must be what lands on disk")
        th.assert_eq(os.stat(target).st_mode & 0o777, 0o600,
                     "sync must install through install(), which means 0600")
        th.assert_eq(s3.download_file.call_args[0][1], "p/django.conf",
                     "the object key must be <prefix>/<remote name>, with the "
                     "prefix's slashes stripped")
        th.assert_eq(_staging_leftovers(os.path.dirname(target)), [],
                     "no staging directory may survive a successful sync")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_creates_the_target_directory(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        target = os.path.join(root, "not", "yet", "django.conf")
        s3 = _s3_publishing("A=1\n")
        config = {"AWS_CONFIG_BUCKET": "b", "AWS_CONFIG_PREFIX": "p"}

        code = cs.sync(s3, config, target, "django.conf", False)

        th.assert_eq(code, 0,
                     "sync must create the target directory itself — a unit "
                     "ordered Before=mojo-asgi.service runs before mojo-asgi's "
                     "ExecStartPre has made /opt/api/var")
        th.assert_true(os.path.exists(target), "the config must be installed")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_refuses_on_sha_mismatch_and_keeps_the_old_config(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        target = os.path.join(root, "django.conf")
        _write(target, "SECRET=old\n")
        s3 = _s3_publishing("SECRET=tampered\n", sha256="0" * 64)
        config = {"AWS_CONFIG_BUCKET": "b", "AWS_CONFIG_PREFIX": "p"}

        code = cs.sync(s3, config, target, "django.conf", False)

        th.assert_eq(code, 1, "a sha mismatch must be a refusal, not an install")
        with open(target) as handle:
            th.assert_eq(handle.read(), "SECRET=old\n",
                         "the existing config must be untouched after a refusal")
        th.assert_eq(_staging_leftovers(root), [],
                     "a refusal must not leave a staging directory behind")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_refuses_an_empty_published_config(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        target = os.path.join(root, "django.conf")
        _write(target, "SECRET=old\n")
        s3 = _s3_publishing("")
        config = {"AWS_CONFIG_BUCKET": "b", "AWS_CONFIG_PREFIX": "p"}

        code = cs.sync(s3, config, target, "django.conf", False)

        th.assert_eq(code, 1, "an empty published config must be refused")
        with open(target) as handle:
            th.assert_eq(handle.read(), "SECRET=old\n",
                         "an empty publish must not blank the running config")
        th.assert_eq(_staging_leftovers(root), [],
                     "a refusal must not leave a staging directory behind")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_keeps_current_config_when_the_download_fails(opts):
    from botocore.exceptions import ClientError

    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        target = os.path.join(root, "django.conf")
        _write(target, "SECRET=old\n")
        s3 = _s3_publishing("SECRET=new\n")
        s3.download_file.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "GetObject")
        config = {"AWS_CONFIG_BUCKET": "b", "AWS_CONFIG_PREFIX": "p"}

        code = cs.sync(s3, config, target, "django.conf", False)

        th.assert_eq(code, 1, "a failed download must report failure")
        with open(target) as handle:
            th.assert_eq(handle.read(), "SECRET=old\n",
                         "a node with stale config serves; a node with no "
                         "config does not start — never replace with nothing")
        th.assert_eq(_staging_leftovers(root), [],
                     "a failed download must not leave a staging directory")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_is_a_noop_when_the_local_file_already_matches(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        target = os.path.join(root, "django.conf")
        payload = "SECRET=same\n"
        _write(target, payload)
        s3 = _s3_publishing(payload)
        config = {"AWS_CONFIG_BUCKET": "b", "AWS_CONFIG_PREFIX": "p"}

        code = cs.sync(s3, config, target, "django.conf", False)

        th.assert_eq(code, 0, "an unchanged config must be a clean no-op")
        th.assert_eq(s3.download_file.call_count, 0,
                     "the head_object sha comparison must short-circuit the "
                     "download entirely, or every timer tick pulls the object")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_dry_run_writes_nothing(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        target = os.path.join(root, "django.conf")
        _write(target, "SECRET=old\n")
        s3 = _s3_publishing("SECRET=new\n")
        config = {"AWS_CONFIG_BUCKET": "b", "AWS_CONFIG_PREFIX": "p"}

        code = cs.sync(s3, config, target, "django.conf", True)

        th.assert_eq(code, 0, "a dry run must report success")
        with open(target) as handle:
            th.assert_eq(handle.read(), "SECRET=old\n",
                         "--dry-run must not modify the installed config")
        th.assert_eq(_staging_leftovers(root), [],
                     "a dry run must not leave a staging directory behind")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_warns_when_no_sha_is_published(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        target = os.path.join(root, "django.conf")
        s3 = _s3_publishing("SECRET=unverified\n", sha256=False)
        config = {"AWS_CONFIG_BUCKET": "b", "AWS_CONFIG_PREFIX": "p"}

        with mock.patch.object(cs, "log") as log:
            code = cs.sync(s3, config, target, "django.conf", False)

        th.assert_eq(code, 0,
                     "an object with no sha256 metadata still installs by "
                     "default — being strict would break every publisher using "
                     "a plain `aws s3 cp` on an unpinned upgrade")
        warnings = " ".join(str(c) for c in log.warning.call_args_list)
        th.assert_in("no sha256 metadata", warnings,
                     f"an unverifiable install must warn; logged: {warnings}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_refuses_unverified_when_require_sha_is_set(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        target = os.path.join(root, "django.conf")
        _write(target, "SECRET=old\n")
        s3 = _s3_publishing("SECRET=unverified\n", sha256=False)
        config = {"AWS_CONFIG_BUCKET": "b", "AWS_CONFIG_PREFIX": "p",
                  "CONFIG_SYNC_REQUIRE_SHA": "true"}

        code = cs.sync(s3, config, target, "django.conf", False)

        th.assert_eq(code, 1,
                     "CONFIG_SYNC_REQUIRE_SHA must turn a missing sha256 into "
                     "a refusal")
        th.assert_eq(s3.download_file.call_count, 0,
                     "the refusal must happen before the object is downloaded")
        with open(target) as handle:
            th.assert_eq(handle.read(), "SECRET=old\n",
                         "the existing config must survive the refusal")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_sync_sweeps_stale_staging_directories(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        target = os.path.join(root, "django.conf")
        stale = os.path.join(root, "config_sync.deadbeef")
        os.makedirs(stale)
        _write(os.path.join(stale, "download"), "half a config")

        s3 = _s3_publishing("SECRET=new\n")
        config = {"AWS_CONFIG_BUCKET": "b", "AWS_CONFIG_PREFIX": "p"}

        code = cs.sync(s3, config, target, "django.conf", False)

        th.assert_eq(code, 0, "the sync itself must still succeed")
        th.assert_true(not os.path.exists(stale),
                       "a staging directory left by a SIGKILLed run must be "
                       "swept — no `finally` ever ran to remove it")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_restart_uses_the_configured_service_name(opts):
    from mojo.deploy import config_sync as cs

    fake_subprocess = mock.Mock()
    fake_subprocess.run.return_value = mock.Mock(returncode=0)
    with mock.patch.object(cs, "subprocess", fake_subprocess), \
            mock.patch.object(cs, "time", mock.Mock()):
        ok = cs.restart_app({"CONFIG_SYNC_SERVICE": "acme-api.service"}, False)

    th.assert_true(ok, "a returncode of 0 must be reported as success")
    command = fake_subprocess.run.call_args[0][0]
    th.assert_eq(command, ["systemctl", "restart", "acme-api.service"],
                 f"CONFIG_SYNC_SERVICE must pick the unit, got {command}")


@th.django_unit_test()
def test_restart_defaults_to_mojo_asgi(opts):
    from mojo.deploy import config_sync as cs

    fake_subprocess = mock.Mock()
    fake_subprocess.run.return_value = mock.Mock(returncode=0)
    with mock.patch.object(cs, "subprocess", fake_subprocess), \
            mock.patch.object(cs, "time", mock.Mock()):
        cs.restart_app({}, False)

    command = fake_subprocess.run.call_args[0][0]
    th.assert_eq(command, ["systemctl", "restart", cs.DEFAULT_SERVICE],
                 f"an unset CONFIG_SYNC_SERVICE must fall back to "
                 f"{cs.DEFAULT_SERVICE}, got {command}")


@th.django_unit_test()
def test_restart_dry_run_runs_nothing(opts):
    from mojo.deploy import config_sync as cs

    fake_subprocess = mock.Mock()
    fake_time = mock.Mock()
    with mock.patch.object(cs, "subprocess", fake_subprocess), \
            mock.patch.object(cs, "time", fake_time):
        ok = cs.restart_app({}, True)

    th.assert_true(ok, "a dry-run restart must report success")
    th.assert_eq(fake_subprocess.run.call_count, 0,
                 "--dry-run must not run systemctl")
    th.assert_eq(fake_time.sleep.call_count, 0,
                 "--dry-run must not burn the jitter delay")


@th.django_unit_test()
def test_sync_restarts_only_when_config_sync_restart_is_set(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        target = os.path.join(root, "django.conf")
        base = {"AWS_CONFIG_BUCKET": "b", "AWS_CONFIG_PREFIX": "p"}

        with mock.patch.object(cs, "restart_app", return_value=True) as restart:
            cs.sync(_s3_publishing("A=1\n"), dict(base), target,
                    "django.conf", False)
        th.assert_eq(restart.call_count, 0,
                     "without CONFIG_SYNC_RESTART the app must be left alone")

        with mock.patch.object(cs, "restart_app", return_value=True) as restart:
            config = dict(base, CONFIG_SYNC_RESTART="yes",
                          CONFIG_SYNC_SERVICE="acme-api.service")
            cs.sync(_s3_publishing("A=2\n"), config, target,
                    "django.conf", False)
        th.assert_eq(restart.call_count, 1,
                     "CONFIG_SYNC_RESTART must trigger exactly one restart")
        th.assert_eq(restart.call_args[0][0].get("CONFIG_SYNC_SERVICE"),
                     "acme-api.service",
                     "restart_app must receive the config so it can read the "
                     "configured service name")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_main_exits_zero_when_unconfigured(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        path = os.path.join(root, "bootstrap.conf")
        _write(path, "# nothing here\n")
        code = cs.main(["--config", path, "--target",
                        os.path.join(root, "django.conf")])
        th.assert_eq(code, 0,
                     "an unconfigured box must exit 0 so this can ship dormant "
                     "in a base image that still bakes its config in")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_main_refuses_a_bucket_without_a_prefix(opts):
    from mojo.deploy import config_sync as cs

    root = _tempdir()
    try:
        path = os.path.join(root, "bootstrap.conf")
        _write(path, "AWS_CONFIG_BUCKET=cfg-bucket\n")
        code = cs.main(["--config", path, "--target",
                        os.path.join(root, "django.conf")])
        th.assert_eq(code, 1,
                     "a bucket with no prefix must be refused rather than "
                     "guessing which environment's config to pull")
    finally:
        shutil.rmtree(root, ignore_errors=True)
