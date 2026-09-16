"""Execute the shipped client with a fake browser/transport; no network or tokens."""
import json
from pathlib import Path
import shutil
import subprocess

from testit import helpers as th

TESTIT_TIER = "bug"
ROOT = Path(__file__).resolve().parents[2]


def _payloads(method, branded):
    node = shutil.which("node")
    assert node, "Node.js is required for the hosted-auth client regression"
    result = subprocess.run(
        [node, str(Path(__file__).with_suffix(".js")),
         str(ROOT / "mojo/apps/account/static/account/mojo-auth.js"), method,
         "brand" if branded else "plain"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _check(method, branded):
    calls = _payloads(method, branded)
    assert len(calls) == (1 if method == "sms" else 2), calls
    for call in calls:
        if branded:
            assert call["body"].get("group_uuid") == "test-brand-4622", call
        else:
            assert "group_uuid" not in call["body"], call
    complete = calls[-1]["body"]
    assert complete["duid"] == "test-device", complete
    assert complete["bouncer_token"] == "test-bouncer", complete
    if method == "sms":
        assert complete["username"] == "test-user" and complete["code"] == "123456"
    else:
        assert complete["challenge_id"] == "test-challenge"
        assert complete["credential"]["response"]["signature"] == "AQ"
        assert ("username" in calls[0]["body"]) == (method == "named")


@th.django_unit_test("hosted discoverable passkey carries group on begin and complete")
def test_hosted_discoverable_group(opts):
    _check("discoverable", True)


@th.django_unit_test("hosted username passkey carries group on begin and complete")
def test_hosted_named_group(opts):
    _check("named", True)


@th.django_unit_test("hosted SMS verification carries group")
def test_hosted_sms_group(opts):
    _check("sms", True)


@th.django_unit_test("hosted auth helpers preserve calls without options")
def test_hosted_legacy_options(opts):
    for method in ("discoverable", "named", "sms"):
        _check(method, False)


@th.django_unit_test("hosted login template supplies resolved group to both completions")
def test_hosted_template_group(opts):
    template = (ROOT / "mojo/apps/account/templates/account/login.html").read_text()
    assert "loginWithPasskeyDiscoverable({ group_uuid: cfg.groupUuid })" in template
    assert "verifySmsLogin(phone, code, { group_uuid: cfg.groupUuid })" in template
