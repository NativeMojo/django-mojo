"""Run the hosted auth decline-message regression against the shipped script."""
import json
from pathlib import Path
import shutil
import subprocess

from testit import helpers as th

TESTIT_TIER = "bug"
ROOT = Path(__file__).resolve().parents[2]


@th.django_unit_test("hosted auth declines show explanations and useful next steps")
def test_decline_messages(opts):
    node = shutil.which("node")
    assert node, "Node.js is required for the hosted-auth decline regression"
    result = subprocess.run(
        [node, str(Path(__file__).with_suffix(".js")),
         str(ROOT / "mojo/apps/account/static/account/mojo-auth.js")],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    totals = json.loads(result.stdout)
    assert totals["passed"] >= 20 and totals["failed"] == 0, "Every decline-message check must pass"
