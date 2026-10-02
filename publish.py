#!/usr/bin/env python
"""
Release script for django-mojo.

Driven by an agent, never by hand. The division of labour is deliberate:

    the agent   bumps the version in pyproject.toml, mojo/__init__.py and
                uv.lock, and COMMITS that as the release commit
    this script verifies, builds, pushes, publishes to PyPI and tags

So this script never writes to the working tree and never commits. It refuses
to run against a dirty tree, because a release whose source was not committed
first is unreproducible — and a PyPI version number can never be reused.

One file is uploaded: the wheel, built and checked in a private temporary
folder and named to `uv publish` by its path. No source archive is built, and
dist/ in the checkout is neither written nor read (maestro #6348).

It also asks for no input. There are no release notes here: notes belong on the
maestro board, and once maestro's project release notes ship (#1494) this script
will push them from there. See post_release_notes().

Nothing here imports from `mojo`. This script runs before the package is built
and must work with no configured project — the same constraint testit/testenv.py
carries, and for the same reason.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV_FILE = Path(".env")
PYPROJECT_FILE = Path("pyproject.toml")
INIT_FILE = Path("mojo/__init__.py")
LOCK_FILE = Path("uv.lock")

PACKAGE_NAME = "django-mojo"
# Must match wheel_name() in scripts/release_wheel_only.py, which refuses a
# folder holding anything but this one file.
WHEEL_NAME = "django_mojo-{version}-py3-none-any.whl"
PYPI_JSON_URL = "https://pypi.org/pypi/{name}/{version}/json"
PYPI_SIMPLE_URL = "https://pypi.org/simple/{name}/"

# How long to wait, after the upload, for the release to become resolvable by
# the world. Overridable with PUBLISH_VISIBLE_TIMEOUT / PUBLISH_VISIBLE_INTERVAL;
# the default outlasts any normal CDN lag by two orders of magnitude.
VISIBILITY_TIMEOUT = 600.0
VISIBILITY_INTERVAL = 5.0

# What require_release_note returns under --note-by-agent: no note was read,
# and the calling agent owns both the check and the publish.
NOTE_BY_AGENT = "note-by-agent"

# The files the agent is expected to have bumped and committed before calling us.
VERSION_FILES = (PYPROJECT_FILE, INIT_FILE, LOCK_FILE)


class PublishError(Exception):
    """A release precondition failed, or a command did."""
    pass


def load_env(path, environ):
    """Fill `environ` from a .env file, keeping what is already set.

    UV_PUBLISH_TOKEN lives there. Called first in main(), not at import, so
    loading this file as a module changes nothing.
    """
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            environ.setdefault(key.strip(), value.strip())


def say(message, prefix="==> "):
    """Progress output. Plain print, not logging — nothing here needs a logger,
    and `mojo.helpers.logit` is unavailable to a script that cannot import mojo."""
    print(f"{prefix}{message}", flush=True)


def run(argv, dry_run=False, capture=True):
    """Run a command as an argv list — never a shell string.

    argv means no quoting, so a version or a branch name can never be
    reinterpreted by a shell. There is no shell=True anywhere in this file.
    """
    printable = " ".join(argv)
    if dry_run:
        say(f"[dry-run] would run: {printable}")
        return ""

    say(f"running: {printable}")
    try:
        result = subprocess.run(argv, text=True, capture_output=capture, timeout=300)
    except subprocess.TimeoutExpired:
        raise PublishError(f"command timed out: {printable}")
    except FileNotFoundError:
        raise PublishError(f"command not found: {argv[0]}")

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise PublishError(f"command failed: {printable}" + (f"\n{detail}" if detail else ""))

    return (result.stdout or "").strip() if capture else ""


def git(*args, dry_run=False):
    """Read-only git helpers must run even under --dry-run, or the rehearsal
    reports on a state it never looked at. Callers pass dry_run only for the
    commands that change something."""
    return run(["git", *args], dry_run=dry_run)


def validate_environment(args):
    """uv present, pyproject present, and a PyPI token when we intend to upload."""
    run(["uv", "--version"])

    if not PYPROJECT_FILE.exists():
        raise PublishError("pyproject.toml not found — run this from the repo root")

    # Only an actual upload needs the token. Requiring it for --nopypi or
    # --dry-run would block the two modes that exist to be run without one.
    if not args.nopypi and not args.dry_run:
        if not os.environ.get("UV_PUBLISH_TOKEN"):
            raise PublishError("UV_PUBLISH_TOKEN is not set. Add it to your .env file.")


def require_clean_tree():
    """Refuse to release from a tree with uncommitted changes.

    Two reasons, both load-bearing. The release must be reproducible from the
    commit it claims to be — PyPI versions are permanent and cannot be re-cut.
    And this repo runs concurrent agent sessions that stage files at arbitrary
    moments, so anything uncommitted here may not even be the release's work.
    """
    dirty = git("status", "--porcelain")
    if dirty:
        paths = "\n  ".join(dirty.splitlines())
        raise PublishError(
            "the working tree has uncommitted changes; commit the release first:\n"
            f"  {paths}")


def get_current_version():
    """The version from pyproject.toml's [project] table.

    Anchored to the start of a line: an unanchored search would take the first
    `version = "..."` anywhere in the file, including one in a [tool.*] table.
    """
    content = PYPROJECT_FILE.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
    if not match:
        raise PublishError("no version found in pyproject.toml")
    return match.group(1)


def require_version_consistency(version):
    """All three version files must already agree.

    The agent bumps three files by hand; bumping two of them is the obvious way
    for that to go wrong, and it would otherwise be caught only by a user who
    installed the wheel and read __version__.
    """
    init_text = INIT_FILE.read_text(encoding="utf-8")
    init_match = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.MULTILINE)
    if not init_match:
        raise PublishError(f"no __version__ found in {INIT_FILE}")
    if init_match.group(1) != version:
        raise PublishError(
            f"version mismatch: pyproject.toml says {version}, "
            f"{INIT_FILE} says {init_match.group(1)}")

    lock_text = LOCK_FILE.read_text(encoding="utf-8")
    lock_match = re.search(
        r'name = "' + re.escape(PACKAGE_NAME) + r'"\nversion = "([^"]+)"', lock_text)
    if not lock_match:
        raise PublishError(f"no {PACKAGE_NAME} entry found in {LOCK_FILE}")
    if lock_match.group(1) != version:
        raise PublishError(
            f"version mismatch: pyproject.toml says {version}, "
            f"{LOCK_FILE} says {lock_match.group(1)} — run `uv lock` and commit it")

    # Catches lock drift the version line alone would not show (a changed
    # dependency bound). Cheap, and it runs before anything irreversible.
    run(["uv", "lock", "--check"])


def require_unreleased(version):
    """Refuse to re-cut a version that already exists as a tag or on PyPI."""
    tag = f"v{version}"
    existing = git("tag", "--list", tag)
    if existing.strip():
        raise PublishError(
            f"tag {tag} already exists — bump the version before releasing again")

    url = PYPI_JSON_URL.format(name=PACKAGE_NAME, version=version)
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            if response.status == 200:
                raise PublishError(
                    f"{PACKAGE_NAME} {version} is already on PyPI — "
                    "a version can never be re-published, so bump it")
    except urllib.error.HTTPError as err:
        if err.code != 404:
            say(f"warning: could not check PyPI for {version} (HTTP {err.code})")
    except urllib.error.URLError as err:
        # Offline or PyPI unreachable. The upload itself will fail cleanly if
        # the version exists, so this is a courtesy check, not a gate.
        say(f"warning: could not reach PyPI to pre-check {version} ({err.reason})")


# ----------------------------------------------------------------------
# maestro release notes
#
# The note itself is written by the `/maestro-release-note` skill BEFORE this
# script runs — an agent writes prose, a release script cannot. What this file
# owns is the two mechanical halves the skill must not be trusted to remember:
# refusing to publish a version that has no note, and flipping that note live
# once the release actually shipped.
#
# Stdlib only, and no import from `testit.maestro` even though it solves the
# same discovery problem: that module pulls in `requests`, `objict` and
# `mojo.helpers`, none of which exist before the build. The duplication is the
# price of this script standing alone, which the module docstring requires.
# ----------------------------------------------------------------------

MCP_CONFIG_PATHS = ("~/.claude.json", "~/.claude/settings.json")
REPO_CONFIG = Path(".claude") / "maestro.json"
CONNECTOR_MARKER = "/mcp/k/"


def _load_json(path):
    try:
        with open(os.path.expanduser(str(path))) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _split_connector_url(url):
    """(base, key) — a connector url carries its own credential."""
    if CONNECTOR_MARKER in url:
        base, _, key = url.partition(CONNECTOR_MARKER)
        return base.rstrip("/"), (key.strip("/") or None)
    # A connector address may carry a query (e.g. `/mcp?tools=full`); the REST
    # base is the origin path in front of /mcp either way.
    base = urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl().rstrip("/")
    if base.endswith("/mcp"):
        base = base[:-len("/mcp")]
    return base.rstrip("/"), None


def _auth_scheme(key, declared=None):
    """mojo routes on the Authorization SCHEME, and the wrong one is a flat 401.

    maestro issues its api key as a JWT, so it authenticates as `Bearer` even
    though everyone calls it an api key. Same reasoning as testit/maestro.py.
    """
    if declared:
        return declared
    if key and key.count(".") == 2 and key.startswith("eyJ"):
        return "Bearer"
    return "apikey"


def maestro_credentials():
    """(url, key, scheme, project) from the MCP server this machine already has.

    Read, never asked for: anyone releasing this package already has the
    maestro MCP installed, and the repo already records its project id.
    """
    project = (_load_json(REPO_CONFIG) or {}).get("project")

    for path in MCP_CONFIG_PATHS:
        data = _load_json(path)
        if not data:
            continue
        blocks = [data.get("mcpServers")]
        projects = data.get("projects")
        if isinstance(projects, dict):
            blocks.extend(entry.get("mcpServers") for entry in projects.values()
                          if isinstance(entry, dict))
        for block in blocks:
            if not isinstance(block, dict):
                continue
            for name, cfg in block.items():
                if "maestro" not in str(name).lower() or not isinstance(cfg, dict):
                    continue
                url, key, scheme = None, None, None
                raw = cfg.get("url")
                if isinstance(raw, str) and raw:
                    url, key = _split_connector_url(raw)
                headers = cfg.get("headers")
                if key is None and isinstance(headers, dict):
                    for header, value in headers.items():
                        if str(header).lower() != "authorization":
                            continue
                        if not isinstance(value, str):
                            continue
                        parts = value.split(None, 1)
                        if len(parts) == 2:
                            scheme, key = parts[0].strip(), parts[1].strip()
                        else:
                            key = parts[0].strip()
                        break
                if url and key:
                    return url, key, _auth_scheme(key, scheme), project
    return None, None, None, project


def maestro_request(method, path, params=None, payload=None, timeout=20):
    """One authenticated call. Returns the decoded `data`, or raises."""
    url, key, scheme, _project = maestro_credentials()
    if not url or not key:
        raise PublishError(
            "no maestro credential found — install the maestro MCP server; "
            "from an agent session, confirm the note yourself and pass "
            "--note-by-agent; pass --skip-notes only when maestro is down")

    target = f"{url}{path}"
    if params:
        target = f"{target}?{urllib.parse.urlencode(params)}"
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(target, data=body, method=method)
    request.add_header("Authorization", f"{scheme} {key}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as err:
        detail = (err.read().decode() or "")[:200]
        raise PublishError(f"maestro {method} {path} failed: HTTP {err.code} {detail}".rstrip())
    except urllib.error.URLError as err:
        raise PublishError(f"maestro is unreachable ({err.reason})")
    return decoded.get("data", decoded)


def find_release_note(version, project):
    """The ProjectRelease row for `version`, or None.

    `include_drafts` is not optional here and is the whole reason this
    function exists rather than a one-line request: the list is
    published-only by default, and the note we are gating on has NOT been
    published yet — publishing it is the last thing a successful release
    does. Without the flag this check can only ever see the PREVIOUS
    release and refuses every time.

    Matching is done here rather than with a `version=` query parameter:
    that parameter is not a supported filter and silently returns nothing.
    """
    rows = maestro_request(
        "GET", "/api/maestro/project/release",
        params={"group": project, "graph": "list", "size": 100,
                "include_drafts": 1})
    if isinstance(rows, dict):
        rows = rows.get("data") or []
    wanted = str(version).lstrip("v")
    for row in rows or []:
        if str(row.get("version", "")).lstrip("v") == wanted:
            return row
    return None


def require_release_note(version, project, skip=False, by_agent=False):
    """Refuse to release a version nobody wrote a note for.

    A precondition, NOT a closing step, and that ordering is the whole point: a
    PyPI version can never be reused, so a note check that runs after the
    upload has nothing left to protect. Draft is what we require — publishing
    it is what `post_release_notes` does once the release actually shipped.

    Under --note-by-agent nothing is requested and nothing is verified here.
    An agent session's maestro connection is handed to it at launch and stored
    in no file this script can read, so the agent confirms the draft with its
    own tools before the run and publishes it after (maestro #6349).
    """
    if skip:
        say("release note check skipped (--skip-notes)")
        return None
    if by_agent:
        if not project:
            raise PublishError(
                ".claude/maestro.json names no project — the calling agent "
                "needs it to publish the release note")
        say("release note not checked by this script: the calling agent confirms it")
        return NOTE_BY_AGENT
    if not project:
        raise PublishError(
            ".claude/maestro.json names no project — cannot check for a "
            "release note (or pass --skip-notes)")

    note = find_release_note(version, project)
    if note is None:
        raise PublishError(
            f"no maestro release note for {version} — run "
            f"/maestro-release-note first, or pass --skip-notes")
    if note.get("status") == "published":
        say(f"release note for {version} is already published")
    else:
        say(f"release note for {version} found (draft)")
    return note


def current_branch():
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise PublishError("HEAD is detached — check out a branch before releasing")
    return branch


def build(version, out_dir):
    """Build the wheel into `out_dir`, check it there, and return its path.

    Runs for real under --dry-run as well: a rehearsal that builds nothing
    cannot fail where the release would. `out_dir` is a private temporary
    folder, so nothing another session leaves in dist/ can be uploaded, and
    the file that was checked is the file that goes up.

    No source archive is built. One packs every file `.gitignore` does not
    name, including each agent worktree, and the index refusing it for size
    AFTER the wheel was up is how 1.31.4 went out half-uploaded (maestro
    #6348). The last step refuses a wheel holding anything git does not track.
    """
    out_dir = str(out_dir)
    run([sys.executable, "scripts/vendor_admin_portal.py", "--check"], capture=False)
    run(["uv", "build", "--wheel", "--out-dir", out_dir], capture=False)
    run([sys.executable, "scripts/verify_admin_portal_package.py", "--dist", out_dir,
         "--build-smoke"], capture=False)
    run([sys.executable, "scripts/release_wheel_only.py", "--dist", out_dir,
         "--version", version], capture=False)
    return Path(out_dir) / WHEEL_NAME.format(version=version)


def push_source(branch, dry_run=False):
    """Push the release commit BEFORE uploading to PyPI.

    Ordering is deliberate: everything reversible happens first. If the push
    fails we have published nothing, and if the upload later fails the source is
    already on the remote. The reverse order can leave a permanent PyPI version
    whose commit exists only on one laptop.

    An SSH push failure is fatal here on purpose — never fall back to another
    credential path.
    """
    run(["git", "push", "origin", branch], dry_run=dry_run, capture=False)


def publish_to_pypi(wheel, dry_run=False):
    """The one irreversible step: upload the one checked wheel, by its path.

    A bare `uv publish` uploads everything in dist/. Naming the file means
    nothing else can go up with it.

    The token is read from the environment by uv rather than passed in argv,
    where it would be visible in `ps` to any local user.
    """
    run(["uv", "publish", str(wheel)], dry_run=dry_run, capture=False)


def _url_ok(url):
    """(reached-with-200, body). Never raises — a poll survives blips."""
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200, response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return False, ""


def wait_for_pypi_visibility(version, timeout=None, interval=None):
    """Block until the uploaded release is resolvable by the world.

    Two endpoints, deliberately: the JSON API reflects an upload almost
    instantly and is what the fleet's pin resolver reads, while the Simple
    index is what pip actually resolves through — a separately cached
    document that lagged behind in every publish-then-deploy race. "Released"
    means BOTH answer, so a deploy started the moment this script returns can
    never ask for a version the world cannot yet see. (The node-side install
    retries remain the belt for anything that still slips.)

    A timeout warns and returns False instead of raising: the upload has
    already happened and a PyPI version can never be re-cut, so aborting here
    would misreport a release that shipped — and would strand a rerun behind
    the already-on-PyPI precondition.
    """
    # Read here, not at import: .env is loaded by main().
    if timeout is None:
        timeout = float(os.environ.get("PUBLISH_VISIBLE_TIMEOUT", VISIBILITY_TIMEOUT))
    if interval is None:
        interval = float(os.environ.get("PUBLISH_VISIBLE_INTERVAL", VISIBILITY_INTERVAL))
    json_url = PYPI_JSON_URL.format(name=PACKAGE_NAME, version=version)
    simple_url = PYPI_SIMPLE_URL.format(name=PACKAGE_NAME)
    # Filenames normalize the dashes; anchor the version so 1.15.1 can never
    # satisfy a wait for 1.15.13.
    marker = re.compile(
        re.escape(PACKAGE_NAME.replace("-", "_")) + "-"
        + re.escape(version) + r"[.-]")
    say(f"waiting for {PACKAGE_NAME} {version} to be resolvable on PyPI...")
    started = time.monotonic()
    deadline = started + timeout
    while True:
        json_ok, _ = _url_ok(json_url)
        simple_ok = False
        if json_ok:
            reached, body = _url_ok(simple_url)
            simple_ok = reached and bool(marker.search(body))
        if json_ok and simple_ok:
            say(f"{PACKAGE_NAME} {version} is resolvable "
                f"(JSON API + Simple index, {time.monotonic() - started:.0f}s)")
            return True
        if time.monotonic() >= deadline:
            say(f"WARNING: {PACKAGE_NAME} {version} is uploaded but not yet "
                f"resolvable everywhere (json_api={json_ok}, "
                f"simple_index={simple_ok}) — hold deploys until it is; "
                "node installs retry on their own regardless")
            return False
        time.sleep(interval)


def tag_release(version, branch, dry_run=False):
    tag = f"v{version}"
    run(["git", "tag", "-a", tag, "-m", f"Release {tag}"], dry_run=dry_run, capture=False)
    run(["git", "push", "origin", tag], dry_run=dry_run, capture=False)


def post_release_notes(version, note, project=None, dry_run=False):
    """Flip this version's maestro note from draft to published.

    Runs LAST, after the tag, because publishing a note for a release that
    then failed to ship is the one direction that lies. `require_release_note`
    already proved the note exists — the release is not gated on this call.

    A failure here is reported, not raised: PyPI has the package and the tag is
    pushed, so aborting would misreport a release that happened. Publishing the
    note by hand afterwards is a two-second fix; un-publishing a package is not
    possible at all.

    Under --note-by-agent the publish is the calling agent's. Returns the line
    main() prints last for it, and only on a real run: a rehearsal must never
    tell an agent to publish the note of a release that did not ship.
    """
    if note == NOTE_BY_AGENT:
        if dry_run:
            say(f"[dry-run] release note step skipped for {version}: "
                "the calling agent publishes it after a real release")
            return None
        return f'NEXT: publish_release(project={project}, version="{version}")'
    if note is None:
        say(f"release notes for {version}: nothing to publish")
        return
    if note.get("status") == "published":
        say(f"release notes for {version}: already published")
        return
    if dry_run:
        say(f"[dry-run] would publish release note {note.get('id')} for {version}")
        return

    payload = {"status": "published"}
    # Anchors the note to what shipped, and becomes the `git log` span start
    # for the NEXT release's note.
    commit = git("rev-parse", "HEAD")
    if commit:
        payload["commit_ref"] = commit
    try:
        maestro_request(
            "POST", f"/api/maestro/project/release/{note.get('id')}",
            payload=payload)
        say(f"published release note for {version}")
    except PublishError as err:
        say(f"warning: {version} shipped but its release note is still a draft "
            f"({err}) — publish it from the board")


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Release django-mojo. The version must already be bumped and "
            "committed; this script verifies, builds, pushes, publishes and tags."))
    parser.add_argument(
        "--nopypi", action="store_true",
        help="Skip the PyPI upload (still verifies, builds, pushes and tags)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help=("Run every check and the build for real, but push, upload and "
              "tag nothing"))
    notes = parser.add_mutually_exclusive_group()
    notes.add_argument(
        "--skip-notes", action="store_true",
        help=("Release without a maestro release note. The gate is fail-closed "
              "on purpose — reach for this only when maestro is down"))
    notes.add_argument(
        "--note-by-agent", action="store_true",
        help=("For an agent session, which has no maestro login in any file: "
              "the agent has confirmed the draft note and publishes it after "
              "the release. This script does not verify that"))
    return parser.parse_args(argv)


def main(argv=None):
    try:
        load_env(ENV_FILE, os.environ)
        args = parse_arguments(argv)

        if args.dry_run:
            say("DRY RUN — checks and the build run for real, "
                "nothing is pushed or published")

        # Everything below the build is ordered so the irreversible step (the
        # PyPI upload) happens last and only after the source is on the remote.
        validate_environment(args)
        require_clean_tree()

        version = get_current_version()
        say(f"releasing version {version}")

        require_version_consistency(version)
        require_unreleased(version)
        # Before the build, and well before PyPI: a version can never be
        # reused, so every precondition has to fail while failing is still free.
        _url, _key, _scheme, project = maestro_credentials()
        note = require_release_note(
            version, project, skip=args.skip_notes, by_agent=args.note_by_agent)

        branch = current_branch()

        # Only this run can see the folder, and it is gone when the block ends.
        with tempfile.TemporaryDirectory(prefix="django-mojo-release-") as out_dir:
            wheel = build(version, Path(out_dir).resolve())
            # Again, after the build: sessions share this checkout, and the
            # wheel check compares file names with git, not file contents.
            require_clean_tree()
            push_source(branch, dry_run=args.dry_run)

            visible = True
            if args.nopypi:
                say("skipping PyPI upload (--nopypi)")
            else:
                publish_to_pypi(wheel, dry_run=args.dry_run)
                if not args.dry_run:
                    visible = wait_for_pypi_visibility(version)

        tag_release(version, branch, dry_run=args.dry_run)
        next_step = post_release_notes(
            version, note, project=project, dry_run=args.dry_run)

        if args.dry_run:
            say(f"dry run complete for {version}")
        elif visible:
            say(f"released {version}")
        else:
            say(f"released {version} — but it is NOT yet resolvable on PyPI "
                "(see the WARNING above); hold deploys until it is")
        if next_step:
            # Last line on purpose: it is the one step left, and it is the agent's.
            say(next_step, prefix="")

    except PublishError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
