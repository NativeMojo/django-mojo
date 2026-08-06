"""Tests for the maestro test-run reporter (testit/maestro.py).

Pure in-process tests — no database, no dev-server traffic, no real HTTP. Every
test restores the environment it touched, because the reporter reads process
environment and a leaked MAESTRO_* would change how later modules behave.
"""
import os
from testit import helpers as th

_ENV_KEYS = (
    "MAESTRO_URL", "MAESTRO_API_KEY", "MAESTRO_PROJECT", "MAESTRO_SUITE",
    "MAESTRO_VERSION", "MAESTRO_TIMEOUT", "CI", "GITHUB_ACTIONS",
    "GITHUB_REF_NAME", "GITHUB_SHA", "GITHUB_SERVER_URL", "GITHUB_REPOSITORY",
    "GITHUB_RUN_ID",
)


class _Env:
    """Set/clear MAESTRO_* and CI vars, restoring every one on exit."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for key in _ENV_KEYS:
            self.saved[key] = os.environ.get(key)
            os.environ.pop(key, None)
        for key, value in self.values.items():
            if value is not None:
                os.environ[key] = str(value)
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def _opts(**kwargs):
    from objict import objict
    base = dict(maestro=False, no_maestro=False, config_data={})
    base.update(kwargs)
    return objict(base)


def _settings(**kwargs):
    from objict import objict
    base = dict(url="https://m.example.com", key="secret-key", project=None,
                suite=None, version=None, timeout=5.0, diagnostics=True)
    base.update(kwargs)
    return objict(base)


def _report(**kwargs):
    """A report shaped exactly like runner._build_agent_report's output."""
    base = {
        "status": "passed", "total": 10, "passed": 9, "failed": 0, "skipped": 1,
        "ran": {"total": 10, "passed": 9, "failed": 0, "skipped": 1},
        "duration": 12.5,
        "started_at": 1754500000.0,
        "modules": {"test_alpha": {"tests": 10, "passed": 9, "failed": 0,
                                   "skipped": 1, "duration": 12.5}},
        "slowest": [{"test_name": "slow_one", "duration": 9.5}],
        "failures": [],
        "conf_drift": [],
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# The enable gate
# ---------------------------------------------------------------------------
@th.unit_test("maestro reporter: environment variables alone never enable it")
def test_env_alone_does_not_enable(opts):
    """The one that matters most: django-mojo is a public framework, and
    bin/run_tests sources .env with `set -a`, so a developer may well already
    have MAESTRO_API_KEY exported for the MCP server. Inheriting it must never
    start reporting test results."""
    from testit import maestro

    with _Env(MAESTRO_URL="https://m.example.com", MAESTRO_API_KEY="k",
              MAESTRO_PROJECT="12"):
        settings = maestro.setup(_opts())

    assert settings is None, (
        "a fully populated environment with no explicit --maestro and no config "
        f"block must leave the reporter off, got {settings!r}"
    )


@th.unit_test("maestro reporter: --maestro and a config block each enable it")
def test_enable_paths(opts):
    from testit import maestro

    with _Env(MAESTRO_URL="https://m.example.com", MAESTRO_API_KEY="k", MAESTRO_PROJECT="12"):
        by_flag = maestro.setup(_opts(maestro=True))
        by_config = maestro.setup(_opts(config_data={"maestro": {"suite": "api"}}))

    assert by_flag is not None, "--maestro plus url and key should enable the reporter"
    assert by_flag.project == 12, f"project should parse to an int, got {by_flag.project!r}"
    assert by_config is not None, "a maestro config block plus url and key should enable it"
    assert by_config.suite == "api", f"suite should come from the config block, got {by_config.suite!r}"


@th.unit_test("maestro reporter: url and key alone enable it, with no project")
def test_enables_without_project(opts):
    """A project-scoped API key already carries the project, and Test Run Spec
    v1 says the field is normally omitted — requiring it would make the simplest
    correct CI setup look like a misconfiguration."""
    from testit import maestro

    with _Env(MAESTRO_URL="https://m.example.com", MAESTRO_API_KEY="k"):
        settings = maestro.setup(_opts(maestro=True))

    assert settings is not None, "url plus key with no project must enable the reporter"
    assert settings.project is None, (
        f"project must stay unset so the key decides, got {settings.project!r}"
    )


@th.unit_test("maestro reporter: an empty or false config block means off")
def test_empty_block_is_off(opts):
    from testit import maestro

    with _Env(MAESTRO_URL="https://m.example.com", MAESTRO_API_KEY="k"):
        empty = maestro.setup(_opts(config_data={"maestro": {}}))
        disabled = maestro.setup(_opts(config_data={"maestro": False}))

    assert empty is None, f'"maestro": {{}} must read as off, got {empty!r}'
    assert disabled is None, f'"maestro": false must read as off, got {disabled!r}'


@th.unit_test("maestro reporter: --no-maestro overrides every enable path")
def test_no_maestro_wins(opts):
    from testit import maestro

    with _Env(MAESTRO_URL="https://m.example.com", MAESTRO_API_KEY="k"):
        settings = maestro.setup(_opts(maestro=True, no_maestro=True,
                                       config_data={"maestro": {"suite": "api"}}))

    assert settings is None, f"--no-maestro must win over both enables, got {settings!r}"


@th.unit_test("maestro reporter: env values override the config block")
def test_env_overrides_config(opts):
    from testit import maestro

    with _Env(MAESTRO_URL="https://env.example.com", MAESTRO_API_KEY="k",
              MAESTRO_SUITE="from-env"):
        settings = maestro.setup(_opts(config_data={
            "maestro": {"url": "https://config.example.com", "suite": "from-config"}}))

    assert settings.url == "https://env.example.com", (
        f"MAESTRO_URL should win over the config block, got {settings.url!r}")
    assert settings.suite == "from-env", (
        f"MAESTRO_SUITE should win over the config block, got {settings.suite!r}")


@th.unit_test("maestro reporter: a missing key is silent by config, warned by flag")
def test_missing_key(opts):
    """CI has the key and a laptop does not — that is the steady state for a
    project with a config block, and it must not nag on every local run. Asking
    for it explicitly is different: say why nothing happened."""
    from testit import maestro

    with _Env(MAESTRO_URL="https://m.example.com"):
        by_config = maestro.setup(_opts(config_data={"maestro": {"url": "https://m.example.com"}}))
        by_flag = maestro.setup(_opts(maestro=True))

    assert by_config is None, "a config-block enable with no key must not report"
    assert by_flag is None, "--maestro with no key must not report"


@th.unit_test("maestro reporter: a bearer key never crosses a network in cleartext")
def test_url_safety(opts):
    from testit import maestro

    refused = ("http://maestro.example.com", "ftp://m.example.com",
               "file:///etc/passwd", "http://localhost.attacker.com")
    accepted = ("http://127.0.0.1:8000", "http://localhost:9000",
                "https://maestro.example.com")

    for url in refused:
        with _Env(MAESTRO_URL=url, MAESTRO_API_KEY="k"):
            settings = maestro.setup(_opts(maestro=True))
        assert settings is None, f"{url} must be refused, got {settings!r}"

    for url in accepted:
        with _Env(MAESTRO_URL=url, MAESTRO_API_KEY="k"):
            settings = maestro.setup(_opts(maestro=True))
        assert settings is not None, f"{url} should be accepted, got None"


@th.unit_test("maestro reporter: a non-integer project is a warning, not a crash")
def test_bad_project_degrades(opts):
    from testit import maestro

    with _Env(MAESTRO_URL="https://m.example.com", MAESTRO_API_KEY="k",
              MAESTRO_PROJECT="not-a-number"):
        settings = maestro.setup(_opts(maestro=True))

    assert settings is not None, "a bad project must not disable reporting — the key carries it"
    assert settings.project is None, f"the bad value must be dropped, got {settings.project!r}"


@th.unit_test("maestro reporter: setup never raises, whatever opts it is handed")
def test_setup_never_raises(opts):
    from testit import maestro

    class _Hostile:
        @property
        def config_data(self):
            raise RuntimeError("exploding opts")
        maestro = True
        no_maestro = False

    with _Env(MAESTRO_URL="https://m.example.com", MAESTRO_API_KEY="k"):
        settings = maestro.setup(_Hostile())

    assert settings is None, "an opts object that raises must degrade to 'off', not propagate"


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------
@th.unit_test("maestro payload: emits canonical suites/suite/file names")
def test_canonical_names(opts):
    from testit import maestro

    report = _report(status="failed", failed=1, failures=[{
        "test_name": "test_thing", "module": "test_alpha", "test_file": "1_test_thing",
        "function": "test_thing", "status": "failed", "assertion": "expected 200, got 403",
        "file_path": "/repo/tests/test_alpha/1_test_thing.py", "line": 42,
    }])
    with _Env():
        payload = maestro.build_payload(report, _settings())

    assert "suites" in payload, "the per-group block must be sent as 'suites'"
    assert "modules" not in payload, "testit's 'modules' name must not reach the wire"
    failure = payload["failures"][0]
    assert failure["suite"] == "test_alpha", f"'module' must be sent as 'suite', got {failure!r}"
    assert failure["file"] == "/repo/tests/test_alpha/1_test_thing.py", (
        f"file_path must be sent as 'file', got {failure.get('file')!r}")
    for gone in ("module", "test_file", "file_path"):
        assert gone not in failure, f"{gone} is not a Test Run Spec v1 field, got {failure!r}"


@th.unit_test("maestro payload: None-valued failure fields are omitted, not sent or crashed on")
def test_none_fields_are_dropped(opts):
    """Regression-grade. The report builder assigns assertion/file_path/line
    unconditionally from .get(), so all three are None in ordinary runs — a
    failure with an empty message, or a test whose source could not be located.
    Capping a None raises, and the outer guard would swallow that into 'the
    reporter silently never pushes'."""
    from testit import maestro

    report = _report(status="failed", failed=1, failures=[{
        "test_name": "test_thing", "module": None, "test_file": "1_test_thing",
        "function": None, "status": "failed", "assertion": None,
        "file_path": None, "line": None,
    }])
    with _Env():
        payload = maestro.build_payload(report, _settings())

    failure = payload["failures"][0]
    for absent in ("assertion", "line", "suite", "function"):
        assert absent not in failure, (
            f"a None {absent} must be omitted rather than sent, got {failure!r}")
    assert failure["file"] == "1_test_thing", (
        "a None file_path must fall through to test_file rather than shadowing it, "
        f"got {failure.get('file')!r}")


@th.unit_test("maestro payload: server log tails and test source never leave the machine")
def test_diagnostic_fields_dropped(opts):
    from testit import maestro

    report = _report(status="failed", failed=1, failures=[{
        "test_name": "test_thing", "module": "test_alpha", "status": "failed",
        "assertion": "boom", "test_source": "def test_thing(opts): ...",
        "server_log_tail": "DB_PASSWORD=hunter2 in the log tail",
    }])
    with _Env():
        payload = maestro.build_payload(report, _settings())

    failure = payload["failures"][0]
    assert "server_log_tail" not in failure, (
        f"the server error-log tail must never be sent, got {failure!r}")
    assert "test_source" not in failure, f"test source must never be sent, got {failure!r}"
    for key in ("ran", "slowest", "conf_drift"):
        assert key not in payload, f"{key} is testit-only and must not be sent, got payload[{key!r}]"


@th.unit_test("maestro payload: diagnostics:false sends counts without failure detail")
def test_diagnostics_off(opts):
    from testit import maestro

    report = _report(status="failed", failed=1, failures=[{
        "test_name": "test_thing", "module": "test_alpha", "status": "failed",
        "assertion": "expected token abc123, got xyz",
    }])
    with _Env():
        payload = maestro.build_payload(report, _settings(diagnostics=False))

    assert "failures" not in payload, (
        f"diagnostics:false must omit the failure list entirely, got {payload.get('failures')!r}")
    assert payload["failed"] == 1, "counters must still be reported with diagnostics off"
    assert "suites" in payload, "the per-suite breakdown must still be reported"


@th.unit_test("maestro payload: build_payload does not mutate the report it was given")
def test_payload_does_not_mutate_report(opts):
    from testit import maestro
    import copy

    report = _report(status="failed", failed=1, failures=[{
        "test_name": "t", "module": "test_alpha", "test_file": "f", "status": "failed",
        "assertion": "x" * 900, "server_log_tail": "secret",
    }])
    before = copy.deepcopy(report)
    with _Env():
        maestro.build_payload(report, _settings())

    assert report == before, (
        "the report written to test_failures.json must be untouched by the wire copy")


@th.unit_test("maestro payload: caps match the spec's storage limits")
def test_caps(opts):
    from testit import maestro

    failures = [{"test_name": "t%d" % i, "module": "m", "status": "failed",
                 "assertion": "a" * 900, "traceback": "b" * 5000} for i in range(60)]
    modules = {"m%d" % i: {"tests": 1, "passed": 1, "failed": 0} for i in range(400)}
    report = _report(status="failed", failed=60, failures=failures, modules=modules)
    with _Env():
        payload = maestro.build_payload(report, _settings())

    assert len(payload["failures"]) == 50, (
        f"at most 50 failures are stored, so at most 50 should be sent, got {len(payload['failures'])}")
    assert len(payload["suites"]) == 300, (
        f"the suite map keeps 300 entries, got {len(payload['suites'])}")
    first = payload["failures"][0]
    assert len(first["assertion"]) == 500, (
        f"assertion caps at 500 chars, got {len(first['assertion'])}")
    assert len(first["traceback"]) == 2000, (
        f"traceback caps at 2000 chars, got {len(first['traceback'])}")


@th.unit_test("maestro payload: long free-text fields cap at 300 chars")
def test_text_cap(opts):
    from testit import maestro

    report = _report(status="failed", failed=1, failures=[{
        "test_name": "t" * 900, "module": "m", "status": "failed", "assertion": "x"}])
    with _Env():
        payload = maestro.build_payload(report, _settings())

    assert len(payload["failures"][0]["test_name"]) == 300, (
        f"test_name caps at 300, got {len(payload['failures'][0]['test_name'])}")


@th.unit_test("maestro payload: envelope carries schema, runner and started")
def test_envelope(opts):
    from testit import maestro
    import mojo

    with _Env():
        payload = maestro.build_payload(_report(), _settings(suite="api", version="1.4.2"))

    assert payload["schema"] == "maestro.testrun/1", f"wrong schema: {payload['schema']!r}"
    assert payload["runner"]["name"] == "testit", f"wrong runner name: {payload['runner']!r}"
    assert payload["runner"]["version"] == mojo.__version__, (
        f"runner version should be django-mojo's, got {payload['runner'].get('version')!r}")
    assert payload["started"].endswith("Z"), (
        f"started must be UTC ISO-8601 ending in Z, got {payload['started']!r}")
    assert payload["suite"] == "api", f"suite should be sent when set, got {payload.get('suite')!r}"
    assert payload["version"] == "1.4.2", f"version should be sent when set, got {payload.get('version')!r}"
    assert payload["source"] == "local", f"a non-CI run is 'local', got {payload['source']!r}"
    for reserved in ("group", "group_uuid"):
        assert reserved not in payload, f"{reserved} is reserved by the API framework"


@th.unit_test("maestro payload: unset envelope fields are omitted")
def test_envelope_omits_unset(opts):
    from testit import maestro

    with _Env():
        payload = maestro.build_payload(_report(), _settings())

    for absent in ("project", "suite", "version"):
        assert absent not in payload, (
            f"{absent} must be omitted when unset so the key/server default applies, "
            f"got {payload.get(absent)!r}")


@th.unit_test("maestro payload: CI populates source and a run_url")
def test_ci_context(opts):
    from testit import maestro

    with _Env(CI="1", GITHUB_ACTIONS="true", GITHUB_REF_NAME="main",
              GITHUB_SHA="e6e5b37", GITHUB_SERVER_URL="https://github.com",
              GITHUB_REPOSITORY="mojo/django-mojo", GITHUB_RUN_ID="7"):
        payload = maestro.build_payload(_report(), _settings())

    assert payload["source"] == "ci", f"a CI run must report source 'ci', got {payload['source']!r}"
    ref = payload["ref"]
    assert ref["branch"] == "main", f"branch should come from GITHUB_REF_NAME, got {ref!r}"
    assert ref["commit"] == "e6e5b37", f"commit should come from GITHUB_SHA, got {ref!r}"
    assert ref["run_url"] == "https://github.com/mojo/django-mojo/actions/runs/7", (
        f"run_url should be assembled from the Actions variables, got {ref.get('run_url')!r}")


@th.unit_test("maestro payload: serializes through json.dumps with a str fallback")
def test_payload_serializes(opts):
    """requests' json= kwarg has no default=, unlike the file write this report
    also feeds — a Path or a datetime would raise there and the reporter would
    silently never push while test_failures.json looked perfect."""
    from testit import maestro
    import json
    from pathlib import Path

    report = _report(status="failed", failed=1, failures=[{
        "test_name": "t", "module": "m", "status": "failed",
        "assertion": "x", "line": 1}])
    report["duration"] = Path("/not/a/number")
    with _Env():
        payload = maestro.build_payload(report, _settings())

    body = json.dumps(payload, default=str)
    assert json.loads(body)["duration"] == "/not/a/number", (
        "a non-JSON-native value must serialize via default=str rather than raising")


# ---------------------------------------------------------------------------
# Failure isolation — the guarantee the whole item rests on
# ---------------------------------------------------------------------------
class _Response:
    def __init__(self, status_code, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


@th.unit_test("maestro push: no network failure can raise or change a run's outcome")
def test_push_failures_are_contained(opts):
    from unittest import mock
    from testit import maestro
    import requests

    cases = [
        ("connection error", requests.exceptions.ConnectionError("no route")),
        ("timeout", requests.exceptions.Timeout("timed out")),
        ("ssl error", requests.exceptions.SSLError("bad cert")),
        ("keyboard interrupt", KeyboardInterrupt()),
        ("unexpected error", RuntimeError("something else entirely")),
    ]
    for label, error in cases:
        with mock.patch.object(requests, "post", side_effect=error):
            with _Env():
                result = maestro.report_run(_settings(), lambda: _report())
        assert result is False, f"a {label} must report failure, got {result!r}"

    statuses = [(500, "server error"), (401, "bad key"), (403, "forbidden"),
                (302, "redirect"), (400, "contract drift")]
    for code, label in statuses:
        with mock.patch.object(requests, "post", return_value=_Response(code, "nope")):
            with _Env():
                result = maestro.report_run(_settings(), lambda: _report())
        assert result is False, f"HTTP {code} ({label}) must report failure, got {result!r}"


@th.unit_test("maestro push: a broken report builder cannot escape the guard")
def test_build_failure_is_contained(opts):
    from testit import maestro

    def _explode():
        raise ValueError("report builder is broken")

    with _Env():
        result = maestro.report_run(_settings(), _explode)

    assert result is False, (
        "a failure building the report must degrade to False, never propagate into the "
        f"runner's exit path, got {result!r}")


@th.unit_test("maestro push: a successful post sends the key as a header and JSON as the body")
def test_push_success(opts):
    from unittest import mock
    from testit import maestro
    import json
    import requests

    captured = {}

    def _fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response(200, payload={"data": {"recorded": True}})

    with mock.patch.object(requests, "post", side_effect=_fake_post):
        with _Env():
            result = maestro.report_run(_settings(suite="api"), lambda: _report())

    assert result is True, f"a 200 must report success, got {result!r}"
    assert captured["url"] == "https://m.example.com/api/maestro/testrun", (
        f"wrong endpoint: {captured['url']!r}")
    assert captured["headers"]["Authorization"] == "apikey secret-key", (
        "the key must travel as an apikey Authorization header")
    assert captured["headers"]["Content-Type"] == "application/json", (
        f"wrong content type: {captured['headers'].get('Content-Type')!r}")
    assert captured["allow_redirects"] is False, (
        "redirects must never be followed — they could carry the key to another host")
    body = json.loads(captured["data"])
    assert body["total"] == 10, f"the body must round-trip as JSON, got {body!r}"
    assert body["suite"] == "api", f"the envelope must reach the wire, got {body!r}"


# ---------------------------------------------------------------------------
# Partial runs
# ---------------------------------------------------------------------------
@th.unit_test("maestro reporting: a partial run is never reported as the suite's result")
def test_partial_runs_refused(opts):
    """maestro reads the latest push per suite as the project's status, so a
    filtered or cut-short run would overwrite a real verdict with a fragment."""
    from testit import runner, helpers

    was_aborted = helpers.TEST_RUN.aborted
    try:
        helpers.TEST_RUN.aborted = False
        clean = _opts(test_modules=[], ignore_modules=[])
        assert runner._push_refused(clean) is None, (
            "a complete unfiltered run must be reportable")

        wide = _opts(test_modules=[], ignore_modules=[], extra_list=["slow"], full=True)
        assert runner._push_refused(wide) is None, (
            "--extra/--full is a complete run of a wider tier and must still report")

        filtered = _opts(test_modules=["test_helpers"], ignore_modules=[])
        assert runner._push_refused(filtered) is not None, (
            "a -t run is not the suite's result and must not be reported")

        ignored = _opts(test_modules=[], ignore_modules=["test_aws"])
        assert runner._push_refused(ignored) is not None, (
            "an --ignore run is not the suite's result and must not be reported")

        helpers.TEST_RUN.aborted = True
        assert runner._push_refused(clean) is not None, (
            "a run cut short by stop-on-fail or the abort key must not be reported")
    finally:
        helpers.TEST_RUN.aborted = was_aborted


@th.unit_test("maestro reporting: a stop-on-fail setup error marks the run aborted")
def test_stop_on_fail_marks_aborted(opts):
    """The flag is set where TestitAbort is raised, not where it is caught.

    This exercises the setup-error path deliberately: it records no failure and
    touches no counter, so the probe cannot pollute the run it is part of — and
    it is exactly the case a catch-site flag would miss, since an aborted setup
    leaves `failed` at zero and looks like a clean run.
    """
    from testit import runner, helpers

    class _Module:
        @staticmethod
        def setup_explodes(_opts):
            raise RuntimeError("deliberate setup failure")

    was_aborted = helpers.TEST_RUN.aborted
    before = (helpers.TEST_RUN.total, helpers.TEST_RUN.failed)
    try:
        helpers.TEST_RUN.aborted = False
        stop_opts = _opts(stop=True, verbose=False, errors=False)

        raised = False
        try:
            runner.run_setup(stop_opts, _Module, "setup_explodes", "m", "t")
        except helpers.TestitAbort:
            raised = True

        assert raised, "a setup error under stop-on-fail must raise TestitAbort"
        assert helpers.TEST_RUN.aborted is True, (
            "the raise site must mark the run aborted, or a run cut short during setup "
            "would be reported to maestro as the project's verdict"
        )
        assert (helpers.TEST_RUN.total, helpers.TEST_RUN.failed) == before, (
            "this probe must not disturb the run's own counters, got "
            f"{(helpers.TEST_RUN.total, helpers.TEST_RUN.failed)} vs {before}"
        )
    finally:
        helpers.TEST_RUN.aborted = was_aborted


@th.unit_test("maestro reporting: the agent report still carries its documented shape")
def test_report_shape_unchanged(opts):
    """The reporter split _write_agent_report into build + write. The file it
    writes is what every agent and baseline reads, so its shape is a contract."""
    from testit import runner, helpers

    old_agent = helpers.AGENT_MODE
    helpers.AGENT_MODE = True
    try:
        report = runner._build_agent_report(opts)
    finally:
        helpers.AGENT_MODE = old_agent

    for key in ("status", "total", "passed", "failed", "skipped", "duration",
                "ran", "modules", "slowest", "failures", "conf_drift", "started_at"):
        assert key in report, f"the agent report must still carry {key!r}, got {sorted(report)}"
