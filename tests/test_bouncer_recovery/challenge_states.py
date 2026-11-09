"""
The hosted bot-check page's states (maestro #9132), run in node: the REAL
mojo-hosted-bouncer.js mounts on a fake DOM built from the RENDERED challenge
page (challenge_states.js) and is driven through ready -> checking ->
verified, every failure reason, the 429 countdown, the review step and its
receipt, copy, and Restart.
"""
import json
import shutil
import subprocess
from pathlib import Path

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "framework"

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "mojo/apps/account/static/account/mojo-hosted-bouncer.js"


def _page(config=None):
    from test_bouncer_recovery.challenge_page import challenge, parse, by_id, text
    html = challenge(config=config).content.decode('utf-8')
    tree = parse(html)
    return tree.root, json.loads(text(by_id(tree, 'mbg-config')))


@th.django_unit_test("hosted challenge states: check, verified, failures, countdown, review, receipt, copy, restart")
def test_challenge_states(opts):
    from test_bouncer_recovery.challenge_page import REFERENCE
    node = shutil.which('node')
    assert_true(node, "Node.js is required for the hosted-page regression")
    tree, config = _page()
    tree_no_ticket, config_no_ticket = _page({'review_ticket': '', 'next_action': 'recovery', 'reason': 'operator'})
    payload = json.dumps({
        'script': str(SCRIPT), 'reference': REFERENCE,
        'tree': tree, 'config': config,
        'tree_no_ticket': tree_no_ticket, 'config_no_ticket': config_no_ticket,
    })
    result = subprocess.run([node, str(Path(__file__).with_suffix('.js'))],
                            input=payload, capture_output=True, text=True, timeout=30)
    assert_eq(result.returncode, 0,
              "the hosted challenge state machine must pass: " + (result.stderr or result.stdout))
