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
