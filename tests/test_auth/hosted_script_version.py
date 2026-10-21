"""The hosted pages load mojo-auth.js behind a cache version, and the script
is served with a day-long public cache. A change to the script that keeps the
old version leaves browsers on the old script: maestro #6226 changed what a
429 shows and would not have reached anyone with the file cached.

The version is the first ten hex digits of the script's SHA-256, so it can't
be forgotten: changing the script without it fails here, with the value to
set.
"""
import hashlib
import re
from pathlib import Path

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "core"

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "mojo/apps/account/static/account/mojo-auth.js"
TEMPLATES = ROOT / "mojo/apps/account/templates/account"


@th.django_unit_test("hosted pages: the script's cache version follows the script's content")
def test_hosted_script_version_matches_its_content(opts):
    expected = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()[:10]
    found = []
    for template in sorted(TEMPLATES.glob("*.html")):
        for version in re.findall(r"mojo-auth\.js\?v=([^\"'&\s]+)", template.read_text(encoding="utf-8")):
            found.append((template.name, version))
    assert_true(found, "the hosted pages must load mojo-auth.js behind a cache version")
    for name, version in found:
        assert_eq(version, expected,
                  f"mojo-auth.js changed: set its version in {name} to ?v={expected}, "
                  "or browsers keep the cached copy for a day")


THEME = ROOT / "mojo/apps/account/static/account/mojo-auth-theme.css"


@th.django_unit_test("hosted pages: the theme stylesheet's cache version follows its content")
def test_hosted_theme_version_matches_its_content(opts):
    # Same day-long public cache as the script: new auth_base markup (the
    # #8579 OAuth pending view) must never meet a day-old stylesheet.
    expected = hashlib.sha256(THEME.read_bytes()).hexdigest()[:10]
    found = []
    for template in sorted(TEMPLATES.glob("*.html")):
        for version in re.findall(r"mojo-auth-theme\.css\?v=([^\"'&\s]+)", template.read_text(encoding="utf-8")):
            found.append((template.name, version))
    assert_true(found, "the hosted pages must load mojo-auth-theme.css behind a cache version")
    for name, version in found:
        assert_eq(version, expected,
                  f"mojo-auth-theme.css changed: set its version in {name} to ?v={expected}, "
                  "or browsers keep the cached copy for a day")
    _assert_every_reference_versioned("mojo-auth-theme.css")


def _assert_every_reference_versioned(filename):
    for template in sorted(TEMPLATES.glob("*.html")):
        source = template.read_text(encoding="utf-8")
        loads = len(re.findall(re.escape(filename) + r"(?![\w.-])", source))
        versioned = len(re.findall(re.escape(filename) + r"\?v=", source))
        assert_eq(versioned, loads,
                  f"{template.name} loads {filename} {loads} time(s) but only {versioned} behind ?v=: "
                  "an unversioned load keeps a day-old copy")


HOSTED_BOUNCER = ROOT / "mojo/apps/account/static/account/mojo-hosted-bouncer.js"


@th.django_unit_test("hosted pages: the hosted bouncer script's cache version follows its content")
def test_hosted_bouncer_version_matches_its_content(opts):
    # The #9132 challenge markup and this script ship together: a cached
    # previous script must never run against changed markup.
    expected = hashlib.sha256(HOSTED_BOUNCER.read_bytes()).hexdigest()[:10]
    found = []
    for template in sorted(TEMPLATES.glob("*.html")):
        for version in re.findall(r"mojo-hosted-bouncer\.js\?v=([^\"'&\s]+)", template.read_text(encoding="utf-8")):
            found.append((template.name, version))
    names = {name for name, _ in found}
    assert_true({"bouncer_challenge.html", "auth_base.html", "bouncer_decoy.html"} <= names,
                f"the challenge, auth_base and decoy must load mojo-hosted-bouncer.js behind a version, got {sorted(names)}")
    for name, version in found:
        assert_eq(version, expected,
                  f"mojo-hosted-bouncer.js changed: set its version in {name} to ?v={expected}, "
                  "or a cached old script runs against the new markup")
    _assert_every_reference_versioned("mojo-hosted-bouncer.js")


@th.django_unit_test("static serving: a ?v= that is not the file's content hash is never cached")
def test_static_version_mismatch_is_not_cached(opts):
    from mojo.apps.account.rest.bouncer.static import _serve_static

    current = hashlib.sha256(HOSTED_BOUNCER.read_bytes()).hexdigest()[:10]
    matched = _serve_static("mojo-hosted-bouncer.js", current, debug=False)
    assert_true(matched["Cache-Control"].startswith("public, max-age="),
                f"a matching version gets the day cache, got {matched['Cache-Control']!r}")
    assert_eq(matched.content, HOSTED_BOUNCER.read_bytes(), "the file is served byte for byte")
    stale = _serve_static("mojo-hosted-bouncer.js", "0000000000", debug=False)
    assert_eq(stale["Cache-Control"], "no-store",
              "a mismatched version (mixed deploy) must not pin this file under that key for a day")
    plain = _serve_static("mojo-hosted-bouncer.js", "", debug=False)
    assert_true(plain["Cache-Control"].startswith("public, max-age="),
                "an unversioned load keeps the day cache")
    debug = _serve_static("mojo-hosted-bouncer.js", current, debug=True)
    assert_eq(debug["Cache-Control"], "no-store", "DEBUG still serves no-store")
    assert_eq(_serve_static("missing.js").status_code, 404, "an unknown file is a 404")
