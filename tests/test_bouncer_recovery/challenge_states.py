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


CUSTOM_GROUP = "test-11759-challenge-states"
CUSTOM_HEADING = "Welcome to Example"
CUSTOM_TEXT = "We check every visit to keep accounts safe."


def _page(config=None, group=None):
    from test_bouncer_recovery.challenge_page import challenge, parse, by_id, text
    html = challenge(config=config, group=group).content.decode('utf-8')
    tree = parse(html)
    return tree.root, json.loads(text(by_id(tree, 'mbg-config')))


@th.django_unit_test("hosted challenge states: check, verified, failures, countdown, review, receipt, copy, restart")
def test_challenge_states(opts):
    from test_bouncer_recovery.challenge_page import REFERENCE
    node = shutil.which('node')
    assert_true(node, "Node.js is required for the hosted-page regression")
    tree, config = _page()
    tree_no_ticket, config_no_ticket = _page({'review_ticket': '', 'next_action': 'recovery', 'reason': 'operator'})
    tree_custom = _custom_tree()
    payload = json.dumps({
        'script': str(SCRIPT), 'reference': REFERENCE,
        'tree': tree, 'config': config,
        'tree_no_ticket': tree_no_ticket, 'config_no_ticket': config_no_ticket,
        'tree_custom': tree_custom,
        'custom_copy': [CUSTOM_HEADING, CUSTOM_TEXT],
    })
    result = subprocess.run([node, str(Path(__file__).with_suffix('.js'))],
                            input=payload, capture_output=True, text=True, timeout=30)
    assert_eq(result.returncode, 0,
              "the hosted challenge state machine must pass: " + (result.stderr or result.stdout))


def _custom_tree():
    """The page rendered for a test-owned group with its own heading and
    sentence (#11759); settings never touch another group or the process."""
    from mojo.apps.account.models import Group
    from mojo.apps.account.models.setting import Setting

    Group.objects.filter(name=CUSTOM_GROUP).delete()
    group = Group.objects.create(name=CUSTOM_GROUP, kind="organization", metadata={})
    try:
        Setting.objects.create(key='BOUNCER_CHALLENGE_HEADING', value=CUSTOM_HEADING, group=group)
        Setting.objects.create(key='BOUNCER_CHALLENGE_TEXT', value=CUSTOM_TEXT, group=group)
        try:
            Setting.warm_cache(group_id=group.pk)
        except Exception:
            pass
        tree, _ = _page(group=group)
        return tree
    finally:
        Setting.objects.filter(group__name=CUSTOM_GROUP).delete()
        Group.objects.filter(name=CUSTOM_GROUP).delete()
