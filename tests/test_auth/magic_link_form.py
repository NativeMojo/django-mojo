"""Hosted magic-link sending prevents repeated submissions and explains resend."""
import shutil
import subprocess
from pathlib import Path
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


@th.tier("framework")
@th.django_unit_test("hosted magic link: sent state, duplicate guard, resend wait and recovery")
def test_magic_link_form(opts):
    node = shutil.which("node")
    assert_true(node, "Node.js is required for the hosted-page regression")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([
        node, str(Path(__file__).with_suffix(".js")),
        str(root / "mojo/apps/account/templates/account/login.html"),
    ], capture_output=True, text=True, timeout=20)
    assert_eq(result.returncode, 0, "Hosted magic-link behavior must pass: " + result.stderr)
