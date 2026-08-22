"""Default-tier end-to-end allowlist read through real settings (item #2558).

The matcher regressions in redirect_uri.py drive the `deployment_entries=`
seam; the one branch they no longer exercise is the sentinel default — the
real `settings.get("ALLOWED_REDIRECT_URLS", kind="list")` read. This covers
it with ZERO writes: the test project pins
`ALLOWED_REDIRECT_URLS = ["https://example.com/"]` in its file settings, so
the admitted control proves the read resolves the pinned entry and the
refusal proves the matcher runs against what it read.

Two requests only — /begin throttles anonymous callers.
"""
from urllib.parse import quote

from testit import helpers as th

PROVIDER = "google"
REFUSAL = "redirect_uri is not on the allowlist"


@th.django_unit_setup()
def setup_redirect_settings_read(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")


@th.django_unit_test("oauth: /begin resolves the deployment allowlist from real settings")
def test_begin_reads_pinned_allowlist(opts):
    refused = "https://example.com.evil.tld/"
    resp = opts.client.get(
        f"/api/auth/oauth/{PROVIDER}/begin"
        f"?redirect_uri={quote(refused, safe='')}")
    body = resp.response
    assert resp.status_code == 400, (
        f"/begin must refuse the attacker-registered suffix host {refused!r} "
        f"against the pinned file entry, got {resp.status_code}: {body}")
    assert body.get("error") == REFUSAL, (
        f"the refusal must be the shared allowlist message, got "
        f"{body.get('error')!r}")
    data = body.get("data") or {}
    assert not data.get("state"), (
        f"a refused begin must mint no OAuth state, got {data.get('state')!r}")

    allowed = "https://example.com/callback"
    ok = opts.client.get(
        f"/api/auth/oauth/{PROVIDER}/begin"
        f"?redirect_uri={quote(allowed, safe='')}")
    assert ok.status_code == 200, (
        f"the pinned entry's own host must begin normally — this is what "
        f"proves the settings read resolved the file value rather than the "
        f"allowlist being broken outright. Got {ok.status_code}: {ok.response}")
    assert ok.response.data.auth_url, (
        f"an admitted begin must return an auth_url: {ok.response}")
    assert ok.response.data.state, (
        f"an admitted begin must mint OAuth state: {ok.response}")
