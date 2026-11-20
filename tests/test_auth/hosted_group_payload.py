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
        assert complete["username"] == "test-user" and complete["code"] == "123456", "SMS credentials must remain unchanged"
    else:
        assert complete["challenge_id"] == "test-challenge", "complete must carry the begin challenge"
        assert complete["credential"]["response"]["signature"] == "AQ", "credential encoding must remain unchanged"
        assert ("username" in calls[0]["body"]) == (method == "named"), "only named passkey begin must include username"


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
    assert "loginWithPasskeyDiscoverable({ group_uuid: cfg.groupUuid })" in template, "hosted passkey must supply its page group"
    assert "verifySmsLogin(phone, code, { group_uuid: cfg.groupUuid })" in template, "hosted SMS verification must supply its page group"


def _oauth_begin_url(provider, branded):
    calls = _payloads(f"oauth-{provider}", branded)
    assert len(calls) == 1, calls
    assert calls[0]["body"] is None, "OAuth begin is a GET with no body"
    return calls[0]["url"]


@th.django_unit_test("hosted OAuth begin carries the group for Google and Microsoft")
def test_hosted_oauth_begin_group(opts):
    from urllib.parse import urlsplit, parse_qs
    for provider in ("google", "microsoft"):
        url = _oauth_begin_url(provider, True)
        parts = urlsplit(url)
        assert parts.path == f"/api/auth/oauth/{provider}/begin", url
        query = parse_qs(parts.query)
        assert query.get("group_uuid") == ["test-brand-4622"], url
        assert query.get("redirect_uri") == ["https://auth.example.test/login?redirect=%2Fhome"], url


@th.django_unit_test("OAuth begin without options sends no group, as before")
def test_hosted_oauth_begin_legacy(opts):
    for provider in ("google", "apple", "github", "microsoft"):
        url = _oauth_begin_url(provider, False)
        assert url == (f"https://auth.example.test/api/auth/oauth/{provider}/begin"
                       "?redirect_uri=https%3A%2F%2Fauth.example.test%2Flogin%3Fredirect%3D%252Fhome"), url


@th.django_unit_test("hosted pages pass their group on every OAuth start")
def test_hosted_oauth_templates_forward_group(opts):
    import re
    starts = re.compile(r"MojoAuth\.(startOAuthLogin|start(?:Google|Apple|GitHub|Microsoft)Login)\(([^)]*)\)")
    found = 0
    for name in ("login.html", "register.html", "auth_base.html"):
        source = (ROOT / "mojo/apps/account/templates/account" / name).read_text()
        for match in starts.finditer(source):
            found += 1
            assert "group_uuid:" in match.group(2), f"{name}: {match.group(0)} must pass the page's group_uuid"
    assert found >= 9, f"expected the four buttons on login and register plus the retry, found {found}"
