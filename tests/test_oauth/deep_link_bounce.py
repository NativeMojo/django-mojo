"""Regression: the OAuth callback bounce reaches a custom-scheme deep link.

A `myapp://callback` entry PASSES `_validate_redirect_uri` at `/begin` and is
stored as `frontend_uri` in the OAuth state — but before #1102 the `/callback`
bounce used a stock `HttpResponseRedirect`, whose Django default
`allowed_schemes=['http','https','ftp']`. So the 302 to the deep link raised
`DisallowedRedirect`, which the generic error handler turned into a **500** plus
a level-12 incident, drivable by two anonymous requests. Native-app OAuth login
was broken end to end even though the allowlist admitted the scheme.

The fix is fail-closed by construction: the vetted scheme is derived and stamped
into state at MINT time (`/begin`, where the allowlist verdict is in scope),
re-checked against the FINAL merged URL at the callback, and everything else is
floored to a 400. This module locks in each leg of that:

  * a deep-link redirect_uri round-trips /begin -> /callback as a 302 to the
    deep link (THE regression — a 500 before the fix);
  * the reverse-DNS single-slash form (`com.example.app:/oauth`) survives with
    its slash intact;
  * a deep-link `frontend_uri` with NO stamped `frontend_scheme` (a pre-#1102
    or forged state) floors to a 400 — the widening is bound to the mint, not
    the candidate;
  * a custom scheme injected via the UNVALIDATED `Origin` header is never
    stamped, so the callback floors it to a clean 400 (red-team negative);
  * a plain https landing bounces exactly as before and stamps NO scheme (the
    state shape stays byte-identical);
  * `redirect_allowlist.matchable_scheme` parses safe shapes and refuses the
    sinks.

The `oauth._vetted_bounce_scheme` unit test, which overrides
`django.conf.settings` in-process, lives in
tests/test_oauth_extended_serial/deep_link_bounce.py (maestro item #1839).

Test shape mirrors the sibling modules: the endpoint tests mint state through a
real `/begin` (reading `data.state`) and call `/callback` with
`allow_redirects=False`, reading `Location` case-insensitively from
`last_response.headers`. Where an allowlist entry beyond the pinned
`["https://example.com/"]` is needed, a global `Setting` row is installed as a
strict SUPERSET of that pin (parallel-safe) and removed in a `finally` — the
same pattern as `redirect_uri.py`'s endpoint test.
"""
from urllib.parse import quote, urlsplit, parse_qs

from testit import helpers as th

TESTIT_TIER = "bug"

PROVIDER = "google"
SETTING_KEY = "ALLOWED_REDIRECT_URLS"

# The callback-bounce refusal message. Shared with the DisallowedRedirect floor
# so a 400 here is provably OUR fail-closed path, not an unrelated value error.
BOUNCE_REFUSAL = "Cannot return to the redirect_uri in this OAuth state"


def _location(opts):
    """Read the Location header of the last response, case-insensitively."""
    headers = opts.client.last_response.headers
    return next((v for k, v in headers.items() if k.lower() == "location"), None)


@th.django_unit_setup()
def setup_deep_link_bounce(opts):
    """Drop any stranded global ALLOWED_REDIRECT_URLS row before this module runs.

    `settings.get` is DB-first, so a row left by an interrupted run would shadow
    the pinned file setting and change what /begin admits. Defensive — this is
    also the row two tests below install and remove in a finally.
    """
    from mojo.apps.account.models.setting import Setting

    Setting.remove(SETTING_KEY)


# ---------------------------------------------------------------------------
# The named regression and its reverse-DNS cousin.
# ---------------------------------------------------------------------------

@th.django_unit_test("oauth: a deep-link redirect_uri round-trips /begin -> /callback as a 302")
def test_deep_link_begin_callback_round_trip(opts):
    """THE regression. `myapp://callback` is admitted at /begin, and the callback
    now 302s the browser to it — where before the fix the bounce raised
    DisallowedRedirect and the request became a 500 with a level-12 incident."""
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services.oauth import get_provider

    svc = get_provider(PROVIDER)
    deep_uri = "myapp://callback"
    # A strict SUPERSET of the pinned entry: the brief window it shadows the file
    # setting cannot narrow the allowlist for a package running in parallel.
    Setting.set(SETTING_KEY, '["https://example.com/", "myapp://callback"]')
    try:
        resp = opts.client.get(
            f"/api/auth/oauth/{PROVIDER}/begin?redirect_uri={quote(deep_uri, safe='')}")
        assert resp.status_code == 200, (
            f"/begin with an allowlisted deep-link redirect_uri must succeed, "
            f"got {resp.status_code}: {resp.response}")
        state = resp.response.data.state
        assert state, f"/begin must mint state for the deep-link flow: {resp.response}"

        stored = svc.peek_state(state)
        assert stored, "the minted state must be retrievable via peek_state"
        assert stored.get("frontend_uri") == deep_uri, (
            f"frontend_uri must be stored verbatim; got {stored.get('frontend_uri')!r}")
        assert stored.get("frontend_scheme") == "myapp", (
            f"the vetted custom scheme must be stamped into state at mint time; "
            f"got {stored.get('frontend_scheme')!r}")

        resp = opts.client.get(
            f"/api/auth/oauth/{PROVIDER}/callback?code=dl_testcode&state={state}",
            allow_redirects=False)
        assert resp.status_code == 302, (
            f"the callback must 302 to the deep link (was a 500 before #1102); "
            f"got {resp.status_code}: {resp.response}")
        location = _location(opts)
        assert location and location.startswith("myapp://callback?"), (
            f"the bounce must land on the deep link; got {location!r}")
        params = parse_qs(urlsplit(location).query)
        assert params.get("code") == ["dl_testcode"], (
            f"the bounce must carry the provider code; got {params.get('code')!r} in {location}")
        assert params.get("state") == [state], (
            f"the bounce must carry the state; got {params.get('state')!r} in {location}")
    finally:
        Setting.remove(SETTING_KEY)


@th.django_unit_test("oauth: a reverse-DNS single-slash deep link round-trips with the slash intact")
def test_deep_link_reverse_dns_single_slash_form(opts):
    """`com.example.app:/oauth` is a valid deep link with an empty authority and
    a single leading slash. The bounce must preserve that exact shape — no `//`
    authority sneaks in from urlunsplit."""
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services.oauth import get_provider

    svc = get_provider(PROVIDER)
    deep_uri = "com.example.app:/oauth"
    Setting.set(SETTING_KEY, f'["https://example.com/", "{deep_uri}"]')
    try:
        resp = opts.client.get(
            f"/api/auth/oauth/{PROVIDER}/begin?redirect_uri={quote(deep_uri, safe='')}")
        assert resp.status_code == 200, (
            f"/begin with a reverse-DNS deep link must succeed, got "
            f"{resp.status_code}: {resp.response}")
        state = resp.response.data.state
        assert state, f"/begin must mint state: {resp.response}"
        stored = svc.peek_state(state)
        assert stored.get("frontend_scheme") == "com.example.app", (
            f"the reverse-DNS scheme must be stamped; got {stored.get('frontend_scheme')!r}")

        resp = opts.client.get(
            f"/api/auth/oauth/{PROVIDER}/callback?code=dl_testcode&state={state}",
            allow_redirects=False)
        assert resp.status_code == 302, (
            f"the callback must 302, got {resp.status_code}: {resp.response}")
        location = _location(opts)
        assert location and location.startswith("com.example.app:/oauth?"), (
            f"the single slash must be preserved in the bounce; got {location!r}")
        params = parse_qs(urlsplit(location).query)
        assert params.get("code") == ["dl_testcode"], (
            f"the bounce must carry the code; got {params.get('code')!r} in {location}")
        assert params.get("state") == [state], (
            f"the bounce must carry the state; got {params.get('state')!r} in {location}")
    finally:
        Setting.remove(SETTING_KEY)


# ---------------------------------------------------------------------------
# Fail-closed: the widening is bound to the vetted mint, never the candidate.
# ---------------------------------------------------------------------------

@th.django_unit_test("oauth: a deep-link bounce without a stamped scheme fails closed to 400")
def test_deep_link_bounce_is_bound_to_the_vetted_mint(opts):
    """State minted directly with a deep-link `frontend_uri` but NO
    `frontend_scheme` — the shape a pre-#1102 state has, or one an attacker could
    forge. The widening is keyed off the stamp, not the candidate, so this must
    NOT widen to `myapp`; it floors to a 400."""
    from mojo.apps.account.services.oauth import get_provider

    svc = get_provider(PROVIDER)
    callback_uri = "http://localhost:9009/api/auth/oauth/google/callback"
    state = svc.create_state(extra={
        "redirect_uri": callback_uri,
        "frontend_uri": "myapp://callback",
    })
    resp = opts.client.get(
        f"/api/auth/oauth/{PROVIDER}/callback?code=dl_testcode&state={state}",
        allow_redirects=False)
    assert resp.status_code == 400, (
        f"a deep-link frontend_uri with no vetted frontend_scheme must floor to "
        f"400 — never widen, never 500; got {resp.status_code}: {resp.response}")
    assert resp.response.get("error") == BOUNCE_REFUSAL, (
        f"the 400 must be the callback-bounce refusal {BOUNCE_REFUSAL!r}, "
        f"proving it is the DisallowedRedirect floor; got {resp.response.get('error')!r}")
    assert _location(opts) is None, (
        f"a refused bounce must emit no Location header; got {_location(opts)!r}")


@th.django_unit_test("oauth: a custom scheme injected via Origin never widens the bounce")
def test_origin_injected_custom_scheme_is_refused(opts):
    """RED-TEAM negative. With no redirect_uri param, `on_oauth_begin` derives
    `frontend_uri` from the UNVALIDATED `Origin` header. A custom scheme arriving
    that way is untrusted, so /begin must stamp NO `frontend_scheme` and the
    callback must floor it to a clean 400 — never a widened deep link, never a
    500."""
    from mojo.apps.account.services.oauth import get_provider

    svc = get_provider(PROVIDER)
    resp = opts.client.get(
        f"/api/auth/oauth/{PROVIDER}/begin",
        headers={"Origin": "myapp://evil"})
    assert resp.status_code == 200, (
        f"/begin must still succeed with an Origin-derived landing, got "
        f"{resp.status_code}: {resp.response}")
    state = resp.response.data.state
    assert state, f"/begin must mint state: {resp.response}"
    stored = svc.peek_state(state)
    assert stored, "state must be retrievable"
    # Sanity: the Origin actually drove the landing URL, else this proves nothing.
    assert urlsplit(stored.get("frontend_uri", "")).scheme == "myapp", (
        f"the Origin header must have driven frontend_uri's scheme for this test "
        f"to mean anything; got {stored.get('frontend_uri')!r}")
    assert "frontend_scheme" not in stored, (
        f"an Origin-derived custom scheme must NOT be stamped into state; got "
        f"{stored.get('frontend_scheme')!r}")

    resp = opts.client.get(
        f"/api/auth/oauth/{PROVIDER}/callback?code=dl_testcode&state={state}",
        allow_redirects=False)
    assert resp.status_code == 400, (
        f"the Origin-injected deep link must floor to a clean 400 — never a "
        f"widened bounce, never a 500; got {resp.status_code}: {resp.response}")
    assert resp.response.get("error") == BOUNCE_REFUSAL, (
        f"the 400 must be the callback-bounce refusal {BOUNCE_REFUSAL!r}; "
        f"got {resp.response.get('error')!r}")
    assert _location(opts) is None, (
        f"the refusal must emit no Location; got {_location(opts)!r}")


# ---------------------------------------------------------------------------
# Control: the http(s) path is byte-for-byte unchanged.
# ---------------------------------------------------------------------------

@th.django_unit_test("oauth: a plain https landing bounces exactly as before, no scheme stamped")
def test_web_redirect_bounce_is_unchanged(opts):
    """The control against the pinned `https://example.com/` — no Setting row.
    An http(s) landing stamps NO `frontend_scheme` (the state shape stays
    byte-identical to before #1102) and bounces with a stock 302."""
    from mojo.apps.account.services.oauth import get_provider

    svc = get_provider(PROVIDER)
    web_uri = "https://example.com/"
    resp = opts.client.get(
        f"/api/auth/oauth/{PROVIDER}/begin?redirect_uri={quote(web_uri, safe='')}")
    assert resp.status_code == 200, (
        f"/begin with the pinned https entry must succeed, got "
        f"{resp.status_code}: {resp.response}")
    state = resp.response.data.state
    assert state, f"/begin must mint state: {resp.response}"
    stored = svc.peek_state(state)
    assert stored.get("frontend_uri") == web_uri, (
        f"frontend_uri must be stored verbatim; got {stored.get('frontend_uri')!r}")
    assert "frontend_scheme" not in stored, (
        f"an http(s) landing must stamp NO frontend_scheme — the state shape must "
        f"stay byte-identical to before #1102; got {stored.get('frontend_scheme')!r}")

    resp = opts.client.get(
        f"/api/auth/oauth/{PROVIDER}/callback?code=web_testcode&state={state}",
        allow_redirects=False)
    assert resp.status_code == 302, (
        f"the https callback must 302 as always, got {resp.status_code}: {resp.response}")
    location = _location(opts)
    assert location and location.startswith("https://example.com/?"), (
        f"the bounce must land on the https URL; got {location!r}")
    params = parse_qs(urlsplit(location).query)
    assert params.get("code") == ["web_testcode"], (
        f"the bounce must carry the code; got {params.get('code')!r} in {location}")
    assert params.get("state") == [state], (
        f"the bounce must carry the state; got {params.get('state')!r} in {location}")


# ---------------------------------------------------------------------------
# In-process unit coverage of the two new helpers.
# ---------------------------------------------------------------------------

@th.django_unit_test("oauth: matchable_scheme parses safe shapes and refuses the sinks")
def test_matchable_scheme_refuses_the_sinks(opts):
    """`matchable_scheme` PARSES, it does not authorize: it is non-empty only for
    a shape `_split` accepts. Every sink and every ambiguous shape returns ''."""
    from mojo.apps.account.services import redirect_allowlist

    # `"myapp://callback\\x"` is the string myapp://callback\x — the backslash
    # guard in _split refuses it, so matchable_scheme returns ''.
    for url in ("javascript:/alert(1)", "data:/text/html", "vbscript:/msgbox",
                "myapp:callback", "mailto:a@b", "myapp://", "//host/x",
                "myapp://callback\\x"):
        assert redirect_allowlist.matchable_scheme(url) == "", (
            f"matchable_scheme must return '' for the unmatchable shape {url!r}, "
            f"got {redirect_allowlist.matchable_scheme(url)!r}")

    assert redirect_allowlist.matchable_scheme("myapp://callback") == "myapp", (
        "a well-formed deep link must yield its scheme")
    assert redirect_allowlist.matchable_scheme("MyApp://CallBack") == "myapp", (
        "the scheme must come back lowercased")
    assert redirect_allowlist.matchable_scheme("https://example.com/") == "https", (
        "an https URL must yield 'https'")
