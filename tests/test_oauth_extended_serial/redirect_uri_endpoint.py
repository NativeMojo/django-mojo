"""
/begin end-to-end allowlist refusal — moved from tests/test_oauth/redirect_uri.py
(maestro item #1839): it writes the protected global ALLOWED_REDIRECT_URLS
Setting row, which is visible to every parallel module while it exists.
"""
from urllib.parse import quote

from testit import helpers as th

PROVIDER = "google"

SETTING_KEY = "ALLOWED_REDIRECT_URLS"
ENTRY = "https://app.example.com"
ALLOWED = "https://app.example.com/callback"
SUFFIXED = "https://app.example.com.evil.tld/"
REFUSAL = "redirect_uri is not on the allowlist"


@th.django_unit_setup()
def setup_redirect_uri(opts):
    """Drop any global ALLOWED_REDIRECT_URLS row before this module runs.

    `settings.get` is DB-first, so a row stranded by a previous interrupted run
    would shadow every in-process override below and turn the whole module into
    a silent false green. This is also the row the endpoint test creates, so the
    long-lived database is cleaned of this module's own data before it writes
    any.
    """
    from mojo.apps.account.models.setting import Setting

    Setting.remove(SETTING_KEY)


@th.django_unit_test("oauth: /begin refuses a suffixed host end to end")
def test_oauth_begin_refuses_suffixed_host_endpoint(opts):
    """The full public endpoint, through a global `Setting` row.

    The row's value is a STRING (a `TextField` cannot hold anything else), so
    this single test covers BOTH bypasses at once: under the old code the string
    exploded into characters and `/begin` returned 200 with a usable `auth_url`
    for the attacker's host.

    Assertions target the 400 and the ABSENCE of `state`, never the contents of
    `auth_url`: `auth_url` always points at the provider and the landing URL only
    surfaces later (stashed in the Redis state as `frontend_uri`), so
    string-matching `auth_url` would pass both before and after the fix.
    """
    from mojo.apps.account.models.setting import Setting

    # A strict superset of the pinned test-project entry, so the brief window
    # this row shadows the file setting cannot narrow the allowlist for a
    # package running in parallel.
    Setting.set(SETTING_KEY, f'["https://example.com/", "{ENTRY}"]')
    try:
        resp = opts.client.get(
            f"/api/auth/oauth/{PROVIDER}/begin"
            f"?redirect_uri={quote(SUFFIXED, safe='')}")
        body = resp.response
        assert resp.status_code == 400, (
            f"/begin must refuse the attacker-registered suffix host "
            f"{SUFFIXED!r}, got {resp.status_code}: {body}")
        assert body.get("error") == REFUSAL, (
            f"the refusal must stay the existing message {REFUSAL!r} — it is "
            f"shared verbatim with the gated-destination refusal so the two are "
            f"not distinguishable; got {body.get('error')!r}")
        data = body.get("data") or {}
        assert not data.get("auth_url"), (
            f"a refused begin must return no auth_url, got {data.get('auth_url')!r}")
        assert not data.get("state"), (
            f"a refused begin must mint no OAuth state — the state is what "
            f"carries frontend_uri to the callback bounce; got "
            f"{data.get('state')!r}")

        ok = opts.client.get(
            f"/api/auth/oauth/{PROVIDER}/begin"
            f"?redirect_uri={quote(ALLOWED, safe='')}")
        assert ok.status_code == 200, (
            f"the allowlisted host itself must still begin normally — this is "
            f"what proves the 400 above is a host match and not a broken read. "
            f"Got {ok.status_code}: {ok.response}")
        assert ok.response.data.auth_url, (
            f"an admitted begin must return an auth_url: {ok.response}")
        assert ok.response.data.state, (
            f"an admitted begin must mint OAuth state: {ok.response}")
    finally:
        Setting.remove(SETTING_KEY)
