"""Push a finished test run to maestro.

Off unless a project explicitly turns it on: django-mojo is a public framework
and a test runner that phones home by default is unacceptable. A push failure
never changes the test run's exit code — every public function here swallows
everything it can raise and degrades to one printed warning.

The wire format is maestro's published Test Run Spec v1
(`docs/web_developer/maestro/TestRuns.md` in the maestro repo). testit's own
report uses older names (`modules` / `module` / `test_file`); this module emits
the canonical `suites` / `suite` / `file`.
"""
import os
import json
import ipaddress
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from objict import objict

from mojo.helpers import paths

SCHEMA = "maestro.testrun/1"
DEFAULT_TIMEOUT = 5.0

# Match the spec's storage caps — sending more than maestro keeps is waste, and
# an oversized body is a 400 that looks like a bug.
MAX_FAILURES = 50
TRACEBACK_MAX = 2000
ASSERTION_MAX = 500
TEXT_MAX = 300
SUITES_MAX = 300

# Per-failure keys we forward, and the cap each one gets. Anything not listed
# is dropped: `test_source` is the test's own source text, and
# `server_log_tail` is 20 lines of the server error log, which may carry
# credentials or customer data and which maestro ignores anyway.
FAILURE_FIELDS = (
    ("test_name", TEXT_MAX),
    ("suite", TEXT_MAX),
    ("file", TEXT_MAX),
    ("function", TEXT_MAX),
    ("status", TEXT_MAX),
    ("assertion", ASSERTION_MAX),
    ("traceback", TRACEBACK_MAX),
)

SUITE_STAT_FIELDS = ("tests", "passed", "failed", "skipped", "duration", "skipped_reason")


def _warn(message):
    print(f"\n  Maestro: {message}")


def _is_loopback(host):
    """True for localhost / 127.0.0.0/8 / ::1 — decided without a DNS lookup.

    Exact equality on the name, never a suffix test: `localhost.attacker.com`
    is not loopback.
    """
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_url(url):
    """Return the url if it is safe to send a bearer key to, else None."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        _warn(f"url must be http or https, got {parsed.scheme or 'no scheme'!r} — not pushing")
        return None
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        _warn(f"refusing to send an API key in cleartext to {parsed.hostname} — use https")
        return None
    return url


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def setup(opts):
    """Resolve reporter settings, or None when the reporter is off.

    Call this EARLY in a run: a misconfiguration should be reported before the
    suite runs rather than ten minutes later, and enabling the reporter has to
    imply agent mode before any test executes (that is what collects a
    failure's file, line and traceback).

    Never raises.
    """
    try:
        if getattr(opts, "no_maestro", False):
            return None

        block = getattr(opts, "config_data", None) or {}
        block = block.get("maestro")
        # An empty dict or `false` is how someone writes "off" — honour that.
        if not isinstance(block, dict) or not block:
            block = None
        explicit = bool(getattr(opts, "maestro", False))
        if not explicit and block is None:
            return None

        block = block or {}
        url = _first(os.environ.get("MAESTRO_URL"), block.get("url"))
        key = os.environ.get("MAESTRO_API_KEY")

        if not url:
            _warn("reporting is enabled but no url is configured (MAESTRO_URL or config maestro.url)")
            return None
        url = _check_url(url)
        if not url:
            return None
        if not key:
            # Silent for a config-block enable: "CI has the key, my laptop does
            # not" is the steady state and must not nag every local run.
            if explicit:
                _warn("--maestro was passed but MAESTRO_API_KEY is not set — not pushing")
            return None

        project = _first(os.environ.get("MAESTRO_PROJECT"), block.get("project"))
        if project is not None:
            try:
                project = int(project)
            except (TypeError, ValueError):
                _warn(f"ignoring non-integer project {project!r} — the API key carries the project")
                project = None

        timeout = _first(os.environ.get("MAESTRO_TIMEOUT"), block.get("timeout"), DEFAULT_TIMEOUT)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT

        return objict(
            url=url.rstrip("/"),
            key=key,
            project=project,
            suite=_first(os.environ.get("MAESTRO_SUITE"), block.get("suite")),
            version=_first(os.environ.get("MAESTRO_VERSION"), block.get("version")),
            timeout=timeout,
            diagnostics=block.get("diagnostics", True) is not False,
        )
    except (Exception, KeyboardInterrupt) as err:
        _warn(f"could not read reporting settings ({err}) — not pushing")
        return None


def _ci_context():
    """(source, ref) from the CI environment. GitHub Actions gets a run_url."""
    ref = {}
    source = "ci" if os.environ.get("CI") else "local"

    if os.environ.get("GITHUB_ACTIONS"):
        source = "ci"
        branch = os.environ.get("GITHUB_REF_NAME")
        commit = os.environ.get("GITHUB_SHA")
        if branch:
            ref["branch"] = branch
        if commit:
            ref["commit"] = commit
        server = os.environ.get("GITHUB_SERVER_URL")
        repo = os.environ.get("GITHUB_REPOSITORY")
        run_id = os.environ.get("GITHUB_RUN_ID")
        if server and repo and run_id:
            ref["run_url"] = f"{server}/{repo}/actions/runs/{run_id}"

    return source, ref


def _git(*args):
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(paths.PROJECT_ROOT),
            capture_output=True,
            timeout=2,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.decode("utf-8", errors="replace").strip()
        return value or None
    except Exception:
        return None


def _git_ref():
    """branch/commit from git, for whatever CI did not already supply."""
    ref = {}
    commit = _git("rev-parse", "--short", "HEAD")
    if commit:
        ref["commit"] = commit
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    # A detached checkout reports "HEAD", which is worse than saying nothing.
    if branch and branch != "HEAD":
        ref["branch"] = branch
    return ref


def _clean(value, limit):
    """A capped string, or None when there is nothing worth sending.

    Every per-failure field can legitimately be None — an empty assertion
    message, a test whose source could not be located, a record with no active
    test — so this is what keeps a cap from raising on one.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value[:limit]
    return value or None


def _put(target, key, value):
    """Assign only when there is a value. maestro reads a missing key and a
    null identically; we send the smaller of the two."""
    if value is not None:
        target[key] = value


def _failure_entry(record):
    """One testit failure record in canonical Test Run Spec v1 shape."""
    entry = {}
    source = {
        "test_name": record.get("test_name"),
        "suite": record.get("module"),
        # `file_path` is assigned unconditionally by the report builder and is
        # frequently None, so it must not shadow a usable `test_file`.
        "file": record.get("file_path") or record.get("test_file"),
        "function": record.get("function"),
        "status": record.get("status"),
        "assertion": record.get("assertion"),
        "traceback": record.get("traceback"),
    }
    for name, limit in FAILURE_FIELDS:
        _put(entry, name, _clean(source.get(name), limit))

    line = record.get("line")
    if isinstance(line, int):
        entry["line"] = line
    return entry


def _suites(modules):
    suites = {}
    for name, stats in list(modules.items())[:SUITES_MAX]:
        if not isinstance(stats, dict):
            continue
        entry = {}
        for field in SUITE_STAT_FIELDS:
            value = stats.get(field)
            if value is None:
                continue
            if field == "skipped_reason":
                _put(entry, field, _clean(value, TEXT_MAX))
            else:
                entry[field] = value
        suites[_clean(name, TEXT_MAX) or "unknown"] = entry
    return suites


def build_payload(report, settings):
    """Translate testit's report into a Test Run Spec v1 body.

    Never mutates `report` — the file on disk and the wire copy are separate.
    """
    source, ref = _ci_context()
    for key, value in _git_ref().items():
        # CI wins: it knows the real branch of a detached checkout.
        ref.setdefault(key, value)

    payload = {
        "schema": SCHEMA,
        "status": report.get("status"),
        "total": report.get("total"),
        "passed": report.get("passed"),
        "failed": report.get("failed"),
        "source": source,
    }
    _put(payload, "project", settings.project)
    _put(payload, "suite", _clean(settings.suite, TEXT_MAX))
    _put(payload, "version", _clean(settings.version, TEXT_MAX))
    _put(payload, "skipped", report.get("skipped"))
    _put(payload, "duration", report.get("duration"))

    started = report.get("started_at")
    if started:
        payload["started"] = datetime.fromtimestamp(
            started, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    runner_version = None
    try:
        import mojo
        runner_version = getattr(mojo, "__version__", None)
    except Exception:
        pass
    payload["runner"] = {"name": "testit"}
    _put(payload["runner"], "version", runner_version)

    if ref:
        payload["ref"] = ref

    modules = report.get("modules")
    if isinstance(modules, dict):
        payload["suites"] = _suites(modules)

    if settings.diagnostics:
        failures = report.get("failures") or []
        payload["failures"] = [_failure_entry(f) for f in failures[:MAX_FAILURES]]

    return payload


def push(settings, payload):
    """POST the payload. Returns True only on a 2xx."""
    response = requests.post(
        f"{settings.url}/api/maestro/testrun",
        data=json.dumps(payload, default=str),
        headers={
            "Authorization": f"apikey {settings.key}",
            "Content-Type": "application/json",
        },
        timeout=settings.timeout,
        # Never follow a redirect: it could carry the key to another host.
        allow_redirects=False,
    )
    if not 200 <= response.status_code < 300:
        body = (response.text or "")[:200]
        _warn(f"push refused with HTTP {response.status_code} {body}".rstrip())
        return False

    suite = payload.get("suite") or "default"
    note = ""
    try:
        data = response.json().get("data") or {}
        if data.get("failures_truncated"):
            note = " (failure list truncated by the server)"
    except Exception:
        pass
    _warn(f"reported {payload.get('total')} tests ({payload.get('status')}) "
          f"as suite '{suite}'{note}")
    return True


def report_run(settings, build_report):
    """Build and push one run. Never raises, whatever goes wrong.

    `build_report` is a zero-argument callable so that building the report
    happens inside this guard too — the exit code of a test run must not depend
    on any of it.
    """
    try:
        report = build_report()
        if not report:
            return False
        return push(settings, build_payload(report, settings))
    except (Exception, KeyboardInterrupt) as err:
        _warn(f"push failed ({type(err).__name__}: {err}) — test results are unaffected")
        return False
