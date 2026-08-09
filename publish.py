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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Load .env if present (UV_PUBLISH_TOKEN lives there).
_env_file = Path(".env")
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _val = _line.split("=", 1)
            os.environ.setdefault(_key.strip(), _val.strip())

PYPROJECT_FILE = Path("pyproject.toml")
INIT_FILE = Path("mojo/__init__.py")
LOCK_FILE = Path("uv.lock")

PACKAGE_NAME = "django-mojo"
PYPI_JSON_URL = "https://pypi.org/pypi/{name}/{version}/json"

# The files the agent is expected to have bumped and committed before calling us.
VERSION_FILES = (PYPROJECT_FILE, INIT_FILE, LOCK_FILE)


class PublishError(Exception):
    """A release precondition failed, or a command did."""
    pass


def say(message):
    """Progress output. Plain print, not logging — nothing here needs a logger,
    and `mojo.helpers.logit` is unavailable to a script that cannot import mojo."""
    print(f"==> {message}", flush=True)


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
    base = url.rstrip("/")
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
            "no maestro credential found — install the maestro MCP server, or "
            "pass --skip-notes to release without a note")

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
    """The ProjectRelease row for `version`, or None."""
    data = maestro_request(
        "GET", "/api/maestro/project/release",
        params={"group": project, "version": version, "graph": "list"})
    rows = data.get("data") if isinstance(data, dict) else None
    if rows is None and isinstance(data, list):
        rows = data
    for row in rows or []:
        # Filter client-side too: a search-style match could widen this.
        if str(row.get("version", "")).lstrip("v") == str(version).lstrip("v"):
            return row
    return None


def require_release_note(version, project, skip=False):
    """Refuse to release a version nobody wrote a note for.

    A precondition, NOT a closing step, and that ordering is the whole point: a
    PyPI version can never be reused, so a note check that runs after the
    upload has nothing left to protect. Draft is what we require — publishing
    it is what `post_release_notes` does once the release actually shipped.
    """
    if skip:
        say("release note check skipped (--skip-notes)")
        return None
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


def build(dry_run=False):
    run(["rm", "-rf", "dist"], dry_run=dry_run, capture=False)
    run(["uv", "build"], dry_run=dry_run, capture=False)


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


def publish_to_pypi(dry_run=False):
    """The one irreversible step.

    The token is read from the environment by uv rather than passed in argv,
    where it would be visible in `ps` to any local user.
    """
    run(["uv", "publish"], dry_run=dry_run, capture=False)


def tag_release(version, branch, dry_run=False):
    tag = f"v{version}"
    run(["git", "tag", "-a", tag, "-m", f"Release {tag}"], dry_run=dry_run, capture=False)
    run(["git", "push", "origin", tag], dry_run=dry_run, capture=False)


def post_release_notes(version, note, dry_run=False):
    """Flip this version's maestro note from draft to published.

    Runs LAST, after the tag, because publishing a note for a release that
    then failed to ship is the one direction that lies. `require_release_note`
    already proved the note exists — the release is not gated on this call.

    A failure here is reported, not raised: PyPI has the package and the tag is
    pushed, so aborting would misreport a release that happened. Publishing the
    note by hand afterwards is a two-second fix; un-publishing a package is not
    possible at all.
    """
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


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Release django-mojo. The version must already be bumped and "
            "committed; this script verifies, builds, pushes, publishes and tags."))
    parser.add_argument(
        "--nopypi", action="store_true",
        help="Skip the PyPI upload (still verifies, builds, pushes and tags)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run every check for real, but execute nothing that changes anything")
    parser.add_argument(
        "--skip-notes", action="store_true",
        help=("Release without a maestro release note. The gate is fail-closed "
              "on purpose — reach for this only when maestro is down"))
    return parser.parse_args()


def main():
    try:
        args = parse_arguments()

        if args.dry_run:
            say("DRY RUN — checks run for real, nothing is pushed or published")

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
        note = require_release_note(version, project, skip=args.skip_notes)

        branch = current_branch()

        build(dry_run=args.dry_run)
        push_source(branch, dry_run=args.dry_run)

        if args.nopypi:
            say("skipping PyPI upload (--nopypi)")
        else:
            publish_to_pypi(dry_run=args.dry_run)

        tag_release(version, branch, dry_run=args.dry_run)
        post_release_notes(version, note, dry_run=args.dry_run)

        say(f"released {version}" if not args.dry_run else f"dry run complete for {version}")

    except PublishError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
