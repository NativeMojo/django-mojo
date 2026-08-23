"""Push a finished test run to maestro.

**On when — and only when — the machine already has maestro installed.** The
url, the api key and the project id are all discovered (see the discovery
section below), so a developer who can read the board through the maestro MCP
gets test reporting with nothing to configure. A machine with no maestro finds
nothing, says nothing and reports nothing, which is what keeps a public
framework from phoning home.

Turn it off with `--no-maestro` for one run, or `maestro: false` in a testit
config file for good.

Not every run is reported: maestro answers "is this project green?" from the
latest push per suite, so a run that is not the whole suite's verdict would
overwrite a real result with a partial one. `runner._push_refused` is the
gate — targeted (`-t`), `--ignore`d and aborted runs never push.

A push failure never changes the test run's exit code — every public function
here swallows everything it can raise and degrades to one printed warning.

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


# ----------------------------------------------------------------------
# Zero-setup discovery
#
# Asking a developer to paste an API key into `.env` was asking for a second
# copy of a credential the machine already holds: anyone who can read a board
# through the maestro MCP has already installed a key and a server url, and a
# repo on that board already records its project id in `.claude/maestro.json`.
# So we read all three rather than demand them again.
#
# This is also STRICTLY safer than the `MAESTRO_API_KEY` environment variable
# it replaces. `bin/run_tests` sources `.env` with `set -a`, so an ambient key
# could pair with an unrelated `MAESTRO_URL` and post results to a host that
# never issued it. Here the url and the key come from the same record — the key
# can only travel to the server it was installed for.
# ----------------------------------------------------------------------

# Both files a client may keep MCP server definitions in. Read-only, and only
# ever from the user's own home.
MCP_CONFIG_PATHS = ("~/.claude.json", "~/.claude/settings.json")
REPO_CONFIG_NAME = os.path.join(".claude", "maestro.json")


def _walk_up(start):
    current = os.path.realpath(str(start))
    while True:
        yield current
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def _load_json(path):
    """Parse a JSON object, or None for anything at all going wrong.

    Missing, unreadable and malformed are the same outcome on purpose: none of
    them is worth a warning, because the overwhelmingly common case is a
    machine that simply has no maestro installed.
    """
    try:
        with open(os.path.expanduser(str(path)), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _repo_config(start=None):
    """`.claude/maestro.json` from the nearest ancestor holding one.

    Searched from the working directory first and `paths.PROJECT_ROOT` second,
    because a testit run's cwd may be the repo or the generated testproject
    inside it, and downstream repos import this module from site-packages where
    `__file__` says nothing about whose repo is being tested.
    """
    bases = [start or os.getcwd()]
    root = getattr(paths, "PROJECT_ROOT", None)
    if root:
        bases.append(root)
    for base in bases:
        for candidate in _walk_up(base):
            data = _load_json(os.path.join(candidate, REPO_CONFIG_NAME))
            if data:
                return data
    return {}


def _split_connector_url(url):
    """(base_url, key) for an MCP server url.

    A connector url carries the key inline as `<server>/mcp/k/<key>` and IS the
    credential; the plain `<server>/mcp` form pairs with an auth header. Both
    reduce to the same api base.
    """
    marker = "/mcp/k/"
    if marker in url:
        base, _, key = url.partition(marker)
        return base.rstrip("/"), (key.strip("/") or None)
    base = url.rstrip("/")
    if base.endswith("/mcp"):
        base = base[:-len("/mcp")]
    return base.rstrip("/"), None


def auth_scheme(key, declared=None):
    """The `Authorization` scheme this credential authenticates under.

    mojo's auth middleware routes on the scheme, and the two it ships are NOT
    interchangeable: `apikey` goes to `ApiKey.validate_token`, `Bearer` to
    `User.validate_jwt`. Sending a credential under the wrong one is a flat 401.

    maestro issues its `user_api_key` as a **JWT** — long-lived (10 years), but
    a JWT, so it authenticates as `Bearer` even though every human calls it "the
    api key". Guessing `apikey` from the name is exactly the mistake that made
    the first working discovery still fail to push.

    A scheme declared by the carrier wins; otherwise the token's own shape
    decides, which is unambiguous — a JWT is three dot-separated base64url
    segments and nothing else here looks like that.
    """
    if declared:
        return declared
    if key and key.count(".") == 2 and key.startswith("eyJ"):
        return "Bearer"
    return "apikey"


def _server_credentials(cfg):
    """(url, key, scheme) out of one MCP server definition, any carrier.

    `scheme` is None unless the config stated one, leaving the choice to
    `auth_scheme`.
    """
    if not isinstance(cfg, dict):
        return None, None, None

    url, key, scheme = None, None, None
    raw = cfg.get("url")
    if isinstance(raw, str) and raw:
        url, key = _split_connector_url(raw)

    headers = cfg.get("headers")
    if key is None and isinstance(headers, dict):
        for name, value in headers.items():
            if str(name).lower() != "authorization" or not isinstance(value, str):
                continue
            # "Bearer <key>" states its scheme; a bare key does not.
            parts = value.split(None, 1)
            if len(parts) == 2:
                scheme, key = parts[0].strip(), parts[1].strip()
            else:
                key = parts[0].strip()
            break

    env = cfg.get("env")
    if key is None and isinstance(env, dict):
        candidate = env.get("MAESTRO_API_KEY")
        if isinstance(candidate, str) and candidate:
            key = candidate

    return url or None, key or None, scheme or None


def _mcp_credentials():
    """(url, key, scheme) from the maestro MCP server this machine already has.

    Only a server whose definition yields both a url and a key is accepted — a
    half-configured entry must not shadow a complete one further down.
    """
    for path in MCP_CONFIG_PATHS:
        data = _load_json(path)
        if not data:
            continue

        blocks = [data.get("mcpServers")]
        # Claude Code also scopes servers per project directory.
        projects = data.get("projects")
        if isinstance(projects, dict):
            blocks.extend(entry.get("mcpServers") for entry in projects.values()
                          if isinstance(entry, dict))

        for block in blocks:
            if not isinstance(block, dict):
                continue
            for name, cfg in block.items():
                if "maestro" not in str(name).lower():
                    continue
                url, key, scheme = _server_credentials(cfg)
                if url and key:
                    return url, key, scheme
    return None, None, None


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

        raw = (getattr(opts, "config_data", None) or {}).get("maestro")
        # `maestro: false` in a config file is the permanent opt-out for a
        # project that has the MCP installed but never wants runs reported.
        if raw is False:
            return None
        block = raw if isinstance(raw, dict) else {}
        explicit = bool(getattr(opts, "maestro", False))

        # Explicit configuration always wins, so CI can point a run at a
        # different server than the developer's own MCP install.
        found_url, found_key, found_scheme = _mcp_credentials()
        url = _first(os.environ.get("MAESTRO_URL"), block.get("url"), found_url)
        # The key is deliberately never read from the config block: that file
        # is committed, and a credential in it would be a credential in git.
        env_key = os.environ.get("MAESTRO_API_KEY")
        key = _first(env_key, found_key)
        # An env key is its own credential and carries none of the MCP entry's
        # scheme, so only let the discovered scheme travel with the discovered key.
        scheme = auth_scheme(key, None if env_key else found_scheme)

        if not url or not key:
            # Nothing found and nothing asked for is the ordinary state of a
            # machine with no maestro — say nothing. Only a run that asked to
            # report gets told why it did not.
            if explicit:
                missing = "url" if not url else "api key"
                _warn(f"--maestro was passed but no {missing} could be found "
                      f"(install the maestro MCP, or set MAESTRO_URL / MAESTRO_API_KEY)")
            return None

        url = _check_url(url)
        if not url:
            return None

        project = _first(os.environ.get("MAESTRO_PROJECT"), block.get("project"),
                         _repo_config().get("project"))
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

        # Each tier preset is a different question — core / framework / all
        # select different sets — so each reports as its own suite. Sharing one
        # would let a later core run report green over a red extended module.
        # (maestro #2790; the historical --all==="full" name is preserved.)
        suite = _first(os.environ.get("MAESTRO_SUITE"), block.get("suite"))
        if suite is None:
            preset = getattr(opts, "selected_preset", None)
            # Lazy import avoids a runner<->maestro cycle at module load.
            from testit.runner import DEFAULT_PRESET
            if getattr(opts, "all", False):
                suite = "full"
            elif preset and preset != DEFAULT_PRESET:
                suite = preset

        return objict(
            url=url.rstrip("/"),
            key=key,
            scheme=scheme,
            project=project,
            suite=suite,
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
            # See auth_scheme: mojo routes on the scheme, and the wrong one is
            # a flat 401. `.get` keeps a hand-built settings object working.
            "Authorization": f"{settings.get('scheme') or 'apikey'} {settings.key}",
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
